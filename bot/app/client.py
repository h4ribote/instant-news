"""Discord client, and where every gateway event lands.

Everything this bot receives from Discord is collected here. A listener decides nothing on its own: it only hands the event to the two parts that keep the mirror (GuildMirror / CountMirror). Unless the set of subscribed events is readable in one place, a missing intent gives "no exception, yet nothing arrives" and there is no way to find it.

Tasks started here:
  delivery          - posts notifications for published articles and DMs tag subscribers (polls the DB queue periodically).
  roles.reconcile   - reconciles roles, members and the owner.
  counts.drain      - recounts votes on marked posts and repairs comments from the thread history.
  counts.reconcile  - reconciles votes and comments.

Only delivery polls, because Discord has no event that would drive it: the server published the article and Discord does not know about it. Everything else happens on Discord's side and arrives as an event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import discord

from common.config import Config
from common.db import Database
from common.settings import SettingsStore

from .comments import CommentMirror
from .counts import CountMirror
from .delivery import run_delivery_loop
from .roles import GuildMirror
from . import votes

logger = logging.getLogger(__name__)


def _naive(value: datetime | None) -> datetime | None:
    """Drop a Discord timestamp to UTC-naive to match the DATETIME columns (same convention as server/app/schemas.py)."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class NewsBot(discord.Client):
    """Discord client holding the delivery loop and the gateway event listeners.

    The DB and the settings are built by the caller and passed in. The bot token lives in the settings table, so reading it needs the DB before anything connects to Discord; connecting and tearing down the DB therefore belongs to the caller, not to this class.
    """

    def __init__(self, config: Config, db: Database, settings: SettingsStore) -> None:
        # Enable only the intents that are needed. An event whose intent is off simply never arrives - no exception, no warning.
        #   guilds          - resolving roles and channels, role create/update/delete, guild update (owner transfer).
        #   members         - member join/leave/update, and member chunking. A privileged intent: it must be enabled in Discord's developer portal.
        #   guild_messages  - thread reply create/edit/delete.
        #   guild_reactions - reaction add/remove/clear. Votes arrive through this.
        #   message_content - the body of a comment. The second privileged intent, also enabled in the developer portal.
        # The DM intents (dm_messages / dm_reactions) stay off: notifications and threads all live inside the guild, and subscription DMs are only sent - no reactions on them are counted.
        #
        # A privileged intent that is requested but left off in the developer portal makes Discord cut the gateway, so forgetting the portal switch always surfaces at startup as PrivilegedIntentsRequired (main.py prints the instructions). Not requesting one fails silently instead - the body just arrives as an empty string - so if the body is needed, always err on the side of requesting it.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
        # No message cache. This bot takes reactions and deletions as raw events and re-fetches any message it needs, so it never reads a cached one; the default would hold the last 1000 messages, paying memory for something unused.
        # Non-raw events (on_reaction_add and friends) fire only for messages that are in the cache. The notification posts this bot follows can be up to count_reconcile_window_days days old and are never cached, so those events could not be relied on anyway - this setting makes it explicit that they are not used.
        super().__init__(intents=intents, max_messages=None)
        self.config = config
        self.db = db
        self.settings = settings
        self.roles = GuildMirror(self, db, settings)
        self.comments = CommentMirror(self, db)
        self.counts = CountMirror(self, db, settings, self.comments)
        self._tasks: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        """Called once the connection is up, before the gateway starts delivering events. The long-running tasks are started here.

        Reconcile is one of these four tasks rather than something a listener runs, because starting a reconcile inside a listener would let reconciles pile up when the gateway re-establishes its session repeatedly in a short window. A listener only files a request; this task is always what runs it.

        Each task re-reads the settings on every pass, so what is passed here is the SettingsStore, not values.
        """
        self._tasks = [
            asyncio.create_task(run_delivery_loop(self, self.db, self.config, self.settings), name='delivery'),
            asyncio.create_task(self.roles.run_reconcile_loop(), name='role_reconcile'),
            asyncio.create_task(self.counts.run_drain_loop(), name='count_drain'),
            asyncio.create_task(self.counts.run_reconcile_loop(), name='count_reconcile'),
        ]

    async def close(self) -> None:
        """Stop the long-running tasks and close the Discord connection. The caller opened the DB, so the caller closes it too."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            # Wait for the cancellations to land. Closing the connection without waiting leaves a task that was mid-delivery or mid-reconcile holding a closed connection and raising pointless exceptions.
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await super().close()

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        """Cleanup for an exception raised by a listener.

        discord.py swallows it and moves on to the next event, and Discord never redelivers that event: left alone, the one change that failed would never reach the mirror. Requesting a reconcile here repairs the swallowed event immediately instead of at the next reconcile interval. While the DB is down the reconcile fails too, but the requests coalesce into one, so the first reconcile after recovery repairs it.
        """
        logger.exception('イベント %s の処理で例外が発生しました。取りこぼした変更を補正するため照合を要求します。', event_method)
        self.roles.request_reconcile(f'{event_method} の処理に失敗したため')
        self.counts.request_reconcile(f'{event_method} の処理に失敗したため')

    # Connection

    async def on_ready(self) -> None:
        """Called when a new gateway session has been established.

        Not called on every reconnect. If the link merely dropped for a moment, discord.py resumes the same session (RESUME) and Discord redelivers the events from the gap, so on_resumed fires instead and nothing needs repairing. When the session was re-established (a re-IDENTIFY), the events from the gap are gone, so a reconcile is requested here; that also repairs changes made while the bot was down.

        This can fire many times in one process, every time Discord invalidates the session. Reconcile requests coalesce into one and a single task always runs it, so duplicates do not matter here.
        """
        logger.info('Discord へ接続しました: %s。', self.user)
        self.roles.request_reconcile('ゲートウェイの新しいセッションを確立したため')
        self.counts.request_reconcile('ゲートウェイの新しいセッションを確立したため')

    async def on_guild_available(self, guild: discord.Guild) -> None:
        """Called when a guild that had gone invisible - during a Discord-side outage, say - comes back.

        Events from that window never arrived, but the session is not re-established, so on_ready does not fire. Without a request here, the changes from that window would sit unmirrored until the next reconcile interval, or until a restart.
        """
        self.roles.request_reconcile(f'guild {guild.id} が利用可能に戻ったため')
        self.counts.request_reconcile(f'guild {guild.id} が利用可能に戻ったため')

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Called when the bot is invited to a guild. If it is the target guild, every role and member is mirrored as of that moment."""
        self.roles.request_reconcile(f'guild {guild.id} へ参加したため')

    # Roles, members, owner

    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.roles.apply_role_upsert(role)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        # Permission changes arrive here too. Even with the same people holding the role, the moment it gains or loses Administrator every holder's admin eligibility changes.
        await self.roles.apply_role_upsert(after)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.roles.apply_role_delete(role)

    async def on_member_join(self, member: discord.Member) -> None:
        await self.roles.apply_member_upsert(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # Role grants and revokes arrive here. The event carries the full post-change role set, so mirror `after` as it is rather than working out a delta.
        await self.roles.apply_member_upsert(after)

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        # The non-raw on_member_remove fires only for a member that is in the cache. Leaving the guild is a revocation of permission in itself and must not depend on cache state, so take the raw event.
        await self.roles.apply_member_remove(payload.user, payload.guild_id)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        # Display name and avatar changes. Mirrored because the article byline uses them.
        await self.roles.apply_user_update(after)

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        # Only the owner transfer is watched; the guild's name and icon are not mirrored.
        if before.owner_id != after.owner_id:
            await self.roles.apply_owner_change(after)

    # Votes and comments.
    # Every listener below only marks; none of them reads or writes a count (see the counts docstring for why).
    # IDs from a guild or thread we do not track are not sorted out here: they simply match no row when the row is looked up.

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        self.counts.mark_message(payload.message_id)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        self.counts.mark_message(payload.message_id)

    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        # Every reaction on the message was removed.
        self.counts.mark_message(payload.message_id)

    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        # Every reaction of one emoji was removed. The event does not carry how many went, so there is nothing to do but recount.
        self.counts.mark_message(payload.message_id)

    async def on_message(self, message: discord.Message) -> None:
        # Only a post inside a thread is a comment. The notification post itself (the message in the parent channel) is the thread's starter message and never appears inside the thread.
        if not await self.comments.apply_upsert(message):
            return
        # Mirroring it means it is a post in an article's comment thread. Seed the vote reactions here so a reader can vote with one press.
        if await votes.seed(message):
            await self.comments.mark_seeded(message.id)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # A comment was edited. payload.message is a Message assembled by discord.py, and its content is always a str: a partial update that carries no body leaves the attribute as an empty string rather than dropping it. Editing a post we never mirrored just updates 0 rows.
        await self.comments.apply_edit(payload.message_id, payload.message.content, _naive(payload.message.edited_at))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        # Whether what was deleted is the notification post itself or a comment is not distinguished here. Both possibilities are passed on, and which row it was falls out when the row is looked up.
        await self.comments.apply_delete([payload.message_id])
        self.counts.mark_message(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        # A bulk delete. The individual delete events do not arrive, so without subscribing to this, deleted comments would stay in the mirror forever.
        await self.comments.apply_delete(list(payload.message_ids))
        for message_id in payload.message_ids:
            self.counts.mark_message(message_id)
