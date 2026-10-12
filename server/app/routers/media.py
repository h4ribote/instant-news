"""Image upload and delivery.

Serving splits in two. An is_public image is served unconditionally and cached forever, justified by it being content-addressed: change the content and the URL changes with it, so old content can never keep being returned. Every other image has the visibility of the article referencing it checked on each request.

An image the caller cannot view returns 404, not 403. A 403 reveals that something is there, and for a limited-visibility article whose very existence is secret, that alone is a leak. A hash or variant that does not exist and an image the caller has no permission for must be indistinguishable in the response.
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

# The hash in a delivery URL is 64 hex digits, lowercase. The CHAR(64) column's collation is case-insensitive, so unless the form is pinned here an uppercase hash matches the row yet not the file name, and the image cannot be served.
_CONTENT_HASH_PATTERN: Final = re.compile(r'\A[0-9a-f]{64}\Z')

# Neither the status code nor the wording differs between "does not exist" and "no permission".
_NOT_FOUND_MESSAGE: Final = '画像が見つかりません。'

# Content-addressed, so what this URL returns never changes again.
_PUBLIC_CACHE_CONTROL: Final = 'public, max-age=31536000, immutable'
# Restricted images are `private` because a copy in a shared cache (a CDN, a corporate proxy) would be handed to other people with no permission check.
_PRIVATE_CACHE_CONTROL: Final = 'private, max-age=300'
# Execute nothing even if the bytes are somehow interpreted as HTML. With nosniff, this closes off image delivery as an XSS path.
_MEDIA_CSP: Final = "default-src 'none'; sandbox"

# Scopes that permit upload; any one of them suffices. articles:write is needed for body images and covers, ads:admin for ad creatives. articles:write:any gets in as an implication of articles:write, but it is an admin scope all the same: a key holding only that passes only when its owner is an admin at the moment of the request.
_UPLOAD_SCOPES: Final = ('articles:write', 'ads:admin')

_UPLOAD_CHUNK_SIZE: Final = 65536


async def _authorize_upload(db: Database, settings: RuntimeSettings, principal: Principal) -> None:
    """Check eligibility to upload.

    The decision is delegated to the same ensure_scope the article and ad endpoints use. Rules written out here (any one scope suffices; if only admin scopes satisfy the request, current admin eligibility is required; scopes are not checked on the session path) would fall behind the day the rules change. Concretely, articles:write:any implies articles:write, so code that inspects which scopes a key carries one by one lets a key holding only write:any through without the admin eligibility check.
    """
    await ensure_scope(db, settings, principal, *_UPLOAD_SCOPES)


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read the uploaded body under a byte cap.

    The declared size is not trusted; what is actually read is counted and the read is cut off there. Reading with no cap lets a single request exhaust memory.
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
    """Read the variant's blob, falling back to orig when it is absent (no variant wider than the original size is produced, so a missing variant file happens normally)."""
    data = read_blob(storage_dir, content_hash, variant)
    if data is None and variant != 'orig':
        data = read_blob(storage_dir, content_hash, 'orig')
    return data


@router.post('/api/media')
async def upload_media(
    file: Annotated[UploadFile, File(description='取り込む画像。復号して WebP へ再符号化したものだけを保存する。')],
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[RuntimeSettings, Depends(get_settings)],
    media: Annotated[MediaConfig, Depends(get_media_config)],
    principal: Annotated[Principal, Depends(get_principal)],
    is_public: Annotated[bool, Query(description='認証なしで配信してよいか。カバー・広告・ロゴ・アイコンは公開、本文中の画像は非公開。')] = False,
    lossless: Annotated[bool, Query(description='可逆で符号化するか。ロゴ・アイコンなどの線画に使う。')] = False,
) -> MediaOut:
    """Ingest an image."""
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


@router.get(
    '/media/{content_hash}/{variant}.webp',
    response_class=Response,
    responses={200: {'description': '画像(WebP)。', 'content': {'image/webp': {'schema': {'type': 'string', 'format': 'binary'}}}}},
)
async def serve_media(
    content_hash: str,
    variant: str,
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    settings: Annotated[RuntimeSettings, Depends(get_settings)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> Response:
    """Serve an image. Where the blobs live (storage_dir) is inseparable from the runtime environment, so it is taken straight from config. The admin roles the visibility check needs live in the settings table, so they are taken from settings."""
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
        # Tell caches explicitly that the response varies by caller. Without this, the previous user's image can stay visible to a different user of the same browser.
        headers = {'Cache-Control': _PRIVATE_CACHE_CONTROL, 'Vary': 'Cookie, Authorization'}

    data = await asyncio.to_thread(_read_variant, config.storage_dir, content_hash, variant)
    if data is None:
        raise NotFound(_NOT_FOUND_MESSAGE)

    headers['X-Content-Type-Options'] = 'nosniff'
    headers['Content-Security-Policy'] = _MEDIA_CSP
    return Response(content=data, media_type='image/webp', headers=headers)
