"""広告の管理用 API(ads:admin)。

このルーターが要求するスコープは ads:admin ただ 1 つである。管理者が発行できるスコープを 1 つに束ねてはならない。広告管理アプリに渡すのは ads:admin だけであり、記事の署名を偽造する力(articles:write:any)を混ぜて渡すと、広告の入稿を任せた相手が任意の記者の名前で記事を出せることになる。

広告枠(ad_slots)を作る API は持たない。枠はフロントエンドのテンプレートが slug で参照する固定の集合であり、移行で投入される。API から枠を増やせるようにしても、それを描画するテンプレートが無ければ何も起きず、「作れるのに出ない枠」という説明のつかない状態が増えるだけである。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Query, status

from common.config import MediaConfig
from common.db import Database
from common.snowflake import to_snowflake

from ..deps import get_db, get_media_config, require_scope
from ..errors import BadRequest, MediaError, NotFound
from ..media.fetch import FetchError
from ..media.service import ingest_url
from ..media.store import VARIANTS
from ..media.transcode import TranscodeError
from ..principals import Principal
from ..schemas import AdOut, AdSlotOut, AdStatDay, AdStatsOut, AdWrite

# 依存性を 1 つの物として持つ。ルーター全体へ課したうえで、主体そのものを要る経路が同じ物を参照することで、FastAPI の依存性キャッシュが 1 要求につき 1 回だけ評価する。
_ads_admin: Final = require_scope('ads:admin')

# 経路ごとの宣言ではなくルーターへ課す。新しい経路を足したときに検査を書き忘れても、素通りする経路が生まれない。
router = APIRouter(tags=['admin:ads'], dependencies=[Depends(_ads_admin)])

# 広告のクリエイティブは必ず自分で配信する。/media/{hash}/{variant}.webp は media.service が返す配信 URL の形。
_MEDIA_URL_PREFIX: Final = '/media/'
_MEDIA_URL_PATTERN: Final = re.compile(r'^/media/(?P<hash>[0-9a-f]{64})/(?P<variant>[^/]+)\.webp$')

_LINK_SCHEMES: Final = ('http', 'https')

# 作成に必須の項目。AdWrite は部分更新にも使うため全ての項目が省略可能であり、作成時の必須はここで検査する。
_REQUIRED_ON_CREATE: Final = ('slot_id', 'advertiser_name', 'image_url', 'link_url')

# NOT NULL の列。部分更新で明示的な null を指定されたら弾く(null が入らないことは DB が守るが、そこで落とすと制約違反の 500 になり、何が悪いのか伝わらない)。
_NOT_NULLABLE: Final = ('slot_id', 'advertiser_name', 'image_url', 'link_url', 'weight', 'is_active')

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
    """パスの ID を 64bit の整数へ。解釈できない値は、存在しない ID と区別する意味が無いため「無い」として扱う。"""
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
    """遷移先が http / https の絶対 URL であることを確かめる。

    この値はクリックの計測を経て転送先になり、フロントエンドではリンクとして描画される。スキームを検査しないと javascript: のような URL を仕込めてしまい、広告の入稿がそのままスクリプトの実行になる。
    """
    parsed = urlsplit(link_url)
    if parsed.scheme.lower() not in _LINK_SCHEMES or not parsed.netloc:
        raise BadRequest(f'link_url は http または https で始まる絶対 URL である必要があります: {link_url}')
    return link_url


def _validate_period(starts_at: datetime | None, ends_at: datetime | None) -> None:
    """掲載期間の前後を検査する。starts_at が ends_at より後の広告はどの時点でも候補に入らず、入稿したのに一度も出ないまま掲載期間だけが過ぎる。"""
    if starts_at is not None and ends_at is not None and starts_at > ends_at:
        raise BadRequest('ends_at は starts_at 以降である必要があります。')


async def _require_public_media(db: Database, image_url: str) -> None:
    """既に取り込まれた画像を指す URL について、実在し、かつ公開扱いであることを確かめる。

    広告は未ログインの読者にも配信するため、非公開の画像を指定されると読者の側では枠が壊れる。ここで media.is_public を 1 へ書き換えて解決してはならない。非公開の画像は閲覧制限のある記事の図版でありうるため、広告に指定するだけでその図版が全読者へ配られることになる。
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
    """クリエイティブの URL を、必ず自分で配信する /media/ の URL へ解決する。

    外部ホストの URL をそのまま保存してはならない。その画像が消えた時点で枠が壊れたまま配信され続け、しかも壊れたことに気付く手立てが無い。入稿の時点で取り込んでしまえば、以後の配信は外部のホストに依存しない。取り込みは公開(is_public=1)として行う。広告は未ログインの読者へも配信する必要がある。
    """
    if image_url.startswith(_MEDIA_URL_PREFIX):
        await _require_public_media(db, image_url)
        return image_url
    try:
        result = await ingest_url(db, media, image_url, uploaded_by=principal.user_id, via_token_id=principal.token_id, is_public=True)
    except (FetchError, TranscodeError) as exc:
        # 取得・変換の失敗は入稿した URL に起因する。要求の誤りとして返さないと、外部ホストの不調が自分の障害として報告される。
        raise MediaError(f'広告のクリエイティブを取り込めませんでした: {exc}') from exc
    return result.url


@router.get('/api/admin/ad-slots')
async def list_ad_slots(db: Annotated[Database, Depends(get_db)]) -> list[AdSlotOut]:
    """広告枠の一覧。推奨寸法を返すのは、外部の管理アプリが入稿前に見え方を自前で再現できるようにするため(コアはプレビュー用のエンドポイントを持たない)。"""
    rows = await db.fetch_all('SELECT id, slug, name, description, recommended_width, recommended_height FROM ad_slots ORDER BY id')
    return [AdSlotOut.model_validate(row) for row in rows]


@router.get('/api/admin/ads')
async def list_ads(db: Annotated[Database, Depends(get_db)]) -> list[AdOut]:
    """広告の一覧。掲載を止めた広告(is_active=0)も含める。実績を追うには止めたものが見え続ける必要がある。"""
    rows = await db.fetch_all(f'{_AD_SELECT} ORDER BY a.created_at DESC, a.id DESC')
    return [_ad_out(row) for row in rows]


@router.post('/api/admin/ads', status_code=status.HTTP_201_CREATED)
async def create_ad(
    payload: AdWrite,
    principal: Annotated[Principal, Depends(_ads_admin)],
    db: Annotated[Database, Depends(get_db)],
    media: Annotated[MediaConfig, Depends(get_media_config)],
) -> AdOut:
    """広告を入稿する。"""
    missing = [name for name in _REQUIRED_ON_CREATE if getattr(payload, name) is None]
    if missing:
        raise BadRequest(f'広告の作成には次の項目が必要です: {", ".join(missing)}')
    slot_id = to_snowflake(payload.slot_id)
    await _require_slot(db, slot_id)
    _validate_period(payload.starts_at, payload.ends_at)
    link_url = _validated_link_url(payload.link_url)
    image_url = await _resolve_image_url(db, media, principal, payload.image_url)
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
    )
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
    """広告を部分更新する。省略された項目は現在の値のまま残す。

    「省略された」と「明示的に null を指定した」は別物として扱う(pydantic の model_fields_set で区別する)。alt_text・starts_at・ends_at への null は、代替テキストを外す・掲載開始を即時にする・掲載終了を無期限にする、という意味のある指定である。
    """
    identifier = _parse_ad_id(ad_id)
    current = await _fetch_ad(db, identifier)
    fields = payload.model_fields_set
    for name in _NOT_NULLABLE:
        if name in fields and getattr(payload, name) is None:
            raise BadRequest(f'{name} には null を指定できません。')
    # 期間は、指定されなかった側を現在の値で補って検査する。片方だけの更新で前後が逆転した広告が生まれるのを防ぐ。
    _validate_period(
        payload.starts_at if 'starts_at' in fields else current['starts_at'],
        payload.ends_at if 'ends_at' in fields else current['ends_at'],
    )
    # 代入する断片はここに書かれた固定の文字列だけであり、値は全て %s で渡す。要求の内容が SQL の構造に触れることはない。
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
    """広告の掲載を止める。行は消さず is_active=0 にする。

    物理削除にしてはならない。ad_stats_daily は ads を CASCADE で参照しているため、行を消すとその広告が何回表示され何回押されたかの実績ごと消える。掲載の終わった広告こそ、実績を突き合わせる相手である。

    応答は更新後の広告そのものを返す。DELETE が「消滅」ではなく「停止」であることが、応答の is_active=false としてその場で分かる。
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
    """広告の日次実績。合計は返した範囲の合計であり、日ごとの値と必ず一致する。

    実績の無い日は行を持たない(表示が 0 の日に 0 の行を作らない)。日付の穴は、その日の表示が無かったことを意味する。
    """
    identifier = _parse_ad_id(ad_id)
    if await db.fetch_val('SELECT id FROM ads WHERE id = %s', identifier) is None:
        # 存在しない広告の実績を空の成功として返すと、ID を打ち間違えたことが「実績ゼロ」と見分けられない。
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
