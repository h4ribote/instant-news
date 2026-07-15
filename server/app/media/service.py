"""画像の取り込みの組み立て。取得・変換・保存・登録を 1 つの流れにまとめる。

記事の本文中の画像は、公開時ではなく保存のたびに取り込む。Discord の CDN の URL は約 1 日で失効するため、公開まで待つと、下書きのまま置かれた記事は失効した後の取得になり必ず失敗する。書いた時点で手元に写しておく以外に、記事が後から画像を失わない方法は無い。

取り込みの失敗を例外にしない(ingest_article_images)。外部のホストが一時的に落ちていることと、記事を保存できないことの間に関係は無い。失敗した URL は本文にそのまま残し、何が取り込めなかったかを IngestFailure として返して執筆者へ伝える。黙って成功として扱うと、執筆者は元の URL が残ったことに気付かないまま、いずれその画像が消える。
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

# 配信 URL の前置き。本文中の URL が既にここを指しているなら、取り込み済みであるため取得しない。
MEDIA_URL_PREFIX: Final = '/media/'
# media.source_url の桁数。追跡のために元の URL を残すが、列に収まらない長さの URL で保存そのものを失敗させない。
_SOURCE_URL_MAX_LENGTH: Final = 1000


@dataclass(frozen=True)
class IngestResult:
    """取り込みの結果。url は配信 URL(/media/{hash}/orig.webp)。"""

    media_id: int
    content_hash: str
    url: str


@dataclass(frozen=True)
class IngestFailure:
    """取り込めなかった画像。source_url は本文にそのまま残っている URL。"""

    source_url: str
    reason: str


def media_url(content_hash: str, variant: str = 'orig') -> str:
    """配信 URL を組み立てる。"""
    return f'{MEDIA_URL_PREFIX}{content_hash}/{variant}.webp'


def _is_media_url(url: str) -> bool:
    """既に取り込み済みの画像を指す URL か。絶対 URL でも相対 URL でもパスだけを見れば判定できる。"""
    try:
        return urlsplit(url).path.startswith(MEDIA_URL_PREFIX)
    except ValueError:
        return False


def _write_blobs(cfg: MediaConfig, encoded: EncodedImage) -> None:
    """まだ存在しない変種だけを書き出す。

    内容アドレスであるため、既にあるファイルは必ず同じ内容である。存在を見てから書くことで、重複排除で既存の行を再利用する場合にも、実体が失われていれば書き直す(登録簿だけが残って画像が 404 になる状態から回復する)。
    """
    for variant, payload in encoded.variants.items():
        if blob_exists(cfg.storage_dir, encoded.content_hash, variant):
            continue
        write_blob(cfg.storage_dir, encoded.content_hash, variant, payload)


async def _check_quota(db: Database, cfg: MediaConfig, uploaded_by: int, additional: int) -> None:
    """利用量の上限を検査する。既存の実体を再利用する場合は呼ばない(同じバイト列を二度数えない)。"""
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
    """バイト列を変換して保存し、登録簿へ載せる。既に同じ内容があれば、その行を再利用する。

    変換は CPU を占有するため別スレッドへ回す。取り込みは要求の処理中に起こり、記事の保存を待たせる。
    """
    try:
        encoded = await asyncio.to_thread(encode_to_webp, data, max_pixels=cfg.max_pixels, lossless=lossless)
    except TranscodeError as exc:
        raise MediaError(str(exc)) from exc

    # content_hash は変換後の内容そのものであるため、同じハッシュの行は必ず同じ画像を指す。可逆・非可逆の違いも出力が変わる以上ハッシュが変わり、別の行になる。
    existing = await db.fetch_one('SELECT id FROM media WHERE content_hash = %s', encoded.content_hash)
    if existing is None:
        await _check_quota(db, cfg, uploaded_by, encoded.byte_size)

    # 登録簿へ載せる前に実体を書く。逆順にすると、書き込みに失敗した場合に「行はあるが画像が無い」状態が残り、配信が 404 になる。先に書けば、余るのは参照されないファイルだけで済む。
    await asyncio.to_thread(_write_blobs, cfg, encoded)

    if existing is None:
        # ON DUPLICATE KEY UPDATE … 同じ画像を同時に取り込んだ場合に一意キーで落ちないようにする。id = LAST_INSERT_ID(id) は、衝突したときに既存の行の id を lastrowid として返させるための定型。
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
        # 非公開として取り込まれていた画像が、後から公開の用途(カバー・広告・ロゴ)で取り込まれることがある。その場合は公開へ上げる。上げないと、同じ内容が既にあったというだけの理由で、カバーが未ログインの閲覧者に配信されず og:image が壊れる。公開を要求できるのは同じバイト列を現に持っている者だけであるため、これで新たに漏れるものは無い。逆向き(公開から非公開へ)は既存のカバーを壊すため行わない。
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
    """URL から画像を取得して取り込む。取得できない場合は MediaError。"""
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
    """本文中の画像を取り込み、URL を配信 URL へ差し替える。

    戻り値は (差し替え後の body_md, 取り込んだ media.id の一覧, 失敗の一覧)。**失敗しても例外を投げない。**

    取り込みは 1 枚ずつ順に行う。並行にすると総所要時間は縮むが、復号した画素が同時にメモリへ載る。1 枚あたりの上限が数千万画素である以上、数十枚を同時に復号すればメモリを使い果たす。順に行えば、同時に載るのは常に 1 枚分で済む。

    本文中の画像は is_public=False で取り込む。参照する記事の閲覧可否を配信時に検査するためであり、公開してよいかどうかは記事の閲覧範囲が決める。
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
            # 締切を過ぎたら、残りは取得を試みずに失敗として積む。1 枚あたりの締切は fetch_image が別に持つため、ここで打ち切るのは「次の 1 枚を始めるかどうか」だけでよい。取得の途中で中断すると、変換や登録の最中に割り込むことになり、中途半端な状態を作りうる。
            failures.append(IngestFailure(source_url=url, reason='記事全体の取得時間の上限を超えたため、この画像は元の URL のまま残ります。'))
            continue
        try:
            result = await ingest_url(db, cfg, url, uploaded_by=uploaded_by, via_token_id=via_token_id, is_public=False)
        except AppError as exc:
            failures.append(IngestFailure(source_url=url, reason=exc.message))
            continue
        except Exception:
            # 想定していない失敗で記事の保存そのものを落とさない。原因はログにだけ残し、応答には出さない(例外の文言にはテーブル名・パスなどが混ざる)。
            logger.exception('画像の取り込みで想定外の例外が発生しました: %s', url)
            failures.append(IngestFailure(source_url=url, reason='画像を取り込めませんでした。'))
            continue
        mapping[url] = result.url
        if result.media_id not in media_ids:
            media_ids.append(result.media_id)

    # 取り込めなかった URL は mapping に載らないため、そのまま本文に残る。
    return rewrite_image_urls(body_md, mapping), media_ids, failures
