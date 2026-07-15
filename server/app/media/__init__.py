"""画像の取り込み・変換・保存。

責務を 4 つに分ける。store は内容アドレスでの実体の読み書き、fetch は外部からの取得(SSRF 対策)、transcode は復号と WebP への再符号化、service はそれらを繋ぐ取り込みの流れである。分けているのは、fetch と transcode がそれぞれ独立に「信用できない入力を扱う境界」であり、混ぜると何をどこで検査したのかが追えなくなるためである。
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
