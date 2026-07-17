"""Background loop for scheduled publishing.

**The server decides publication; the bot only sends.** If the bot's polling were the trigger for publication, articles past their scheduled time would stay unpublished for as long as the bot is down, and articles that should be readable on the web would vanish. Because the server publishes and only enqueues the notification, publishing proceeds even while the bot is down, and the bot picks up the backlog and sends it once it recovers.

Never let the loop die on an exception. If one article failing to publish stopped the scheduler, every later scheduled article would stay unpublished and nobody would see it happen. Failures are logged and retried on the next pass.

Settings are re-read every pass. Reading them once at startup would mean a server restart before a change to the poll interval takes effect. Settings are held briefly, so re-reading does not make every pass a DB query.
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

# Max articles handled in one pass, so that one pass takes a bounded time however large the backlog of scheduled articles grows. Whatever is left over is picked up by the next pass.
BATCH_LIMIT: Final = 100

# Seconds to wait before the next pass, used only when the settings could not be read. When the DB is down or the settings table is broken, polling continues with no interval to go on, so this keeps the loop from spinning without a wait and hammering the DB. Never used while the settings are readable: the poll interval comes from settings.
RETRY_INTERVAL_SECONDS: Final = 15


async def _publish_due_articles(db: Database, settings: RuntimeSettings) -> int:
    """Publish scheduled articles whose time has arrived and return how many were published."""
    due = await db.fetch_all(
        "SELECT id FROM articles WHERE status = 'scheduled' AND scheduled_at IS NOT NULL AND scheduled_at <= NOW() ORDER BY scheduled_at LIMIT %s",
        BATCH_LIMIT,
    )
    published = 0
    for row in due:
        article_id = int(row['id'])
        try:
            async with db.transaction() as conn:
                # The state can change between picking the row up and publishing it (the schedule was cancelled, another process published it), so claim the row and re-check the condition. SKIP LOCKED means that even with several servers running, no article is published twice.
                claimed = await db.fetch_val(
                    "SELECT id FROM articles WHERE id = %s AND status = 'scheduled' AND scheduled_at <= NOW() FOR UPDATE SKIP LOCKED",
                    article_id,
                    conn=conn,
                )
                if claimed is None:
                    continue
                # Publishing and enqueuing the notification ride on the same transaction, leaving no window in which the publish alone commits and the notification is lost.
                await publish_article(db, settings, article_id, conn=conn)
        except Exception:
            logger.exception('予約記事の公開に失敗しました。次の周回で再試行します。 article_id=%s', article_id)
            continue
        published += 1
        logger.info('予約記事を公開しました。 article_id=%s', article_id)
    return published


async def run_publish_scheduler(app: FastAPI) -> None:
    """Keep running the scheduled-publishing poll loop. Started by main.py's lifespan, which cancels it on shutdown."""
    db: Database = app.state.db
    store: SettingsStore = app.state.settings
    logger.info('予約公開のスケジューラを開始しました。')
    # The poll interval as last logged. Logging it every pass would fill the log with the polling itself, so log it only when it changes.
    logged_interval: int | None = None
    while True:
        # The wait to use if the settings cannot be read, replaced with the value from settings as soon as they are.
        interval = RETRY_INTERVAL_SECONDS
        # asyncio.CancelledError derives from BaseException, so `except Exception` does not catch it: a stop request passes straight through here and leaves the loop.
        try:
            settings = await store.snapshot()
            # Read before publishing, so that the wait until the next pass follows the setting even when publishing fails.
            interval = settings.discord.bot.publish_poll_interval_seconds
            if interval != logged_interval:
                logger.info('予約公開の巡回間隔は %d 秒です。', interval)
                logged_interval = interval
            await _publish_due_articles(db, settings)
        except Exception:
            logger.exception('予約公開の巡回で例外が発生しました。次の周回へ進みます。')
        await asyncio.sleep(interval)
