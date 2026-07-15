"""記事のコメント(スレッドの返信)の写し。

コメントの実体は Discord のスレッドにあり、この表が持つのはその写しである。書き込みは常に Discord 側で行われ、この写しが書き込みを受け付けることはない。フロントエンドがコメントを読めるようにするためだけに存在する。

本文を読むには message_content(特権 intent)が要る。bot はこれを常に要求するため、開発者ポータルでの有効化を忘れていれば起動時に接続を断られる(要求しない場合に限り、イベントだけが届いて本文が空文字になる)。

写しは 2 つの経路で保たれる。

  ・ゲートウェイのイベント(スレッドへの投稿・編集・削除)。投稿された時点で届くため、記事面のコメント欄はほぼ即座に追随する。
  ・照合(reconcile)。スレッドの履歴を読み直して写しを突き合わせる。bot が停止している間の投稿は届かないため、これが無いとその間のコメントが永久に欠ける。

履歴の読み直しは、そのスレッドの全件を読む。1 回の問い合わせが 100 件を運ぶため続きだけを読んでも費用はほとんど変わらず、全件を読めば、既存のコメントに付いた票の補正・消されたコメントの取り込み・投票用の絵文字の付け直しまで同じ 1 回で片付く。

投稿者に users への外部キーを張らない。コメントの主は guild のメンバーとは限らず(他の bot・webhook・退会した利用者)、外部キーにすると、写せない投稿が 1 件現れた瞬間にそのコメント欄全体が欠ける。表示名の解決は読み出し側が users を LEFT JOIN して行う。

この bot 自身の投稿は写さない。bot が書くのは記事の通知そのものであり、それはコメントではない。他の bot の投稿は写す(スレッドで喋る bot が居るなら、それは読者から見て会話の一部である)。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Final

import discord

from common.db import Database

from . import votes

logger = logging.getLogger(__name__)

# コメントとして写すメッセージの種類。既定の投稿と返信だけを採る。参加・ピン留めなどの案内は Discord がスレッドへ流す作り物であり、読者にとってのコメントではない。
_COMMENT_TYPES: Final = frozenset({discord.MessageType.default, discord.MessageType.reply})

# 本文・投稿者・時刻を写す。投票の数はここで触らない(数を書くのは掃き出しのワーカだけであり、ここが 0 で上書きすると、数え直した票が投稿の編集のたびに消える)。
_UPSERT_SQL: Final = """
    INSERT INTO article_comments (post_id, article_id, message_id, author_discord_user_id, body, posted_at, edited_at)
         VALUES (%s, %s, %s, %s, %s, %s, %s) AS new
    ON DUPLICATE KEY UPDATE
        body = new.body, edited_at = new.edited_at
"""

# 通知(スレッド)を id で引く。thread_id には一意索引があるため、guild 中のどのスレッドの投稿が届いてもこの 1 文で落とせる。
_POST_BY_THREAD_SQL: Final = 'SELECT id, article_id FROM article_discord_posts WHERE thread_id = %s'

_DELETE_SQL: Final = 'DELETE FROM article_comments WHERE message_id = %s'

_MARK_SEEDED_SQL: Final = 'UPDATE article_comments SET reactions_seeded_at = NOW() WHERE message_id = %s'

_IS_SEEDED_SQL: Final = 'SELECT reactions_seeded_at FROM article_comments WHERE message_id = %s'

# 票の数を書く唯一の文。本文を写す側とは別にしてあり、本文の更新が票を 0 に潰さないようにする。
_UPDATE_VOTES_SQL: Final = 'UPDATE article_comments SET up_count = %s, down_count = %s, last_synced_at = NOW() WHERE message_id = %s'


def is_comment(message: discord.Message, self_id: int | None) -> bool:
    """その投稿をコメントとして写すか。"""
    if message.author.id == self_id:
        return False
    return message.type in _COMMENT_TYPES


class CommentMirror:
    """スレッドの返信を article_comments へ写す。

    投票の数は書かない。数を書くのは掃き出しのワーカ 1 本だけであり、その分担は counts の docstring に述べたとおりである。ここが書くのは本文・投稿者・時刻に限る。
    """

    def __init__(self, client: discord.Client, db: Database) -> None:
        self._client = client
        self._db = db

    async def apply_upsert(self, message: discord.Message) -> bool:
        """スレッドへの投稿を写す。写したら True。

        対象のスレッドでなければ何もしない。guild 中のあらゆるスレッドの投稿がここへ届くため、記事のコメント欄でないものを黙って落とすのは正常な動作である。
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
        """コメントの編集を写す。写していない投稿の編集なら何も起きない(0 行が更新される)。

        本文だけを書き換える。既に数えた票はそのまま残す(編集は投票を無かったことにしない)。
        """
        await self._db.execute(
            'UPDATE article_comments SET body = %s, edited_at = %s WHERE message_id = %s',
            body, edited_at, message_id,
        )

    async def apply_delete(self, message_ids: list[int]) -> None:
        """コメントの削除を写す。

        行ごと消す。Discord から消えた発言を写しに残すと、記事面にだけ残り続けることになる。
        """
        for message_id in message_ids:
            await self._db.execute(_DELETE_SQL, message_id)

    async def mark_seeded(self, message_id: int) -> None:
        await self._db.execute(_MARK_SEEDED_SQL, message_id)

    async def backfill(self, thread: discord.Thread) -> int:
        """スレッドの履歴を丸ごと読み直し、写しをその内容に揃える。写した件数を返す。

        既に写した続きだけを読む作りにはしない。履歴は 1 回の問い合わせで 100 件を運ぶため、続きだけを読んでも、スレッドが空でも、費用はほとんど変わらない。そして全件を読めば次の 3 つが同時に片付く。

          ・既存のコメントに付いた票の補正。履歴のメッセージは本文と一緒にリアクションも運んでくるため、コメント 1 件ずつ取得し直さずに数を揃えられる。続きだけを読む作りでは、古いコメントの票を取りこぼしたまま二度と正せない。
          ・消えたコメントの取り込み。bot が停止している間の削除はイベントとして届かず、履歴にも現れない。写しにだけ残った行は、ここで履歴に無いものを消す以外に気付く手段が無い。
          ・投票用の絵文字の付け直し。bot の停止中に投稿されたコメントや、付与に失敗したコメントには絵文字が無い。ここで付け直さないと、そのコメントにだけ 1 押しでの投票ができないまま残る。

        履歴を読み始めた時刻より後に投稿されたコメントは消さない。読んでいる最中に届いた投稿は履歴には現れず、しかしイベント側の経路が既に写しているため、単純に「履歴に無い行を消す」とその 1 件を消してしまう。スノーフレークは時刻を含むため、読み始めの時刻から作った id を境にすれば、その隙間を除ける。

        oldest_first を明示するのは、既定が新しい順であり、暗黙の向きに頼ると読む順序が意図と逆になるためである。
        """
        post = await self._db.fetch_one(_POST_BY_THREAD_SQL, thread.id)
        if post is None:
            return 0

        # 履歴を読み始める前に境を決める。これより後の投稿は、履歴に現れなくても消さない。
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
        """まだ投票用の絵文字が付いていないコメントに付ける。

        付け終えた印が残っている行には何もしないため、履歴を読み直すたびに Discord へ問い合わせることはない。付けられなければ印を残さず、次の照合でもう一度試す。
        """
        row = await self._db.fetch_one(_IS_SEEDED_SQL, message.id)
        if row is None or row['reactions_seeded_at'] is not None:
            return
        if await votes.seed(message):
            await self.mark_seeded(message.id)

    async def _delete_missing(self, post_id: int, seen: list[int], cutoff: int) -> int:
        """履歴に無かったコメントを写しから消す。読み始めより後の投稿は残す。"""
        params: list[Any] = [post_id, cutoff]
        sql = 'DELETE FROM article_comments WHERE post_id = %s AND message_id < %s'
        if seen:
            sql += f' AND message_id NOT IN ({", ".join(["%s"] * len(seen))})'
            params.extend(seen)
        return await self._db.execute(sql, *params)


def _naive(value: datetime | None) -> datetime | None:
    """Discord の時刻を、DATETIME 列に合わせて UTC の naive へ落とす。

    MySQL の DATETIME は時間帯を持たない。tz 付きの値をそのまま渡すとドライバが tzinfo を無視して壁時計の値を書き込み、時刻が静かにずれる。本システムは日時を UTC の naive で揃えているため(server/app/schemas.py と同じ規約)、ここでも UTC へ寄せてから tzinfo を落とす。
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)
