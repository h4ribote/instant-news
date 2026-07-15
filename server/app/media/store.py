"""画像の実体を内容アドレスで置くブロブストア。

保存先は {storage_dir}/{hash[0:2]}/{hash[2:4]}/{hash}.{variant}.webp とする。ハッシュの先頭 4 桁でディレクトリを二段に分けるのは、1 つのディレクトリに数十万のファイルが並ぶと ext4 でも列挙が重くなるためである。

内容アドレスであるため、同じ内容のファイルは必ず同じパスに落ちる。書き込みは常に同じ内容の上書きにしかならないが、途中まで書かれたファイルを配信が読むことは起こりうるため、一時ファイルへ書いてから置換する。

content_hash と variant は配信の URL からそのまま渡ってくる値であるため、パスを組み立てる前に必ず検査する。検査を省くと ../ を含む値でストレージの外を指せる。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final
from uuid import uuid4

# 生成・配信する変種。'orig' は原寸で必須。数字は横幅(ピクセル)を表し、原寸より広い変種は生成しない。
VARIANTS: Final = ('orig', '1200w', '400w')

# content_hash は SHA-256 の 16 進表記(小文字)。大文字を許すと、DB の照合(CHAR(64) の ai_ci は大小を区別しない)は通るのにファイル名が一致せず、存在する画像が 404 になる。
_CONTENT_HASH_PATTERN: Final = re.compile(r'\A[0-9a-f]{64}\Z')


def blob_path(storage_dir: Path, content_hash: str, variant: str) -> Path:
    """画像の実体のパスを返す。content_hash が 16 進 64 桁(小文字)でない場合、または未知の変種の場合は ValueError。"""
    if not _CONTENT_HASH_PATTERN.match(content_hash):
        raise ValueError(f'content_hash は SHA-256 の 16 進 64 桁(小文字)である必要があります: {content_hash!r}')
    if variant not in VARIANTS:
        raise ValueError(f'未知の変種です: {variant!r}。指定できるのは {", ".join(VARIANTS)} です。')
    return storage_dir / content_hash[0:2] / content_hash[2:4] / f'{content_hash}.{variant}.webp'


def write_blob(storage_dir: Path, content_hash: str, variant: str, data: bytes) -> None:
    """画像の実体を書き出す。既に同じパスにファイルがあれば置き換える(内容アドレスであるため中身は同一)。

    一時ファイルへ書いてから os.replace で置換する。同一ディレクトリ内の置換は原子的であり、配信側が中途半端なファイルを読むことがない。fsync まで行うのは、DB の行だけが残ってファイルが壊れている状態を電源断で作らないため。
    """
    path = blob_path(storage_dir, content_hash, variant)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同じ内容を同時に書く取り込みが競合しても互いの一時ファイルを壊さないよう、プロセスと呼び出しごとに一意な名前にする。
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
    """画像の実体を読む。無ければ None(配信側は変種が無ければ orig へ落とすため、欠落は例外ではない)。"""
    try:
        return blob_path(storage_dir, content_hash, variant).read_bytes()
    except FileNotFoundError:
        return None


def blob_exists(storage_dir: Path, content_hash: str, variant: str) -> bool:
    """画像の実体があるか。"""
    return blob_path(storage_dir, content_hash, variant).is_file()
