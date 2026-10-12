"""Article endpoints.

An article the caller cannot view returns 404, not 403. A 403 reveals that something is there, and for a limited-visibility article whose very existence is secret, that alone is a leak. The rule applies uniformly to the detail, the revision history and the diff: hiding the article body achieves nothing if the title and body can still be read out of the revision history.

The business rules live in articles.py; this module sticks to receiving requests and assembling responses, so that the scheduled-publishing loop can call the same save and publish procedures.

Reads address an article by slug (the permalink), writes by id (immutable). GET /api/articles/by-id/{id} closes the gap for clients that hold only the id. A slug the article used to have answers 301 to its current slug, but only to a viewer who may see the article; anyone else gets the same 404 as for a slug that never existed.

The detail, by-id, create, update and publish responses carry the article's ETag. PATCH and publish honour If-Match when it is present and answer 412 when the article moved on; without it they behave as before. POST honours Idempotency-Key, replaying the article a first request under the same key created. Creates and updates made with an API key report the key's rate limit standing in their headers (app.rate_limit).
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Final
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings
from common.snowflake import to_snowflake

from ..article_query import LATEST_ORDER, article_paging, fetch_article_page, published_and_visible
from ..articles import article_etag, ensure_may_edit, has_write_any, publish_article, resolve_moved_slug, save_article
from ..deps import get_config, get_db, get_principal_optional, get_settings, require_scope
from ..errors import BadRequest, Conflict, MovedPermanently, NotFound
from ..idempotency import IdempotencyRace, IdempotentResult, find_replay, idempotency_request
from ..paging import KeysetOrder, PagingParams, SortKey, fetch_keyset, page_request, paging_params
from ..principals import Principal
from ..rate_limit import RATE_LIMIT_HEADERS_DOC, track_rate_limit
from ..schemas import (
    ArticleCreate,
    ArticleListItem,
    ArticleOut,
    ArticleSaveOut,
    ArticleUpdate,
    CommentAuthorOut,
    CommentListOut,
    CommentOut,
    DiscordPostOut,
    MediaFailure,
    Page,
    RevisionDiffOut,
    RevisionListItem,
    RevisionOut,
)
from ..visibility import can_view_article

router = APIRouter(tags=['articles'])

# Comments per page. Larger than for articles because each comment is small and they are read straight through as a conversation.
_comment_paging: Final = paging_params(50, 200)
_revision_paging: Final = paging_params(20, 100)

# Comments oldest first, because comments are a conversation; the id orders comments posted in the same second.
_COMMENT_ORDER: Final = KeysetOrder(
    (
        SortKey('c.posted_at', lambda row: row['posted_at'], 'datetime'),
        SortKey('c.id', lambda row: row['id'], 'int'),
    )
)
# Revisions newest first: the latest revision is what a reader looks for, and the history is read back from there.
_REVISION_ORDER: Final = KeysetOrder((SortKey('r.revision_number', lambda row: row['revision_number'], 'int', descending=True),))

# One entity-tag inside an If-Match list: an optional weak marker and the quoted opaque value.
_ENTITY_TAG_RE: Final = re.compile(r'\s*(W/)?"([^"]*)"\s*')

_IF_MATCH_DESCRIPTION: Final = 'GET / 保存の応答の ETag。付いていれば、記事がその版から更新されていた場合に 412 を返す。'

# The endpoint name an article creation's Idempotency-Key is recorded under.
_CREATE_ENDPOINT: Final = 'POST /api/articles'

# Response headers as declared in the OpenAPI document.
_ETAG_HEADER: Final[dict[str, Any]] = {'ETag': {'description': '記事の編集可能な状態を表す強い ETag("<id>-<version>")。', 'schema': {'type': 'string'}}}
_MOVED: Final[dict[int | str, dict[str, Any]]] = {
    301: {
        'description': '記事が過去に使っていた slug。同じ URL の slug を現在のものに替えた先へ転送する。',
        'headers': {'Location': {'description': '転送先。', 'schema': {'type': 'string'}}},
    }
}

# Up/down votes and the comment count are summed over all of the article's notification posts: one article can be notified to several channels, so article_discord_posts holds several rows per article, each with its own independent counts.
# When the subtraction is done in SQL, always subtract values that have been through SUM(). up_count and down_count are unsigned, so a per-row subtraction (dp.up_count - dp.down_count) does not clamp the moment down-votes exceed up-votes; the statement itself fails. SUM() returns DECIMAL, which can hold negatives.
_ARTICLE_COLUMNS: Final = """
    articles.id, articles.slug, articles.title, articles.excerpt, articles.body_md, articles.body_html,
    articles.cover_image_url, articles.author_id, articles.publisher_id, articles.category_id,
    articles.series_id, articles.series_position, articles.visibility, articles.byline_mode,
    articles.is_breaking, articles.is_sponsored, articles.sponsor_name, articles.status,
    articles.view_count, articles.version, articles.scheduled_at, articles.published_at, articles.created_at, articles.updated_at,
    u.discord_user_id AS author_discord_user_id, u.username AS author_username,
    u.global_name AS author_global_name, u.avatar_hash AS author_avatar_hash,
    p.role_id AS publisher_role_id, p.slug AS publisher_slug, p.name AS publisher_name,
    p.description AS publisher_description, p.logo_url AS publisher_logo_url, p.stance AS publisher_stance,
    c.slug AS category_slug, c.name AS category_name, c.position AS category_position,
    c.notify_channel_id AS category_notify_channel_id, c.include_in_latest AS category_include_in_latest,
    s.slug AS series_slug, s.title AS series_title,
    (SELECT COALESCE(SUM(dp.up_count), 0) FROM article_discord_posts dp WHERE dp.article_id = articles.id) AS up_count,
    (SELECT COALESCE(SUM(dp.down_count), 0) FROM article_discord_posts dp WHERE dp.article_id = articles.id) AS down_count,
    (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = articles.id) AS comment_count
"""

_ARTICLE_FROM: Final = """
    FROM articles
    JOIN users u ON u.id = articles.author_id
    JOIN categories c ON c.id = articles.category_id
    LEFT JOIN publishers p ON p.id = articles.publisher_id
    LEFT JOIN series s ON s.id = articles.series_id
"""

_REVISION_SELECT: Final = """
SELECT r.id, r.revision_number, r.editor_id, r.via_token_id, r.title, r.body_md, r.excerpt, r.note,
       r.is_correction, r.created_at,
       u.discord_user_id AS editor_discord_user_id, u.username AS editor_username,
       u.global_name AS editor_global_name, u.avatar_hash AS editor_avatar_hash
  FROM article_revisions r
  JOIN users u ON u.id = r.editor_id
"""

_COMMENT_SELECT: Final = """
SELECT c.id, c.message_id, c.author_discord_user_id, c.body, c.up_count, c.down_count,
       c.posted_at, c.edited_at,
       u.username AS author_username, u.global_name AS author_global_name,
       u.avatar_hash AS author_avatar_hash
  FROM article_comments c
  LEFT JOIN users u ON u.discord_user_id = c.author_discord_user_id
"""


# Rows to response shapes


def _author_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['author_id'],
        'discord_user_id': row['author_discord_user_id'],
        'username': row['author_username'],
        'global_name': row['author_global_name'],
        'avatar_hash': row['author_avatar_hash'],
    }


def _publisher_data(row: dict[str, Any]) -> dict[str, Any] | None:
    if row['publisher_id'] is None:
        return None
    return {
        'id': row['publisher_id'],
        'role_id': row['publisher_role_id'],
        'slug': row['publisher_slug'],
        'name': row['publisher_name'],
        'description': row['publisher_description'],
        'logo_url': row['publisher_logo_url'],
        'stance': row['publisher_stance'],
    }


def _category_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['category_id'],
        'slug': row['category_slug'],
        'name': row['category_name'],
        'position': row['category_position'],
        'notify_channel_id': row['category_notify_channel_id'],
        'include_in_latest': bool(row['category_include_in_latest']),
    }


def _series_data(row: dict[str, Any]) -> dict[str, Any] | None:
    if row['series_id'] is None:
        return None
    return {'id': row['series_id'], 'slug': row['series_slug'], 'title': row['series_title']}


def _list_item_data(row: dict[str, Any], tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'slug': row['slug'],
        'title': row['title'],
        'excerpt': row['excerpt'],
        'cover_image_url': row['cover_image_url'],
        'author': _author_data(row),
        'publisher': _publisher_data(row),
        'category': _category_data(row),
        'series': _series_data(row),
        'series_position': row['series_position'],
        'tags': tags,
        'visibility': row['visibility'],
        'byline_mode': row['byline_mode'],
        'is_breaking': bool(row['is_breaking']),
        'is_sponsored': bool(row['is_sponsored']),
        'sponsor_name': row['sponsor_name'],
        'status': row['status'],
        'view_count': int(row['view_count']),
        # SUM() comes back from MySQL as Decimal, so convert explicitly instead of passing it straight into an int field.
        'up_count': int(row['up_count']),
        'down_count': int(row['down_count']),
        'score': int(row['up_count']) - int(row['down_count']),
        'comment_count': int(row['comment_count']),
        'published_at': row['published_at'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def _revision_item_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'revision_number': row['revision_number'],
        'editor': {
            'id': row['editor_id'],
            'discord_user_id': row['editor_discord_user_id'],
            'username': row['editor_username'],
            'global_name': row['editor_global_name'],
            'avatar_hash': row['editor_avatar_hash'],
        },
        'via_token_id': row['via_token_id'],
        'title': row['title'],
        'excerpt': row['excerpt'],
        'note': row['note'],
        'is_correction': bool(row['is_correction']),
        'created_at': row['created_at'],
    }


def _comment_out(row: dict[str, Any]) -> CommentOut:
    return CommentOut(
        id=row['id'],
        message_id=row['message_id'],
        author=CommentAuthorOut(
            discord_user_id=row['author_discord_user_id'],
            username=row['author_username'],
            global_name=row['author_global_name'],
            avatar_hash=row['author_avatar_hash'],
        ),
        body=row['body'],
        up_count=int(row['up_count']),
        down_count=int(row['down_count']),
        # The subtraction is done here. Subtracting the row's columns from each other in SQL is unsigned arithmetic, and the statement fails the moment down-votes exceed up-votes.
        score=int(row['up_count']) - int(row['down_count']),
        posted_at=row['posted_at'],
        edited_at=row['edited_at'],
    )


async def _fetch_tags(db: Database, article_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Fetch the tags of each article.

    JOINing them into the article query itself duplicates an article's row once per tag. One extra query fetches them all at once and assigns them to their articles.
    """
    if not article_ids:
        return {}
    placeholders = ', '.join(['%s'] * len(article_ids))
    rows = await db.fetch_all(
        f'SELECT at.article_id, t.id, t.slug, t.name, t.description, t.icon_url, t.notify_channel_id '
        f'FROM article_tags at JOIN tags t ON t.id = at.tag_id WHERE at.article_id IN ({placeholders}) ORDER BY t.name',
        *article_ids,
    )
    grouped: dict[int, list[dict[str, Any]]] = {article_id: [] for article_id in article_ids}
    for row in rows:
        grouped[row['article_id']].append(
            {
                'id': row['id'],
                'slug': row['slug'],
                'name': row['name'],
                'description': row['description'],
                'icon_url': row['icon_url'],
                'notify_channel_id': row['notify_channel_id'],
            }
        )
    return grouped


def _thread_url(guild_id: int, thread_id: int | None) -> str | None:
    """URL that opens the thread. A thread is itself a channel, so it is addressed in the same form as a channel URL.

    The server builds it because the URL needs the target guild's ID: guild_id exists only in the platform settings, and the UI has no way to know it.
    """
    if thread_id is None:
        return None
    return f'https://discord.com/channels/{guild_id}/{thread_id}'


async def _fetch_discord_posts(db: Database, settings: RuntimeSettings, article_id: int) -> list[DiscordPostOut]:
    """Fetch the breakdown of an article's notification posts. Rows not yet sent are returned too: that a post has not gone out yet is a fact the article page may know."""
    rows = await db.fetch_all(
        'SELECT dp.channel_id, dp.message_id, dp.thread_id, dp.up_count, dp.down_count, dp.posted_at, '
        '(SELECT COUNT(*) FROM article_comments ac WHERE ac.post_id = dp.id) AS comment_count '
        'FROM article_discord_posts dp WHERE dp.article_id = %s ORDER BY dp.enqueued_at, dp.id',
        article_id,
    )
    return [
        DiscordPostOut(
            channel_id=row['channel_id'],
            message_id=row['message_id'],
            thread_id=row['thread_id'],
            thread_url=_thread_url(settings.discord.guild_id, row['thread_id']),
            up_count=int(row['up_count']),
            down_count=int(row['down_count']),
            # The subtraction is done here. Subtracting the row's columns from each other in SQL is unsigned arithmetic, and the statement fails.
            score=int(row['up_count']) - int(row['down_count']),
            comment_count=int(row['comment_count']),
            posted_at=row['posted_at'],
        )
        for row in rows
    ]


async def _article_out(db: Database, settings: RuntimeSettings, row: dict[str, Any]) -> ArticleOut:
    """Assemble the article detail response."""
    tags = (await _fetch_tags(db, [row['id']])).get(row['id'], [])
    role_rows = await db.fetch_all('SELECT role_id FROM article_visibility_roles WHERE article_id = %s', row['id'])
    correction_rows = await db.fetch_all(
        f'{_REVISION_SELECT} WHERE r.article_id = %s AND r.is_correction = 1 ORDER BY r.revision_number',
        row['id'],
    )
    data = _list_item_data(row, tags)
    data.update(
        {
            'version': int(row['version']),
            'body_md': row['body_md'],
            'body_html': row['body_html'],
            'visibility_role_ids': [role_row['role_id'] for role_row in role_rows],
            'scheduled_at': row['scheduled_at'],
            'corrections': [_revision_item_data(correction) for correction in correction_rows],
            'discord_posts': await _fetch_discord_posts(db, settings, int(row['id'])),
        }
    )
    return ArticleOut.model_validate(data)


# Loading an article and the visibility check


def _parse_id(value: str) -> int:
    """Parse the path ID to int. A value that cannot be read as an ID gives 404: there is no point distinguishing it from an article that does not exist."""
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound('記事が見つかりません。') from exc


async def _load_article(db: Database, *, article_id: int | None = None, slug: str | None = None) -> dict[str, Any] | None:
    if article_id is not None:
        return await db.fetch_one(f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_FROM} WHERE articles.id = %s', article_id)
    return await db.fetch_one(f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_FROM} WHERE articles.slug = %s', slug)


def _moved_location(request: Request, new_slug: str) -> str:
    """The URL of the same endpoint with the slug segment replaced by the article's current slug. Every other path parameter and the query string are kept."""
    params = {**request.path_params, 'slug': new_slug}
    path = request.scope['route'].path_format.format(**{name: quote(str(value), safe='') for name, value in params.items()})
    query = request.url.query
    return f'{path}?{query}' if query else path


async def _require_viewable(db: Database, settings: RuntimeSettings, principal: Principal | None, request: Request, slug: str) -> dict[str, Any]:
    """Load an article by slug and 404 if it cannot be viewed. settings is needed for the admin check, which includes the roles delegated through the settings.

    A slug the article used to have raises MovedPermanently towards the current one, after the same visibility check: redirecting first would reveal the new slug of an article the viewer cannot see.
    """
    row = await _load_article(db, slug=slug)
    if row is None:
        moved_id = await resolve_moved_slug(db, slug)
        moved = await _load_article(db, article_id=moved_id) if moved_id is not None else None
        if moved is not None and await can_view_article(db, principal, moved, settings=settings):
            raise MovedPermanently(_moved_location(request, str(moved['slug'])))
        raise NotFound('記事が見つかりません。')
    if not await can_view_article(db, principal, row, settings=settings):
        raise NotFound('記事が見つかりません。')
    return row


def _set_etag(response: Response, row: dict[str, Any]) -> None:
    response.headers['ETag'] = article_etag(int(row['id']), int(row['version']))


def _if_match_versions(if_match: str | None, article_id: int) -> frozenset[int] | None:
    """Read If-Match into the set of versions it accepts for this article.

    None means nothing to check: the header is absent, or is * (any current representation, and the article's existence is checked anyway). Following RFC 9110 the comparison is strong, so a weak entity-tag never matches; neither does a tag of another article or one this server did not issue. A header that names nothing matching yields the empty set, which no version satisfies.
    """
    if if_match is None or if_match.strip() == '*':
        return None
    versions: set[int] = set()
    prefix = f'{article_id}-'
    for part in if_match.split(','):
        match = _ENTITY_TAG_RE.fullmatch(part)
        if match is None or match.group(1):
            continue
        opaque = match.group(2)
        version = opaque[len(prefix):]
        if opaque.startswith(prefix) and version.isascii() and version.isdigit():
            versions.add(int(version))
    return frozenset(versions)


async def _require_editable(db: Database, settings: RuntimeSettings, principal: Principal, article_id: int) -> dict[str, Any]:
    """Load an article and confirm the principal may edit and publish it."""
    row = await _load_article(db, article_id=article_id)
    if row is None:
        raise NotFound('記事が見つかりません。')
    ensure_may_edit(principal, row, write_any=await has_write_any(db, settings, principal))
    return row


# Listing


@router.get('/api/articles')
async def list_articles(
    category: str | None = Query(default=None, description='カテゴリの slug で絞る。'),
    tag: str | None = Query(default=None, description='タグの slug で絞る。'),
    publisher: str | None = Query(default=None, description='報道社の slug で絞る。'),
    series: str | None = Query(default=None, description='連載の slug で絞る。'),
    author: str | None = Query(default=None, description='著者の利用者 ID で絞る。'),
    latest_only: bool = Query(default=False, description='新着一覧に載せるカテゴリの記事だけを返す。'),
    paging: PagingParams = Depends(article_paging),
    db: Database = Depends(get_db),
    principal: Principal | None = Depends(get_principal_optional),
) -> Page[ArticleListItem]:
    """Return published articles newest first. The visibility filter is always applied, and no admin clause is added on top of it: see visibility_sql_filter for what that leaves out.

    Each filter is a condition on articles' own columns (a subquery resolves a slug to its ID), so the listing runs on the articles indexes and the total, when asked for, counts over articles alone.
    """
    where, params = published_and_visible(principal)
    conditions = [where]
    filters: dict[str, Any] = {}

    if category is not None:
        conditions.append('articles.category_id = (SELECT id FROM categories WHERE slug = %s)')
        params.append(category)
        filters['category'] = category
    if publisher is not None:
        conditions.append('articles.publisher_id = (SELECT id FROM publishers WHERE slug = %s)')
        params.append(publisher)
        filters['publisher'] = publisher
    if series is not None:
        conditions.append('articles.series_id = (SELECT id FROM series WHERE slug = %s)')
        params.append(series)
        filters['series'] = series
    if author is not None:
        try:
            author_id = to_snowflake(author)
        except ValueError as exc:
            raise BadRequest(f'author は利用者 ID として解釈できません: {author}') from exc
        conditions.append('articles.author_id = %s')
        params.append(author_id)
        filters['author'] = author_id
    if tag is not None:
        # Filter with EXISTS: a JOIN duplicates the row of an article that has several tags.
        conditions.append('EXISTS (SELECT 1 FROM article_tags atg JOIN tags t ON t.id = atg.tag_id WHERE atg.article_id = articles.id AND t.slug = %s)')
        params.append(tag)
        filters['tag'] = tag
    if latest_only:
        # Keeps recurring articles such as market prices from filling the site-wide latest feed.
        conditions.append('articles.category_id IN (SELECT id FROM categories WHERE include_in_latest = 1)')
        filters['latest_only'] = True

    return await fetch_article_page(
        db,
        page_request('articles', filters, paging),
        where=' AND '.join(conditions),
        where_args=params,
        order=LATEST_ORDER,
    )


# Saving and publishing


@router.post(
    '/api/articles',
    status_code=201,
    dependencies=[Depends(track_rate_limit)],
    responses={201: {'headers': {**_ETAG_HEADER, **RATE_LIMIT_HEADERS_DOC, 'Idempotent-Replayed': {'description': '同じ Idempotency-Key の最初の要求の結果を再生したときに true。', 'schema': {'type': 'string'}}}}},
)
async def create_article(
    payload: ArticleCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, description='再送で記事が重複しないためのキー(UUID など)。同じキーと同じ本体の再送には、最初の要求が作った記事を返す。'),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal = Depends(require_scope('articles:write')),
) -> ArticleSaveOut:
    """Create an article.

    With an Idempotency-Key, a key already used with the same body replays the article it created (checked before the rate limit and before any image is fetched, so a replay costs neither), and one used with a different body is refused.
    """
    idempotency = idempotency_request(_CREATE_ENDPOINT, idempotency_key, payload)
    if idempotency is not None:
        replayed = await find_replay(db, principal.user_id, idempotency)
        if replayed is not None:
            return await _replay_response(db, settings, response, replayed)
    try:
        article_id, failures = await save_article(db, config, settings, principal, article_id=None, payload=payload, idempotency=idempotency)
    except IdempotencyRace:
        replayed = await find_replay(db, principal.user_id, idempotency) if idempotency is not None else None
        if replayed is None:
            raise Conflict('同じ Idempotency-Key の要求が同時に処理されています。再送してください。') from None
        return await _replay_response(db, settings, response, replayed)
    return await _save_response(db, settings, response, article_id, [MediaFailure(source_url=failure.source_url, reason=failure.reason) for failure in failures])


async def _replay_response(db: Database, settings: RuntimeSettings, response: Response, replayed: IdempotentResult) -> ArticleSaveOut:
    """The response to a resend: the article as it stands now, with the images the first request could not ingest."""
    response.headers['Idempotent-Replayed'] = 'true'
    failures = (replayed.replay or {}).get('media_failures', [])
    return await _save_response(db, settings, response, replayed.resource_id, [MediaFailure.model_validate(failure) for failure in failures])


@router.patch(
    '/api/articles/{article_id}',
    dependencies=[Depends(track_rate_limit)],
    responses={200: {'headers': {**_ETAG_HEADER, **RATE_LIMIT_HEADERS_DOC}}},
)
async def update_article(
    article_id: str,
    payload: ArticleUpdate,
    response: Response,
    if_match: str | None = Header(default=None, description=_IF_MATCH_DESCRIPTION),
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal = Depends(require_scope('articles:write')),
) -> ArticleSaveOut:
    """Partially update an article: only the fields the body carries change."""
    parsed = _parse_id(article_id)
    saved_id, failures = await save_article(
        db,
        config,
        settings,
        principal,
        article_id=parsed,
        payload=payload,
        expected_versions=_if_match_versions(if_match, parsed),
    )
    return await _save_response(db, settings, response, saved_id, [MediaFailure(source_url=failure.source_url, reason=failure.reason) for failure in failures])


async def _save_response(db: Database, settings: RuntimeSettings, response: Response, article_id: int, failures: list[MediaFailure]) -> ArticleSaveOut:
    """Assemble the save response.

    Images that could not be ingested always go into the response. A URL that failed to ingest stays in the body as it is, and on an external CDN it eventually expires and the image disappears, so treating the save as a silent success leaves a rotting link in the article that nobody ever notices.
    """
    row = await _load_article(db, article_id=article_id)
    if row is None:
        raise NotFound('記事が見つかりません。')
    _set_etag(response, row)
    return ArticleSaveOut(article=await _article_out(db, settings, row), media_failures=failures)


@router.post('/api/articles/{article_id}/publish', responses={200: {'headers': _ETAG_HEADER}})
async def publish(
    article_id: str,
    response: Response,
    if_match: str | None = Header(default=None, description=_IF_MATCH_DESCRIPTION),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal = Depends(require_scope('articles:write')),
) -> ArticleOut:
    """Publish an article: freeze the byline and enqueue the notifications in the same transaction."""
    parsed = _parse_id(article_id)
    await _require_editable(db, settings, principal, parsed)
    await publish_article(db, settings, parsed, expected_versions=_if_match_versions(if_match, parsed))
    row = await _load_article(db, article_id=parsed)
    if row is None:
        raise NotFound('記事が見つかりません。')
    _set_etag(response, row)
    return await _article_out(db, settings, row)


# Detail and revision history


@router.get('/api/articles/by-id/{article_id}', responses={200: {'headers': _ETAG_HEADER}})
async def get_article_by_id(
    article_id: str,
    response: Response,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> ArticleOut:
    """Return the article detail by its immutable id. 404 if it cannot be viewed.

    view_count is not advanced: this is how the editor and API clients load an article they are about to write, which is not a read by a reader.
    """
    row = await _load_article(db, article_id=_parse_id(article_id))
    if row is None or not await can_view_article(db, principal, row, settings=settings):
        raise NotFound('記事が見つかりません。')
    _set_etag(response, row)
    return await _article_out(db, settings, row)


@router.get('/api/articles/{slug}', responses={200: {'headers': _ETAG_HEADER}, **_MOVED})
async def get_article(
    slug: str,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> ArticleOut:
    """Return the article detail. 404 if it cannot be viewed, 301 if the slug is one the article used to have."""
    row = await _require_viewable(db, settings, principal, request, slug)
    # updated_at is explicitly written back to its own value. Omit it and ON UPDATE CURRENT_TIMESTAMP fires, advancing the last-updated time on every view, so an article that was edited can no longer be told apart from one that was merely read.
    await db.execute('UPDATE articles SET view_count = view_count + 1, updated_at = updated_at WHERE id = %s', row['id'])
    row['view_count'] = int(row['view_count']) + 1
    _set_etag(response, row)
    return await _article_out(db, settings, row)


@router.get('/api/articles/{slug}/comments', responses=_MOVED)
async def list_comments(
    slug: str,
    request: Request,
    paging: PagingParams = Depends(_comment_paging),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> CommentListOut:
    """Return one page of an article's comments, oldest first. 404 if the article cannot be viewed.

    A comment's visibility is the article's visibility itself. Returning comments to someone who cannot read the article leaks what was said about it and how much, while the body stays hidden. The check is delegated to the same _require_viewable the article detail uses; a separate check written here would go stale on its own the day the article-side rule changes.

    Oldest first because comments are a conversation. Sorting by score puts a reply ahead of the message it answers and breaks the context.

    This API is read-only and accepts no writes. The real comments are the replies in the Discord thread, and that is the only place to write them (thread_url is the entry point).
    """
    article = await _require_viewable(db, settings, principal, request, slug)
    article_id = int(article['id'])
    fetched = await fetch_keyset(
        db,
        page_request('comments', {'article_id': article_id}, paging),
        select=_COMMENT_SELECT,
        count_from='FROM article_comments c',
        where='c.article_id = %s',
        where_args=[article_id],
        order=_COMMENT_ORDER,
    )
    # A thread is opened per notification post, so one article can have several. The one offered as the entry point is the thread of the notification post sent first; comments from any thread count as the same article's comments.
    thread_row = await db.fetch_one(
        'SELECT thread_id FROM article_discord_posts WHERE article_id = %s AND thread_id IS NOT NULL ORDER BY enqueued_at, id LIMIT 1',
        article_id,
    )
    return CommentListOut(
        items=[_comment_out(row) for row in fetched.rows],
        per_page=fetched.per_page,
        next_cursor=fetched.next_cursor,
        prev_cursor=fetched.prev_cursor,
        total=fetched.total,
        thread_url=_thread_url(settings.discord.guild_id, thread_row['thread_id']) if thread_row else None,
    )


@router.get('/api/articles/{slug}/revisions', responses=_MOVED)
async def list_revisions(
    slug: str,
    request: Request,
    paging: PagingParams = Depends(_revision_paging),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> Page[RevisionListItem]:
    """Return one page of the revision history, newest first."""
    article = await _require_viewable(db, settings, principal, request, slug)
    article_id = int(article['id'])
    fetched = await fetch_keyset(
        db,
        page_request('revisions', {'article_id': article_id}, paging),
        select=_REVISION_SELECT,
        count_from='FROM article_revisions r',
        where='r.article_id = %s',
        where_args=[article_id],
        order=_REVISION_ORDER,
    )
    return Page[RevisionListItem](
        items=[RevisionListItem.model_validate(_revision_item_data(row)) for row in fetched.rows],
        per_page=fetched.per_page,
        next_cursor=fetched.next_cursor,
        prev_cursor=fetched.prev_cursor,
        total=fetched.total,
    )


@router.get('/api/articles/{slug}/revisions/{revision_number}', responses=_MOVED)
async def get_revision(
    slug: str,
    revision_number: int,
    request: Request,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> RevisionOut:
    """Return a past revision's snapshot."""
    article = await _require_viewable(db, settings, principal, request, slug)
    row = await _fetch_revision(db, article['id'], revision_number)
    data = _revision_item_data(row)
    data['body_md'] = row['body_md']
    return RevisionOut.model_validate(data)


@router.get('/api/articles/{slug}/revisions/{revision_number}/diff', responses=_MOVED)
async def get_revision_diff(
    slug: str,
    revision_number: int,
    request: Request,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> RevisionDiffOut:
    """Return the diff against the immediately preceding revision as an array of unified-diff lines."""
    article = await _require_viewable(db, settings, principal, request, slug)
    current = await _fetch_revision(db, article['id'], revision_number)
    # The immediately preceding revision is not necessarily revision_number - 1. The numbers run from 1 within an article, but taking the largest number below this one as the predecessor keeps the order right even where the history has gaps.
    previous = await db.fetch_one(
        f'{_REVISION_SELECT} WHERE r.article_id = %s AND r.revision_number < %s ORDER BY r.revision_number DESC LIMIT 1',
        article['id'],
        revision_number,
    )
    previous_body = previous['body_md'] if previous else ''
    previous_number = previous['revision_number'] if previous else None
    diff = difflib.unified_diff(
        previous_body.splitlines(),
        current['body_md'].splitlines(),
        fromfile=f'revision {previous_number}' if previous_number is not None else '(初版の前)',
        tofile=f'revision {current["revision_number"]}',
        # splitlines() has already dropped the trailing newlines, so the diff must not add them back.
        lineterm='',
    )
    return RevisionDiffOut(revision_number=current['revision_number'], previous_revision_number=previous_number, diff=list(diff))


async def _fetch_revision(db: Database, article_id: int, revision_number: int) -> dict[str, Any]:
    row = await db.fetch_one(
        f'{_REVISION_SELECT} WHERE r.article_id = %s AND r.revision_number = %s',
        article_id,
        revision_number,
    )
    if row is None:
        raise NotFound('その改版は存在しません。')
    return row
