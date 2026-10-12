"""Read API for the taxonomy (categories, tags, publishers, series).

The article listings under these resources (a tag's timeline, a series' articles) go through the shared projection in app.article_query (fetch_article_page, published_and_visible), so a listed article has the same shape and the same visibility narrowing here as on /api/articles and in search. Only a listing with an order of its own is a sub-resource here; a publisher's articles, newest first, are /api/articles?publisher=. The category and tag counts reuse published_and_visible directly, so those counts never include an article the caller could not open.

When building SQL, only constants written in this module are interpolated into f-strings (join clauses, orderings, the number of %s in an IN clause); values that came from the request are always passed as placeholders (%s).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends

from common.db import Database

from ..article_query import SERIES_ORDER, TIMELINE_ORDER, article_paging, fetch_article_page, published_and_visible
from ..deps import get_db, get_principal_optional
from ..errors import NotFound
from ..paging import PagingParams, page_request
from ..principals import Principal
from ..rankings import ARTICLE_SCORE_SQL, article_totals_sql
from ..schemas import (
    ArticleListItem,
    CategoryOut,
    ItemList,
    Page,
    PublisherDetailOut,
    PublisherOut,
    PublisherStats,
    SeriesOut,
    TagDetailOut,
    UserBrief,
)

router = APIRouter(tags=['taxonomy'])


class CategoryListItem(CategoryOut):
    """An item of the category listing.

    article_count counts only the published articles the caller can view. Counting every article would leak the existence and the number of unreadable articles through the count.
    """

    article_count: int


# Categories


@router.get('/api/categories')
async def list_categories(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> ItemList[CategoryListItem]:
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
    return ItemList[CategoryListItem](items=[CategoryListItem.model_validate(row) for row in rows])


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


@router.get('/api/tags')
async def list_tags(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> ItemList[TagDetailOut]:
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
    return ItemList[TagDetailOut](items=[TagDetailOut.model_validate(row) for row in rows])


@router.get('/api/tags/{slug}')
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


@router.get('/api/tags/{slug}/timeline')
async def tag_timeline(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    paging: Annotated[PagingParams, Depends(article_paging)],
) -> Page[ArticleListItem]:
    """The timeline: that tag's articles walked in ascending published_at order.

    Unlike the latest listing, this one runs oldest-first because it is read for how a topic unfolded, not for the newest events. On a geopolitical topic the value is in the founding of a state, a change of territory and the signing of a treaty standing in chronological order; descending order presents that thread backwards.
    """
    tag_id = await db.fetch_val('SELECT id FROM tags WHERE slug = %s', slug)
    if tag_id is None:
        raise NotFound('そのタグは存在しません。')
    where, args = published_and_visible(principal)
    return await fetch_article_page(
        db,
        page_request('tag-timeline', {'tag_id': int(tag_id)}, paging),
        joins='JOIN article_tags atg ON atg.article_id = articles.id',
        where=f'atg.tag_id = %s AND {where}',
        where_args=[tag_id, *args],
        order=TIMELINE_ORDER,
    )


# Publishers

_PUBLISHER_COLUMNS: Final = '''
    p.id AS id, p.role_id AS role_id, p.slug AS slug, p.name AS name,
    p.description AS description, p.logo_url AS logo_url, p.stance AS stance
'''


@router.get('/api/publishers')
async def list_publishers(db: Annotated[Database, Depends(get_db)]) -> ItemList[PublisherOut]:
    """The publisher listing. Dissolved publishers (role_id NULL) are not dropped, because they live on as the byline of past articles."""
    rows = await db.fetch_all(f'SELECT {_PUBLISHER_COLUMNS} FROM publishers p ORDER BY p.name')
    return ItemList[PublisherOut](items=[PublisherOut.model_validate(row) for row in rows])


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


@router.get('/api/publishers/{slug}')
async def get_publisher(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> PublisherDetailOut:
    """A publisher's profile, affiliated reporters and track record. Its articles are the paged listing /api/articles?publisher=.

    The track record counts only the published articles the caller can view. Counting every article would leak the existence of unreadable articles and the size of their reactions as numbers, and would make the article count in the track record disagree with the publisher's article listing.
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
    return PublisherDetailOut(**publisher_row, members=members, stats=stats)


# Series

_SERIES_COLUMNS: Final = '''
    s.id AS id, s.slug AS slug, s.title AS title, s.description AS description,
    s.cover_image_url AS cover_image_url, s.created_at AS created_at, s.updated_at AS updated_at,
    o.id AS owner_id, o.discord_user_id AS owner_discord_user_id, o.username AS owner_username,
    o.global_name AS owner_global_name, o.avatar_hash AS owner_avatar_hash
'''

_SERIES_JOINS: Final = 'FROM series s JOIN users o ON o.id = s.owner_id'


def _series_out(row: Mapping[str, Any]) -> SeriesOut:
    """Map a series row into the shape with owner nested."""
    return SeriesOut(
        id=row['id'],
        slug=row['slug'],
        title=row['title'],
        description=row['description'],
        cover_image_url=row['cover_image_url'],
        owner=UserBrief(
            id=row['owner_id'],
            discord_user_id=row['owner_discord_user_id'],
            username=row['owner_username'],
            global_name=row['owner_global_name'],
            avatar_hash=row['owner_avatar_hash'],
        ),
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


@router.get('/api/series')
async def list_series(db: Annotated[Database, Depends(get_db)]) -> ItemList[SeriesOut]:
    """The series listing, most recently updated first."""
    rows = await db.fetch_all(f'SELECT {_SERIES_COLUMNS} {_SERIES_JOINS} ORDER BY s.updated_at DESC, s.id DESC')
    return ItemList[SeriesOut](items=[_series_out(row) for row in rows])


async def _require_series(db: Database, slug: str) -> dict[str, Any]:
    row = await db.fetch_one(f'SELECT {_SERIES_COLUMNS} {_SERIES_JOINS} WHERE s.slug = %s', slug)
    if row is None:
        raise NotFound('その連載は存在しません。')
    return row


@router.get('/api/series/{slug}')
async def get_series(slug: str, db: Annotated[Database, Depends(get_db)]) -> SeriesOut:
    """A single series. Its articles are the paged listing /api/series/{slug}/articles."""
    return _series_out(await _require_series(db, slug))


@router.get('/api/series/{slug}/articles')
async def series_articles(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    paging: Annotated[PagingParams, Depends(article_paging)],
) -> Page[ArticleListItem]:
    """The articles of a series in installment order.

    A series is an ordered whole, so its articles are ordered by ascending series_position. Articles with no position set go to the end, oldest published first among them (a series is read from installment 1, and ordering it latest-first reverses the thread).
    """
    series_id = int((await _require_series(db, slug))['id'])
    where, args = published_and_visible(principal)
    return await fetch_article_page(
        db,
        page_request('series-articles', {'series_id': series_id}, paging),
        where=f'articles.series_id = %s AND {where}',
        where_args=[series_id, *args],
        order=SERIES_ORDER,
    )
