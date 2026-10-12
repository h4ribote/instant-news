"""The visibility check for articles and images.

Access is the product of two axes: status (the publication stage) and visibility. Both must pass. Check status alone and a draft leaks as a publicly scoped article; check visibility alone and unpublished draft copy gets read.

The check lives in this module. Article detail, listings, full-text search, image serving and og meta injection all have to answer the same question, and written per route one of them always ends up laxer; a single lax spot voids the access restriction everywhere.

Return 404 rather than 403 for anything the viewer cannot see (errors.NotFound). A 403 reveals that something is there, and for a limited-visibility article whose very existence is secret, that alone is a leak. Obeying this is the caller's job; this module returns only a boolean.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from common.db import Database
from common.settings import RuntimeSettings

from .principals import Principal, is_admin

# Only articles at this status are visible to users other than the author and an admin. draft / in_review / scheduled are draft copy not yet made public; archived has been withdrawn.
PUBLISHED: Final = 'published'


async def can_view_article(db: Database, principal: Principal | None, article: Mapping[str, Any], *, settings: RuntimeSettings) -> bool:
    """Return whether principal may view article.

    article is a row from articles and must carry id / author_id / status / visibility. The row itself is passed in so that callers which already hold it, such as listings, do not re-fetch it.

    settings is needed for the admin check: the third condition of admin eligibility (holding a role listed in the settings' admin.role_ids) cannot be decided without them, and making them optional would leave a caller that forgot to pass them quietly failing to treat a delegated admin as an admin. The caller passes the settings resolved from the settings table on this request, because a change to the admin roles has to reach the visibility check immediately.
    """
    author_id = article.get('author_id')
    if principal is not None and author_id is not None and int(author_id) == principal.user_id:
        # What you wrote yourself is readable whatever its status and visibility.
        return True
    if await _is_administrator(db, settings, principal):
        # Admins read unpublished draft copy too. Pre-publication review cannot work if the people accountable for editing see only their own articles.
        return True
    if article.get('status') != PUBLISHED:
        return False
    visibility = article.get('visibility')
    if visibility == 'public':
        return True
    # Anything but public is open only to logged-in users currently in the target guild. A former member's session may still exist, but the moment is_guild_member turns false they can no longer read.
    if principal is None or not principal.is_guild_member:
        return False
    if visibility == 'members':
        return True
    if visibility == 'restricted':
        return await _has_visibility_role(db, principal, int(article['id']))
    # Refuse an unknown visibility. When the enum grows, being invisible until the check is extended is the correct side to fall on, whereas visible by default would leak.
    return False


def visibility_sql_filter(principal: Principal | None) -> tuple[str, list[Any]]:
    """Return the condition fragment to splice into the WHERE of listing and search queries, and its parameters.

    The fragment is closed in parentheses, so callers can AND it on directly. **The condition refers to the articles table by the name articles.** The calling query must not alias articles: alias it and the articles.* this fragment names becomes an unresolvable column, and the query fails at runtime.

    This answers the visibility axis only, and it carries no admin clause at all. The status filter (published only, by default), an admin's access to unpublished articles, and an admin's access to published articles of limited visibility are all the caller's to add. Deciding admin means reading the roles' permission bits from the DB, which a synchronous function taking no database handle cannot do.

    The author's own articles are included to keep the listing in step with can_view_article. Without it, a limited-visibility article you wrote would be readable in detail yet absent from the listing. That same split still stands for admins, because no caller adds the admin clause: an admin holding none of a restricted article's roles reads it in detail yet never sees it listed.
    """
    if principal is None:
        return ("(articles.visibility = 'public')", [])
    # clauses and params are appended in the same order. Let the placeholder order and the parameter order drift apart and the check passes against the wrong role.
    clauses: list[str] = ["articles.visibility = 'public'", 'articles.author_id = %s']
    params: list[Any] = [principal.user_id]
    if principal.is_guild_member:
        clauses.append("articles.visibility = 'members'")
        if principal.role_ids:
            placeholders = _placeholders(len(principal.role_ids))
            clauses.append(
                f"(articles.visibility = 'restricted' AND EXISTS ("
                f'SELECT 1 FROM article_visibility_roles avr WHERE avr.article_id = articles.id AND avr.role_id IN ({placeholders})))'
            )
            params.extend(_ordered_role_ids(principal))
    return '(' + ' OR '.join(clauses) + ')', params


async def can_view_media(db: Database, principal: Principal | None, media_id: int, is_public: bool, *, settings: RuntimeSettings) -> bool:
    """Return whether principal may receive the image media_id.

    Public images (covers, ads, logos, icons) are served without authentication. Any other image is served if principal can view even one of the articles referencing it: the same bytes may be referenced from several articles, so a single readable article already puts that user in a position to see the image.

    Never skip the reference check and serve on "logged in" alone. The images inside an access-restricted article would then be handed to every member who cannot read it.
    """
    if is_public:
        return True
    # Pull the referencing articles. article_media is a fact recorded when the body was saved, so the body is not re-read to guess.
    articles = await db.fetch_all(
        'SELECT a.id, a.author_id, a.status, a.visibility'
        ' FROM article_media am JOIN articles a ON a.id = am.article_id'
        ' WHERE am.media_id = %s',
        media_id,
    )
    for article in articles:
        if await can_view_article(db, principal, article, settings=settings):
            return True
    return False


def _placeholders(count: int) -> str:
    """Build count placeholders to splice into an IN clause.

    The values themselves are never embedded in the SQL. Only the run of %s is built here; the values travel as parameters. Role IDs are already normalized to int, but a habit of concatenating here ends up repeated somewhere that takes strings.
    """
    return ', '.join(['%s'] * count)


def _ordered_role_ids(principal: Principal) -> list[int]:
    """Return the roles principal holds in a stable order.

    frozenset iteration order can vary from run to run. Fixing it makes the same query for the same principal always produce the same SQL and the same parameters, so log correlation and prepared-statement reuse keep working.
    """
    return sorted(principal.role_ids)


async def _is_administrator(db: Database, settings: RuntimeSettings, principal: Principal | None) -> bool:
    """Return whether principal is an admin. Unauthenticated (None) is false.

    The decision itself is left to principals.is_admin. A separate implementation here would split the definition of an admin across two places, and one of them always gets left behind: while this file held a version that ignored delegation via the admin.role_ids setting, a delegated admin could edit an article but got a 404 on viewing it. "It fails closed, so it is safe" is no account of correctness when that delegation is advertised as a central feature. Keep the check in one place now that the settings come from the settings table as well; split it in two and the same drift returns in the same shape.
    """
    if principal is None:
        return False
    return await is_admin(db, settings, principal)


async def _has_visibility_role(db: Database, principal: Principal, article_id: int) -> bool:
    """Return whether principal holds at least one role permitted to view the article."""
    if not principal.role_ids:
        return False
    role_ids = _ordered_role_ids(principal)
    found = await db.fetch_val(
        f'SELECT 1 FROM article_visibility_roles WHERE article_id = %s AND role_id IN ({_placeholders(len(role_ids))}) LIMIT 1',
        article_id,
        *role_ids,
    )
    return found is not None
