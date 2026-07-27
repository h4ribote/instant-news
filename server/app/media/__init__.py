"""Image ingest, conversion, and storage.

Four responsibilities: store reads and writes blobs by content address, fetch retrieves bytes from remote hosts (SSRF defence), transcode decodes and re-encodes to WebP, and service is the ingest flow that ties them together.
They stay separate because fetch and transcode are each independently a boundary handling untrusted input, and merging them loses track of what was checked where.
"""

from __future__ import annotations

from .fetch import FetchError, fetch_image
from .service import (
    IngestFailure,
    IngestResult,
    ingest_article_images,
    ingest_bytes,
    ingest_url,
    media_url,
)
from .store import VARIANTS, blob_exists, blob_path, read_blob, write_blob
from .transcode import EncodedImage, TranscodeError, encode_to_webp

__all__ = [
    'VARIANTS',
    'EncodedImage',
    'FetchError',
    'IngestFailure',
    'IngestResult',
    'TranscodeError',
    'blob_exists',
    'blob_path',
    'encode_to_webp',
    'fetch_image',
    'ingest_article_images',
    'ingest_bytes',
    'ingest_url',
    'media_url',
    'read_blob',
    'write_blob',
]
