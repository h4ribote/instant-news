"""The publisher ranking tally.

The real reactions live on Discord: the bot mirrors up-vote (up_count) and down-vote (down_count) counts into article_discord_posts, and comments into article_comments. One article may be notified to several channels, so an article's up/down votes are the sum over all of its rows, and a publisher's track record is in turn the sum over all of that publisher's articles.

The score is computed per article by the rule below and then summed per publisher (up-votes as up, down-votes as down, comment count as c).

  - 2 x down >= up: the article is being piled on. The score is up + c; down-votes are not added.
  - 2 x down < up: it is not being piled on. The score is up + (down / 2, floored) + c; down-votes are added at half weight, as evidence of interest.

Down-votes never subtract from the score. A down-vote marks "read, and disagreed with", not "not read", so as long as it stays small relative to the up-votes it counts as a measure of attention. An article whose down-votes come close to its up-votes, on the other hand, does not get that attention counted towards the track record. This is a different quantity from the article score shown to readers (up-votes - down-votes); it exists only to decide the ordering.

The test is always applied per article. Being piled on is a property of an article: testing it against the publisher's totals would let a single pile-on change the rating of every other article of that publisher at once. The tally is therefore two-stage (fold per article, then fold per publisher). A single GROUP BY leaves nowhere to apply the condition per article.

The tally is restricted to status='published' AND visibility='public' so that articles with access restrictions are not mixed into a public ranking. The ranking is a single artefact anyone can see; counting members-only or role-restricted articles would leak both that "something is there" and the size of its reactions to people who cannot read it.

Sponsored articles (is_sponsored) are included. They are disclosed, so readers react knowing the article is an ad, and that reaction is a fact that actually happened. Excluding them would mean only disclosed articles lose track record, which creates an incentive not to disclose.
"""

from __future__ import annotations

from typing import Any, Final

from common.db import Database

# The default period, and the default number of publishers returned in one call.
DEFAULT_DAYS: Final = 7
DEFAULT_LIMIT: Final = 10

# The score of one article: the rule from the module docstring written out, applied to the per-article folded table (aliased a).
# FLOOR truncates so that a single down-vote is never brought in as 0.5 of a point; scores are integers.
ARTICLE_SCORE_SQL: Final = '''
    CASE WHEN 2 * a.down_votes >= a.up_votes
         THEN a.up_votes + a.comments
         ELSE a.up_votes + FLOOR(a.down_votes / 2) + a.comments
    END
'''


def article_totals_sql(where: str) -> str:
    '''Build the query that folds up/down votes and comment count per article. The outer query must always alias the result a, because ARTICLE_SCORE_SQL refers to it by that name.

    where takes the caller's condition narrowing down the articles to tally. It may contain placeholders, but values themselves must never be interpolated into it as text.

    An article with no notify target (neither its category nor any of its tags has a notify channel) cannot have up/down votes, yet still has to count towards the article count, so LEFT JOIN keeps it.
    The comment count is a correlated subquery rather than a join because joining the notification rows multiplies one article into as many rows as it has notify targets, inflating the up/down sums by that factor.
    '''
    return f'''
        SELECT articles.id AS article_id,
               articles.publisher_id AS publisher_id,
               COALESCE(SUM(adp.up_count), 0) AS up_votes,
               COALESCE(SUM(adp.down_count), 0) AS down_votes,
               (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = articles.id) AS comments
          FROM articles
          LEFT JOIN article_discord_posts adp ON adp.article_id = articles.id
         WHERE {where}
         GROUP BY articles.id
    '''


# Sum the per-article scores for each publisher.
_RANKING_SQL: Final = f'''
    SELECT
        p.id,
        p.role_id,
        p.slug,
        p.name,
        p.description,
        p.logo_url,
        p.stance,
        COUNT(*) AS article_count,
        COALESCE(SUM(a.up_votes), 0) AS up_count,
        COALESCE(SUM(a.down_votes), 0) AS down_count,
        COALESCE(SUM(a.comments), 0) AS comment_count,
        COALESCE(SUM({ARTICLE_SCORE_SQL}), 0) AS score
    FROM ({article_totals_sql("articles.status = 'published' AND articles.visibility = 'public' AND articles.published_at >= NOW() - INTERVAL %s DAY")}) a
    JOIN publishers p ON p.id = a.publisher_id
    GROUP BY p.id
    ORDER BY score DESC, article_count DESC, p.name ASC
    LIMIT %s
'''


async def publisher_ranking(db: Database, *, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Return the publisher ranking for the last days days, score descending, at most limit rows.

    The score rule is the one stated in the module docstring. Ties are broken by article count descending, then by name, so the result order does not shift from call to call.

    Each row returned has the shape {'publisher': {...}, 'article_count': int, 'up_count': int, 'down_count': int, 'comment_count': int, 'score': int}. publisher holds the publisher row verbatim, so it validates as PublisherOut as-is.

    days and limit are expected to be range-checked by the caller (the router's query parameters), but a value below 1 would only produce an empty result or break the SQL without conveying the intent, so it is rejected outright here.
    """
    if days < 1:
        raise ValueError(f'集計期間の日数は 1 以上で指定してください: {days}')
    if limit < 1:
        raise ValueError(f'取得件数は 1 以上で指定してください: {limit}')
    rows = await db.fetch_all(_RANKING_SQL, days, limit)
    return [
        {
            'publisher': {
                'id': row['id'],
                'role_id': row['role_id'],
                'slug': row['slug'],
                'name': row['name'],
                'description': row['description'],
                'logo_url': row['logo_url'],
                'stance': row['stance'],
            },
            'article_count': int(row['article_count']),
            # SUM() returns DECIMAL on MySQL, so coerce to int before it goes out as JSON.
            'up_count': int(row['up_count']),
            'down_count': int(row['down_count']),
            'comment_count': int(row['comment_count']),
            'score': int(row['score']),
        }
        for row in rows
    ]
