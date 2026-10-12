"""Ad delivery and tracking.

The core owns delivery (a weighted random selection over a slot's candidates) and tracking (daily impression and click tallies), and nothing else. Advertisers, run plans, billing and the management UI belong to an external application; never add anything here that crosses that line. The run period is the one judgement the core keeps: require an external app to run the scheduler and the end of a run stops taking effect the moment that app goes down.

There is deliberately no preview endpoint. A URL that force-renders an arbitrary ad without authentication lets anyone build a link that puts an arbitrary image on the real news site, and makes an ad that is not running look as though it is. An external management app reproduces the look before submission from the recommended dimensions ad_slots returns (GET /api/admin/ad-slots).
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

# For both serve and click the response itself is the moment the count is taken: if it gets cached and reused the impression goes uncounted, and the same ad stays stuck in the slot.
_NO_STORE: Final = 'no-store'

# Candidates currently running. weight = 0 rows are excluded because probability 0 in a weighted random selection means "never show this".
# It also keeps the draw well-defined: if every candidate were 0 the weights would sum to 0 and no draw is possible.
_SELECT_CANDIDATES: Final = """
    SELECT id, advertiser_name, image_url, body_text, alt_text, weight
    FROM ads
    WHERE slot_id = %s
      AND is_active = 1
      AND weight >= 1
      AND (starts_at IS NULL OR starts_at <= NOW())
      AND (ends_at IS NULL OR ends_at >= NOW())
"""

# Add to the daily rollup: insert the day's row, or increment it if it already exists. One row per event would grow the table on every single impression.
_COUNT_IMPRESSION: Final = """
    INSERT INTO ad_stats_daily (ad_id, stat_date, impressions, clicks) VALUES (%s, CURRENT_DATE, 1, 0)
    ON DUPLICATE KEY UPDATE impressions = impressions + 1
"""

_COUNT_CLICK: Final = """
    INSERT INTO ad_stats_daily (ad_id, stat_date, impressions, clicks) VALUES (%s, CURRENT_DATE, 0, 1)
    ON DUPLICATE KEY UPDATE clicks = clicks + 1
"""


def click_url(ad_id: int) -> str:
    """URL that interposes click tracking. The serve response does not carry link_url directly because clicks can only be counted if every click goes through this route."""
    return f'/ads/{ad_id}/click'


def _weighted_choice(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick one candidate by a random draw weighted on weight. None if there are no candidates.

    Weight is the relative frequency with which a candidate is selected, not a rank: a weight=3 ad runs three times as often as a weight=1 ad.
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
    """Select one ad for the slot; the response itself counts as one impression. The creative is image_url for an image slot and body_text for a text slot, as format says.

    With no candidates, ad comes back null and the frontend removes the slot itself. Leaving an empty slot keeps only the pretence that something should be there.

    An unknown slot slug is a 404. Slots are a fixed set seeded by the migration, so a slug we do not know is an error in the frontend template. Treating it as zero candidates (ad: null) would let that error hide, indistinguishable from a slot that happens to have no ads.
    """
    response.headers['Cache-Control'] = _NO_STORE
    slot_row = await db.fetch_one('SELECT id, slug, format FROM ad_slots WHERE slug = %s', slot)
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
            format=slot_row['format'],
            advertiser_name=chosen['advertiser_name'],
            image_url=chosen['image_url'],
            body_text=chosen['body_text'],
            click_url=click_url(chosen['id']),
            alt_text=chosen['alt_text'],
        )
    )


@router.get(
    '/ads/{ad_id}/click',
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={302: {'description': '広告のリンク先へ転送する。', 'headers': {'Location': {'description': 'リンク先。', 'schema': {'type': 'string'}}}}},
)
async def click_ad(ad_id: str, db: Annotated[Database, Depends(get_db)]) -> RedirectResponse:
    """Count one click and forward to the ad's click destination.

    Deliberately not filtered by run period or is_active: a click on an ad that was shown really happened, even if the run has since ended. Rejecting it here would count the impression but not the click, so the click-through rate would read lower than reality.

    The click destination is an arbitrary URL, but only an ads:admin principal can set it (submission also checks it is an absolute http / https URL), so this is not an open redirector anyone can plant a destination in.
    """
    try:
        identifier = to_snowflake(ad_id)
    except ValueError as exc:
        # A value that cannot be read as an ID: there is no point distinguishing it from an ID that does not exist.
        raise NotFound('広告がありません。') from exc
    row = await db.fetch_one('SELECT id, link_url FROM ads WHERE id = %s', identifier)
    if row is None:
        raise NotFound('広告がありません。')
    await db.execute(_COUNT_CLICK, row['id'])
    return RedirectResponse(url=row['link_url'], status_code=status.HTTP_302_FOUND, headers={'Cache-Control': _NO_STORE})
