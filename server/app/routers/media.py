"""画像のアップロードと配信。

配信の判定は 2 つに分かれる。is_public の画像は無条件に配信し、内容アドレスであることを根拠に永久にキャッシュさせる(内容が変われば URL も変わるため、古い内容が返り続けることが起こらない)。そうでない画像は、参照する記事の閲覧可否をそのつど検査する。

閲覧できない画像には 404 を返す。403 は「そこに何かがある」ことを教えてしまい、存在自体が秘密である限定公開の記事では、それだけで漏洩になる。存在しないハッシュ・変種と、権限が無い画像は、応答から区別できてはならない。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from common.config import Config, MediaConfig
from common.db import Database
from common.settings import RuntimeSettings

from ..deps import ensure_scope, get_config, get_db, get_media_config, get_principal, get_principal_optional, get_settings
from ..errors import MediaError, NotFound
from ..media.service import ingest_bytes
from ..media.store import VARIANTS, read_blob
from ..principals import Principal
from ..schemas import MediaOut
from ..visibility import can_view_media

router = APIRouter()

# 配信 URL のハッシュは 16 進 64 桁(小文字)。DB の CHAR(64) の照合は大小を区別しないため、ここで表記を絞らないと、大文字のハッシュが行に当たったのにファイル名と一致せず配信できない、という食い違いが起こる。
_CONTENT_HASH_PATTERN: Final = re.compile(r'\A[0-9a-f]{64}\Z')

# 存在しない場合と権限が無い場合で、状態コードも文言も変えない。
_NOT_FOUND_MESSAGE: Final = '画像が見つかりません。'

# 内容アドレスであるため、この URL の内容は二度と変わらない。
_PUBLIC_CACHE_CONTROL: Final = 'public, max-age=31536000, immutable'
# public を付けてはならない。共有キャッシュ(CDN・企業のプロキシ)に載ると、権限を検査せずに他人へ配られる。
_PRIVATE_CACHE_CONTROL: Final = 'private, max-age=300'
# 万一 HTML として解釈されても何も実行させない。nosniff と併せて、画像の配信が XSS の経路になることを塞ぐ。
_MEDIA_CSP: Final = "default-src 'none'; sandbox"

# アップロードを許すスコープ。このいずれかで足りる。articles:write は本文中の画像とカバー、ads:admin は広告のクリエイティブのために要る。articles:write:any は articles:write の含意として入るが、管理者スコープであることに変わりはなく、それだけを持つキーは要求の時点で所有者が管理者である場合にしか通らない。
_UPLOAD_SCOPES: Final = ('articles:write', 'ads:admin')

_UPLOAD_CHUNK_SIZE: Final = 65536


async def _authorize_upload(db: Database, settings: RuntimeSettings, principal: Principal) -> None:
    """アップロードの資格を検査する。

    判定は記事や広告のエンドポイントと同じ ensure_scope に委ねる。「いずれかのスコープで足りる」「管理者スコープでしか成立しないなら現在の管理者資格を問う」「セッション経由ではスコープを検査しない」という規則を、この 1 箇所だけ手書きで持つと、規則が変わったときにここだけが取り残される。実際、articles:write:any は articles:write を含意するため、キーにどのスコープが載っているかを個別に見る書き方では、write:any だけを持つキーが管理者資格の検査を経ずに通ってしまう。
    """
    await ensure_scope(db, settings, principal, *_UPLOAD_SCOPES)


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """アップロードされた本体を上限付きで読む。

    申告された大きさを信じず、実際に読んだ量を数えて打ち切る。上限を設けずに読むと、1 回の要求でメモリを使い果たさせられる。
    """
    buffer = bytearray()
    while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise MediaError(f'画像が大きすぎます(1 枚あたり {max_bytes} バイトまで)。')
    if not buffer:
        raise MediaError('画像が空です。')
    return bytes(buffer)


def _read_variant(storage_dir: Path, content_hash: str, variant: str) -> bytes | None:
    """変種の実体を読む。無ければ原寸へ落とす(原寸より広い変種は生成されないため、変種のファイルが無いことは正常に起こる)。"""
    data = read_blob(storage_dir, content_hash, variant)
    if data is None and variant != 'orig':
        data = read_blob(storage_dir, content_hash, 'orig')
    return data


@router.post('/api/media', response_model=MediaOut)
async def upload_media(
    file: Annotated[UploadFile, File(description='取り込む画像。復号して WebP へ再符号化したものだけを保存する。')],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[RuntimeSettings, Depends(get_settings)],
    media: Annotated[MediaConfig, Depends(get_media_config)],
    principal: Annotated[Principal, Depends(get_principal)],
    is_public: Annotated[bool, Query(description='認証なしで配信してよいか。カバー・広告・ロゴ・アイコンは公開、本文中の画像は非公開。')] = False,
    lossless: Annotated[bool, Query(description='可逆で符号化するか。ロゴ・アイコンなどの線画に使う。')] = False,
) -> MediaOut:
    """画像を取り込む。"""
    await _authorize_upload(db, settings, principal)
    data = await _read_upload(file, media.fetch.max_bytes)
    result = await ingest_bytes(
        db,
        media,
        data,
        uploaded_by=principal.user_id,
        via_token_id=principal.token_id,
        is_public=is_public,
        lossless=lossless,
    )
    return MediaOut(id=result.media_id, content_hash=result.content_hash, url=result.url)


@router.get('/media/{content_hash}/{variant}.webp', response_class=Response)
async def serve_media(
    content_hash: str,
    variant: str,
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    settings: Annotated[RuntimeSettings, Depends(get_settings)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> Response:
    """画像を配信する。実体の置き場所(storage_dir)は実行環境と一体であるため config から直接取る。閲覧範囲の判定に要る管理ロールは settings 表にあるため settings から取る。"""
    if not _CONTENT_HASH_PATTERN.match(content_hash) or variant not in VARIANTS:
        raise NotFound(_NOT_FOUND_MESSAGE)
    row = await db.fetch_one('SELECT id, is_public FROM media WHERE content_hash = %s', content_hash)
    if row is None:
        raise NotFound(_NOT_FOUND_MESSAGE)

    media_id = int(row['id'])
    if row['is_public']:
        headers = {'Cache-Control': _PUBLIC_CACHE_CONTROL}
    else:
        if not await can_view_media(db, principal, media_id, False, settings=settings):
            raise NotFound(_NOT_FOUND_MESSAGE)
        # 応答が要求元によって変わることを、キャッシュへ明示する。これが無いと、同じ閲覧器の別の利用者へ前の利用者の画像が残って見えうる。
        headers = {'Cache-Control': _PRIVATE_CACHE_CONTROL, 'Vary': 'Cookie, Authorization'}

    data = await asyncio.to_thread(_read_variant, config.storage_dir, content_hash, variant)
    if data is None:
        raise NotFound(_NOT_FOUND_MESSAGE)

    headers['X-Content-Type-Options'] = 'nosniff'
    headers['Content-Security-Policy'] = _MEDIA_CSP
    return Response(content=data, media_type='image/webp', headers=headers)
