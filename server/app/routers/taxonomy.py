"""Read API for the taxonomy (categories, tags, publishers, series).

This module also holds the article listing projection (fetch_article_page), which the search router reuses. A listed article has the same shape (ArticleListItem) whatever the filter condition, and writing the assembly separately per condition would let the byline, reaction counts and tag resolution diverge between paths.

Every listing goes through the visibility filter (visibility_sql_filter) and additionally imposes status='published'. The first keeps articles the caller must not read out of sight; the second keeps not-yet-published articles out of the public index. With the visibility filter alone, an author or an admin would see their own drafts (published_at NULL) mixed into the timeline and into search results. A draft is reached through the writer's own article listing, not lined up in the topical index. The fragment carries no admin clause and this module adds none, so a listing shows an admin only what their own roles allow, even for an article can_view_article would let them open; visibility_sql_filter records that gap.

When building SQL, only constants written in this module are interpolated into f-strings (join clauses, orderings, the number of %s in an IN clause); values that came from the request are always passed as placeholders (%s).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from common.db import Database

from ..deps import get_db, get_principal_optional
from ..errors import NotFound
from ..principals import Principal
from ..rankings import ARTICLE_SCORE_SQL, article_totals_sql
from ..schemas import (
    ArticleListItem,
    CategoryOut,
    Page,
    PublisherDetailOut,
    PublisherOut,
    PublisherStats,
    SeriesBrief,
    SeriesDetailOut,
    SeriesOut,
    TagDetailOut,
    TagOut,
    UserBrief,
)
from ..visibility import visibility_sql_filter

router = APIRouter(tags=['taxonomy'])

# Items per page of a listing. The tag timeline and search take only page in the query, so the count is fixed here.
DEFAULT_PER_PAGE: Final = 20
# Max articles attached to a publisher or series page. A detail page is there to give an overview of that publisher or series; the full set is reachable through the filters on /api/articles. Without a cap, the page of a publisher with many articles is the one that grows unboundedly heavy.
DETAIL_ARTICLES_LIMIT: Final = 50


class CategoryListItem(CategoryOut):
    """An item of the category listing.

    article_count counts only the published articles the caller can view. Counting every article would leak the existence and the number of unreadable articles through the count.
    """

    article_count: int


class PublisherPageOut(PublisherDetailOut):
    """The publisher page response: profile, affiliated reporters and track record, plus a listing of that publisher's articles."""

    articles: list[ArticleListItem] = Field(default_factory=list)


# The article listing projection (the search router uses it too).

# The columns of a listed article. DictCursor collapses same-named columns last-one-wins, so every column from a joined table is given a prefixed alias.
_ARTICLE_COLUMNS: Final = '''
    articles.id AS id,
    articles.slug AS slug,
    articles.title AS title,
    articles.excerpt AS excerpt,
    articles.cover_image_url AS cover_image_url,
    articles.series_position AS series_position,
    articles.visibility AS visibility,
    articles.byline_mode AS byline_mode,
    articles.is_breaking AS is_breaking,
    articles.is_sponsored AS is_sponsored,
    articles.sponsor_name AS sponsor_name,
    articles.status AS status,
    articles.view_count AS view_count,
    articles.published_at AS published_at,
    articles.created_at AS created_at,
    articles.updated_at AS updated_at,
    au.id AS author_id,
    au.discord_user_id AS author_discord_user_id,
    au.username AS author_username,
    au.global_name AS author_global_name,
    au.avatar_hash AS author_avatar_hash,
    pb.id AS publisher_id,
    pb.role_id AS publisher_role_id,
    pb.slug AS publisher_slug,
    pb.name AS publisher_name,
    pb.description AS publisher_description,
    pb.logo_url AS publisher_logo_url,
    pb.stance AS publisher_stance,
    ct.id AS category_id,
    ct.slug AS category_slug,
    ct.name AS category_name,
    ct.position AS category_position,
    ct.notify_channel_id AS category_notify_channel_id,
    ct.include_in_latest AS category_include_in_latest,
    sr.id AS series_id,
    sr.slug AS series_slug,
    sr.title AS series_title,
    COALESCE((SELECT SUM(adp.up_count) FROM article_discord_posts adp WHERE adp.article_id = articles.id), 0) AS up_count,
    COALESCE((SELECT SUM(adp.down_count) FROM article_discord_posts adp WHERE adp.article_id = articles.id), 0) AS down_count,
    (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = articles.id) AS comment_count
'''

# The joins that resolve an article's byline and taxonomy. author_id and category_id are NOT NULL foreign keys, so an inner join is enough and does not change the row count. The reaction counts are correlated subqueries rather than joins because joining multiplies an article into as many rows as it has notify targets, which then needs deduping, and because it would fold every notification row for a tally only one page of articles needs.
_ARTICLE_JOINS: Final = '''
    FROM articles
    JOIN users au ON au.id = articles.author_id
    JOIN categories ct ON ct.id = articles.category_id
    LEFT JOIN publishers pb ON pb.id = articles.publisher_id
    LEFT JOIN series sr ON sr.id = articles.series_id
'''


def published_and_visible(principal: Principal | None) -> tuple[str, list[Any]]:
    """Return the WHERE fragment narrowing to published articles the caller can view, and its arguments."""
    visibility_sql, visibility_args = visibility_sql_filter(principal)
    return f"articles.status = 'published' AND ({visibility_sql})", list(visibility_args)


async def _fetch_tags_for_articles(db: Database, article_ids: Sequence[int]) -> dict[int, list[TagOut]]:
    """Fetch the tags for a set of article IDs in one query, grouped by article ID.

    One query per article would cost as many round trips as there are items on the page, so they are fetched together.
    """
    if not article_ids:
        return {}
    placeholders = ', '.join(['%s'] * len(article_ids))
    rows = await db.fetch_all(
        f'''
        SELECT atg.article_id AS article_id, t.id AS id, t.slug AS slug, t.name AS name,
               t.description AS description, t.icon_url AS icon_url, t.notify_channel_id AS notify_channel_id
        FROM article_tags atg
        JOIN tags t ON t.id = atg.tag_id
        WHERE atg.article_id IN ({placeholders})
        ORDER BY t.name
        ''',
        *article_ids,
    )
    grouped: dict[int, list[TagOut]] = {}
    for row in rows:
        grouped.setdefault(int(row['article_id']), []).append(TagOut.model_validate(row))
    return grouped


def _article_list_item(row: Mapping[str, Any], tags: list[TagOut]) -> ArticleListItem:
    """Map one row of the listing query to an ArticleListItem."""
    publisher: PublisherOut | None = None
    if row['publisher_id'] is not None:
        publisher = PublisherOut(
            id=row['publisher_id'],
            role_id=row['publisher_role_id'],
            slug=row['publisher_slug'],
            name=row['publisher_name'],
            description=row['publisher_description'],
            logo_url=row['publisher_logo_url'],
            stance=row['publisher_stance'],
        )
    series: SeriesBrief | None = None
    if row['series_id'] is not None:
        series = SeriesBrief(id=row['series_id'], slug=row['series_slug'], title=row['series_title'])
    return ArticleListItem(
        id=row['id'],
        slug=row['slug'],
        title=row['title'],
        excerpt=row['excerpt'],
        cover_image_url=row['cover_image_url'],
        author=UserBrief(
            id=row['author_id'],
            discord_user_id=row['author_discord_user_id'],
            username=row['author_username'],
            global_name=row['author_global_name'],
            avatar_hash=row['author_avatar_hash'],
        ),
        publisher=publisher,
        category=CategoryOut(
            id=row['category_id'],
            slug=row['category_slug'],
            name=row['category_name'],
            position=row['category_position'],
            notify_channel_id=row['category_notify_channel_id'],
            include_in_latest=bool(row['category_include_in_latest']),
        ),
        series=series,
        series_position=row['series_position'],
        tags=tags,
        visibility=row['visibility'],
        byline_mode=row['byline_mode'],
        is_breaking=bool(row['is_breaking']),
        is_sponsored=bool(row['is_sponsored']),
        sponsor_name=row['sponsor_name'],
        status=row['status'],
        view_count=int(row['view_count']),
        # SUM() returns DECIMAL on MySQL, so coerce to int.
        up_count=int(row['up_count']),
        down_count=int(row['down_count']),
        # The subtraction is done here. Subtracting the row's columns from each other in SQL is unsigned arithmetic, and the statement fails the moment down-votes exceed up-votes.
        score=int(row['up_count']) - int(row['down_count']),
        comment_count=int(row['comment_count']),
        published_at=row['published_at'],
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


async def fetch_article_page(
    db: Database,
    *,
    where: str,
    where_args: Sequence[Any],
    order_by: str,
    order_args: Sequence[Any] = (),
    joins: str = '',
    page: int,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page[ArticleListItem]:
    """Return one page of the articles matching the condition, in the listing representation.

    where / order_by / joins are SQL fragments the caller writes as constants inside its own module; values that came from the request must never be mixed into them. Values are passed as placeholders via where_args / order_args.

    The total is counted with COUNT(*) over the same condition. That count query does not need the byline/taxonomy joins (the inner joins are against NOT NULL foreign keys and do not change the count, and the outer joins cannot change it at all), so only the joins used for narrowing are passed.
    """
    total = int(await db.fetch_val(f'SELECT COUNT(*) FROM articles {joins} WHERE {where}', *where_args) or 0)
    offset = (page - 1) * per_page
    rows: list[dict[str, Any]] = []
    # A page of results past the total is certain to hold no rows, so the row query is not issued at all.
    if offset < total:
        rows = await db.fetch_all(
            f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_JOINS} {joins} WHERE {where} ORDER BY {order_by} LIMIT %s OFFSET %s',
            *where_args,
            *order_args,
            per_page,
            offset,
        )
    tags_by_article = await _fetch_tags_for_articles(db, [int(row['id']) for row in rows])
    items = [_article_list_item(row, tags_by_article.get(int(row['id']), [])) for row in rows]
    return Page[ArticleListItem](
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        has_next=offset + len(items) < total,
    )


# Categories


@router.get('/api/categories', response_model=list[CategoryListItem])
async def list_categories(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> list[CategoryListItem]:
    """The category listing, returned in the navigation's order (position), with the article count attached."""
    where, args = published_and_visible(principal)
    rows = await db.fetch_all(
        f'''
        SELECT c.id AS id, c.slug AS slug, c.name AS name, c.position AS position,
               c.notify_channel_id AS notify_channel_id, c.include_in_latest AS include_in_latest,
               (SELECT COUNT(*) FROM articles WHERE articles.category_id = c.id AND {where}) AS article_count
        FROM categories c
        ORDER BY c.position, c.name
        ''',
        *args,
    )
    return [CategoryListItem.model_validate(row) for row in rows]


# Tags

_TAG_COLUMNS: Final = '''
    t.id AS id, t.slug AS slug, t.name AS name, t.description AS description,
    t.icon_url AS icon_url, t.notify_channel_id AS notify_channel_id
'''


def _tag_article_count(where: str) -> str:
    """Correlated subquery counting the viewable published articles attached to a tag."""
    return f'''
        (SELECT COUNT(*) FROM article_tags atg JOIN articles ON articles.id = atg.article_id
         WHERE atg.tag_id = t.id AND {where})
    '''


@router.get('/api/tags', response_model=list[TagDetailOut])
async def list_tags(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> list[TagDetailOut]:
    """The tag listing, most articles first, ties ordered by name."""
    where, args = published_and_visible(principal)
    rows = await db.fetch_all(
        f'''
        SELECT {_TAG_COLUMNS}, {_tag_article_count(where)} AS article_count
        FROM tags t
        ORDER BY article_count DESC, t.name
        ''',
        *args,
    )
    return [TagDetailOut.model_validate(row) for row in rows]


@router.get('/api/tags/{slug}', response_model=TagDetailOut)
async def get_tag(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> TagDetailOut:
    """A single tag. Used for the heading of the tag page (the topic hub)."""
    where, args = published_and_visible(principal)
    row = await db.fetch_one(
        f'''
        SELECT {_TAG_COLUMNS}, {_tag_article_count(where)} AS article_count
        FROM tags t
        WHERE t.slug = %s
        ''',
        *args,
        slug,
    )
    if row is None:
        raise NotFound('そのタグは存在しません。')
    return TagDetailOut.model_validate(row)


@router.get('/api/tags/{slug}/timeline', response_model=Page[ArticleListItem])
async def tag_timeline(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> Page[ArticleListItem]:
    """The timeline: that tag's articles walked in ascending published_at order.

    Unlike the latest listing, this one runs oldest-first because it is read for how a topic unfolded, not for the newest events. On a geopolitical topic the value is in the founding of a state, a change of territory and the signing of a treaty standing in chronological order; descending order presents that thread backwards. Articles sharing a timestamp are ordered by ascending ID so the order does not waver across a page boundary.
    """
    tag_id = await db.fetch_val('SELECT id FROM tags WHERE slug = %s', slug)
    if tag_id is None:
        raise NotFound('そのタグは存在しません。')
    where, args = published_and_visible(principal)
    return await fetch_article_page(
        db,
        joins='JOIN article_tags atg ON atg.article_id = articles.id',
        where=f'atg.tag_id = %s AND {where}',
        where_args=[tag_id, *args],
        order_by='articles.published_at ASC, articles.id ASC',
        page=page,
    )


# Publishers

_PUBLISHER_COLUMNS: Final = '''
    p.id AS id, p.role_id AS role_id, p.slug AS slug, p.name AS name,
    p.description AS description, p.logo_url AS logo_url, p.stance AS stance
'''


@router.get('/api/publishers', response_model=list[PublisherOut])
async def list_publishers(db: Annotated[Database, Depends(get_db)]) -> list[PublisherOut]:
    """The publisher listing. Dissolved publishers (role_id NULL) are not dropped, because they live on as the byline of past articles."""
    rows = await db.fetch_all(f'SELECT {_PUBLISHER_COLUMNS} FROM publishers p ORDER BY p.name')
    return [PublisherOut.model_validate(row) for row in rows]


async def _publisher_members(db: Database, role_id: int | None) -> list[UserBrief]:
    """The reporters affiliated with a publisher. Affiliation is expressed by a Discord role, so this pulls the users holding that role_id.

    A user who has left the guild can leave a user_roles row behind, so only those currently in the guild make up the roster. A publisher whose role_id is NULL is dissolved and has nobody affiliated.
    """
    if role_id is None:
        return []
    rows = await db.fetch_all(
        '''
        SELECT u.id AS id, u.discord_user_id AS discord_user_id, u.username AS username,
               u.global_name AS global_name, u.avatar_hash AS avatar_hash
        FROM user_roles ur
        JOIN users u ON u.id = ur.user_id
        WHERE ur.role_id = %s AND u.is_guild_member = 1
        ORDER BY u.username
        ''',
        role_id,
    )
    return [UserBrief.model_validate(row) for row in rows]


@router.get('/api/publishers/{slug}', response_model=PublisherPageOut)
async def get_publisher(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> PublisherPageOut:
    """A publisher's profile, affiliated reporters, track record and article listing.

    The track record counts only the published articles the caller can view. Counting every article would leak the existence of unreadable articles and the size of their reactions as numbers, and would make the article count in the track record disagree with the article listing sitting on the same page.
    """
    publisher_row = await db.fetch_one(f'SELECT {_PUBLISHER_COLUMNS} FROM publishers p WHERE p.slug = %s', slug)
    if publisher_row is None:
        raise NotFound('その報道社は存在しません。')
    publisher_id = int(publisher_row['id'])
    where, args = published_and_visible(principal)
    # The score follows the same rule as the ranking. A separate expression here would make the score shown on the publisher page disagree with that publisher's score in the ranking. That is why the expression itself is borrowed from rankings, and why the two-stage shape (fold per article, then sum) is kept. For the rule, see the rankings module docstring.
    # Unlike the ranking, the scope is limited to the articles the viewer can read (published_and_visible) and is not cut off by period: the publisher page is where that publisher's track record is shown in full.
    stats_row = await db.fetch_one(
        f'''
        SELECT COUNT(*) AS article_count,
               COALESCE(SUM(a.up_votes), 0) AS up_count,
               COALESCE(SUM(a.down_votes), 0) AS down_count,
               COALESCE(SUM(a.comments), 0) AS comment_count,
               COALESCE(SUM({ARTICLE_SCORE_SQL}), 0) AS score
        FROM ({article_totals_sql(f'articles.publisher_id = %s AND {where}')}) a
        ''',
        publisher_id,
        *args,
    )
    stats = PublisherStats(
        article_count=int(stats_row['article_count']) if stats_row else 0,
        up_count=int(stats_row['up_count']) if stats_row else 0,
        down_count=int(stats_row['down_count']) if stats_row else 0,
        comment_count=int(stats_row['comment_count']) if stats_row else 0,
        score=int(stats_row['score']) if stats_row else 0,
    )
    members = await _publisher_members(db, publisher_row['role_id'])
    articles = await fetch_article_page(
        db,
        where=f'articles.publisher_id = %s AND {where}',
        where_args=[publisher_id, *args],
        order_by='articles.published_at DESC, articles.id DESC',
        page=1,
        per_page=DETAIL_ARTICLES_LIMIT,
    )
    return PublisherPageOut(**publisher_row, members=members, stats=stats, articles=articles.items)


# Series

_SERIES_COLUMNS: Final = '''
    s.id AS id, s.slug AS slug, s.title AS title, s.description AS description,
    s.cover_image_url AS cover_image_url, s.created_at AS created_at, s.updated_at AS updated_at,
    o.id AS owner_id, o.discord_user_id AS owner_discord_user_id, o.username AS owner_username,
    o.global_name AS owner_global_name, o.avatar_hash AS owner_avatar_hash
'''

_SERIES_JOINS: Final = 'FROM series s JOIN users o ON o.id = s.owner_id'


def _series_out(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map a series row into the shape with owner nested."""
    return {
        'id': row['id'],
        'slug': row['slug'],
        'title': row['title'],
        'description': row['description'],
        'cover_image_url': row['cover_image_url'],
        'owner': UserBrief(
            id=row['owner_id'],
            discord_user_id=row['owner_discord_user_id'],
            username=row['owner_username'],
            global_name=row['owner_global_name'],
            avatar_hash=row['owner_avatar_hash'],
        ),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


@router.get('/api/series', response_model=list[SeriesOut])
async def list_series(db: Annotated[Database, Depends(get_db)]) -> list[SeriesOut]:
    """The series listing, most recently updated first."""
    rows = await db.fetch_all(f'SELECT {_SERIES_COLUMNS} {_SERIES_JOINS} ORDER BY s.updated_at DESC, s.id DESC')
    return [SeriesOut.model_validate(_series_out(row)) for row in rows]


@router.get('/api/series/{slug}', response_model=SeriesDetailOut)
async def get_series(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> SeriesDetailOut:
    """A single series and the articles in it.

    A series is an ordered whole, so its articles are ordered by ascending series_position. Articles with no position set go to the end, oldest published first among them (a series is read from installment 1, and ordering it latest-first reverses the thread).
    """
    row = await db.fetch_one(f'SELECT {_SERIES_COLUMNS} {_SERIES_JOINS} WHERE s.slug = %s', slug)
    if row is None:
        raise NotFound('その連載は存在しません。')
    where, args = published_and_visible(principal)
    articles = await fetch_article_page(
        db,
        where=f'articles.series_id = %s AND {where}',
        where_args=[int(row['id']), *args],
        order_by='articles.series_position IS NULL, articles.series_position ASC, articles.published_at ASC, articles.id ASC',
        page=1,
        per_page=DETAIL_ARTICLES_LIMIT,
    )
    return SeriesDetailOut.model_validate({**_series_out(row), 'articles': articles.items})
