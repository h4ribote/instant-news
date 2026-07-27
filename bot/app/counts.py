"""Mirror of the votes on notification posts and comments.

Counts :arrow_up: as an up-vote and :arrow_down: as a down-vote and mirrors those counts into article_discord_posts and article_comments. The decision of which emoji to count lives in votes. The publisher ranking and the article page's score are built from these values.

Driven by gateway events: a reaction being added or removed arrives as an event, so we wait for it and recount.

But the delta an event carries is never applied as delta arithmetic. The event contributes only a mark that this post's count changed (mark_message); the value itself comes from refetching the message and overwriting with the current count. Four reasons, none of which delta arithmetic can close:

  - No event carries a total. MESSAGE_REACTION_REMOVE_EMOJI (every reaction of one emoji removed at once) does not say how many were removed, and neither does the bulk message delete event. With no way to learn what to subtract, delta arithmetic cannot get back to the right value.
  - Switching a vote from up to down delivers two events, an add and a remove, and their arrival order is not guaranteed. Delta arithmetic then leaves up and down each off by one, with nothing to bring them back.
  - Delta arithmetic drifts silently on missed events and on double delivery, and that drift accumulates. An overwrite that drifts is back at the right value on the next update.
  - up_count and down_count are counts and are held unsigned. Subtracting 1 from 0 makes MySQL fail with an error (the statement fails; the value is not clamped). A missed event routinely leaves the DB count below the real one, so delta arithmetic would have to carry the lower-bound handling itself, always.

Limiting the writer to a single worker follows from the same reasoning. discord.py runs each listener as its own independent task, so overwrites of the same row can run concurrently. With listeners that only mark and one worker that drains the marks, a row has exactly one writer, and no other write interleaves between fetching the message and writing. The marks are a set, so hundreds of reactions in a short burst collapse into one query to Discord.

Re-reading a thread's history (repairing comments) is done by this worker too. The history carries both the comment bodies and the vote counts, so handing it to a separate task would put two writers on the same row.

Reconciliation is the guard against a missed event. Gateway events are at-most-once: nothing is delivered while the bot is down, discord.py swallows an exception raised by a listener and moves on, and Discord never redelivers. Misses must therefore be assumed. Reconciliation only re-marks the notification posts and threads in the window; the same worker still does the fetching and overwriting. That is, "a missed event still ends up at the right value" is satisfied by the pair of re-marking and the overwriting worker, not by periodically overwriting everything.

Reconciliation is limited to notification posts published within count_reconcile_window_days because it fetches every single one of them in that window. Going back without limit would mean firing queries proportional to the total number of articles every cycle, including articles whose reactions stopped long ago.
The event path needs no such cutoff, because it fetches only the posts that actually changed and a quiet article costs no queries at all (events still arrive for notification posts older than the window, and their counts are updated as usual). The difference between the two is not whether queries happen but whether their number scales with change or with the total number of articles.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Final

import discord

from common.db import Database
from common.settings import RuntimeSettings, SettingsStore

from . import resolve_channel, votes
from .comments import CommentMirror

logger = logging.getLogger(__name__)

# Seconds to wait between a mark arriving and the drain. Votes land on a new article in a burst, so refetching once per mark would pull the same message over and over. The wait delays how soon the counts are applied, but it is a few seconds and does not affect the score tally.
DEBOUNCE_SECONDS: Final = 5.0

# Seconds to wait for the next pass when the settings themselves could not be read. The real interval lives in the settings, so while they cannot be read there is no way to know it. A DB blip or seed values not yet bootstrapped are recoverable states, so re-read at a short fixed interval.
SETTINGS_RETRY_SECONDS: Final = 30

# Reconciliation targets: notification posts already sent and still inside the window.
_RECONCILE_TARGETS_SQL: Final = """
    SELECT message_id, thread_id
      FROM article_discord_posts
     WHERE message_id IS NOT NULL
       AND posted_at >= NOW() - INTERVAL %s DAY
"""

# Resolve notification post rows from the marked ids. message_id carries a unique index, so any message arriving in the guild can be resolved or dropped in this one statement.
_RESOLVE_POSTS_SQL: Final = """
    SELECT id, article_id, channel_id, message_id
      FROM article_discord_posts
     WHERE message_id IN ({placeholders})
"""

# Look the same ids up as comments too. One id never matches both (a notification post itself is not mirrored as a comment).
_RESOLVE_COMMENTS_SQL: Final = """
    SELECT c.id, c.message_id, p.thread_id AS channel_id
      FROM article_comments c
      JOIN article_discord_posts p ON p.id = c.post_id
     WHERE c.message_id IN ({placeholders})
"""

_UPDATE_POST_SQL: Final = 'UPDATE article_discord_posts SET up_count = %s, down_count = %s, last_synced_at = NOW() WHERE id = %s'

_UPDATE_COMMENT_SQL: Final = 'UPDATE article_comments SET up_count = %s, down_count = %s, last_synced_at = NOW() WHERE id = %s'


class CountMirror:
    """Mirror of the vote counts. Events only leave a mark; a single worker refetches and overwrites."""

    def __init__(self, client: discord.Client, db: Database, store: SettingsStore, comments: CommentMirror) -> None:
        self._client = client
        self._db = db
        self._store = store
        self._comments = comments
        # Messages to recount (both notification posts themselves and comments). An id we do not track simply matches no row when the rows are resolved, so the listeners do not filter by guild.
        self._dirty_messages: set[int] = set()
        # Threads whose history needs re-reading.
        self._dirty_threads: set[int] = set()
        self._marked = asyncio.Event()
        self._reconcile_requested = asyncio.Event()

    def mark_message(self, message_id: int) -> None:
        """Record that the votes on a post may have changed (a reaction added or removed, or all of them cleared)."""
        self._dirty_messages.add(message_id)
        self._marked.set()

    def mark_thread(self, thread_id: int) -> None:
        """Record that a thread's history needs re-reading (repairing missed comments)."""
        self._dirty_threads.add(thread_id)
        self._marked.set()

    def request_reconcile(self, reason: str) -> None:
        """Prompt a reconcile without waiting for the next scheduled one. Called from wherever a missed event is suspected."""
        logger.info('投票とコメントの照合を要求しました(理由: %s)。', reason)
        self._reconcile_requested.set()

    # Drain

    async def run_drain_loop(self) -> None:
        """Keep recounting marked posts. This loop is the only writer of vote counts in this bot.

        Exceptions are swallowed here and the loop moves on to the next pass. If a single failure (a transient Discord outage, a DB blip) killed this loop, no article's score would ever update again. Marks lost by a failed drain are gone, but reconciliation re-marks the notification posts in the window, so that pass's miss is corrected there.
        """
        await self._client.wait_until_ready()
        while True:
            await self._marked.wait()
            # Batch the marks that arrive in a burst. Marks arriving during this wait are handled in the same pass.
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._marked.clear()
            # Swap the sets before draining. Marks that arrive mid-fetch go into the new set and are recounted on the next pass (so votes cast after the fetch are not missed). An await placed here would throw away the marks arriving in that gap.
            messages, threads = self._dirty_messages, self._dirty_threads
            self._dirty_messages, self._dirty_threads = set(), set()
            try:
                await self._drain(messages, threads)
            except Exception:
                logger.exception('投票の掃き出しで例外が発生しました。次に印が付いたときに再試行します。')

    async def _drain(self, messages: set[int], threads: set[int]) -> None:
        if messages:
            await self._drain_messages(messages)
        for thread_id in threads:
            try:
                await self._backfill_thread(thread_id)
            except Exception:
                logger.exception('スレッド %s の履歴の読み直しに失敗しました。次の照合で再試行します。', thread_id)

    async def _drain_messages(self, messages: set[int]) -> None:
        params = list(messages)
        placeholders = ', '.join(['%s'] * len(params))
        posts = await self._db.fetch_all(_RESOLVE_POSTS_SQL.format(placeholders=placeholders), *params)
        comments = await self._db.fetch_all(_RESOLVE_COMMENTS_SQL.format(placeholders=placeholders), *params)
        for row in posts:
            await self._sync_one(row, _UPDATE_POST_SQL)
        for row in comments:
            await self._sync_one(row, _UPDATE_COMMENT_SQL)

    async def _sync_one(self, row: Mapping[str, Any], update_sql: str) -> None:
        """Recount one post's votes and overwrite the row.

        A post that cannot be fetched keeps the last counted value. The reactions an article gathered do not become nothing just because the notification post was deleted, and overwriting with 0 would erase the publisher's past track record from the publisher ranking.
        """
        try:
            message = await self._fetch_message(row)
        except Exception:
            logger.exception('メッセージ %s を取得できなかったため、票の数え直しを飛ばします。', row['message_id'])
            return
        if message is None:
            return
        up, down = votes.tally(message)
        await self._db.execute(update_sql, up, down, row['id'])

    async def _fetch_message(self, row: Mapping[str, Any]) -> discord.Message | None:
        channel = await resolve_channel(self._client, int(row['channel_id']))
        if channel is None:
            return None
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning('チャンネル %s からメッセージを取得できません(種類: %s)。', row['channel_id'], type(channel).__name__)
            return None
        try:
            return await channel.fetch_message(int(row['message_id']))
        except discord.NotFound:
            # The post was deleted. A comment's row is removed entirely by the delete event, so reaching here means the notification post itself was deleted. Keep the last counted value.
            logger.info('メッセージ %s が見つからないため、最後に数えた値を残します(削除された可能性があります)。', row['message_id'])
            return None
        except discord.Forbidden:
            logger.warning('チャンネル %s のメッセージを読む権限が bot にありません。', row['channel_id'])
            return None

    async def _backfill_thread(self, thread_id: int) -> None:
        thread = await resolve_channel(self._client, thread_id)
        if thread is None:
            return
        if not isinstance(thread, discord.Thread):
            logger.warning('スレッド %s として記録されたチャンネルがスレッドではありません(種類: %s)。', thread_id, type(thread).__name__)
            return
        await self._comments.backfill(thread)

    # Reconciliation

    async def run_reconcile_loop(self) -> None:
        """Re-mark the notification posts in the window when a reconcile is requested, or when count_reconcile_interval_seconds has elapsed.

        Exceptions are swallowed here and the loop moves on to the next pass. If this loop died merely because the settings could not be read, missed events would never be corrected again.
        """
        await self._client.wait_until_ready()
        while True:
            try:
                settings = await self._store.snapshot()
            except Exception:
                logger.exception('設定を読み取れないため、投票の照合を %d 秒後に再試行します。', SETTINGS_RETRY_SECONDS)
                await asyncio.sleep(SETTINGS_RETRY_SECONDS)
                continue
            interval = settings.discord.bot.count_reconcile_interval_seconds
            # Wait for a request before reconciling. The first run after startup comes from on_ready's request.
            try:
                await asyncio.wait_for(self._reconcile_requested.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._reconcile_requested.clear()
            try:
                await self.reconcile(settings)
            except Exception:
                logger.exception('投票の照合で例外が発生しました。%d 秒後に再試行します。', interval)

    async def reconcile(self, settings: RuntimeSettings) -> None:
        """Mark every notification post and thread in the window. The drain worker does the fetching and overwriting.

        Marking is all this does, to keep the row's writer single. If reconciliation overwrote rows itself it would write the same rows concurrently with the drain worker, and a stale refetched value could wipe out a newer value that came from an event. Marking any number of times gives the same result.
        """
        rows = await self._db.fetch_all(_RECONCILE_TARGETS_SQL, settings.discord.bot.count_reconcile_window_days)
        for row in rows:
            self.mark_message(int(row['message_id']))
            if row['thread_id'] is not None:
                self.mark_thread(int(row['thread_id']))
        logger.info('公開から %d 日以内の通知 %d 件に照合の印を付けました。', settings.discord.bot.count_reconcile_window_days, len(rows))
