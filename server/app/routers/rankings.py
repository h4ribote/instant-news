"""報道社ランキングの API。集計そのものは app.rankings が持つ。"""

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
    """直近 days 日の報道社ランキングを score(反応数 + 返信数)の降順で返す。

    集計対象は公開済みかつ visibility='public' の記事に限る(閲覧制限のある記事を公開のランキングに混ぜない)。認証を要さないのはランキングが誰にとっても同一の公開の成果物であるためで、要求元によって順位が変わることはない。

    days・limit の上限は、際限のない期間・件数を指定して全件の集計を強いる要求を防ぐために設ける。
    """
    rows = await publisher_ranking(db, days=days, limit=limit)
    return [PublisherRankingItem.model_validate(row) for row in rows]
