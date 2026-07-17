"""The publisher ranking API. The tally itself lives in app.rankings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from common.db import Database

from ..deps import get_db
from ..rankings import DEFAULT_DAYS, DEFAULT_LIMIT, publisher_ranking
from ..schemas import PublisherRankingItem

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
