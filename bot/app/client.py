"""Discord クライアントと、ゲートウェイのイベントの受け口。

この bot が Discord から受け取るものは、すべてここに集める。各リスナは自分では何も決めず、写しを保つ 2 つの部品(GuildMirror / CountMirror)へ渡すだけである。どのイベントを購読しているかが 1 箇所で読み取れないと、intents の不足で「例外も出ないのに何も届かない」状態を見つけられない。

起動するタスク:
  delivery                    … 公開された記事の通知とタグ購読者への DM を送る(DB の待ち行列を周期的に見に行く)。
  roles.reconcile             … ロール・メンバー・オーナーの照合。
  counts.drain                … 印の付いた投稿の投票を数え直し、スレッドの履歴からコメントを補正する。
  counts.reconcile            … 投票とコメントの照合。

delivery だけが周期的に「見に行く」作りなのは、それを駆動するイベントが Discord に無いためである。記事を公開したのは server であり、Discord はそれを知らない。残りはすべて Discord 側の出来事であり、イベントとして届く。
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
    """Discord の時刻を、DATETIME 列に合わせて UTC の naive へ落とす(規約は server/app/schemas.py と同じ)。"""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class NewsBot(discord.Client):
    """送信ループとゲートウェイのイベントのリスナを抱える Discord クライアント。

    DB と設定は起動側が用意して渡す。トークンは settings 表にあり、それを読むには Discord へ繋ぐ前から DB が要るため、DB の接続と後始末はこのクラスではなく起動側が持つ。
    """

    def __init__(self, config: Config, db: Database, settings: SettingsStore) -> None:
        # 必要な intents だけを有効にする。有効にしていないイベントは、例外も警告も無く、ただ届かない。
        #   guilds         … ロールとチャンネルの解決、ロールの作成・更新・削除、guild の更新(オーナーの移譲)。
        #   members        … メンバーの参加・離脱・更新と、その取り寄せ。特権 intent であり、Discord の開発者ポータルで有効にする必要がある。
        #   guild_messages … スレッドの返信の投稿・編集・削除。
        #   guild_reactions… リアクションの追加・削除・全消し。投票はこれで届く。
        #   message_content… コメントの本文。2 つ目の特権 intent であり、開発者ポータルで有効にする必要がある。
        # DM 側(dm_messages / dm_reactions)は有効にしない。通知もスレッドも guild の中にあり、購読 DM は送るだけで反応を数えない。
        #
        # 特権 intent は、要求しておいて開発者ポータルで有効にしていないと Discord がゲートウェイを切る。したがって設定漏れは起動時に PrivilegedIntentsRequired として必ず露見する(main.py がその案内を出す)。逆に、要求しなければ何も起きずに本文だけが空文字で届くため、本文が要るなら必ず要求する側に倒す。
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.guild_messages = True
        intents.guild_reactions = True
        intents.message_content = True
        # メッセージのキャッシュを持たない。この bot はリアクションと削除を raw のイベントで受け、必要なメッセージはそのつど取得し直すため、キャッシュ上のメッセージを一度も読まない。既定では直近 1000 件を抱え続けることになり、使わないものにメモリを払うだけである。
        # raw でないイベント(on_reaction_add など)はキャッシュに載っているメッセージでしか発火しない。この bot が追う通知は最大で count_reconcile_window_days 日前のものでありキャッシュに載っていないため、そもそも当てにできない。それらを使っていないことが、この指定で明示される。
        super().__init__(intents=intents, max_messages=None)
        self.config = config
        self.db = db
        self.settings = settings
        self.roles = GuildMirror(self, db, settings)
        self.comments = CommentMirror(self, db)
        self.counts = CountMirror(self, db, settings, self.comments)
        self._tasks: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        """接続の確立後、ゲートウェイの受信を始める前に呼ばれる。ここで常駐タスクを起動する。

        照合をこの 4 本のうちの 1 つとして持つのは、イベントのリスナの中で照合を始めると、ゲートウェイが短時間に何度も張り直したときに照合が積み重なるためである。リスナは要求を出すだけで、走らせるのは常にこのタスクである。

        各タスクは設定を周回のたびに読み直すため、ここで渡すのは値ではなく SettingsStore である。
        """
        self._tasks = [
            asyncio.create_task(run_delivery_loop(self, self.db, self.config, self.settings), name='delivery'),
            asyncio.create_task(self.roles.run_reconcile_loop(), name='role_reconcile'),
            asyncio.create_task(self.counts.run_drain_loop(), name='count_drain'),
            asyncio.create_task(self.counts.run_reconcile_loop(), name='count_reconcile'),
        ]

    async def close(self) -> None:
        """常駐タスクを止め、Discord との接続を閉じる。DB を開いたのは起動側であるため、閉じるのも起動側が行う。"""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            # キャンセルが行き渡るのを待つ。待たずに接続を閉じると、送信や照合の途中だったタスクが閉じたコネクションを掴んで無用な例外を出す。
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await super().close()

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        """リスナが送出した例外の後始末。

        discord.py は例外を握り潰して次のイベントへ進み、Discord はそのイベントを再送しない。すなわち失敗した 1 件の変更は、放っておくと永久に写しへ入らない。ここで照合を促すことで、握り潰されたイベントが次の照合の間隔ではなく直ちに正される。DB が落ちている間は照合も失敗するが、要求は 1 回にまとまるため、復旧後の最初の照合で正される。
        """
        logger.exception('イベント %s の処理で例外が発生しました。取りこぼした変更を補正するため照合を要求します。', event_method)
        self.roles.request_reconcile(f'{event_method} の処理に失敗したため')
        self.counts.request_reconcile(f'{event_method} の処理に失敗したため')

    # ------------------------------------------------------------------
    # 接続
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        """ゲートウェイの新しいセッションが確立したときに呼ばれる。

        再接続のたびに呼ばれるわけではない。回線が一時的に切れただけなら discord.py は同じセッションを再開(RESUME)し、その間のイベントは Discord が再送するため、呼ばれるのは on_resumed であり補正は要らない。セッションを張り直した(再 IDENTIFY した)場合はその間のイベントが失われているため、ここで照合を要求する。bot が停止していた間の変更もこれで補正される。

        1 つのプロセスで何度も呼ばれうる(Discord がセッションを無効にするたびに起こる)。照合は何度要求しても 1 回にまとまり、走らせるのは常に 1 本のタスクであるため、ここでは重複を気にしなくてよい。
        """
        logger.info('Discord へ接続しました: %s。', self.user)
        self.roles.request_reconcile('ゲートウェイの新しいセッションを確立したため')
        self.counts.request_reconcile('ゲートウェイの新しいセッションを確立したため')

    async def on_guild_available(self, guild: discord.Guild) -> None:
        """Discord 側の障害などで見えなくなっていた guild が戻ったときに呼ばれる。

        見えなかった間のイベントは届いていないが、セッションは張り直されないため on_ready は呼ばれない。ここで要求しないと、その間の変更が次の照合の間隔まで(あるいは再起動まで)残る。
        """
        self.roles.request_reconcile(f'guild {guild.id} が利用可能に戻ったため')
        self.counts.request_reconcile(f'guild {guild.id} が利用可能に戻ったため')

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """bot が guild へ招待されたときに呼ばれる。対象の guild であれば、その時点の全ロール・全メンバーを写す。"""
        self.roles.request_reconcile(f'guild {guild.id} へ参加したため')

    # ------------------------------------------------------------------
    # ロール・メンバー・オーナー
    # ------------------------------------------------------------------

    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.roles.apply_role_upsert(role)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        # 権限(permissions)の変更もここで届く。ロールを持つ人が変わらなくても、そのロールが Administrator を得た・失った瞬間に、保有者全員の管理者資格が変わる。
        await self.roles.apply_role_upsert(after)

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.roles.apply_role_delete(role)

    async def on_member_join(self, member: discord.Member) -> None:
        await self.roles.apply_member_upsert(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # ロールの付与・剥奪はここで届く。イベントは変更後の全ロールを運ぶため、差分を自分で求めずに after をそのまま写す。
        await self.roles.apply_member_upsert(after)

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        # 非 raw の on_member_remove はキャッシュに居るメンバーの離脱でしか呼ばれない。離脱は権限の剥奪そのものであり、キャッシュの状態に左右されてはならないため raw で受ける。
        await self.roles.apply_member_remove(payload.user, payload.guild_id)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        # 表示名・アバターの変更。記事の署名に使うため写す。
        await self.roles.apply_user_update(after)

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        # オーナーの移譲だけを見る。guild の名前やアイコンは写していない。
        if before.owner_id != after.owner_id:
            await self.roles.apply_owner_change(after)

    # ------------------------------------------------------------------
    # 投票とコメント
    #
    # どのリスナも印を付けるだけで、数を読み書きしない(理由は counts の docstring を参照)。
    # 対象外の guild やスレッドの id が混ざっても、行を引く時点で落ちるだけであるため、ここでは選り分けない。
    # ------------------------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        self.counts.mark_message(payload.message_id)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        self.counts.mark_message(payload.message_id)

    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        # メッセージのリアクションが全て外された。
        self.counts.mark_message(payload.message_id)

    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        # ある絵文字のリアクションだけが全て外された。何件外れたかは運ばれないため、数え直すほかない。
        self.counts.mark_message(payload.message_id)

    async def on_message(self, message: discord.Message) -> None:
        # スレッドへの投稿だけがコメントである。通知そのもの(親チャンネルのメッセージ)は開始メッセージであり、スレッドの中には現れない。
        if not await self.comments.apply_upsert(message):
            return
        # 写せたということは記事のコメント欄への投稿である。読者が 1 押しで投票できるよう、ここで絵文字を付ける。
        if await votes.seed(message):
            await self.comments.mark_seeded(message.id)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # コメントの編集。payload.message は discord.py が組み立てた Message であり、content は必ず str である(本文を運ばない部分的な更新であっても、この属性は空文字になるだけで欠けない)。写していない投稿の編集なら 0 行が更新されて終わる。
        await self.comments.apply_edit(payload.message_id, payload.message.content, _naive(payload.message.edited_at))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        # 消されたのが通知そのものか、コメントかは、ここでは区別しない。両方の可能性を伝え、どちらの行が該当するかは引く時点で決まる。
        await self.comments.apply_delete([payload.message_id])
        self.counts.mark_message(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        # まとめて削除された場合。1 件ずつの削除イベントは届かないため、これを購読しないと消えたコメントが残り続ける。
        await self.comments.apply_delete(list(payload.message_ids))
        for message_id in payload.message_ids:
            self.counts.mark_message(message_id)
