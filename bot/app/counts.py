"""通知メッセージとコメントに付いた投票の写し。

:arrow_up: を賛成、:arrow_down: を反対として数え、その数を article_discord_posts と article_comments へ写す。どの絵文字を数えるかの判断は votes に集めてある。報道社ランキングと記事面の点数はこの値を使う。

ゲートウェイのイベントで駆動する。リアクションが付いた・外れたという事実はイベントとして届くため、それを待って数え直す。

ただしイベントが運ぶ差分をそのまま加減算しない。イベントからは「この投稿の数が変わった」という印だけを受け取り(mark_message)、実際の値は対象のメッセージを取得し直して現在の数で上書きする。理由は 4 つあり、どれも加減算では塞げない:

  ・総数を運ぶイベントが無い。「この絵文字のリアクションが全て外された」というイベント(MESSAGE_REACTION_REMOVE_EMOJI)は何件外れたかを運ばず、まとめて削除されたメッセージのイベントも同様である。引くべき数を知る手段が無い以上、加減算では正しい値に戻せない。
  ・賛成から反対へ押し替えると、追加と削除の 2 つのイベントが届く。その到着の順序は保証されないため、加減算では賛成と反対がそれぞれ 1 ずつずれたまま戻らなくなる。
  ・加減算は取りこぼしと二重の配送で静かにずれ、そのずれが累積する。上書きなら、ずれても次の更新で必ず正しい値に戻る。
  ・up_count・down_count は個数であり符号なしで持つ。0 から 1 を引くと MySQL はエラーで失敗する(値が丸まるのではなく文が失敗する)。取りこぼしにより DB の数が実際より小さいことは普通に起こるため、加減算は下限の扱いを常に自分で背負うことになる。

書き手を 1 本のワーカに限るのも同じ理由による。discord.py はリスナをそれぞれ独立したタスクとして起動するため、同じ行への上書きが並行しうる。印を付けるだけのリスナと、それを掃き出す 1 本のワーカに分ければ、行の書き手は常に 1 つであり、メッセージを取得してから書き込むまでの間に別の書き込みが割り込まない。印は集合であるため、短時間に何百件のリアクションが付いても Discord への問い合わせは 1 回にまとまる。

スレッドの履歴の読み直し(コメントの補正)もこのワーカが行う。履歴は本文と投票の数の両方を運んでくるため、これを別のタスクに任せると、同じ行を 2 つの書き手が書くことになる。

照合(reconcile)は、イベントを取りこぼした場合の備えである。ゲートウェイのイベントは at-most-once であり、bot が停止している間のイベントは届かず、リスナが例外を送出すれば discord.py はそれを握り潰して次へ進み、Discord は再送しない。したがって取りこぼしは必ず起こる前提で作る必要がある。照合は対象範囲の通知とスレッドに印を付け直すだけであり、実際の取得と上書きは同じワーカが行う。すなわち「取りこぼしても正しい値に戻る」という要求は、周期的な全件の上書きではなく、印を付け直すことと上書きするワーカの組で満たす。

照合の対象を公開から count_reconcile_window_days 以内に限るのは、照合が対象期間の通知を 1 件残らず取得するためである。際限なく遡ると、反応がとうに止んだ記事まで含めて、記事の総数に比例した問い合わせを周期ごとに投げ続けることになる。
イベントの経路にこの打ち切りは要らない。取得するのは実際に変化した投稿だけであり、静かな記事には 1 件の問い合わせも発生しないためである(この範囲を過ぎた通知にもイベントは届き、数はそのまま更新される)。両者の違いは問い合わせの有無ではなく、それが変化に比例するか、記事の総数に比例するかである。
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

# 印が付いてから掃き出すまでに待つ秒数。新しい記事には投票が一斉に付くため、1 件ごとに取得し直すと同じメッセージを何度も取りに行くことになる。ここで待つぶん反映は遅れるが、待つのは数秒であり、点数の集計に影響しない。
DEBOUNCE_SECONDS: Final = 5.0

# 設定そのものを読み取れなかったときに次の周回まで待つ秒数。本来の間隔は設定の中にあるため、それが読めない間は本来の間隔を知る術がない。DB の瞬断や初期値の未投入で読めない状態は復旧しうるので、短めの一定間隔で読み直す。
SETTINGS_RETRY_SECONDS: Final = 30

# 照合の対象。送信済みで、まだ対象の期間内にある通知。
_RECONCILE_TARGETS_SQL: Final = """
    SELECT message_id, thread_id
      FROM article_discord_posts
     WHERE message_id IS NOT NULL
       AND posted_at >= NOW() - INTERVAL %s DAY
"""

# 印の付いた id から通知の行を引く。message_id には一意索引があるため、guild 中のどのメッセージが届いてもこの 1 文で落とせる。
_RESOLVE_POSTS_SQL: Final = """
    SELECT id, article_id, channel_id, message_id
      FROM article_discord_posts
     WHERE message_id IN ({placeholders})
"""

# 同じ id をコメントとしても引く。1 つの id が両方に当たることはない(通知そのものはコメントとして写さない)。
_RESOLVE_COMMENTS_SQL: Final = """
    SELECT c.id, c.message_id, p.thread_id AS channel_id
      FROM article_comments c
      JOIN article_discord_posts p ON p.id = c.post_id
     WHERE c.message_id IN ({placeholders})
"""

_UPDATE_POST_SQL: Final = 'UPDATE article_discord_posts SET up_count = %s, down_count = %s, last_synced_at = NOW() WHERE id = %s'

_UPDATE_COMMENT_SQL: Final = 'UPDATE article_comments SET up_count = %s, down_count = %s, last_synced_at = NOW() WHERE id = %s'


class CountMirror:
    """投票の数の写し。イベントは印を付けるだけで、1 本のワーカが取得し直して上書きする。"""

    def __init__(self, client: discord.Client, db: Database, store: SettingsStore, comments: CommentMirror) -> None:
        self._client = client
        self._db = db
        self._store = store
        self._comments = comments
        # 数え直すべきメッセージ(通知そのものとコメントの両方)。対象外の id が混ざっても、行を引く時点で落ちるだけであるため、リスナ側で guild を選り分けない。
        self._dirty_messages: set[int] = set()
        # 履歴を読み直すべきスレッド。
        self._dirty_threads: set[int] = set()
        self._marked = asyncio.Event()
        self._reconcile_requested = asyncio.Event()

    def mark_message(self, message_id: int) -> None:
        """投稿に付いた票が変わったかもしれないことを記録する(リアクションの増減・全消し)。"""
        self._dirty_messages.add(message_id)
        self._marked.set()

    def mark_thread(self, thread_id: int) -> None:
        """スレッドの履歴を読み直すべきことを記録する(取りこぼしたコメントの補正)。"""
        self._dirty_threads.add(thread_id)
        self._marked.set()

    def request_reconcile(self, reason: str) -> None:
        """次の照合を待たずに照合を促す。イベントの取りこぼしが疑われる場面から呼ぶ。"""
        logger.info('投票とコメントの照合を要求しました(理由: %s)。', reason)
        self._reconcile_requested.set()

    # ------------------------------------------------------------------
    # 掃き出し
    # ------------------------------------------------------------------

    async def run_drain_loop(self) -> None:
        """印の付いた投稿を数え直し続ける。この bot で投票の数を書くのはこのループだけである。

        例外はここで握り潰して次の周回へ進む。1 回の失敗(Discord の一時的な障害、DB の瞬断)でこのループが死ぬと、以降どの記事の点数も二度と更新されない。掃き出しに失敗した印は失われるが、照合が対象範囲の通知に印を付け直すため、その回の取りこぼしはそこで正される。
        """
        await self._client.wait_until_ready()
        while True:
            await self._marked.wait()
            # 一斉に届く印をまとめる。この間に届いた印も同じ 1 回で処理される。
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._marked.clear()
            # 掃き出す前に入れ替える。取得している最中に届いた印は次の集合へ入り、次の周回で数え直される(取得より後に付いた票を取りこぼさないため)。ここに await を挟むと、その隙に届いた印が捨てられる。
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
        """1 つの投稿の票を数え直して上書きする。

        引けなかった投稿は最後に数えた値を残す。記事が集めた反応は、通知の投稿が消されたからといって無かったことにはならず、0 で上書きすると報道社ランキングから過去の実績が消える。
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
            # 投稿が削除された場合。コメントであれば削除のイベントで行ごと消えるため、ここへ来るのは通知そのものが消されたときである。最後に数えた値を残す。
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

    # ------------------------------------------------------------------
    # 照合
    # ------------------------------------------------------------------

    async def run_reconcile_loop(self) -> None:
        """照合を要求されたとき、または count_reconcile_interval_seconds が過ぎたときに、対象範囲の通知へ印を付け直す。

        例外はここで握り潰して次の周回へ進む。設定を読めなかっただけでこのループが死ぬと、以降イベントの取りこぼしが二度と正されなくなる。
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
            # 要求を待ってから照合する。起動直後の 1 回目は on_ready の要求で走る。
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
        """対象範囲の通知とスレッドすべてに印を付ける。取得と上書きは掃き出しのワーカが行う。

        ここで印を付けるだけに留めるのは、書き手を 1 つに保つためである。照合が自分で上書きすると、掃き出しのワーカと同じ行を並行して書き、取得し直した古い値がイベント由来の新しい値を消しうる。印であれば何度付けても結果は変わらない。
        """
        rows = await self._db.fetch_all(_RECONCILE_TARGETS_SQL, settings.discord.bot.count_reconcile_window_days)
        for row in rows:
            self.mark_message(int(row['message_id']))
            if row['thread_id'] is not None:
                self.mark_thread(int(row['thread_id']))
        logger.info('公開から %d 日以内の通知 %d 件に照合の印を付けました。', settings.discord.bot.count_reconcile_window_days, len(rows))
