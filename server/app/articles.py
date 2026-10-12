"""Service layer for saving and publishing articles.

This module knows nothing about HTTP, so the routers and the background scheduled-publishing loop can drive it through the same calls. It raises the AppError subclasses from errors.py; the boundary (the exception handlers) turns them into HTTP responses.

The heart of the module is that save and publish treat Discord roles in opposite ways. Eligibility at save time (whose name may be written under, whether breaking news may be flagged) is re-read from the current roles every time, but the byline settled at publish time (articles.publisher_id) is frozen and never moves again, however the roles change afterwards. Who reported under which publisher's name and when is a past fact, not a function of the current affiliation.

Images are ingested on every save rather than at publish time. Discord CDN URLs expire in about 24 hours, so waiting until publish means an article left sitting as a draft is fetched after expiry, when the original image is already gone.

Every save and every publication that changes the row advances articles.version, the value the ETag carries. A caller that passes expected_versions (taken from If-Match) gets PreconditionFailed when the article moved on since it read it, instead of overwriting someone else's edit.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

from aiomysql import Connection, IntegrityError

from common.config import Config, effective_media_config
from common.db import Database
from common.settings import RuntimeSettings
from common.snowflake import to_snowflake

from .errors import ArticleError, ArticleForbidden, Conflict, NotFound, PreconditionFailed, RateLimited
from .idempotency import IdempotencyRequest, claim, complete
from .markdown import render_markdown
from .media.service import IngestFailure, ingest_article_images
from .principals import Principal, has_scope, is_admin
from .schemas import ArticleCreate, ArticleUpdate, visible_text

# ArticleError / ArticleForbidden are re-exported from errors.py, never redefined here. Exception-to-HTTP conversion is centralized in errors.py, so a same-named class defined here would fall outside the handler table and business-rule violations would come back as 500 instead of 400 or 403.
__all__ = [
    'ArticleError',
    'ArticleForbidden',
    'article_etag',
    'has_write_any',
    'ensure_may_edit',
    'RATE_LIMIT_WINDOW_SECONDS',
    'RateLimitStatus',
    'rate_limit_status',
    'resolve_moved_slug',
    'save_article',
    'publish_article',
    'resolve_notify_channels',
    'enqueue_notifications',
]

logger = logging.getLogger(__name__)

# Characters a slug may keep: Unicode word characters (Japanese included) plus - and ., everything else collapsed to -. The slug goes out verbatim as a URL path segment, so nothing that could turn into whitespace or a separator may survive.
_SLUG_STRIP_RE: Final = re.compile(r'[^\w.-]+', re.UNICODE)
# Leaves room for a numbering suffix (-2 and so on) inside articles.slug's VARCHAR(200).
_SLUG_BASE_MAX_LENGTH: Final = 190
# Fallback for a title made entirely of symbols, which slugifies to an empty string.
_SLUG_FALLBACK: Final = 'article'
# Where to give up when generated slugs keep colliding.
_SLUG_MAX_ATTEMPTS: Final = 1000
# MySQL's ER_DUP_ENTRY.
_DUPLICATE_ENTRY: Final = 1062
# Slugs no article may take. /api/articles/by-id/{id} shares its first segment with /api/articles/{slug}/..., so an article slugged by-id would have its sub-resources shadowed.
_RESERVED_SLUGS: Final = frozenset({'by-id'})

# Columns of the existing row read when saving an article.
_EXISTING_COLUMNS: Final = (
    'id, slug, author_id, publisher_id, category_id, series_id, series_position, title, excerpt, body_md, '
    'cover_image_url, status, visibility, byline_mode, is_breaking, is_sponsored, sponsor_name, scheduled_at, published_at, version'
)

ArticlePayload = ArticleCreate | ArticleUpdate


# Small helpers.


def _specified(payload: ArticlePayload, name: str) -> bool:
    """Whether the request sets this field.

    A creation sets every field: an omitted one takes the default ArticleCreate declares. An update sets only the fields it carries, and the value alone cannot tell "omitted" from "explicitly set to null": the first keeps the existing value, the second erases it.
    """
    return isinstance(payload, ArticleCreate) or name in payload.model_fields_set


def _merged(payload: ArticlePayload, existing: dict[str, Any] | None, name: str) -> Any:
    """Return the value the request sets, else the existing value."""
    if _specified(payload, name) or existing is None:
        return getattr(payload, name)
    return existing[name]


def _merged_id(payload: ArticlePayload, existing: dict[str, Any] | None, name: str) -> int | None:
    """Merge an ID field, normalizing it to int. Requests carry IDs as strings; the DB stores BIGINT."""
    if _specified(payload, name) or existing is None:
        value = getattr(payload, name)
        return None if value is None else to_snowflake(value)
    return existing[name]


def article_etag(article_id: int, version: int) -> str:
    """The strong ETag for an article's editable state."""
    return f'"{article_id}-{version}"'


def _precondition_failed(article_id: int, version: int) -> PreconditionFailed:
    """The 412 for a stale If-Match. The current ETag goes into details so the caller can tell which state it is behind."""
    return PreconditionFailed(details={'etag': article_etag(article_id, version)})


def _flag(value: Any) -> int:
    """Coerce to the 0/1 a TINYINT(1) column takes. The value arrives as int from the DB and as bool from the request."""
    return 1 if value else 0


def _slugify(title: str) -> str:
    """Build the base slug from a title.

    There is no way to transliterate a Japanese title into ASCII, and dropping non-ASCII would leave the slug empty, so Unicode word characters are kept as they are: browser and server both handle percent-encoding transparently.
    """
    normalized = unicodedata.normalize('NFKC', title).lower()
    slug = _SLUG_STRIP_RE.sub('-', normalized).strip('-.')
    if len(slug) > _SLUG_BASE_MAX_LENGTH:
        slug = slug[:_SLUG_BASE_MAX_LENGTH].rstrip('-.')
    # '.' and '..' carry special meaning as path segments, so neither may become a slug.
    if not slug or slug in ('.', '..'):
        return _SLUG_FALLBACK
    return slug


# Eligibility checks.


async def has_write_any(db: Database, settings: RuntimeSettings, principal: Principal) -> bool:
    """Whether the principal may name any author and any publisher freely.

    Via an API key the scope alone is not enough: the owner must meet admin eligibility at the moment of the request. A key's eligibility is never frozen at issue time, so an old key owned by a user who has since lost admin stops passing from that moment on. Via a session, being an admin is the same power directly.

    An articles:write key owned by an admin must go through this check too. Looking only at whether the owner is an admin would let a key that is supposed to pin the author to its owner reach into other people's articles, erasing the distinction between the two scopes.
    """
    if principal.via_token:
        return has_scope(principal, 'articles:write:any') and await is_admin(db, settings, principal)
    return await is_admin(db, settings, principal)


def ensure_may_edit(principal: Principal, existing: dict[str, Any], *, write_any: bool) -> None:
    """Check that the principal may edit or publish an existing article. Only the author themselves, or a principal that may name any author freely."""
    if write_any:
        return
    if existing['author_id'] != principal.user_id:
        raise ArticleForbidden('他の利用者が著者の記事は編集できません。')


RATE_LIMIT_WINDOW_SECONDS: Final = 3600


@dataclass(frozen=True)
class RateLimitStatus:
    """Where one API key stands against the hourly create/update limit. reset_seconds is how long until the oldest save in the window falls out of it and frees one unit; 0 when nothing is used."""

    limit: int
    used: int
    reset_seconds: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


async def rate_limit_status(db: Database, settings: RuntimeSettings, token_id: int, *, conn: Connection | None = None) -> RateLimitStatus:
    """Count the key's saves (revisions) in the sliding window. The same count both enforces the limit and is reported to the client."""
    row = await db.fetch_one(
        'SELECT COUNT(*) AS used, COALESCE(TIMESTAMPDIFF(SECOND, NOW(), MIN(created_at) + INTERVAL %s SECOND), 0) AS reset_seconds '
        'FROM article_revisions WHERE via_token_id = %s AND created_at >= NOW() - INTERVAL %s SECOND',
        RATE_LIMIT_WINDOW_SECONDS,
        token_id,
        RATE_LIMIT_WINDOW_SECONDS,
        conn=conn,
    )
    used = int(row['used']) if row else 0
    reset_seconds = max(int(row['reset_seconds']), 0) if row and used else 0
    return RateLimitStatus(limit=settings.articles.api_rate_limit_per_hour, used=used, reset_seconds=reset_seconds)


async def _ensure_within_rate_limit(db: Database, settings: RuntimeSettings, principal: Principal, *, conn: Connection | None = None) -> None:
    """Check the hourly create/update limit for one API key.

    The limit is per key, not per user and not per endpoint. Automated posting can run away by nature, so only the key that hit the limit stops: the same person's other keys and posts from the Web UI are untouched. Posting by hand does not run away in this sense, so the session path is exempt.

    The call that passes conn is what actually enforces the limit. Taking the key's row FOR UPDATE before counting serializes saves from the same key, so no other request can slip in after the count and before the revision insert. Counting without taking it lets every concurrent request read the same pre-insert value: against a limit of 60, 500 requests fired at once all pass, and the limit only bites on serial requests. The token row acts as a per-key mutex.

    The call without conn is an early estimate, there only to keep the heavy work of fetching images from remote hosts from running for a request that will be rejected anyway. Passing it does not guarantee the limit has not been reached.
    """
    if principal.token_id is None:
        return
    if conn is not None:
        await db.fetch_val('SELECT id FROM api_tokens WHERE id = %s FOR UPDATE', principal.token_id, conn=conn)
    status = await rate_limit_status(db, settings, principal.token_id, conn=conn)
    if status.used < status.limit:
        return
    # Even when the oldest save has already left the window (0 right at the boundary), keep at least 1 second so an immediate retry is not invited.
    raise RateLimited(
        f'このキーの記事の作成・更新は 1 時間あたり {status.limit} 件までです。',
        retry_after=max(status.reset_seconds, 1),
        details={'limit': status.limit, 'used': status.used},
    )


async def _resolve_author_id(db: Database, principal: Principal, payload: ArticlePayload, existing: dict[str, Any] | None, *, write_any: bool) -> int:
    """Decide the author.

    An articles:write key pins the author to the key's owner, holding the power the key grants down to what that person could do by hand from the Web UI. Only articles:write:any (and an admin session) may name someone else as the author.
    """
    current = existing['author_id'] if existing is not None else principal.user_id
    if not _specified(payload, 'author_id') or payload.author_id is None:
        return current
    requested = to_snowflake(payload.author_id)
    if requested == current:
        return current
    if not write_any:
        raise ArticleForbidden('他の利用者を著者に指定する権限がありません。')
    if await db.fetch_val('SELECT id FROM users WHERE id = %s', requested) is None:
        raise ArticleError(f'著者に指定された利用者が存在しません: {requested}')
    return requested


async def _ensure_may_sign_publisher(db: Database, principal: Principal, publisher_id: int, *, write_any: bool) -> None:
    """Check that the principal may publish under that publisher's name. The condition is holding the role that represents the affiliation."""
    if write_any:
        return
    role_id = await db.fetch_val('SELECT role_id FROM publishers WHERE id = %s', publisher_id)
    if role_id is None or role_id not in principal.role_ids:
        raise ArticleForbidden('この報道社を名乗る権限がありません。所属を表すロールが必要です。')


def _ensure_byline_unfrozen(existing: dict[str, Any] | None, author_id: int, publisher_id: int | None) -> None:
    """Refuse any request that changes a published article's byline (author and publisher).

    _freeze_publisher settles the byline at publish time by verifying that the author really holds the publisher's role, and that only holds if the pair never moves afterwards. Rewriting even one half after publication leaves an unverified pair on the article, presented as a verified byline. Publishing first and attaching a publisher afterwards, or replacing only the author of a legitimately published article, must not be an ordering that walks past the verification.

    Who reported under which publisher's name and when is a past fact, and even a principal that may name any author has no legitimate reason to rewrite it after the fact. A specification that changes nothing (an update where the caller sends the whole article back) passes through, so corrections to a published article's body or headline are not blocked.
    """
    if existing is None or existing['published_at'] is None:
        return
    if publisher_id != existing['publisher_id']:
        raise ArticleForbidden('公開済みの記事の報道社は変更できません。署名は公開の時点で確定します。')
    if author_id != existing['author_id']:
        raise ArticleForbidden('公開済みの記事の著者は変更できません。署名は公開の時点で確定します。')


def _ensure_may_flag_breaking(settings: RuntimeSettings, principal: Principal) -> None:
    """Check that the principal may flag an article as breaking news.

    An empty list in the setting means nobody may. That is not an unfilled setting but a valid intent, "we do not use breaking news", and must never fall back to letting anyone flag one.
    """
    allowed = settings.articles.breaking.allowed_role_ids
    if not allowed or principal.role_ids.isdisjoint(allowed):
        raise ArticleForbidden('速報を立てる権限がありません。')


# Resolving referenced entities.


async def _resolve_tag_ids(db: Database, slugs: list[str]) -> list[int]:
    """Resolve tag slugs to IDs. A slug that does not exist is an error.

    Posting an article never adds tags of its own accord. Creating a tag belongs to taxonomy:admin, and if a misspelling silently produced a new tag the classification would quietly rot.
    """
    if not slugs:
        return []
    placeholders = ', '.join(['%s'] * len(slugs))
    rows = await db.fetch_all(f'SELECT id, slug FROM tags WHERE slug IN ({placeholders})', *slugs)
    found = {row['slug']: row['id'] for row in rows}
    missing = [slug for slug in slugs if slug not in found]
    if missing:
        raise ArticleError(f'存在しないタグが指定されました: {", ".join(missing)}')
    return [found[slug] for slug in slugs]


async def _resolve_visibility_role_ids(db: Database, role_ids: list[str]) -> list[int]:
    """Validate the roles allowed to view and return them as a list of ints. A role that has not been synced is an error."""
    if not role_ids:
        return []
    ids = [to_snowflake(value) for value in role_ids]
    placeholders = ', '.join(['%s'] * len(ids))
    rows = await db.fetch_all(f'SELECT id FROM discord_roles WHERE id IN ({placeholders})', *ids)
    found = {row['id'] for row in rows}
    missing = [str(value) for value in ids if value not in found]
    if missing:
        raise ArticleError(f'存在しないロールが指定されました: {", ".join(missing)}')
    return ids


async def _slug_taken_by_other(db: Database, conn: Connection, slug: str, *, exclude_id: int | None) -> bool:
    """Whether another article holds the slug, either as its current slug or as one it used to have.

    A former slug stays reserved for its article, because /articles/{slug} answers it with a redirect: letting another article take it would make an old link point at a different article.
    """
    live = await db.fetch_val(
        'SELECT id FROM articles WHERE slug = %s AND (%s IS NULL OR id <> %s)',
        slug,
        exclude_id,
        exclude_id,
        conn=conn,
    )
    if live is not None:
        return True
    moved = await db.fetch_val(
        'SELECT article_id FROM article_slug_history WHERE slug = %s AND (%s IS NULL OR article_id <> %s)',
        slug,
        exclude_id,
        exclude_id,
        conn=conn,
    )
    return moved is not None


async def _unique_slug(db: Database, conn: Connection, base: str, *, exclude_id: int | None) -> str:
    """Build an unused slug from base, appending a number on collision or on a reserved slug."""
    candidate = base
    for attempt in range(1, _SLUG_MAX_ATTEMPTS + 1):
        if candidate not in _RESERVED_SLUGS and not await _slug_taken_by_other(db, conn, candidate, exclude_id=exclude_id):
            return candidate
        candidate = f'{base}-{attempt + 1}'
    raise Conflict('slug を決められませんでした。タイトルを変えるか slug を明示してください。')


async def _ensure_slug_free(db: Database, conn: Connection, slug: str, *, exclude_id: int | None) -> None:
    """Check that an explicitly given slug is free. Unlike generation, it never silently appends a number; a taken slug is reported rather than replaced by a different URL."""
    if slug in _RESERVED_SLUGS:
        raise ArticleError(f'この slug は予約されているため使えません: {slug}')
    if await _slug_taken_by_other(db, conn, slug, exclude_id=exclude_id):
        raise Conflict(f'この slug は既に使われています(過去に他の記事が使っていた slug も含みます): {slug}')


async def resolve_moved_slug(db: Database, slug: str) -> int | None:
    """Return the ID of the article that used to have this slug, or None. Callers redirect to the article's current slug only after checking that the viewer may see it."""
    article_id = await db.fetch_val('SELECT article_id FROM article_slug_history WHERE slug = %s', slug)
    return None if article_id is None else int(article_id)


# Saving.


async def save_article(
    db: Database,
    config: Config,
    settings: RuntimeSettings,
    principal: Principal,
    *,
    article_id: int | None,
    payload: ArticlePayload,
    expected_versions: frozenset[int] | None = None,
    idempotency: IdempotencyRequest | None = None,
) -> tuple[int, list[IngestFailure]]:
    """Create or update an article. Returns (article ID, the images that could not be ingested).

    A None article_id creates from an ArticleCreate; anything else updates from an ArticleUpdate. Images that could not be ingested are returned rather than raised: a temporary fault at a remote host must never stop an article going out. But a URL that failed stays in the body as it is and will eventually expire, taking the image with it, so the failure is never silently counted as success and is always reported to the caller.

    expected_versions holds the versions If-Match accepts (None when the request carries no If-Match, or carries *); a current version outside it raises PreconditionFailed. Without it the save still refuses with Conflict when another save lands between reading the row here and locking it, because the fields this request omits would otherwise be written back with the values read before that save.

    idempotency, on a creation, records the Idempotency-Key in the creating transaction together with the images that failed (a replay reports them again). It raises IdempotencyRace when another request under the key committed first, and the creation rolls back.

    Both config and settings are taken because the settings image ingestion needs come from two different places. The storage location (storage_dir) is bound to the runtime environment and lives in config.yaml; the individual limits live in the settings table, where an admin can change them. effective_media_config folds the two into one.
    """
    existing: dict[str, Any] | None = None
    if article_id is not None:
        existing = await db.fetch_one(f'SELECT {_EXISTING_COLUMNS} FROM articles WHERE id = %s', article_id)
        if existing is None:
            raise NotFound('記事が見つかりません。')

    write_any = await has_write_any(db, settings, principal)
    if existing is not None:
        ensure_may_edit(principal, existing, write_any=write_any)
        # Checked after the edit permission, so the version of an article the caller may not edit is never disclosed, and before ingesting images, so a stale save costs no remote fetches.
        if expected_versions is not None and int(existing['version']) not in expected_versions:
            raise _precondition_failed(int(existing['id']), int(existing['version']))
    # Early estimate only, to avoid fetching images from remote hosts on behalf of a key that is already at the limit. The limit itself is enforced by the check inside the transaction.
    await _ensure_within_rate_limit(db, settings, principal)

    author_id = await _resolve_author_id(db, principal, payload, existing, write_any=write_any)

    title = _merged(payload, existing, 'title')
    body_md = _merged(payload, existing, 'body_md')

    category_id = _merged_id(payload, existing, 'category_id')
    if _specified(payload, 'category_id'):
        if await db.fetch_val('SELECT id FROM categories WHERE id = %s', category_id) is None:
            raise ArticleError(f'存在しないカテゴリが指定されました: {category_id}')

    publisher_id = _merged_id(payload, existing, 'publisher_id')
    _ensure_byline_unfrozen(existing, author_id, publisher_id)
    # The eligibility check runs only for a request that actually changes the byline. Rejecting a specification that changes nothing (an update where the caller sends the whole article back) would narrow the principals able to touch an article down to the current holders of that publisher's role, and an admin could no longer fix the past articles of a dissolved publisher.
    # A changed author changes the byline too. Even with the same publisher, swapping the person publishing under that name makes a new byline, and letting it through would erase the check via "keep the publisher, replace only the author".
    signature_changed = existing is None or publisher_id != existing['publisher_id'] or author_id != existing['author_id']
    if publisher_id is not None and signature_changed:
        if await db.fetch_val('SELECT id FROM publishers WHERE id = %s', publisher_id) is None:
            raise ArticleError(f'存在しない報道社が指定されました: {publisher_id}')
        await _ensure_may_sign_publisher(db, principal, publisher_id, write_any=write_any)

    series_id = _merged_id(payload, existing, 'series_id')
    if series_id is not None and (existing is None or series_id != existing['series_id']):
        if await db.fetch_val('SELECT id FROM series WHERE id = %s', series_id) is None:
            raise ArticleError(f'存在しない連載が指定されました: {series_id}')

    visibility = _merged(payload, existing, 'visibility')
    status = _merged(payload, existing, 'status')
    scheduled_at = _merged(payload, existing, 'scheduled_at')
    if status == 'scheduled' and scheduled_at is None:
        raise ArticleError('status が scheduled のときは scheduled_at が必須です。')

    is_breaking = bool(_merged(payload, existing, 'is_breaking'))
    # The check runs only for a request that raises the flag. Demanding the permission every time the body of an already-breaking article is fixed would leave only those who may flag breaking news able to correct it.
    if is_breaking and not (existing is not None and existing['is_breaking']):
        _ensure_may_flag_breaking(settings, principal)

    is_sponsored = bool(_merged(payload, existing, 'is_sponsored'))
    sponsor_name = _merged(payload, existing, 'sponsor_name')
    # The test is whether the name holds a visible character, not whether it is empty once whitespace is stripped. str.strip() only removes characters for which str.isspace() is true, so U+200B (zero-width space) and U+FEFF survive it and masquerade as a non-empty name. With one of those as the sponsor, the article page and the embed show nothing but the Sponsored label, the advertiser appears nowhere, and requiring the disclosure has bought nothing.
    if is_sponsored and not (sponsor_name and visible_text(sponsor_name)):
        raise ArticleError('is_sponsored が真のときは sponsor_name が必須です。「広告」とだけ書いても開示になりません。')

    excerpt = _merged(payload, existing, 'excerpt')
    cover_image_url = _merged(payload, existing, 'cover_image_url')
    byline_mode = _merged(payload, existing, 'byline_mode')
    series_position = _merged(payload, existing, 'series_position')

    # tags / visibility_role_ids are refreshed to match the request only when the request sets them (a creation always does). None means leave them alone.
    tag_ids: list[int] | None = None
    if _specified(payload, 'tags'):
        tag_ids = await _resolve_tag_ids(db, payload.tags)

    visibility_role_ids: list[int] | None = None
    if _specified(payload, 'visibility_role_ids'):
        visibility_role_ids = await _resolve_visibility_role_ids(db, payload.visibility_role_ids)

    if visibility == 'restricted':
        effective_roles = visibility_role_ids
        if effective_roles is None:
            rows = await db.fetch_all('SELECT role_id FROM article_visibility_roles WHERE article_id = %s', article_id)
            effective_roles = [row['role_id'] for row in rows]
        if not effective_roles:
            raise ArticleError('visibility が restricted のときは visibility_role_ids に 1 つ以上のロールが必要です。')

    # Images are ingested outside the transaction. Fetching from remote hosts can take up to the configured total time budget, and holding the transaction open for that would keep the article's row locked the whole time. media rows are content-addressed, deduped and exist independently of articles, so a later failure of the article save leaves nothing inconsistent: only an unreferenced blob.
    # uploaded_by is the principal that actually caused the ingest, not the author, because the quota should fall on whoever used the resource: images ingested for a post that named someone else as the author under articles:write:any must not eat that other person's quota.
    body_md, media_ids, failures = await ingest_article_images(
        db,
        effective_media_config(config, settings),
        body_md,
        uploaded_by=principal.user_id,
        via_token_id=principal.token_id,
    )
    body_html = render_markdown(body_md)

    async with db.transaction() as conn:
        # The Idempotency-Key is claimed before anything else, so a concurrent resend waits here for this transaction and then replays it.
        idempotency_record: int | None = None
        if idempotency is not None and existing is None:
            idempotency_record = await claim(db, conn, principal.user_id, idempotency)

        # This is where the limit is enforced: take the key's row, count, and insert the revision in one transaction, so no other request can slip in after the count. The key's row is taken before the article's so that this path always locks in the order key then article, never forming a pair that waits on each other in reverse.
        await _ensure_within_rate_limit(db, settings, principal, conn=conn)

        if existing is not None:
            # Lock the article's row before taking a revision number. Concurrent saves of the same article would read the same MAX(revision_number)+1, and the (article_id, revision_number) unique constraint would drop one of them.
            locked_version = await db.fetch_val('SELECT version FROM articles WHERE id = %s FOR UPDATE', article_id, conn=conn)
            if locked_version is None:
                raise NotFound('記事が見つかりません。')
            # Another save landed after the row was read above. Everything merged from that read is stale.
            if int(locked_version) != int(existing['version']):
                if expected_versions is not None:
                    raise _precondition_failed(int(existing['id']), int(locked_version))
                raise Conflict('この記事は同時に別の要求で更新されました。最新の内容を読み込み直してから再送してください。')

        if _specified(payload, 'slug') and payload.slug is not None:
            slug = payload.slug
            # Re-sending the current slug is not a change, so it passes even if it would no longer be accepted as a new one.
            if existing is None or slug != existing['slug']:
                await _ensure_slug_free(db, conn, slug, exclude_id=article_id)
        elif existing is not None:
            # A permalink that changed every time the title was fixed would break URLs already handed out. The slug moves only on an explicit specification.
            slug = existing['slug']
        else:
            slug = await _unique_slug(db, conn, _slugify(title), exclude_id=None)

        columns = (
            slug,
            author_id,
            publisher_id,
            category_id,
            series_id,
            series_position,
            title,
            excerpt,
            body_md,
            body_html,
            cover_image_url,
            status,
            visibility,
            byline_mode,
            _flag(is_breaking),
            _flag(is_sponsored),
            sponsor_name,
            scheduled_at,
        )
        if article_id is None:
            try:
                article_id = await db.execute_insert(
                    'INSERT INTO articles (slug, author_id, publisher_id, category_id, series_id, series_position, title, excerpt, '
                    'body_md, body_html, cover_image_url, status, visibility, byline_mode, is_breaking, is_sponsored, sponsor_name, scheduled_at) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                    *columns,
                    conn=conn,
                )
            except IntegrityError as exc:
                # The slug, the one unique column besides the key, was free when checked but another creation committed it in the meantime (two articles with the same headline at once).
                if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
                    raise Conflict(f'この slug は同時に別の記事が使いました。再送してください: {slug}') from exc
                raise
            if idempotency_record is not None:
                await complete(
                    db,
                    conn,
                    idempotency_record,
                    article_id,
                    {'media_failures': [{'source_url': failure.source_url, 'reason': failure.reason} for failure in failures]},
                )
        else:
            await db.execute(
                'UPDATE articles SET slug = %s, author_id = %s, publisher_id = %s, category_id = %s, series_id = %s, '
                'series_position = %s, title = %s, excerpt = %s, body_md = %s, body_html = %s, cover_image_url = %s, '
                'status = %s, visibility = %s, byline_mode = %s, is_breaking = %s, is_sponsored = %s, sponsor_name = %s, '
                'scheduled_at = %s, version = version + 1 WHERE id = %s',
                *columns,
                article_id,
                conn=conn,
            )
            if slug != existing['slug']:
                # The slug being left keeps answering with a redirect; the slug being taken back stops being a former one.
                await db.execute('INSERT INTO article_slug_history (slug, article_id) VALUES (%s, %s)', existing['slug'], article_id, conn=conn)
                await db.execute('DELETE FROM article_slug_history WHERE slug = %s AND article_id = %s', slug, article_id, conn=conn)

        if tag_ids is not None:
            await db.execute('DELETE FROM article_tags WHERE article_id = %s', article_id, conn=conn)
            for tag_id in tag_ids:
                await db.execute('INSERT INTO article_tags (article_id, tag_id) VALUES (%s, %s)', article_id, tag_id, conn=conn)

        if visibility_role_ids is not None:
            await db.execute('DELETE FROM article_visibility_roles WHERE article_id = %s', article_id, conn=conn)
            for role_id in visibility_role_ids:
                await db.execute('INSERT INTO article_visibility_roles (article_id, role_id) VALUES (%s, %s)', article_id, role_id, conn=conn)

        # article_media is append-only: an edit that drops an image from the body leaves the past revisions still referencing it, and a deleted row would 404 the image the moment someone opens the revision history.
        for media_id in media_ids:
            await db.execute('INSERT IGNORE INTO article_media (article_id, media_id) VALUES (%s, %s)', article_id, media_id, conn=conn)

        revision_number = int(
            await db.fetch_val(
                'SELECT COALESCE(MAX(revision_number), 0) + 1 FROM article_revisions WHERE article_id = %s',
                article_id,
                conn=conn,
            )
        )
        # editor_id records whose name the revision was written under: the byline (the author) as of that revision. Who actually posted it is carried by via_token_id, and the two are different things. Since articles:write:any can publish an article under someone else's byline, accountability cannot be traced without the latter.
        await db.execute(
            'INSERT INTO article_revisions (article_id, revision_number, editor_id, via_token_id, title, body_md, excerpt, note, is_correction) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            article_id,
            revision_number,
            author_id,
            principal.token_id,
            title,
            body_md,
            excerpt,
            payload.revision_note,
            _flag(payload.is_correction),
            conn=conn,
        )

        # A save with status='published' is a publication too. Writing the row's status and leaving it at that would produce a published article with neither the byline frozen nor the notifications enqueued. There is one procedure for publishing and this keeps it that way.
        if status == 'published':
            await publish_article(db, settings, article_id, conn=conn)

    return article_id, failures


# Publishing.


async def _freeze_publisher(db: Database, conn: Connection, article_id: int, author_id: int, publisher_id: int | None) -> int | None:
    """Settle the byline at publish time, verifying that the author really holds the publisher's role.

    Save time checks whether the principal doing the writing may publish under that publisher's name; this checks whether the author being bylined is actually affiliated with it. They are different questions, and even a principal that may name any author cannot turn an affiliation that does not exist into a byline.
    """
    if publisher_id is None:
        return None
    holds = await db.fetch_val(
        'SELECT p.id FROM publishers p JOIN user_roles ur ON ur.role_id = p.role_id WHERE p.id = %s AND ur.user_id = %s',
        publisher_id,
        author_id,
        conn=conn,
    )
    if holds is not None:
        return publisher_id
    # Publication is not stopped. Scheduled publishing runs from a background loop, so raising here would leave the article repeating the same failure and never going out. The byline the author is not entitled to is dropped, the article is published, and the fact is recorded in the log.
    logger.warning(
        '記事 %s の署名を外しました。著者 %s は報道社 %s の所属ロールを保有していません。',
        article_id,
        author_id,
        publisher_id,
    )
    return None


async def publish_article(
    db: Database,
    settings: RuntimeSettings,
    article_id: int,
    *,
    expected_versions: frozenset[int] | None = None,
    conn: Connection | None = None,
) -> None:
    """Publish an article and enqueue its notifications.

    expected_versions holds the versions If-Match accepts (None when there is nothing to check); a current version outside it raises PreconditionFailed. The version advances only when the row actually changes, so publishing an article that is already published leaves it alone.

    Re-publishing does not move published_at (COALESCE). The time an article first went out is a past fact, and fixing it must not reorder it into the listing as a new article. For the same reason the byline is frozen only at the first publication and never moves again, however the author's roles change.

    Notifications are enqueued only at the first publication too; editing a published article produces no new notification. The recipients are resolved afresh from the article's category and tags every time, so enqueueing on later publications would mean that merely adding one tag after publication sends a new-article notification to a new channel and DMs that tag's subscribers about an old article. The (article_id, channel_id) unique constraint only prevents sending twice to the same recipient; a row for a new recipient passes straight through. Fixing a typo must never notify readers.

    Passing conn joins the caller's transaction. It exists so the notification enqueue runs in the same transaction as the publication; split apart, they can produce an article that is published but that nobody is told about.
    """
    if conn is None:
        async with db.transaction() as owned:
            await publish_article(db, settings, article_id, expected_versions=expected_versions, conn=owned)
            return

    row = await db.fetch_one(
        'SELECT id, author_id, publisher_id, published_at, version FROM articles WHERE id = %s FOR UPDATE',
        article_id,
        conn=conn,
    )
    if row is None:
        raise NotFound('記事が見つかりません。')
    if expected_versions is not None and int(row['version']) not in expected_versions:
        raise _precondition_failed(article_id, int(row['version']))

    if row['published_at'] is not None:
        # A second or later publication. Neither the byline nor the notifications are touched; only the status goes back (restoring from archived comes through here). Re-freezing at this point would let an edit made after a reporter transferred rewrite the old byline retroactively.
        # MySQL applies the assignments left to right, so version is computed against the status before this statement changes it.
        await db.execute(
            "UPDATE articles SET version = version + (status <> 'published'), status = 'published' WHERE id = %s",
            article_id,
            conn=conn,
        )
        return

    publisher_id = await _freeze_publisher(db, conn, article_id, row['author_id'], row['publisher_id'])
    await db.execute(
        "UPDATE articles SET status = 'published', publisher_id = %s, published_at = COALESCE(published_at, NOW()), version = version + 1 WHERE id = %s",
        publisher_id,
        article_id,
        conn=conn,
    )
    await enqueue_notifications(db, article_id, conn=conn)


# The notification queue.


async def resolve_notify_channels(db: Database, article_id: int, *, conn: Connection | None = None) -> set[int]:
    """Return the set of notify channels for an article.

    The recipients are the category's and the tags' notify_channel_id, deduplicated. When a category's primary channel and a tag's additional channel are the same, that channel still gets only one message.

    No recipients means an empty set. There is no fallback to a default channel: a fallback would make the intent "this section is not notified" inexpressible, and an article would flow to the default channel even with notify_channel_id set to NULL.
    """
    rows = await db.fetch_all(
        'SELECT c.notify_channel_id AS channel_id FROM articles a JOIN categories c ON c.id = a.category_id '
        'WHERE a.id = %s AND c.notify_channel_id IS NOT NULL '
        'UNION '
        'SELECT t.notify_channel_id AS channel_id FROM article_tags at JOIN tags t ON t.id = at.tag_id '
        'WHERE at.article_id = %s AND t.notify_channel_id IS NOT NULL',
        article_id,
        article_id,
        conn=conn,
    )
    return {int(row['channel_id']) for row in rows}


async def enqueue_notifications(db: Database, article_id: int, *, conn: Connection | None = None) -> None:
    """Enqueue the article's notification recipients. The bot does the actual sending.

    Sending events from server straight to bot would miss every event that arrived while the bot was down. With the DB as the queue, the bot picks up the unsent rows once it is back.

    Both are INSERT IGNORE, so the (article_id, channel_id) and (article_id, user_id) unique constraints structurally prevent sending twice to the same recipient. What prevents a new row when the recipients themselves grow (a tag added after publication) is not a constraint but publish_article's decision to call this function only at the first publication. Those two together are why no flag is needed to avoid re-notifying on a typo fix.
    """
    for channel_id in sorted(await resolve_notify_channels(db, article_id, conn=conn)):
        await db.execute(
            'INSERT IGNORE INTO article_discord_posts (article_id, channel_id) VALUES (%s, %s)',
            article_id,
            channel_id,
            conn=conn,
        )

    # DMs to subscribers are enqueued independently of the channel notifications: a subscription holds even for an article with no destination channel. The author is excluded, since there is no point in being notified about an article you wrote yourself.
    await db.execute(
        'INSERT IGNORE INTO article_subscription_notices (article_id, user_id) '
        'SELECT DISTINCT a.id, ts.user_id FROM articles a '
        'JOIN article_tags at ON at.article_id = a.id '
        'JOIN tag_subscriptions ts ON ts.tag_id = at.tag_id '
        'WHERE a.id = %s AND ts.user_id <> a.author_id',
        article_id,
        conn=conn,
    )
