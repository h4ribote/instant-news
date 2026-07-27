"""Content-addressed blob store for image files.

Blobs live at {storage_dir}/{hash[0:2]}/{hash[2:4]}/{hash}.{variant}.webp. The first 4 hex digits fan out into two directory levels because hundreds of thousands of files in a single directory make enumeration slow even on ext4.

Being content-addressed, identical content always lands on the same path, so a write is only ever an overwrite with identical bytes. Serving can still read a partially written file, so write to a temporary file and replace.

content_hash and variant arrive verbatim from the delivery URL, so validate them before building a path. Skip the check and a value containing ../ points outside the storage directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final
from uuid import uuid4

# The variants generated and served. 'orig' is the original size and is mandatory. The numbers are widths in pixels; no variant wider than the original is produced.
VARIANTS: Final = ('orig', '1200w', '400w')

# content_hash is SHA-256 as lowercase hex. Allowing uppercase would still pass the DB lookup (CHAR(64) with an ai_ci collation ignores case) while failing to match the filename, so an image that exists would 404.
_CONTENT_HASH_PATTERN: Final = re.compile(r'\A[0-9a-f]{64}\Z')


def blob_path(storage_dir: Path, content_hash: str, variant: str) -> Path:
    """Return the path of the image blob. ValueError if content_hash is not 64 lowercase hex digits, or the variant is unknown."""
    if not _CONTENT_HASH_PATTERN.match(content_hash):
        raise ValueError(f'content_hash は SHA-256 の 16 進 64 桁(小文字)である必要があります: {content_hash!r}')
    if variant not in VARIANTS:
        raise ValueError(f'未知の変種です: {variant!r}。指定できるのは {", ".join(VARIANTS)} です。')
    return storage_dir / content_hash[0:2] / content_hash[2:4] / f'{content_hash}.{variant}.webp'


def write_blob(storage_dir: Path, content_hash: str, variant: str, data: bytes) -> None:
    """Write the image blob, replacing any file already at the path (content-addressed, so the bytes are identical).

    Write to a temporary file, then os.replace it. Replacement within one directory is atomic, so the serving side never reads a half-written file. The fsync is there so a power loss cannot leave the DB row behind with a corrupt file.
    """
    path = blob_path(storage_dir, content_hash, variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Name the temporary file uniquely per process and per call, so concurrent ingests writing the same content cannot clobber each other's temporary file.
    temporary = path.parent / f'.{path.name}.{os.getpid()}.{uuid4().hex}.tmp'
    try:
        with open(temporary, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_blob(storage_dir: Path, content_hash: str, variant: str) -> bytes | None:
    """Read the image blob, or None if it is absent (the serving side falls back to orig when a variant is missing, so a miss is not exceptional)."""
    try:
        return blob_path(storage_dir, content_hash, variant).read_bytes()
    except FileNotFoundError:
        return None


def blob_exists(storage_dir: Path, content_hash: str, variant: str) -> bool:
    """Whether the image blob exists."""
    return blob_path(storage_dir, content_hash, variant).is_file()
