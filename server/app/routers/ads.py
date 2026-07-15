"""広告の配信と計測。

コアが担うのは配信(枠に対する重み付き抽選)と計測(表示回数・クリック数の日次集計)だけである。広告主・掲載プラン・課金・管理 UI は外部アプリケーションの担当であり、この線引きを越える機能をここへ足してはならない。掲載期間の判定だけはコアが持つ(外部アプリにスケジューラを要求すると、その外部アプリが止まった瞬間に掲載終了が効かなくなる)。

プレビュー用のエンドポイントは設けない。認証なしで任意の広告を強制的に表示させられる URL は、「実際のニュースサイト上に任意の画像を出したリンク」を誰でも作れることを意味し、配信していない広告を掲載中であるかのように見せられる。外部の管理アプリが入稿前に見え方を再現するための手段は、ad_slots が返す推奨寸法(GET /api/admin/ad-slots)である。
"""

from __future__ import annotations

import random
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import RedirectResponse

from common.db import Database
from common.snowflake import to_snowflake

from ..deps import get_db
from ..errors import NotFound
from ..schemas import AdServed, AdServeOut

router = APIRouter(tags=['ads'])

# 配信もクリックも、その応答そのものが計測の契機である。キャッシュに載って応答が再利用されると表示回数は数えられず、しかも同じ広告が枠へ貼り付いたままになる。
_NO_STORE: Final = 'no-store'

# 掲載中の候補。weight が 0 の行を除くのは、重み付き抽選における確率が 0 であり「決して出さない」と同義であるため(全ての候補が 0 だと重みの合計が 0 になり抽選そのものが定義できない)。
_SELECT_CANDIDATES: Final = """
    SELECT id, advertiser_name, image_url, alt_text, weight
    FROM ads
    WHERE slot_id = %s
      AND is_active = 1
      AND weight >= 1
      AND (starts_at IS NULL OR starts_at <= NOW())
      AND (ends_at IS NULL OR ends_at >= NOW())
"""

# 日次のロールアップへ加算する。イベントを 1 行ずつ記録すると表示のたびに行が増えるため、その日の行を作るか、既にあれば加算する。
_COUNT_IMPRESSION: Final = """
    INSERT INTO ad_stats_daily (ad_id, stat_date, impressions, clicks) VALUES (%s, CURRENT_DATE, 1, 0)
    ON DUPLICATE KEY UPDATE impressions = impressions + 1
"""

_COUNT_CLICK: Final = """
    INSERT INTO ad_stats_daily (ad_id, stat_date, impressions, clicks) VALUES (%s, CURRENT_DATE, 0, 1)
    ON DUPLICATE KEY UPDATE clicks = clicks + 1
"""


def click_url(ad_id: int) -> str:
    """クリックの計測を挟む URL。配信の応答に link_url を直接載せないのは、この経路を必ず通さないとクリックが数えられないため。"""
    return f'/ads/{ad_id}/click'


def _weighted_choice(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """weight を重みとした抽選で 1 件選ぶ。候補が無ければ None。

    重みは候補が選ばれる相対的な頻度であり、順位ではない。weight=3 の広告は weight=1 の広告の 3 倍の頻度で出る。
    """
    if not candidates:
        return None
    return random.choices(candidates, weights=[int(row['weight']) for row in candidates], k=1)[0]


@router.get('/api/ads/serve')
async def serve_ad(
    response: Response,
    slot: Annotated[str, Query(min_length=1, max_length=64, description='ad_slots.slug。フロントエンドのテンプレートが参照する枠の識別子。')],
    db: Annotated[Database, Depends(get_db)],
) -> AdServeOut:
    """枠に対して広告を 1 件選び、その応答をもって表示 1 回として数える。

    候補が 0 件のときは ad を null で返す。フロントはこれを受けて枠自体を消す(空の枠を残すと、そこに何かがあるはずだという体裁だけが残る)。

    枠の slug 自体が存在しない場合は 404 を返す。枠は移行で投入された固定の集合であり、知らない slug はフロントエンドのテンプレートの誤りである。候補 0 件と同じ扱い(ad: null)にすると、その誤りが「たまたま広告が無い枠」と見分けられないまま潜伏する。
    """
    response.headers['Cache-Control'] = _NO_STORE
    slot_row = await db.fetch_one('SELECT id, slug FROM ad_slots WHERE slug = %s', slot)
    if slot_row is None:
        raise NotFound(f'広告枠がありません: {slot}')
    chosen = _weighted_choice(await db.fetch_all(_SELECT_CANDIDATES, slot_row['id']))
    if chosen is None:
        return AdServeOut(ad=None)
    await db.execute(_COUNT_IMPRESSION, chosen['id'])
    return AdServeOut(
        ad=AdServed(
            id=chosen['id'],
            slot=slot_row['slug'],
            advertiser_name=chosen['advertiser_name'],
            image_url=chosen['image_url'],
            click_url=click_url(chosen['id']),
            alt_text=chosen['alt_text'],
        )
    )


@router.get('/ads/{ad_id}/click')
async def click_ad(ad_id: str, db: Annotated[Database, Depends(get_db)]) -> RedirectResponse:
    """クリックを 1 回数え、広告の遷移先へ転送する。

    掲載期間や is_active で絞らない。表示された広告のクリックは、その後に掲載が終わっていても実際に起きたクリックである。ここで弾くと、表示は数えたのにクリックは数えないという食い違いが生じ、クリック率が実態より低く出る。

    転送先は任意の URL だが、これを設定できるのは ads:admin の主体だけであり(入稿時に http / https の絶対 URL であることも検査する)、誰でも転送先を仕込める開かれた転送口ではない。
    """
    try:
        identifier = to_snowflake(ad_id)
    except ValueError as exc:
        # ID として解釈できない値は、存在しない ID と区別する意味が無い。
        raise NotFound('広告がありません。') from exc
    row = await db.fetch_one('SELECT id, link_url FROM ads WHERE id = %s', identifier)
    if row is None:
        raise NotFound('広告がありません。')
    await db.execute(_COUNT_CLICK, row['id'])
    return RedirectResponse(url=row['link_url'], status_code=status.HTTP_302_FOUND, headers={'Cache-Control': _NO_STORE})
