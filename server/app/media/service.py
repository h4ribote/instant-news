"""Image ingest, assembled: fetch, convert, store and catalogue as one flow.

Images in an article body are ingested on every save, not at publish time. Discord CDN URLs expire in about a day, so waiting until publish means an article left sitting as a draft is fetched only after expiry and always fails. Copying the bytes locally at write time is the only way an article does not lose its images later.

Ingest failure is not an exception (ingest_article_images). A remote host being temporarily down has nothing to do with whether the article can be saved. A failed URL is left in the body as-is, and what could not be ingested is returned as IngestFailure to tell the writer. Treating it as a silent success leaves the writer unaware the original URL remained, and that image eventually disappears.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from common.config import MediaConfig
from common.db import Database

from ..errors import AppError, MediaError, QuotaExceeded
from ..markdown import extract_image_urls, rewrite_image_urls
from .fetch import FetchError, fetch_image
from .store import blob_exists, write_blob
from .transcode import EncodedImage, TranscodeError, encode_to_webp

logger = logging.getLogger(__name__)

# Delivery URL prefix. A body URL already pointing here is already ingested, so it is not fetched.
MEDIA_URL_PREFIX: Final = '/media/'
# Width of the media.source_url column. The original URL is kept for tracing, but a URL too long for the column must not fail the save itself.
_SOURCE_URL_MAX_LENGTH: Final = 1000


@dataclass(frozen=True)
class IngestResult:
    """Ingest result. url is the delivery URL (/media/{hash}/orig.webp)."""

    media_id: int
    content_hash: str
    url: str


@dataclass(frozen=True)
class IngestFailure:
    """An image that could not be ingested. source_url is the URL left in the body as-is."""

    source_url: str
    reason: str


def media_url(content_hash: str, variant: str = 'orig') -> str:
    """Build the delivery URL."""
    return f'{MEDIA_URL_PREFIX}{content_hash}/{variant}.webp'


def _is_media_url(url: str) -> bool:
    """Whether the URL points at an already-ingested image. The path alone decides it, for absolute and relative URLs alike."""
    try:
        return urlsplit(url).path.startswith(MEDIA_URL_PREFIX)
    except ValueError:
        return False


def _write_blobs(cfg: MediaConfig, encoded: EncodedImage) -> None:
    """Write only the variants that do not exist yet.

    Paths are content-addressed, so a file that is already there necessarily holds the same content. Checking existence before writing also means that when dedupe reuses an existing row, a missing blob is rewritten, recovering from the state where only the catalogue row remains and the image 404s.
    """
    for variant, payload in encoded.variants.items():
        if blob_exists(cfg.storage_dir, encoded.content_hash, variant):
            continue
        write_blob(cfg.storage_dir, encoded.content_hash, variant, payload)


async def _check_quota(db: Database, cfg: MediaConfig, uploaded_by: int, additional: int) -> None:
    """Check the quota. Not called when an existing blob is reused, so the same bytes are never counted twice."""
    used = await db.fetch_val('SELECT COALESCE(SUM(byte_size), 0) FROM media WHERE uploaded_by = %s', uploaded_by)
    total = int(used or 0) + additional
    if total > cfg.quota_bytes_per_user:
        raise QuotaExceeded(f'取り込んだ画像の合計({total} バイト)が 1 ユーザーあたりの上限({cfg.quota_bytes_per_user} バイト)を超えます。')


async def ingest_bytes(
    db: Database,
    cfg: MediaConfig,
    data: bytes,
    *,
    uploaded_by: int,
    via_token_id: int | None,
    is_public: bool,
    lossless: bool = False,
    source_url: str | None = None,
) -> IngestResult:
    """Convert the bytes, store them, and put them in the catalogue. If the same content is already there, reuse that row.

    Conversion monopolises the CPU, so it goes to a separate thread: ingest runs while a request is being handled and makes the article save wait.
    """
    try:
        encoded = await asyncio.to_thread(encode_to_webp, data, max_pixels=cfg.max_pixels, lossless=lossless)
    except TranscodeError as exc:
        raise MediaError(str(exc)) from exc

    # content_hash is the post-conversion content itself, so rows with the same hash necessarily point at the same image. Lossless vs lossy changes the output, therefore the hash, therefore the row.
    existing = await db.fetch_one('SELECT id FROM media WHERE content_hash = %s', encoded.content_hash)
    if existing is None:
        await _check_quota(db, cfg, uploaded_by, encoded.byte_size)

    # Write the blob before putting the row in the catalogue. In the reverse order, a failed write leaves "row but no image" behind and serving 404s. Writing first, the only leftover is an unreferenced file.
    await asyncio.to_thread(_write_blobs, cfg, encoded)

    if existing is None:
        # ON DUPLICATE KEY UPDATE keeps a concurrent ingest of the same image from failing on the unique key. id = LAST_INSERT_ID(id) is the idiom that makes a collision return the existing row's id as lastrowid.
        media_id = await db.execute_insert(
            'INSERT INTO media (content_hash, width, height, byte_size, is_public, is_animated, is_lossless, source_url, uploaded_by, via_token_id) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)',
            encoded.content_hash,
            encoded.width,
            encoded.height,
            encoded.byte_size,
            1 if is_public else 0,
            1 if encoded.is_animated else 0,
            1 if encoded.is_lossless else 0,
            source_url[:_SOURCE_URL_MAX_LENGTH] if source_url else None,
            uploaded_by,
            via_token_id,
        )
    else:
        media_id = int(existing['id'])

    if is_public:
        # An image ingested as non-public may later be ingested for a public use (cover image, ad, logo), and is then promoted to public. Without the promotion, merely because the same content already existed, a cover image would not be served to logged-out viewers and og:image would break. Only someone who actually holds the same bytes can request public, so the promotion leaks nothing new. The reverse (public to non-public) is never done: it would break existing cover images.
        await db.execute('UPDATE media SET is_public = 1 WHERE id = %s AND is_public = 0', media_id)

    return IngestResult(media_id=media_id, content_hash=encoded.content_hash, url=media_url(encoded.content_hash))


async def ingest_url(
    db: Database,
    cfg: MediaConfig,
    url: str,
    *,
    uploaded_by: int,
    via_token_id: int | None,
    is_public: bool,
) -> IngestResult:
    """Fetch an image from a URL and ingest it. MediaError if it cannot be fetched."""
    try:
        data = await fetch_image(url, cfg.fetch)
    except FetchError as exc:
        raise MediaError(f'画像を取得できませんでした: {exc}') from exc
    return await ingest_bytes(
        db,
        cfg,
        data,
        uploaded_by=uploaded_by,
        via_token_id=via_token_id,
        is_public=is_public,
        source_url=url,
    )


async def ingest_article_images(
    db: Database,
    cfg: MediaConfig,
    body_md: str,
    *,
    uploaded_by: int,
    via_token_id: int | None,
) -> tuple[str, list[int], list[IngestFailure]]:
    """Ingest the images in the body and rewrite their URLs to delivery URLs.

    Returns (rewritten body_md, the ingested media.id list, the failure list). **Never raises on failure.**

    Images are ingested one at a time. Going concurrent would cut the total time, but the decoded pixels would then be in memory together: with a per-image max of tens of millions of pixels, decoding dozens at once exhausts memory. Sequentially, only one image is ever resident.

    Body images are ingested with is_public=False, so that serving checks whether the article referencing them may be viewed; the article's visibility is what decides whether they may be public.
    """
    urls = [url for url in extract_image_urls(body_md) if not _is_media_url(url)]
    failures: list[IngestFailure] = []
    limit = cfg.fetch.max_images_per_article
    targets, overflow = urls[:limit], urls[limit:]
    for url in overflow:
        failures.append(IngestFailure(source_url=url, reason=f'1 記事あたりに取り込める画像は {limit} 枚までです。この画像は元の URL のまま残ります。'))

    mapping: dict[str, str] = {}
    media_ids: list[int] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.fetch.total_timeout_seconds
    for url in targets:
        if loop.time() >= deadline:
            # Past the deadline, the rest are recorded as failures without even attempting a fetch. fetch_image holds its own per-image deadline, so all this deadline decides is whether to start the next image. Cutting off mid-fetch would interrupt a conversion or a catalogue insert and could leave a half-finished state.
            failures.append(IngestFailure(source_url=url, reason='記事全体の取得時間の上限を超えたため、この画像は元の URL のまま残ります。'))
            continue
        try:
            result = await ingest_url(db, cfg, url, uploaded_by=uploaded_by, via_token_id=via_token_id, is_public=False)
        except AppError as exc:
            failures.append(IngestFailure(source_url=url, reason=exc.message))
            continue
        except Exception:
            # An unexpected failure must not take down the article save itself. The cause goes to the log only, never to the response: exception text mixes in table names, paths and the like.
            logger.exception('画像の取り込みで想定外の例外が発生しました: %s', url)
            failures.append(IngestFailure(source_url=url, reason='画像を取り込めませんでした。'))
            continue
        mapping[url] = result.url
        if result.media_id not in media_ids:
            media_ids.append(result.media_id)

    # A URL that could not be ingested is absent from mapping, so it stays in the body as-is.
    return rewrite_image_urls(body_md, mapping), media_ids, failures
