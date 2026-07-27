"""Mirror of article comments (thread replies).

The real comments live in Discord threads; this table holds only the mirror. Writes always happen on the Discord side, and this mirror never accepts one. It exists solely so the frontend can read comments.

Reading a comment's body requires message_content (a privileged intent). The bot always requests it, so if enabling it in the developer portal was forgotten the connection is refused at startup (only when it is not requested do events still arrive with the body as an empty string).

The mirror is kept by two paths.

  - Gateway events (posts, edits and deletions in a thread). They arrive the moment a message is posted, so the comment thread on the article page follows almost immediately.
  - Reconcile. Re-reads the thread history and reconciles the mirror against it. Posts made while the bot is down never arrive as events, so without this the comments from that window are missing forever.

Re-reading the history reads every message in the thread. One request carries 100 messages, so reading only the tail costs barely less, and reading everything repairs the votes on existing comments, catches up on deletions and re-adds the vote reactions in that same pass.

No foreign key from the commenter to users. A commenter need not be a guild member (other bots, webhooks, users who have left), and with a foreign key the whole comment thread would go missing the moment one unmirrorable post appeared. The read side resolves display names by LEFT JOINing users.

This bot's own posts are not mirrored: what the bot writes is the article's notification post itself, which is not a comment. Other bots' posts are mirrored (if a bot talks in the thread, that is part of the conversation as far as a reader is concerned).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Final

import discord

from common.db import Database

from . import votes

logger = logging.getLogger(__name__)

# Message types mirrored as comments: default posts and replies only. Join and pin notices are artifacts Discord itself pushes into the thread, not comments as far as a reader is concerned.
_COMMENT_TYPES: Final = frozenset({discord.MessageType.default, discord.MessageType.reply})

# Mirrors body, commenter and timestamps. Vote counts are untouched here (only the drain worker writes counts, and overwriting them with 0 here would wipe the re-tallied votes on every edit of a post).
_UPSERT_SQL: Final = """
    INSERT INTO article_comments (post_id, article_id, message_id, author_discord_user_id, body, posted_at, edited_at)
         VALUES (%s, %s, %s, %s, %s, %s, %s) AS new
    ON DUPLICATE KEY UPDATE
        body = new.body, edited_at = new.edited_at
"""

# Looks up the notification post by its thread id. thread_id carries a unique index, so a post arriving from any thread in the guild can be resolved or dropped in this one statement.
_POST_BY_THREAD_SQL: Final = 'SELECT id, article_id FROM article_discord_posts WHERE thread_id = %s'

_DELETE_SQL: Final = 'DELETE FROM article_comments WHERE message_id = %s'

_MARK_SEEDED_SQL: Final = 'UPDATE article_comments SET reactions_seeded_at = NOW() WHERE message_id = %s'

_IS_SEEDED_SQL: Final = 'SELECT reactions_seeded_at FROM article_comments WHERE message_id = %s'

# The only statement that writes vote counts. Kept separate from the body upsert so that a body update cannot crush the counts to 0.
_UPDATE_VOTES_SQL: Final = 'UPDATE article_comments SET up_count = %s, down_count = %s, last_synced_at = NOW() WHERE message_id = %s'


def is_comment(message: discord.Message, self_id: int | None) -> bool:
    """Whether this message is mirrored as a comment."""
    if message.author.id == self_id:
        return False
    return message.type in _COMMENT_TYPES


class CommentMirror:
    """Mirrors thread replies into article_comments.

    Writes no vote counts. The single drain worker is the only writer of counts, per the division of labour set out in the counts docstring. This class writes body, commenter and timestamps only.
    """

    def __init__(self, client: discord.Client, db: Database) -> None:
        self._client = client
        self._db = db

    async def apply_upsert(self, message: discord.Message) -> bool:
        """Mirror a post made to a thread. True if it was mirrored.

        Does nothing if the thread is not one we track. Posts from every thread in the guild land here, so silently dropping anything that is not an article's comment thread is normal behaviour.
        """
        if not isinstance(message.channel, discord.Thread):
            return False
        if not is_comment(message, self._client.user.id if self._client.user else None):
            return False
        post = await self._db.fetch_one(_POST_BY_THREAD_SQL, message.channel.id)
        if post is None:
            return False
        await self._db.execute(
            _UPSERT_SQL,
            post['id'], post['article_id'], message.id, message.author.id,
            message.content, _naive(message.created_at), _naive(message.edited_at),
        )
        return True

    async def apply_edit(self, message_id: int, body: str, edited_at: datetime | None) -> None:
        """Mirror a comment edit. An edit to a post that was never mirrored does nothing (0 rows updated).

        Rewrites the body only, leaving votes already tallied in place (an edit does not undo votes).
        """
        await self._db.execute(
            'UPDATE article_comments SET body = %s, edited_at = %s WHERE message_id = %s',
            body, edited_at, message_id,
        )

    async def apply_delete(self, message_ids: list[int]) -> None:
        """Mirror comment deletions.

        Deletes the whole row. A message that is gone from Discord but kept in the mirror would still show on the article page and nowhere else.
        """
        for message_id in message_ids:
            await self._db.execute(_DELETE_SQL, message_id)

    async def mark_seeded(self, message_id: int) -> None:
        await self._db.execute(_MARK_SEEDED_SQL, message_id)

    async def backfill(self, thread: discord.Thread) -> int:
        """Re-read the thread's whole history and bring the mirror in line with it. Returns the number of comments mirrored.

        Deliberately not built to read only what follows the last mirrored message. History carries 100 messages per request, so reading only the tail — or reading an empty thread — costs almost the same. Reading everything settles three things at once.

          - Repairing votes on existing comments. History messages carry their reactions along with the body, so the counts can be brought in line without re-fetching each comment one by one. Reading only the tail would leave the missed votes on older comments wrong forever, with nothing to put them right.
          - Catching up on deletions. A deletion made while the bot was down never arrives as an event and appears nowhere in the history, so the only way to catch a row left behind in the mirror is to delete here whatever the history does not contain.
          - Re-adding the vote reactions. Comments posted while the bot was down, and comments where adding failed, have no reactions. Without re-adding them here, those comments alone stay without one-press voting.

        Comments posted after the moment the history read began are not deleted. A post arriving mid-read appears nowhere in the history, yet the event path has already mirrored it, so a plain "delete every row not in the history" would delete that one. A snowflake embeds its timestamp, so using an id built from the read's start time as the cutoff excludes that window.

        oldest_first is spelled out because the default is newest-first, and relying on the implicit direction would read the thread in the opposite order to the one intended.
        """
        post = await self._db.fetch_one(_POST_BY_THREAD_SQL, thread.id)
        if post is None:
            return 0

        # Fix the cutoff before the history read starts. Posts after it are not deleted even when the history does not contain them.
        cutoff = discord.utils.time_snowflake(datetime.now(timezone.utc))
        self_id = self._client.user.id if self._client.user else None

        seen: list[int] = []
        written = 0
        async for message in thread.history(limit=None, oldest_first=True):
            if not is_comment(message, self_id):
                continue
            seen.append(message.id)
            up, down = votes.tally(message)
            await self._db.execute(
                _UPSERT_SQL,
                post['id'], post['article_id'], message.id, message.author.id,
                message.content, _naive(message.created_at), _naive(message.edited_at),
            )
            await self._db.execute(_UPDATE_VOTES_SQL, up, down, message.id)
            await self._seed_if_needed(message)
            written += 1

        removed = await self._delete_missing(int(post['id']), seen, cutoff)
        if written or removed:
            logger.info('スレッド %s の履歴と写しを突き合わせました(コメント %d 件、写しにだけ残っていた %d 件を削除)。', thread.id, written, removed)
        return written

    async def _seed_if_needed(self, message: discord.Message) -> None:
        """Add the vote reactions to comments that do not have them yet.

        Rows carrying the mark that seeding finished are skipped, so re-reading the history does not query Discord every time. If adding fails, no mark is left and the next reconcile tries again.
        """
        row = await self._db.fetch_one(_IS_SEEDED_SQL, message.id)
        if row is None or row['reactions_seeded_at'] is not None:
            return
        if await votes.seed(message):
            await self.mark_seeded(message.id)

    async def _delete_missing(self, post_id: int, seen: list[int], cutoff: int) -> int:
        """Delete from the mirror the comments absent from the history. Posts made after the read started are kept."""
        params: list[Any] = [post_id, cutoff]
        sql = 'DELETE FROM article_comments WHERE post_id = %s AND message_id < %s'
        if seen:
            sql += f' AND message_id NOT IN ({", ".join(["%s"] * len(seen))})'
            params.extend(seen)
        return await self._db.execute(sql, *params)


def _naive(value: datetime | None) -> datetime | None:
    """Drop a Discord timestamp to UTC-naive to match the DATETIME column.

    MySQL DATETIME carries no timezone. Passing a tz-aware value straight through makes the driver ignore tzinfo and write the wall-clock reading, silently shifting the time. This system keeps datetimes UTC-naive throughout (the same convention as server/app/schemas.py), so convert to UTC here before dropping tzinfo.
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)
