"""Notification post and DM delivery loop.

When server publishes an article it enqueues one article_discord_posts row with message_id = NULL per destination channel, and one article_subscription_notices row with sent_at = NULL per subscriber of the article's tags. This loop drains those two queues, sends, and marks what it managed to send. A row that could not be sent stays unmarked and is picked up again on the next pass.

Nothing here decides whether an article may be published. The enqueued row *is* the fact that server committed the publication, and the bot only sends it. Letting the bot inspect state and call off a send would split the authority over publishing across two places.

The send interval, the day window for reopening threads and the breaking-news mention target are re-read from the settings table on every pass, because an admin can change each of them from the admin screen and no change may require a bot restart. Only base_url — used for the article URL and the absolute cover image URL — comes from Config, since it is bound to the runtime environment.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Final

import discord

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings, SettingsStore

from . import resolve_channel, votes
from .embeds import ArticleView, OutgoingMessage, build_channel_message, build_subscription_dm, thread_name

logger = logging.getLogger(__name__)

# Max rows handled in one pass. The cut keeps a single pass short so a large backlog cannot make the other queue, or the loop as a whole, wait; rows left over are picked up where they stopped on the next pass.
POST_BATCH_LIMIT: Final = 50
DM_BATCH_LIMIT: Final = 100

# Minutes until a thread auto-archives. 7 days is the longest Discord allows, and since the thread is what catches comments it should stay open as long as possible (an archived thread can still be read, but takes no new comments).
THREAD_AUTO_ARCHIVE_MINUTES: Final = 10080

# Seconds to wait for the next pass when the settings themselves could not be read. The real interval lives in the settings, so while they cannot be read there is no way to know it. A DB blip or seed values not yet bootstrapped are recoverable states, so re-read at a short fixed interval.
SETTINGS_RETRY_SECONDS: Final = 30

_ARTICLE_SQL: Final = """
    SELECT a.id, a.slug, a.title, a.excerpt, a.cover_image_url, a.is_breaking, a.is_sponsored,
           a.sponsor_name, a.byline_mode, a.published_at,
           c.name AS category_name,
           p.name AS publisher_name, p.logo_url AS publisher_logo_url, p.stance AS publisher_stance,
           u.discord_user_id AS author_discord_user_id, u.username AS author_username,
           u.global_name AS author_global_name, u.avatar_hash AS author_avatar_hash
      FROM articles a
      JOIN categories c ON c.id = a.category_id
      JOIN users u      ON u.id = a.author_id
      LEFT JOIN publishers p ON p.id = a.publisher_id
     WHERE a.id = %s
"""

_ARTICLE_TAGS_SQL: Final = """
    SELECT t.name
      FROM article_tags atg
      JOIN tags t ON t.id = atg.tag_id
     WHERE atg.article_id = %s
     ORDER BY t.name
"""

_PENDING_POSTS_SQL: Final = """
    SELECT id, article_id, channel_id
      FROM article_discord_posts
     WHERE message_id IS NULL
     ORDER BY enqueued_at, id
     LIMIT %s
"""

_MARK_POSTED_SQL: Final = """
    UPDATE article_discord_posts
       SET message_id = %s, posted_at = NOW()
     WHERE id = %s AND message_id IS NULL
"""

_SET_THREAD_SQL: Final = 'UPDATE article_discord_posts SET thread_id = %s WHERE id = %s'

_MARK_SEEDED_SQL: Final = 'UPDATE article_discord_posts SET reactions_seeded_at = NOW() WHERE id = %s'

# Rows that were sent but where the vote reactions failed to go on. Left that way, readers have to reach for the emoji picker to vote on that one article. As with retrying thread creation, a sent row never shows up in the unsent queue, so unless it is picked up here the reactions are never added.
_PENDING_REACTIONS_SQL: Final = """
    SELECT id, channel_id, message_id
      FROM article_discord_posts
     WHERE message_id IS NOT NULL
       AND reactions_seeded_at IS NULL
       AND posted_at >= NOW() - INTERVAL %s DAY
     ORDER BY posted_at, id
     LIMIT %s
"""

# Rows that were sent but where only thread creation failed. A row with message_id filled in never shows up in the unsent queue, so unless it is picked up here the comment thread never opens. Rows past the window over which votes and comments are reconciled are excluded: a cutoff so the bot does not keep trying to open threads forever, and by then nobody is reading the article either.
_PENDING_THREADS_SQL: Final = """
    SELECT id, article_id, channel_id, message_id
      FROM article_discord_posts
     WHERE message_id IS NOT NULL
       AND thread_id IS NULL
       AND posted_at >= NOW() - INTERVAL %s DAY
     ORDER BY posted_at, id
     LIMIT %s
"""

# Unsent subscription DMs. The body states which tag subscription delivered it, so the intersection of the article's tags and the subscriber's subscribed tags is fetched alongside.
_PENDING_NOTICES_SQL: Final = """
    SELECT n.article_id, n.user_id, u.discord_user_id,
           MIN(n.enqueued_at) AS first_enqueued_at,
           GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ', ') AS matched_tags
      FROM article_subscription_notices n
      JOIN users u ON u.id = n.user_id
      LEFT JOIN article_tags atg      ON atg.article_id = n.article_id
      LEFT JOIN tag_subscriptions ts  ON ts.user_id = n.user_id AND ts.tag_id = atg.tag_id
      LEFT JOIN tags t                ON t.id = ts.tag_id
     WHERE n.sent_at IS NULL
     GROUP BY n.article_id, n.user_id, u.discord_user_id
     ORDER BY first_enqueued_at, n.article_id, n.user_id
     LIMIT %s
"""

_MARK_NOTICE_SENT_SQL: Final = """
    UPDATE article_subscription_notices
       SET sent_at = NOW()
     WHERE article_id = %s AND user_id = %s AND sent_at IS NULL
"""


async def run_delivery_loop(client: discord.Client, db: Database, config: Config, store: SettingsStore) -> None:
    """Drain the two queues every publish_poll_interval_seconds.

    Settings are re-read on every pass. Exceptions are swallowed here and the loop moves on to the next pass: if it died merely because the settings could not be read, no notification post and no DM would ever be sent again for any article (server keeps publishing, so the queues would only grow).
    """
    await client.wait_until_ready()
    while True:
        try:
            settings = await store.snapshot()
        except Exception:
            logger.exception('設定を読み取れないため、通知の送信を %d 秒後に再試行します。', SETTINGS_RETRY_SECONDS)
            await asyncio.sleep(SETTINGS_RETRY_SECONDS)
            continue
        interval = settings.discord.bot.publish_poll_interval_seconds
        try:
            await deliver_pending(client, db, config, settings)
        except Exception:
            logger.exception('通知の送信で例外が発生しました。%d 秒後に再試行します。', interval)
        await asyncio.sleep(interval)


async def deliver_pending(client: discord.Client, db: Database, config: Config, settings: RuntimeSettings) -> None:
    """Handle one pass worth of unsent notification posts, threads and subscription DMs."""
    # One article is notified to several channels, so within a pass an article is never fetched twice.
    cache: dict[int, ArticleView | None] = {}
    await _post_pending_notifications(client, db, config, settings, cache)
    await _open_pending_threads(client, db, settings, cache)
    await _seed_pending_votes(client, db, settings)
    await _send_pending_subscription_dms(client, db, config, cache)


async def _load_article(db: Database, article_id: int) -> ArticleView | None:
    row = await db.fetch_one(_ARTICLE_SQL, article_id)
    if row is None:
        return None
    tags = await db.fetch_all(_ARTICLE_TAGS_SQL, article_id)
    return ArticleView.from_row(row, [str(tag['name']) for tag in tags])


async def _article(db: Database, cache: dict[int, ArticleView | None], article_id: int) -> ArticleView | None:
    if article_id not in cache:
        cache[article_id] = await _load_article(db, article_id)
    return cache[article_id]


async def _text_channel(client: discord.Client, channel_id: int) -> discord.TextChannel | None:
    """Look up a text channel usable as a notification destination.

    A comment thread is opened on every notification post, so the channel must not only accept messages but be of a kind that can create a thread from one. Announcement channels qualify too.
    """
    channel = await resolve_channel(client, channel_id)
    if channel is None:
        return None
    if not isinstance(channel, discord.TextChannel):
        logger.warning('チャンネル %s はテキストチャンネルではないため通知できません(種類: %s)。通知先にはメッセージとスレッドを作れるテキストチャンネルを設定してください。', channel_id, type(channel).__name__)
        return None
    return channel


async def _post_pending_notifications(client: discord.Client, db: Database, config: Config, settings: RuntimeSettings, cache: dict[int, ArticleView | None]) -> None:
    rows = await db.fetch_all(_PENDING_POSTS_SQL, POST_BATCH_LIMIT)
    for row in rows:
        try:
            await _post_one(client, db, config, settings, cache, row)
        except Exception:
            logger.exception('記事 %s のチャンネル %s への通知に失敗しました。次の周回で再試行します。', row['article_id'], row['channel_id'])


async def _post_one(client: discord.Client, db: Database, config: Config, settings: RuntimeSettings, cache: dict[int, ArticleView | None], row: Mapping[str, Any]) -> None:
    article = await _article(db, cache, int(row['article_id']))
    if article is None:
        logger.warning('記事 %s が見つからないため通知を保留します。', row['article_id'])
        return
    channel = await _text_channel(client, int(row['channel_id']))
    if channel is None:
        return

    message = await channel.send(**_send_kwargs(build_channel_message(config, settings, article)))
    # Commit message_id immediately after sending. Writing it only after the thread is created and the reactions are added would, if the bot died in between, send the same article again on the next pass. A double post is irreversible for readers, whereas a thread that never opened and reactions that never went on both recover on the next pass. The ordering is the point: an exception below this line still leaves the sent record behind.
    updated = await db.execute(_MARK_POSTED_SQL, message.id, row['id'])
    if updated == 0:
        logger.error('記事 %s のチャンネル %s への通知を送信しましたが、既に送信済みとして記録されていました。メッセージ %s が重複している可能性があります。', row['article_id'], row['channel_id'], message.id)
        return
    logger.info('記事 %s をチャンネル %s へ通知しました(メッセージ %s)。', row['article_id'], row['channel_id'], message.id)
    await _seed_votes(db, message, int(row['id']))
    await _open_thread(db, message, article, int(row['id']))


def _send_kwargs(message: OutgoingMessage) -> dict[str, Any]:
    """Map an OutgoingMessage onto discord.py's send arguments. allowed_mentions is always passed explicitly."""
    return {'content': message.content, 'embed': message.embed, 'allowed_mentions': message.allowed_mentions}


async def _seed_votes(db: Database, message: discord.Message, post_id: int) -> None:
    """Add the vote reactions to a notification post and record that they went on.

    If they cannot be added, return without recording; the row stays in the queue and is picked up again on the next pass. Readers can add the reactions themselves, so a failure here still leaves the notification post itself valid.
    """
    if await votes.seed(message):
        await db.execute(_MARK_SEEDED_SQL, post_id)


async def _open_thread(db: Database, message: discord.Message, article: ArticleView, post_id: int) -> None:
    """Open the comment thread on a notification post and record thread_id.

    Comments are taken in the Discord thread and the DB holds only a mirror of them, so unless the thread is opened here the article has nowhere to be commented on. A message that already carries a thread is not given a second one; its existing thread is recorded instead, which avoids opening a duplicate when an earlier pass created the thread but died before recording it.
    """
    thread = message.thread
    if thread is None:
        thread = await message.create_thread(name=thread_name(article), auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES)
    await db.execute(_SET_THREAD_SQL, thread.id, post_id)


async def _open_pending_threads(client: discord.Client, db: Database, settings: RuntimeSettings, cache: dict[int, ArticleView | None]) -> None:
    rows = await db.fetch_all(_PENDING_THREADS_SQL, settings.discord.bot.count_reconcile_window_days, POST_BATCH_LIMIT)
    for row in rows:
        try:
            await _open_thread_for_posted(client, db, cache, row)
        except Exception:
            logger.exception('記事 %s の通知メッセージ %s のスレッド作成に失敗しました。次の周回で再試行します。', row['article_id'], row['message_id'])


async def _open_thread_for_posted(client: discord.Client, db: Database, cache: dict[int, ArticleView | None], row: Mapping[str, Any]) -> None:
    article = await _article(db, cache, int(row['article_id']))
    if article is None:
        return
    channel = await _text_channel(client, int(row['channel_id']))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(row['message_id']))
    except discord.NotFound:
        logger.info('通知メッセージ %s は存在しないため、スレッドを開けません(削除された可能性があります)。', row['message_id'])
        return
    await _open_thread(db, message, article, int(row['id']))


async def _seed_pending_votes(client: discord.Client, db: Database, settings: RuntimeSettings) -> None:
    """Pick up notification posts whose vote reactions failed to go on."""
    rows = await db.fetch_all(_PENDING_REACTIONS_SQL, settings.discord.bot.count_reconcile_window_days, POST_BATCH_LIMIT)
    for row in rows:
        try:
            channel = await _text_channel(client, int(row['channel_id']))
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(int(row['message_id']))
            except discord.NotFound:
                # The notification post itself was deleted. Nothing is left to react to, so drop the row from the queue; picking it up again on the next pass would only reach the same result.
                await db.execute(_MARK_SEEDED_SQL, row['id'])
                continue
            await _seed_votes(db, message, int(row['id']))
        except Exception:
            logger.exception('通知メッセージ %s への投票用の絵文字の付与に失敗しました。次の周回で再試行します。', row['message_id'])


async def _send_pending_subscription_dms(client: discord.Client, db: Database, config: Config, cache: dict[int, ArticleView | None]) -> None:
    rows = await db.fetch_all(_PENDING_NOTICES_SQL, DM_BATCH_LIMIT)
    for row in rows:
        try:
            await _send_one_dm(client, db, config, cache, row)
        except Exception:
            logger.exception('記事 %s の購読 DM(ユーザー %s)の送信に失敗しました。次の周回で再試行します。', row['article_id'], row['discord_user_id'])


async def _send_one_dm(client: discord.Client, db: Database, config: Config, cache: dict[int, ArticleView | None], row: Mapping[str, Any]) -> None:
    article = await _article(db, cache, int(row['article_id']))
    if article is None:
        logger.warning('記事 %s が見つからないため購読 DM を保留します。', row['article_id'])
        return
    discord_user_id = int(row['discord_user_id'])

    user = client.get_user(discord_user_id)
    if user is None:
        try:
            user = await client.fetch_user(discord_user_id)
        except discord.NotFound:
            # A deleted account can never be fetched again, so a retry would never succeed; fold the row away as sent.
            logger.warning('Discord ユーザー %s が存在しないため、記事 %s の購読 DM を送信済みとして扱います。', discord_user_id, row['article_id'])
            await db.execute(_MARK_NOTICE_SENT_SQL, row['article_id'], row['user_id'])
            return

    try:
        await user.send(**_send_kwargs(build_subscription_dm(config, article, row['matched_tags'])))
    except discord.Forbidden:
        # DMs are turned off, the bot is blocked, or there is no Discord server in common. That is the recipient's own setting, so waiting will not clear it.
        # Not marking it sent here would retry the same recipient forever and clog the queue.
        logger.info('Discord ユーザー %s は DM を受け付けないため、記事 %s の購読 DM を送信済みとして扱います。', discord_user_id, row['article_id'])
    else:
        logger.info('記事 %s の購読 DM を Discord ユーザー %s へ送信しました。', row['article_id'], discord_user_id)
    await db.execute(_MARK_NOTICE_SENT_SQL, row['article_id'], row['user_id'])
