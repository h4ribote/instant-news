"""予約公開のバックグラウンドループ。

**公開の判断は server が行い、bot は送信だけを行う。**公開の契機を bot 側の巡回に持たせると、bot が落ちている間は予約時刻を過ぎた記事が公開されないままになり、Web で読めるはずの記事が消える。server が公開して通知の待ち行列へ積むだけにしておけば、bot が落ちていても公開は進み、bot は復帰後に溜まった行を拾って送れる。

ループは例外で死なせない。1 本の記事の公開に失敗しただけでスケジューラが止まると、以後の全ての予約記事が公開されなくなり、しかもそのことは誰にも見えない。失敗はログへ出し、次の周回で再試行する。

設定は周回のたびに読み直す。起動時に 1 度だけ読むと、巡回間隔の変更を反映するのに server の再起動が要る。設定は短い時間だけ保持されるため、読み直しが毎回 DB への問い合わせになるわけではない。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from fastapi import FastAPI

from common.db import Database
from common.settings import RuntimeSettings, SettingsStore

from .articles import publish_article

logger = logging.getLogger(__name__)

# 1 周回で扱う記事の上限。予約が大量に溜まっていても 1 周回の所要時間を有限に保つ。捌けなかった分は次の周回が拾う。
BATCH_LIMIT: Final = 100

# 設定を読めなかったときにだけ使う、次の周回までの待ち時間(秒)。DB が落ちている・settings 表が壊れているといった状況で、間隔を決められないまま巡回を続けることになるため、待たずに回して DB を叩き続けないようにする。設定を読めている間はこの値を使わない(巡回間隔は settings が決める)。
RETRY_INTERVAL_SECONDS: Final = 15


async def _publish_due_articles(db: Database, settings: RuntimeSettings) -> int:
    """公開時刻を過ぎた予約記事を公開し、公開した本数を返す。"""
    due = await db.fetch_all(
        "SELECT id FROM articles WHERE status = 'scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= NOW() ORDER BY scheduled_at LIMIT %s",
        BATCH_LIMIT,
    )
    published = 0
    for row in due:
        article_id = int(row['id'])
        try:
            async with db.transaction() as conn:
                # 拾ってから公開するまでの間に状態が変わりうる(予約が取り消された、他のプロセスが公開した)ため、行を確保したうえで条件を検査し直す。SKIP LOCKED により、server を複数動かしても同じ記事を二重に公開しない。
                claimed = await db.fetch_val(
                    "SELECT id FROM articles WHERE id = %s AND status = 'scheduled' AND scheduled_at <= NOW() FOR UPDATE SKIP LOCKED",
                    article_id,
                    conn=conn,
                )
                if claimed is None:
                    continue
                # 公開と通知の待ち行列への追加を同じトランザクションに乗せる。公開だけが commit されて通知が失われる隙間を作らない。
                await publish_article(db, settings, article_id, conn=conn)
        except Exception:
            logger.exception('予約記事の公開に失敗しました。次の周回で再試行します。 article_id=%s', article_id)
            continue
        published += 1
        logger.info('予約記事を公開しました。 article_id=%s', article_id)
    return published


async def run_publish_scheduler(app: FastAPI) -> None:
    """予約公開の巡回を続ける。main.py の lifespan が起動し、停止時に cancel する。"""
    db: Database = app.state.db
    store: SettingsStore = app.state.settings
    logger.info('予約公開のスケジューラを開始しました。')
    # 直近でログに出した巡回間隔。周回ごとに出すと巡回そのものがログを埋めるため、変わったときにだけ出す。
    logged_interval: int | None = None
    while True:
        # 設定を読めなかった場合の待ち時間。読めた時点で settings の値へ差し替える。
        interval = RETRY_INTERVAL_SECONDS
        # asyncio.CancelledError は BaseException 派生であるため except Exception では捕まらず、停止要求はここを素通りしてループを抜ける。
        try:
            settings = await store.snapshot()
            # 公開の前に読む。公開が失敗しても、次の周回までの待ち時間は設定どおりになる。
            interval = settings.discord.bot.publish_poll_interval_seconds
            if interval != logged_interval:
                logger.info('予約公開の巡回間隔は %d 秒です。', interval)
                logged_interval = interval
            await _publish_due_articles(db, settings)
        except Exception:
            logger.exception('予約公開の巡回で例外が発生しました。次の周回へ進みます。')
        await asyncio.sleep(interval)
