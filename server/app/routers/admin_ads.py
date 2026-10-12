"""Ad administration API (ads:admin).

This router requires exactly one scope: ads:admin. Never bundle the scopes an admin can issue into one. Hand the ad-management app ads:admin and nothing else: mix in the power to forge an article's byline (articles:write:any) and whoever you trusted with ad submission can publish articles under any reporter's name.

There is no API for creating ad slots (ad_slots). Slots are a fixed set that the frontend templates reference by slug, seeded by the migration. Letting the API add slots would achieve nothing without a template to render them, and would only accumulate inexplicable slots that can be created but never appear.

Submitting an ad honours Idempotency-Key, like creating an article: an ad has no unique field, so a resend after a lost response would otherwise run the same creative twice.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query, Response, status

from common.config import MediaConfig
from common.db import Database
from common.snowflake import to_snowflake

from ..deps import get_db, get_media_config, require_scope
from ..errors import BadRequest, Conflict, MediaError, NotFound
from ..idempotency import IdempotencyRace, claim, complete, find_replay, idempotency_request
from ..media.fetch import FetchError
from ..media.service import ingest_url
from ..media.store import VARIANTS
from ..media.transcode import TranscodeError
from ..paging import KeysetOrder, PagingParams, SortKey, fetch_keyset, page_request, paging_params
from ..principals import Principal
from ..schemas import AdOut, AdSlotOut, AdStatDay, AdStatsOut, AdWrite, ItemList, Page

# Held as a single object: imposed on the whole router, and referenced by the routes that need the principal itself, so FastAPI's dependency cache evaluates it only once per request.
_ads_admin: Final = require_scope('ads:admin')

# Imposed on the router rather than declared per route: forgetting the check when adding a route cannot create a route that passes straight through.
router = APIRouter(tags=['admin:ads'], dependencies=[Depends(_ads_admin)])

# Ad creatives are always served by us. /media/{hash}/{variant}.webp is the shape of the delivery URL media.service returns.
_MEDIA_URL_PREFIX: Final = '/media/'
_MEDIA_URL_PATTERN: Final = re.compile(r'^/media/(?P<hash>[0-9a-f]{64})/(?P<variant>[^/]+)\.webp$')

_LINK_SCHEMES: Final = ('http', 'https')

# Required on create. AdWrite doubles as the partial-update body, so every field is optional on the model and the create-time requirement is checked here.
_REQUIRED_ON_CREATE: Final = ('slot_id', 'advertiser_name', 'image_url', 'link_url')

# NOT NULL columns. Reject an explicit null in a partial update: the DB does keep nulls out, but failing there is a constraint-violation 500 that says nothing about what was wrong.
_NOT_NULLABLE: Final = ('slot_id', 'advertiser_name', 'image_url', 'link_url', 'weight', 'is_active')

_CREATE_ENDPOINT: Final = 'POST /api/admin/ads'

_ad_paging: Final = paging_params(20, 100)
# Newest submission first; the id orders ads submitted in the same second.
_AD_ORDER: Final = KeysetOrder(
    (
        SortKey('a.created_at', lambda row: row['created_at'], 'datetime', descending=True),
        SortKey('a.id', lambda row: row['id'], 'int', descending=True),
    )
)

_AD_SELECT: Final = """
    SELECT a.id, a.slot_id, a.advertiser_name, a.image_url, a.link_url, a.alt_text, a.weight,
           a.starts_at, a.ends_at, a.is_active, a.created_at, a.updated_at,
           s.slug AS slot_slug, s.name AS slot_name, s.description AS slot_description,
           s.recommended_width AS slot_recommended_width, s.recommended_height AS slot_recommended_height
    FROM ads AS a
    JOIN ad_slots AS s ON s.id = a.slot_id
"""


def _ad_out(row: dict[str, Any]) -> AdOut:
    return AdOut(
        id=row['id'],
        slot=AdSlotOut(
            id=row['slot_id'],
            slug=row['slot_slug'],
            name=row['slot_name'],
            description=row['slot_description'],
            recommended_width=row['slot_recommended_width'],
            recommended_height=row['slot_recommended_height'],
        ),
        advertiser_name=row['advertiser_name'],
        image_url=row['image_url'],
        link_url=row['link_url'],
        alt_text=row['alt_text'],
        weight=row['weight'],
        starts_at=row['starts_at'],
        ends_at=row['ends_at'],
        is_active=bool(row['is_active']),
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


def _parse_ad_id(value: str) -> int:
    """Parse a path ID into a 64-bit integer. An unparseable value is reported as not found: there is no point distinguishing it from an ID that does not exist."""
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound('広告がありません。') from exc


async def _fetch_ad(db: Database, ad_id: int) -> dict[str, Any]:
    row = await db.fetch_one(f'{_AD_SELECT} WHERE a.id = %s', ad_id)
    if row is None:
        raise NotFound('広告がありません。')
    return row


async def _require_slot(db: Database, slot_id: int) -> None:
    if await db.fetch_val('SELECT id FROM ad_slots WHERE id = %s', slot_id) is None:
        raise BadRequest(f'広告枠がありません: {slot_id}。枠は移行で投入された固定の集合であり、作成の API は無い。GET /api/admin/ad-slots で一覧を取得すること。')


def _validated_link_url(link_url: str) -> str:
    """Check that the click destination is an absolute http / https URL.

    The value becomes the click destination once click tracking has run, and the frontend renders it as a link. Without a scheme check a javascript: URL could be planted, and submitting an ad would amount to executing a script.
    """
    parsed = urlsplit(link_url)
    if parsed.scheme.lower() not in _LINK_SCHEMES or not parsed.netloc:
        raise BadRequest(f'link_url は http または https で始まる絶対 URL である必要があります: {link_url}')
    return link_url


def _validate_period(starts_at: datetime | None, ends_at: datetime | None) -> None:
    """Check the ordering of the run period. An ad whose starts_at is after its ends_at is a candidate at no instant at all: it is submitted, never runs once, and the run period simply passes."""
    if starts_at is not None and ends_at is not None and starts_at > ends_at:
        raise BadRequest('ends_at は starts_at 以降である必要があります。')


async def _require_public_media(db: Database, image_url: str) -> None:
    """For a URL that points at an already-ingested image, check that it really exists and is public.

    Ads are served to logged-out readers too, so a non-public image leaves the slot broken on the reader's side. Do not resolve that by rewriting media.is_public to 1 here: a non-public image may be a figure from an access-restricted article, so merely naming it in an ad would hand that figure to every reader.
    """
    matched = _MEDIA_URL_PATTERN.match(image_url)
    if matched is None:
        raise MediaError(f'画像の URL の形式が不正です: {image_url}。/media/ で始まる URL は、取り込みが返した配信 URL(/media/{{hash}}/{{variant}}.webp)である必要があります。')
    variant = matched['variant']
    if variant not in VARIANTS:
        raise MediaError(f'画像の変種がありません: {variant}。指定できるのは {", ".join(VARIANTS)} です。')
    row = await db.fetch_one('SELECT is_public FROM media WHERE content_hash = %s', matched['hash'])
    if row is None:
        raise MediaError(f'その画像は取り込まれていません: {image_url}')
    if not row['is_public']:
        raise MediaError('非公開の画像は広告に使えません。広告は未ログインの読者にも配信するため、公開として取り込んだ画像を指定してください。')


async def _resolve_image_url(db: Database, media: MediaConfig, principal: Principal, image_url: str) -> str:
    """Resolve a creative's URL to a /media/ URL that we always serve ourselves.

    Never store a remote host's URL as-is: the moment that image disappears the slot goes on being served broken, and there is no way to notice it broke. Ingest at submission time and every later delivery is independent of the remote host. Ingest as public (is_public=1), because ads have to be served to logged-out readers too.
    """
    if image_url.startswith(_MEDIA_URL_PREFIX):
        await _require_public_media(db, image_url)
        return image_url
    try:
        result = await ingest_url(db, media, image_url, uploaded_by=principal.user_id, via_token_id=principal.token_id, is_public=True)
    except (FetchError, TranscodeError) as exc:
        # A fetch or transcode failure originates in the submitted URL. Return it as a bad request, or a remote host's trouble gets reported as our own outage.
        raise MediaError(f'広告のクリエイティブを取り込めませんでした: {exc}') from exc
    return result.url


@router.get('/api/admin/ad-slots')
async def list_ad_slots(db: Annotated[Database, Depends(get_db)]) -> ItemList[AdSlotOut]:
    """The ad slot listing. Recommended dimensions are returned so that an external management app can reproduce the appearance itself before submission: the core has no preview endpoint."""
    rows = await db.fetch_all('SELECT id, slug, name, description, recommended_width, recommended_height FROM ad_slots ORDER BY id')
    return ItemList[AdSlotOut](items=[AdSlotOut.model_validate(row) for row in rows])


@router.get('/api/admin/ads')
async def list_ads(
    db: Annotated[Database, Depends(get_db)],
    paging: Annotated[PagingParams, Depends(_ad_paging)],
) -> Page[AdOut]:
    """One page of the ad listing, newest submission first. Ads that were stopped (is_active=0) are included too: following performance stats requires the stopped ones to stay visible."""
    fetched = await fetch_keyset(
        db,
        page_request('ads', {}, paging),
        select=_AD_SELECT,
        count_from='FROM ads AS a',
        where='TRUE',
        where_args=[],
        order=_AD_ORDER,
    )
    return Page[AdOut](
        items=[_ad_out(row) for row in fetched.rows],
        per_page=fetched.per_page,
        next_cursor=fetched.next_cursor,
        prev_cursor=fetched.prev_cursor,
        total=fetched.total,
    )


@router.post(
    '/api/admin/ads',
    status_code=status.HTTP_201_CREATED,
    responses={201: {'headers': {'Idempotent-Replayed': {'description': '同じ Idempotency-Key の最初の要求の結果を再生したときに true。', 'schema': {'type': 'string'}}}}},
)
async def create_ad(
    payload: AdWrite,
    response: Response,
    principal: Annotated[Principal, Depends(_ads_admin)],
    db: Annotated[Database, Depends(get_db)],
    media: Annotated[MediaConfig, Depends(get_media_config)],
    idempotency_key: Annotated[str | None, Header(description='再送で広告が重複しないためのキー(UUID など)。同じキーと同じ本体の再送には、最初の要求が登録した広告を返す。')] = None,
) -> AdOut:
    """Submit an ad. A resend under the same Idempotency-Key and body returns the ad the first request created."""
    idempotency = idempotency_request(_CREATE_ENDPOINT, idempotency_key, payload)
    if idempotency is not None:
        replayed = await find_replay(db, principal.user_id, idempotency)
        if replayed is not None:
            response.headers['Idempotent-Replayed'] = 'true'
            return _ad_out(await _fetch_ad(db, replayed.resource_id))
    missing = [name for name in _REQUIRED_ON_CREATE if getattr(payload, name) is None]
    if missing:
        raise BadRequest(f'広告の作成には次の項目が必要です: {", ".join(missing)}')
    slot_id = to_snowflake(payload.slot_id)
    await _require_slot(db, slot_id)
    _validate_period(payload.starts_at, payload.ends_at)
    link_url = _validated_link_url(payload.link_url)
    image_url = await _resolve_image_url(db, media, principal, payload.image_url)
    try:
        async with db.transaction() as conn:
            record_id = await claim(db, conn, principal.user_id, idempotency) if idempotency is not None else None
            ad_id = await db.execute_insert(
                'INSERT INTO ads (slot_id, advertiser_name, image_url, link_url, alt_text, weight, starts_at, ends_at, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                slot_id,
                payload.advertiser_name,
                image_url,
                link_url,
                payload.alt_text,
                1 if payload.weight is None else payload.weight,
                payload.starts_at,
                payload.ends_at,
                1 if payload.is_active is None else int(payload.is_active),
                conn=conn,
            )
            if record_id is not None:
                await complete(db, conn, record_id, ad_id)
    except IdempotencyRace:
        replayed = await find_replay(db, principal.user_id, idempotency) if idempotency is not None else None
        if replayed is None:
            raise Conflict('同じ Idempotency-Key の要求が同時に処理されています。再送してください。') from None
        response.headers['Idempotent-Replayed'] = 'true'
        return _ad_out(await _fetch_ad(db, replayed.resource_id))
    return _ad_out(await _fetch_ad(db, ad_id))


@router.get('/api/admin/ads/{ad_id}')
async def get_ad(ad_id: str, db: Annotated[Database, Depends(get_db)]) -> AdOut:
    return _ad_out(await _fetch_ad(db, _parse_ad_id(ad_id)))


@router.patch('/api/admin/ads/{ad_id}')
async def update_ad(
    ad_id: str,
    payload: AdWrite,
    principal: Annotated[Principal, Depends(_ads_admin)],
    db: Annotated[Database, Depends(get_db)],
    media: Annotated[MediaConfig, Depends(get_media_config)],
) -> AdOut:
    """Partially update an ad. Omitted fields keep their current value.

    Omitted and explicitly set to null are treated as different things, told apart by pydantic's model_fields_set. A null for alt_text, starts_at or ends_at is a meaningful instruction: drop the alt text, start the run immediately, leave the end of the run open.
    """
    identifier = _parse_ad_id(ad_id)
    current = await _fetch_ad(db, identifier)
    fields = payload.model_fields_set
    for name in _NOT_NULLABLE:
        if name in fields and getattr(payload, name) is None:
            raise BadRequest(f'{name} には null を指定できません。')
    # Validate the period with the unspecified side filled in from the current value, so that updating only one end cannot produce an ad whose period runs backwards.
    _validate_period(
        payload.starts_at if 'starts_at' in fields else current['starts_at'],
        payload.ends_at if 'ends_at' in fields else current['ends_at'],
    )
    # The assignment fragments are only the fixed strings written here; every value goes through %s. Nothing from the request touches the structure of the SQL.
    assignments: list[str] = []
    args: list[Any] = []
    if 'slot_id' in fields:
        slot_id = to_snowflake(payload.slot_id)
        await _require_slot(db, slot_id)
        assignments.append('slot_id = %s')
        args.append(slot_id)
    if 'advertiser_name' in fields:
        assignments.append('advertiser_name = %s')
        args.append(payload.advertiser_name)
    if 'image_url' in fields:
        assignments.append('image_url = %s')
        args.append(await _resolve_image_url(db, media, principal, payload.image_url))
    if 'link_url' in fields:
        assignments.append('link_url = %s')
        args.append(_validated_link_url(payload.link_url))
    if 'alt_text' in fields:
        assignments.append('alt_text = %s')
        args.append(payload.alt_text)
    if 'weight' in fields:
        assignments.append('weight = %s')
        args.append(payload.weight)
    if 'starts_at' in fields:
        assignments.append('starts_at = %s')
        args.append(payload.starts_at)
    if 'ends_at' in fields:
        assignments.append('ends_at = %s')
        args.append(payload.ends_at)
    if 'is_active' in fields:
        assignments.append('is_active = %s')
        args.append(int(payload.is_active))
    if not assignments:
        return _ad_out(current)
    args.append(identifier)
    await db.execute(f'UPDATE ads SET {", ".join(assignments)} WHERE id = %s', *args)
    return _ad_out(await _fetch_ad(db, identifier))


@router.delete('/api/admin/ads/{ad_id}')
async def deactivate_ad(ad_id: str, db: Annotated[Database, Depends(get_db)]) -> AdOut:
    """Stop an ad from running. The row is kept and is_active set to 0.

    This is a soft delete, not a hard delete: ad_stats_daily references ads with CASCADE, so dropping the row would take the performance stats with it (how many times that ad was shown and how many times it was clicked). An ad whose run has ended is precisely the one you reconcile performance stats against.

    The response is the updated ad itself, so is_active=false shows on the spot that DELETE means stopped, not gone.
    """
    identifier = _parse_ad_id(ad_id)
    await _fetch_ad(db, identifier)
    await db.execute('UPDATE ads SET is_active = 0 WHERE id = %s', identifier)
    return _ad_out(await _fetch_ad(db, identifier))


@router.get('/api/admin/ads/{ad_id}/stats')
async def get_ad_stats(
    ad_id: str,
    db: Annotated[Database, Depends(get_db)],
    from_date: Annotated[date | None, Query(alias='from', description='集計の開始日(この日を含む)。省略すると下限を設けない。')] = None,
    to_date: Annotated[date | None, Query(alias='to', description='集計の終了日(この日を含む)。省略すると上限を設けない。')] = None,
) -> AdStatsOut:
    """An ad's daily performance stats. The totals are over the range returned, and always agree with the per-day values.

    A day with no stats has no row: no zero row is written for a day with 0 impressions. A gap in the dates means there were no impressions that day.
    """
    identifier = _parse_ad_id(ad_id)
    if await db.fetch_val('SELECT id FROM ads WHERE id = %s', identifier) is None:
        # Returning an empty success for an ad that does not exist would make a mistyped ID indistinguishable from genuinely zero stats.
        raise NotFound('広告がありません。')
    if from_date is not None and to_date is not None and from_date > to_date:
        raise BadRequest('from は to 以前の日付である必要があります。')
    conditions = ['ad_id = %s']
    args: list[Any] = [identifier]
    if from_date is not None:
        conditions.append('stat_date >= %s')
        args.append(from_date)
    if to_date is not None:
        conditions.append('stat_date <= %s')
        args.append(to_date)
    rows = await db.fetch_all(f'SELECT stat_date, impressions, clicks FROM ad_stats_daily WHERE {" AND ".join(conditions)} ORDER BY stat_date', *args)
    days = [AdStatDay.model_validate(row) for row in rows]
    return AdStatsOut(
        ad_id=identifier,
        days=days,
        total_impressions=sum(day.impressions for day in days),
        total_clicks=sum(day.clicks for day in days),
    )
