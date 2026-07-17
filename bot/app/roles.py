"""Mirror of the guild's roles, members and owner.

Discord is the source of truth; the DB holds only a mirror. The visibility check and API-key eligibility both read this mirror, so a stale mirror lets someone who left the guild keep reading limited-visibility articles, and lets the key of someone who lost admin keep working.

The mirror is kept by two paths.

  - Gateway events (role create/update/delete, member join/leave/update, ownership transfer). They arrive the moment Discord changes, so permission is revoked the instant a role is removed. Normally this is the only path doing any work.
  - Reconcile. Reads every current role and member of the guild and reconciles the whole mirror against them. It exists to correct missed events, and runs on bot restart, when the gateway establishes a new session, when the guild becomes available again, and every guild_reconcile_interval_seconds.

Reconcile is kept because gateway events are at-most-once. discord.py swallows an exception raised by a listener and moves on (Client._run_event), and Discord does not redeliver that event. So if the DB is unresponsive for even an instant, that one change never enters the mirror. If the gateway connection stays up, no new session is established either, and nobody notices until a restart. Honouring the guarantee that removing a role revokes permission requires every missed event to be corrected within a finite time, and the upper bound on the reconcile interval is that finite time.

Events carry the current state, not a delta (a member-update event carries the full post-change role set; a role-update event carries all of the role's attributes). Each listener therefore writes the state it received as-is and does no delta arithmetic. A duplicate event, or two events arriving out of order, produces the same result.

Listeners and reconcile write the same rows, so they are serialized under one Lock. Run concurrently, a reconcile write can overwrite with a stale value the result of an event that arrived after the reconcile read (example: a role is revoked just after reconcile reads the member list, the listener deletes it, and then reconcile writes a mirror saying the member still holds it). Taking the Lock before reading the guild state guarantees every event that arrived up to the read is already applied, so this overwrite cannot happen — discord.py updates its own cache before dispatching an event.

permissions (Discord's permission bitfield) is always mirrored into discord_roles. Without it there is no way to know whether a role holds Administrator, and API-key issue eligibility based on admin permission cannot be decided.

Deleting a role in Discord deletes the discord_roles row, and foreign keys cascade that to user_roles and article_visibility_roles. publishers.role_id is ON DELETE SET NULL instead, so the publisher row itself survives: the bylines on a dissolved publisher's past articles must not be lost. Do not break that premise here (never go and delete a publisher).

user_roles is rewritten in a single transaction. If another connection could see the intermediate state of a role swap, a request arriving at that instant would be treated as a user holding no roles at all and could be refused an article it is entitled to read.

Within that, write only the diff. Deleting everything and reinserting gives the same result, and inside a transaction the intermediate state is invisible either way, but it rewrites unchanged rows every time and needlessly conflicts with the login-time role update (server side) that writes the same rows, buying nothing but waits and deadlock opportunities on both sides.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from typing import Any, Final, TypeVar

import discord
from aiomysql import Connection

from common.db import Database
from common.settings import RuntimeSettings, SettingsStore

logger = logging.getLogger(__name__)

T = TypeVar('T')

# Batch size: avoids cramming too much into one statement without falling back to a round trip per row.
_CHUNK_SIZE: Final = 500

# Seconds to wait for the next pass when the settings themselves could not be read. The real interval lives in the settings, so while they cannot be read there is no way to know it. A DB blip or seed values not yet bootstrapped are recoverable states, so re-read at a short fixed interval.
SETTINGS_RETRY_SECONDS: Final = 30

# Max wait for member chunking. It is a gateway round trip, and waiting forever for a response that never comes would stop reconcile from ever running again.
CHUNK_TIMEOUT_SECONDS: Final = 120

# Alias the new row for the duplicate case; ON DUPLICATE KEY UPDATE's VALUES() is deprecated, so it is not used.
_UPSERT_ROLE_SQL: Final = """
    INSERT INTO discord_roles (id, name, color, position, permissions) VALUES {values} AS new
    ON DUPLICATE KEY UPDATE
        name = new.name, color = new.color, position = new.position, permissions = new.permissions
"""

# is_guild_member is always overwritten with 1. Writing a constant rather than the new row's value is what restores a user who left the guild and came back.
_UPSERT_USER_SQL: Final = """
    INSERT INTO users (discord_user_id, username, global_name, avatar_hash, is_guild_member) VALUES {values} AS new
    ON DUPLICATE KEY UPDATE
        username = new.username, global_name = new.global_name, avatar_hash = new.avatar_hash, is_guild_member = 1
"""


def _chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(count: int, columns: int = 1) -> str:
    """Build `count` placeholders. Values themselves are always passed as arguments, so all that is built here is the run of %s."""
    if columns == 1:
        return ', '.join(['%s'] * count)
    row = '(' + ', '.join(['%s'] * columns) + ')'
    return ', '.join([row] * count)


def desired_role_ids(member: discord.Member, guild_id: int) -> set[int]:
    """The role IDs to mirror into user_roles.

    Excludes @everyone (whose ID equals the guild ID). @everyone does not appear in Discord's member payload roles, but does appear in discord.Member.roles, and is absent from the roles fetched over OAuth2 at login. If this were the only path adding it, event application, reconcile and the login-time role update would cancel each other out and the same user's row would be added and deleted forever. So that the decision of what to mirror stays in one place, do not mirror member.roles into user_roles anywhere but this function.
    """
    return {role.id for role in member.roles if role.id != guild_id}


class GuildMirror:
    """Keeps the mirror of roles, members and the owner.

    Both the event listeners and the reconcile loop live here. They write the same rows and are serialized under the same Lock, so splitting them apart would make it impossible to read off that the invariant holds.
    """

    def __init__(self, client: discord.Client, db: Database, store: SettingsStore) -> None:
        self._client = client
        self._db = db
        self._store = store
        # Serializes event application and reconcile. See the module docstring.
        self._lock = asyncio.Lock()
        # Signal asking for a reconcile. Any number of set() calls collapse into one reconcile, so callers need not care about duplicate requests.
        self._reconcile_requested = asyncio.Event()

    def request_reconcile(self, reason: str) -> None:
        """Prompt a reconcile without waiting for the next scheduled one. Called from wherever a missed event is suspected."""
        logger.info('ロールの照合を要求しました(理由: %s)。', reason)
        self._reconcile_requested.set()

    # Reconcile

    async def run_reconcile_loop(self) -> None:
        """Reconcile when a reconcile is requested, or when guild_reconcile_interval_seconds has elapsed.

        One task does all the reconciling, so overlapping requests never run two reconciles at once. Starting a reconcile inside the task that dispatches events would let reconciles pile up when the gateway re-establishes its session repeatedly in a short window, fighting each other for row locks until they all fail.

        Exceptions are swallowed here and the loop moves on to the next pass. If this loop died merely because the settings could not be read, missed events would never be corrected again: someone who left the guild would keep reading limited-visibility articles, and the key of someone who lost admin would keep working.
        """
        await self._client.wait_until_ready()
        while True:
            try:
                settings = await self._store.snapshot()
            except Exception:
                logger.exception('設定を読み取れないため、ロールの照合を %d 秒後に再試行します。', SETTINGS_RETRY_SECONDS)
                await asyncio.sleep(SETTINGS_RETRY_SECONDS)
                continue
            interval = settings.discord.bot.guild_reconcile_interval_seconds
            # Wait for a request before reconciling. The first reconcile after startup runs on on_ready's request, so waiting here never delays startup.
            try:
                await asyncio.wait_for(self._reconcile_requested.wait(), timeout=interval)
            except TimeoutError:
                pass
            # Clear before starting the reconcile. A request that arrives mid-reconcile points at a change later than what that reconcile read, so it needs another reconcile on the next pass.
            self._reconcile_requested.clear()
            try:
                await self.reconcile(settings)
            except Exception:
                logger.exception('ロールの照合で例外が発生しました。%d 秒後に再試行します。', interval)

    async def reconcile(self, settings: RuntimeSettings) -> None:
        """Reconcile the target guild's roles, members and owner in full."""
        guild = self._client.get_guild(settings.discord.guild_id)
        if guild is None:
            logger.error('bot が対象の guild %s に参加していないため、ロールを照合できません。bot を招待し、設定の discord.guild_id を確認してください。', settings.discord.guild_id)
            return

        if not guild.chunked:
            # discord.py normally chunks members on connect and keeps them current from events, so this is reached only when chunking did not finish in time. Reconciling with an incomplete member list would wrongly record people who are still here as having left the guild.
            # Wait outside the Lock. It is a gateway round trip, and holding the Lock while waiting would stall all event application for the duration.
            try:
                await asyncio.wait_for(guild.chunk(cache=True), timeout=CHUNK_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.error('guild %s のメンバーを %d 秒以内に取り寄せられなかったため、照合を中止します。次の周回で再試行します。', guild.id, CHUNK_TIMEOUT_SECONDS)
                return

        async with self._lock:
            # Always read the guild state inside the Lock. Using a list read outside it could overwrite, with a stale value, the result of an event that arrived between the read and the write (see the module docstring for why).
            roles = list(guild.roles)
            members = list(guild.members)
            if not roles:
                logger.error('guild %s のロールを 1 つも取得できませんでした。この状態で照合すると全てのロールを削除してしまうため中止します。', guild.id)
                return
            if not members:
                logger.error('guild %s のメンバーを 1 人も取得できませんでした。この状態で照合すると全員を非メンバーとして扱ってしまうため中止します。', guild.id)
                return

            async with self._db.transaction() as conn:
                removed_roles = await self._reconcile_roles(conn, roles)
                await self._reconcile_members(conn, guild, members)
        logger.info('guild %s を照合しました(ロール %d 件、削除 %d 件、メンバー %d 人)。', guild.id, len(roles), removed_roles, len(members))

    async def _reconcile_roles(self, conn: Connection, roles: Sequence[discord.Role]) -> int:
        """Upsert roles and delete the roles that are gone from Discord. Returns the number deleted."""
        await self._upsert_roles(conn, roles)
        role_ids = [role.id for role in roles]
        # Drop roles that are gone. Foreign keys cascade the delete to user_roles and article_visibility_roles, but publishers only gets role_id set to NULL and keeps its row, to protect the bylines on a dissolved publisher's past articles.
        sql = f'DELETE FROM discord_roles WHERE id NOT IN ({_placeholders(len(role_ids))})'
        return await self._db.execute(sql, *role_ids, conn=conn)

    async def _reconcile_members(self, conn: Connection, guild: discord.Guild, members: Sequence[discord.Member]) -> None:
        """Upsert members into users, and refresh user_roles, is_guild_member and is_guild_owner to match the current state.

        Every guild member is mirrored because people who have never logged in still need a users row: naming someone else as the author under articles:write:any cannot save the article if that person does not exist.
        """
        await self._upsert_members(conn, members)

        member_ids = {member.id for member in members}
        existing = await self._db.fetch_all('SELECT id, discord_user_id, is_guild_member, is_guild_owner FROM users', conn=conn)
        user_id_by_discord_id = {int(row['discord_user_id']): int(row['id']) for row in existing}

        # Events cannot tell us who is gone: Discord does not report departures that happened while the bot was down, so this refresh is the only way to detect them.
        departed = [int(row['id']) for row in existing if row['is_guild_member'] and int(row['discord_user_id']) not in member_ids]
        for chunk in _chunks(departed, _CHUNK_SIZE):
            sql = f'UPDATE users SET is_guild_member = 0 WHERE id IN ({_placeholders(len(chunk))})'
            await self._db.execute(sql, *chunk, conn=conn)
            # Always drop a departed user's roles too. The visibility check looks at admin status before is_guild_member, so leaving the roles in place lets a departed admin keep reading limited-visibility articles.
            sql = f'DELETE FROM user_roles WHERE user_id IN ({_placeholders(len(chunk))})'
            await self._db.execute(sql, *chunk, conn=conn)
        if departed:
            logger.info('guild を抜けた利用者 %d 人の is_guild_member とロールを落としました。', len(departed))

        await self._reconcile_owner(conn, guild, existing, user_id_by_discord_id)
        await self._reconcile_user_roles(conn, guild, members, user_id_by_discord_id)

    async def _reconcile_owner(self, conn: Connection, guild: discord.Guild, existing: Sequence[dict[str, Any]], user_id_by_discord_id: dict[int, int]) -> None:
        """Refresh the owner flag to match the current state.

        Always clear is_guild_owner on everyone but the current owner. Ownership can be transferred, so setting the flag without ever clearing it leaves the previous owner holding full authority. The owner is treated as an admin regardless of roles, so that is a state in which revoking permission has no effect.
        """
        stale = [int(row['id']) for row in existing if row['is_guild_owner'] and int(row['discord_user_id']) != guild.owner_id]
        for chunk in _chunks(stale, _CHUNK_SIZE):
            sql = f'UPDATE users SET is_guild_owner = 0 WHERE id IN ({_placeholders(len(chunk))})'
            await self._db.execute(sql, *chunk, conn=conn)
        if stale:
            logger.info('オーナーでなくなった利用者 %d 人の is_guild_owner を落としました。', len(stale))

        owner_user_id = user_id_by_discord_id.get(guild.owner_id) if guild.owner_id is not None else None
        if owner_user_id is None:
            logger.warning('guild %s のオーナー(Discord ユーザー %s)が users に見つからないため、is_guild_owner を立てられません。', guild.id, guild.owner_id)
            return
        await self._db.execute('UPDATE users SET is_guild_owner = 1 WHERE id = %s', owner_user_id, conn=conn)

    async def _reconcile_user_roles(self, conn: Connection, guild: discord.Guild, members: Sequence[discord.Member], user_id_by_discord_id: dict[int, int]) -> None:
        """Refresh user_roles to match what members currently hold.

        Only the diff is applied. Deleting everything and reinserting gives the same result, but it would rewrite the whole table on every reconcile and needlessly conflict with the login-time role update.
        """
        desired: dict[int, set[int]] = {}
        for member in members:
            user_id = user_id_by_discord_id.get(member.id)
            if user_id is None:
                continue
            desired[user_id] = desired_role_ids(member, guild.id)

        current: dict[int, set[int]] = {}
        for row in await self._db.fetch_all('SELECT user_id, role_id FROM user_roles', conn=conn):
            current.setdefault(int(row['user_id']), set()).add(int(row['role_id']))

        # A user who left the guild does not appear in desired here, so every role they held is deleted.
        # Leaving the roles in place would let them keep reading role-restricted articles after leaving.
        to_delete = [(user_id, role_id) for user_id, role_ids in current.items() for role_id in role_ids - desired.get(user_id, frozenset())]
        to_insert = [(user_id, role_id) for user_id, role_ids in desired.items() for role_id in role_ids - current.get(user_id, frozenset())]

        for chunk in _chunks(to_delete, _CHUNK_SIZE):
            params = [value for pair in chunk for value in pair]
            sql = f'DELETE FROM user_roles WHERE (user_id, role_id) IN ({_placeholders(len(chunk), 2)})'
            await self._db.execute(sql, *params, conn=conn)

        for chunk in _chunks(to_insert, _CHUNK_SIZE):
            params = [value for pair in chunk for value in pair]
            # IGNORE so that running at the same time as the login-time role update does not collide.
            sql = f'INSERT IGNORE INTO user_roles (user_id, role_id) VALUES {_placeholders(len(chunk), 2)}'
            await self._db.execute(sql, *params, conn=conn)

        if to_delete or to_insert:
            logger.info('user_roles を照合しました(付与 %d 件、剥奪 %d 件)。', len(to_insert), len(to_delete))

    # Event application.
    # Each listener handles only events for the target guild. If the bot is in other guilds too, mirroring their roles would let the visibility check rest on a role ID from a different guild.

    async def _target_guild_id(self) -> int:
        return (await self._store.snapshot()).discord.guild_id

    async def apply_role_upsert(self, role: discord.Role) -> None:
        """Mirror a role create or update. permissions changes arrive here too: adding or removing Administrator changes the eligibility of everyone holding that role at once."""
        if role.guild.id != await self._target_guild_id():
            return
        async with self._lock:
            await self._upsert_roles(None, [role])
        logger.info('ロール %s(%s)を写しました。', role.id, role.name)

    async def apply_role_delete(self, role: discord.Role) -> None:
        """Mirror a role delete. Foreign keys cascade to user_roles and article_visibility_roles, and publishers.role_id becomes NULL (the publisher row is kept)."""
        if role.guild.id != await self._target_guild_id():
            return
        async with self._lock:
            await self._db.execute('DELETE FROM discord_roles WHERE id = %s', role.id)
        logger.info('ロール %s(%s)の削除を写しました。', role.id, role.name)

    async def apply_member_upsert(self, member: discord.Member) -> None:
        """Mirror a member join or update. The event carries the full post-change role set, so that state is written as-is rather than a delta."""
        if member.guild.id != await self._target_guild_id():
            return
        async with self._lock:
            async with self._db.transaction() as conn:
                # Mirror the roles themselves before the membership rows. If a newly created role was granted at the same instant, a discord_roles row that does not exist yet fails user_roles' foreign key and that one grant is silently dropped.
                await self._upsert_roles(conn, member.roles)
                await self._upsert_members(conn, [member])
                user_id = await self._user_id(conn, member.id)
                if user_id is None:
                    logger.warning('Discord ユーザー %s を users へ写せなかったため、ロールを反映できません。', member.id)
                    return
                await self._apply_user_roles(conn, user_id, desired_role_ids(member, member.guild.id))

    async def apply_member_remove(self, user: discord.User | discord.Member, guild_id: int) -> None:
        """Mirror a member leaving. Clears is_guild_member and always deletes the roles they held.

        The visibility check looks at admin status before is_guild_member. Clearing is_guild_member while leaving the roles in place would let an admin who was kicked from the guild keep reading every draft.
        """
        if guild_id != await self._target_guild_id():
            return
        async with self._lock:
            async with self._db.transaction() as conn:
                user_id = await self._user_id(conn, user.id)
                if user_id is None:
                    return
                await self._db.execute('UPDATE users SET is_guild_member = 0, is_guild_owner = 0 WHERE id = %s', user_id, conn=conn)
                await self._db.execute('DELETE FROM user_roles WHERE user_id = %s', user_id, conn=conn)
        logger.info('Discord ユーザー %s の離脱を写しました(is_guild_member とロールを落としました)。', user.id)

    async def apply_user_update(self, user: discord.User) -> None:
        """Mirror a user's display-name or avatar change. They are used in an article's byline, so a stale row keeps showing the pre-rename name.

        Users with no users row are not mirrored. There is no need to pick up renames of someone who is not a guild member, and creating a row would only add rows that are in users without being in the guild.
        """
        avatar_hash = user.avatar.key if user.avatar is not None else None
        async with self._lock:
            await self._db.execute(
                'UPDATE users SET username = %s, global_name = %s, avatar_hash = %s WHERE discord_user_id = %s',
                user.name, user.global_name, avatar_hash, user.id,
            )

    async def apply_owner_change(self, guild: discord.Guild) -> None:
        """Mirror an ownership transfer. Clearing the old owner and setting the new one happen in a single transaction.

        Written separately, another connection could see a state with two owners (or with none) in between. The owner is an admin regardless of roles, so the former creates an instant where permission does not go away, and the latter an instant where the legitimate admin is locked out.
        """
        if guild.id != await self._target_guild_id():
            return
        async with self._lock:
            async with self._db.transaction() as conn:
                existing = await self._db.fetch_all('SELECT id, discord_user_id, is_guild_member, is_guild_owner FROM users', conn=conn)
                user_id_by_discord_id = {int(row['discord_user_id']): int(row['id']) for row in existing}
                await self._reconcile_owner(conn, guild, existing, user_id_by_discord_id)
        logger.info('guild %s のオーナーが Discord ユーザー %s へ移ったことを写しました。', guild.id, guild.owner_id)

    # Shared writes

    async def _upsert_roles(self, conn: Connection | None, roles: Sequence[discord.Role]) -> None:
        for chunk in _chunks(list(roles), _CHUNK_SIZE):
            params: list[Any] = []
            for role in chunk:
                # permissions is mandatory here: the admin-eligibility check rests on this value alone.
                params.extend([role.id, role.name, role.colour.value, role.position, role.permissions.value])
            sql = _UPSERT_ROLE_SQL.format(values=_placeholders(len(chunk), 5))
            await self._db.execute(sql, *params, conn=conn)

    async def _upsert_members(self, conn: Connection | None, members: Sequence[discord.Member]) -> None:
        for chunk in _chunks(list(members), _CHUNK_SIZE):
            params: list[Any] = []
            for member in chunk:
                # Member.avatar is the account avatar, not the guild-specific one. users holds account-side information, so this is the one to mirror.
                avatar_hash = member.avatar.key if member.avatar is not None else None
                params.extend([member.id, member.name, member.global_name, avatar_hash, 1])
            sql = _UPSERT_USER_SQL.format(values=_placeholders(len(chunk), 5))
            await self._db.execute(sql, *params, conn=conn)

    async def _user_id(self, conn: Connection, discord_user_id: int) -> int | None:
        value = await self._db.fetch_val('SELECT id FROM users WHERE discord_user_id = %s', discord_user_id, conn=conn)
        return int(value) if value is not None else None

    async def _apply_user_roles(self, conn: Connection, user_id: int, desired: set[int]) -> None:
        """Align one person's roles with desired. Only the diff is written; see the module docstring for why not delete-all-then-reinsert."""
        current = {int(row['role_id']) for row in await self._db.fetch_all('SELECT role_id FROM user_roles WHERE user_id = %s', user_id, conn=conn)}
        to_delete = current - desired
        to_insert = desired - current
        if to_delete:
            sql = f'DELETE FROM user_roles WHERE user_id = %s AND role_id IN ({_placeholders(len(to_delete))})'
            await self._db.execute(sql, user_id, *to_delete, conn=conn)
        if to_insert:
            params = [value for role_id in to_insert for value in (user_id, role_id)]
            # IGNORE so that running at the same time as the login-time role update does not collide.
            sql = f'INSERT IGNORE INTO user_roles (user_id, role_id) VALUES {_placeholders(len(to_insert), 2)}'
            await self._db.execute(sql, *params, conn=conn)
