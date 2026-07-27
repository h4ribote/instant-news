"""The Discord bot: sends article notification posts and DMs, and applies gateway events for roles, members, votes and comments.

The bot only sends to Discord and applies the events Discord delivers; it never decides whether an article is published. The server publishes, and at that moment enqueues a row with message_id = NULL in article_discord_posts and a row with sent_at = NULL in article_subscription_notices. The bot just picks that queue up and sends. Handing events straight from server to bot would lose the notifications for articles published while the bot was down; with the DB as the queue, the bot picks the unsent rows back up once it is back.

Gateway events arriving from Discord are a separate matter: they are the only notice of what happened on Discord's side, so the bot subscribes to them (see client.py). They can be missed just the same, so every component that keeps a mirror has a reconcile that assumes misses.

This package holds only the parts shared by the send loops and the event listeners.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


async def resolve_channel(client: discord.Client, channel_id: int) -> discord.abc.GuildChannel | discord.Thread | discord.abc.PrivateChannel | None:
    """Look the channel up in the cache, falling back to the API. None if it does not exist or is not accessible.

    Not raising when the channel is missing is deliberate: one misconfigured or deleted destination channel must not stop sends and recounts for the other destinations.

    Do not use this to read counts off the cached object (Thread.message_count and the like). discord.py does not update a cached thread's reply count, so it hands back the stale value.
    """
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(channel_id)
    except discord.NotFound:
        logger.warning('チャンネル %s が見つかりません。削除されたか、ID の設定が誤っている可能性があります。', channel_id)
    except discord.Forbidden:
        logger.warning('チャンネル %s へアクセスする権限が bot にありません。', channel_id)
    return None
