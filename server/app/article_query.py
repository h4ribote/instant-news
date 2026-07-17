"""The article listing projection, shared by every route that returns a list of articles.

A listed article has the same shape (ArticleListItem) whatever the filter condition, and writing the assembly separately per condition lets the byline, reaction counts and tag resolution diverge between paths. This module holds that one assembly: the SELECT projection, the byline/taxonomy joins, the visibility-narrowed WHERE, a paged fetch (fetch_article_page) and a ranked fetch (fetch_ranked_articles). The taxonomy, search and rankings routers all drive it.

It lives at the service layer rather than inside a router because more than one router needs it, and because rankings.py — which the taxonomy router imports — must be able to reuse it without importing a router back (which would be a cycle).

Every listing goes through the visibility filter (visibility_sql_filter) and additionally imposes status='published'. The first keeps articles the caller must not read out of sight; the second keeps not-yet-published articles out of the public index. With the visibility filter alone, an author or an admin would see their own drafts (published_at NULL) mixed into the timeline and into search results. A draft is reached through the writer's own article listing, not lined up in the topical index. The fragment carries no admin clause and this module adds none, so a listing shows an admin only what their own roles allow, even for an article can_view_article would let them open; visibility_sql_filter records that gap.

When building SQL, only constants written in this module are interpolated into f-strings (join clauses, orderings, the number of %s in an IN clause); values that came from the request are always passed as placeholders (%s).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from common.db import Database

from .principals import Principal
from .schemas import ArticleListItem, CategoryOut, Page, PublisherOut, SeriesBrief, TagOut, UserBrief
from .visibility import visibility_sql_filter

# Items per page of a listing. The tag timeline and search take only page in the query, so the count is fixed here.
DEFAULT_PER_PAGE: Final = 20

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


# The article rankings (the home page sidebar's アクセスランキング and 注目).
#
# Every viewable published article in the latest feed is ranked and returned in order — old articles included, never cut off by a time window — but the score decays with age so fresh articles lead. The decay runs in two regimes split at the fresh window:
#   fresh (age <= window): a gentle decay (a long half-life) so time barely separates articles of the same few days, plus a reactions-and-comments term (the reaction weight) so it is engagement, not the exact hour, that decides the order among fresh articles. This is the "reduce the time weight and lean on reactions and comments for recent articles" rule.
#   aged (age >  window): a strong decay (a short half-life) continuing from the window edge, where the gentle branch has fallen to exactly 0.5. Old articles keep a rank and are still returned, but sink below anything fresh; among themselves, once the score has decayed to near zero, they fall back to newest-first (the id tiebreak).
# The window edge is continuous for the base×weight part: both branches evaluate to base × 0.5 there. The fresh reactions-and-comments term is not carried into the aged branch, so an article loses that boost as it crosses the window — a deliberate step, since the emphasis on engagement is meant for fresh articles only.
ARTICLE_RANKING_FRESH_WINDOW_HOURS: Final = 7 * 24
# Half-life within the fresh window: gentle, so a whole week only halves the weight. This is the "reduced" time weight.
ARTICLE_RANKING_FRESH_HALF_LIFE_HOURS: Final = 7 * 24
# Half-life past the fresh window: strong, so older articles fall away quickly (a day halves the remaining weight) and cannot crowd out fresh ones.
ARTICLE_RANKING_AGED_HALF_LIFE_HOURS: Final = 24
# Within the fresh window, how many base-metric points one net reaction or comment is worth. Set well above 1 so that, among articles of a similar age, reactions and comments — not raw views — drive the order. For 'featured' the base already is this quantity, so the term only scales it; for 'views' it is what lets an engaged fresh article climb over a merely-viewed one.
ARTICLE_RANKING_FRESH_REACTION_WEIGHT: Final = 3
# Default row count for one ranking call.
ARTICLE_RANKING_LIMIT: Final = 5

# Each ranking metric maps to the per-article base quantity it orders on, written in terms of the listing projection's aliases. 'views' ranks on the raw view count; 'featured' on the reader-facing score (up − down) plus the reply count — the same quantity the 注目 card shows. up_count / down_count / comment_count are the aliases the projection's subqueries produce, and MySQL resolves them inside an ORDER BY expression.
_RANKING_BASE_SCORE: Final[dict[str, str]] = {
    'views': 'articles.view_count',
    'featured': '(up_count - down_count + comment_count)',
}

# Age in hours, clamped at 0 so a published_at dated in the future (clock skew) is treated as brand new, never negative.
_AGE_HOURS_SQL: Final = 'GREATEST(TIMESTAMPDIFF(SECOND, articles.published_at, NOW()), 0) / 3600'
# Reactions and comments: the reader-facing score (up − down) plus the reply count. This is the quantity emphasized within the fresh window.
_ENGAGEMENT_SQL: Final = '(up_count - down_count + comment_count)'


def _ranking_score_sql(base: str) -> str:
    """The ORDER BY score for one metric, as SQL over the listing projection's columns and aliases.

    Two regimes by article age (see the ranking constants for the reasoning): within the fresh window the base and a reactions-and-comments term decay gently together; past it the base alone decays strongly, continuing from the window edge where the gentle branch reaches 0.5.
    """
    fresh_weight = f'POW(0.5, ({_AGE_HOURS_SQL}) / {ARTICLE_RANKING_FRESH_HALF_LIFE_HOURS})'
    aged_weight = f'0.5 * POW(0.5, (({_AGE_HOURS_SQL}) - {ARTICLE_RANKING_FRESH_WINDOW_HOURS}) / {ARTICLE_RANKING_AGED_HALF_LIFE_HOURS})'
    return (
        f'CASE WHEN ({_AGE_HOURS_SQL}) <= {ARTICLE_RANKING_FRESH_WINDOW_HOURS} '
        f'THEN ({base} + {ARTICLE_RANKING_FRESH_REACTION_WEIGHT} * {_ENGAGEMENT_SQL}) * ({fresh_weight}) '
        f'ELSE ({base}) * ({aged_weight}) END'
    )


async def fetch_ranked_articles(
    db: Database,
    *,
    metric: str,
    principal: Principal | None,
    limit: int = ARTICLE_RANKING_LIMIT,
) -> list[ArticleListItem]:
    """Return the top articles for one sidebar ranking, already ordered by the server.

    metric selects the base quantity: 'views' ranks on view_count, 'featured' on the reader-facing score (up − down) plus replies. The score decays with age in two regimes (see _ranking_score_sql and the ranking constants): within the fresh window time is nearly flat and reactions and comments drive the order, and past it the score decays strongly so older articles sink. Nothing is cut off by a time window — every viewable published article in the latest feed is ranked, so old articles are still ordered and returned (they simply rank low), which is what fills the ranking when there are few or no fresh articles.

    The scope is published articles the caller can view (published_and_visible), restricted to the categories that appear in the site-wide latest feed (include_in_latest): recurring sections such as market prices are kept out of the rankings, exactly as they are kept out of the latest listing. Because the filter is per-caller, a member's ranking may include members-only articles they can read — the same articles the listing on the same page would show them, never anything they could not open.

    limit is range-checked by the router's query bounds; a value below 1, or a metric outside _RANKING_BASE_SCORE, is rejected here too rather than silently yielding an empty or malformed query.
    """
    if metric not in _RANKING_BASE_SCORE:
        raise ValueError(f'未対応のランキング指標です: {metric}')
    if limit < 1:
        raise ValueError(f'取得件数は 1 以上で指定してください: {limit}')
    where, args = published_and_visible(principal)
    order_by = f'{_ranking_score_sql(_RANKING_BASE_SCORE[metric])} DESC, articles.id DESC'
    rows = await db.fetch_all(
        f'''
        SELECT {_ARTICLE_COLUMNS}
        {_ARTICLE_JOINS}
        WHERE {where} AND ct.include_in_latest = 1
        ORDER BY {order_by}
        LIMIT %s
        ''',
        *args,
        limit,
    )
    tags_by_article = await _fetch_tags_for_articles(db, [int(row['id']) for row in rows])
    return [_article_list_item(row, tags_by_article.get(int(row['id']), [])) for row in rows]
