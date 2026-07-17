"""The ranking API. The publisher tally lives in app.rankings; the article rankings in app.article_query."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from common.db import Database

from ..article_query import ARTICLE_RANKING_LIMIT, fetch_ranked_articles
from ..deps import get_db, get_principal_optional
from ..principals import Principal
from ..rankings import DEFAULT_DAYS, DEFAULT_LIMIT, publisher_ranking
from ..schemas import ArticleListItem, ArticleRankingMetric, PublisherRankingItem

router = APIRouter(tags=['rankings'])


@router.get('/api/rankings/publishers', response_model=list[PublisherRankingItem])
async def get_publisher_ranking(
    db: Annotated[Database, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_DAYS,
    limit: Annotated[int, Query(ge=1, le=100)] = DEFAULT_LIMIT,
) -> list[PublisherRankingItem]:
    """Return the publisher ranking for the last days days, descending by score (reactions + replies).

    The tally is restricted to status='published' articles with visibility='public', so that articles with access restrictions are not mixed into a public ranking. No authentication is required because the ranking is a single public artefact, identical for everyone; the order never varies with the caller.

    days and limit are capped so that no request can force a tally over everything by naming an unbounded period or row count.
    """
    rows = await publisher_ranking(db, days=days, limit=limit)
    return [PublisherRankingItem.model_validate(row) for row in rows]


@router.get('/api/rankings/articles', response_model=list[ArticleListItem])
async def get_article_ranking(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    metric: Annotated[ArticleRankingMetric, Query(description='並べ替えの基準。views はアクセス数、featured は反応(賛否+コメント)。')] = 'views',
    limit: Annotated[int, Query(ge=1, le=100)] = ARTICLE_RANKING_LIMIT,
) -> list[ArticleListItem]:
    """Return the top articles for one sidebar ranking, ordered by the server.

    metric selects the base quantity (views or featured). The score decays with age in two regimes: within a one-week window time is nearly flat and reactions and comments drive the order, and past it the score decays strongly so older articles sink. No time window cuts the tally off — every viewable article in the latest feed is ranked, so old articles are still ordered and returned (they just rank low) and the ranking is never empty while any such article exists. The rule is stated in full on app.article_query.fetch_ranked_articles.

    The visibility filter is applied per caller (get_principal_optional), exactly as the article listing is, so the ranking never lists an article the caller could not open — a member may see members-only articles they can read, an anonymous caller only public ones. limit is capped so no request can force an unbounded row count.
    """
    return await fetch_ranked_articles(db, metric=metric, principal=principal, limit=limit)
