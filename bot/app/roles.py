"""guild のロール・メンバー・オーナーの写し。

真実のソースは Discord であり、DB が持つのはその写しである。閲覧範囲の判定と API キーの資格判定はこの写しを参照するため、写しが古いままだと、guild を抜けた人が限定記事を読み続けたり、管理者を外れた人のキーが通り続けたりする。

写しは 2 つの経路で保たれる。

  ・ゲートウェイのイベント(ロールの作成・更新・削除、メンバーの参加・離脱・更新、オーナーの移譲)。Discord 側の変更が起きた時点で届くため、ロールを外した瞬間に権限が失効する。常態ではこちらだけが働く。
  ・照合(reconcile)。guild の現在の全ロール・全メンバーを読んで写しを丸ごと突き合わせる。イベントの取りこぼしを正すためにあり、bot の再起動時・ゲートウェイの新しいセッションの確立時・guild が利用可能に戻った時、そして guild_reconcile_interval_seconds ごとに走る。

照合を残すのは、ゲートウェイのイベントが at-most-once であるためである。discord.py はリスナが送出した例外を握り潰して次へ進み(Client._run_event)、Discord はそのイベントを再送しない。したがって DB が一瞬でも応答しなければ、その 1 件の変更は永久に写しへ入らない。ゲートウェイの接続が生きたままなら新しいセッションも張られず、再起動するまで誰もそれに気付かない。「ロールを外すことが権限の剥奪である」という約束を守るには、イベントの取りこぼしが必ず有限時間で正される必要がある。照合の間隔の上限がその有限時間そのものである。

イベントが運ぶのは差分ではなく現在の状態である(メンバーの更新イベントは変更後の全ロールを、ロールの更新イベントはロールの全属性を運ぶ)。したがって各リスナは受け取った状態をそのまま書けばよく、加減算をしない。同じイベントが 2 度届いても、順序が入れ替わっても、結果は変わらない。

リスナと照合は同じ行を書くため、1 つの Lock で直列化する。並行して走ると、照合が読んだ時点より後に届いたイベントの結果を、照合の書き込みが古い値で上書きしうる(例: 照合がメンバーの一覧を読んだ直後にロールが剥奪され、リスナがそれを消し、その後に照合が「まだ持っている」という写しを書く)。Lock を取ってから guild の状態を読むことで、読んだ時点までに届いたイベントは必ず反映済みとなり、この上書きは起こらない。discord.py はイベントを配送する前に自身のキャッシュを更新するためである。

discord_roles には permissions(Discord の権限ビットフィールド)を必ず含めて写す。これが無いと「このロールは Administrator を持つか」を知る手段が無く、管理者権限に基づく API キーの発行資格を判定できない。

Discord でロールを削除すると discord_roles の行も消え、外部キーにより user_roles と article_visibility_roles の行が連鎖して消える。一方 publishers.role_id は ON DELETE SET NULL であり、報道社の行そのものは残る。解散した報道社の過去記事の署名が失われてはならないためであり、この前提をここで壊してはならない(報道社を消しに行かない)。

user_roles の書き換えは 1 つのトランザクションで行う。ロールの入れ替えの途中の状態が他の接続から見えると、その瞬間に届いた要求が「ロールを 1 つも持たない利用者」として扱われ、閲覧できるはずの記事を拒否されうる。

そのうえで、書くのは差分だけに限る。全消し→再投入でも結果は同じであり、トランザクションの中である限り途中の状態も見えないが、変更の無い行まで毎回書き換えることになり、同じ行を書くログイン時のロール更新(server 側)と無用に競合して、双方の待ちとデッドロックの機会を増やすだけである。
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

# 1 文への詰め込み過ぎを避けつつ、1 行ずつの往復も避けるための区切り。
_CHUNK_SIZE: Final = 500

# 設定そのものを読み取れなかったときに次の周回まで待つ秒数。本来の間隔は設定の中にあるため、それが読めない間は本来の間隔を知る術がない。DB の瞬断や初期値の未投入で読めない状態は復旧しうるので、短めの一定間隔で読み直す。
SETTINGS_RETRY_SECONDS: Final = 30

# メンバーの取り寄せを待つ上限。ゲートウェイの往復であり、応答が返らないまま待ち続けると照合が二度と走らなくなる。
CHUNK_TIMEOUT_SECONDS: Final = 120

# 重複時に参照する新しい行は別名で受ける(ON DUPLICATE KEY UPDATE の VALUES() は非推奨のため使わない)。
_UPSERT_ROLE_SQL: Final = """
    INSERT INTO discord_roles (id, name, color, position, permissions) VALUES {values} AS new
    ON DUPLICATE KEY UPDATE
        name = new.name, color = new.color, position = new.position, permissions = new.permissions
"""

# is_guild_member は常に 1 で上書きする。一度 guild を抜けて戻ってきた利用者をここで復帰させるため、新しい行の値ではなく定数を書く。
_UPSERT_USER_SQL: Final = """
    INSERT INTO users (discord_user_id, username, global_name, avatar_hash, is_guild_member) VALUES {values} AS new
    ON DUPLICATE KEY UPDATE
        username = new.username, global_name = new.global_name, avatar_hash = new.avatar_hash, is_guild_member = 1
"""


def _chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _placeholders(count: int, columns: int = 1) -> str:
    """個数分のプレースホルダを組み立てる。値そのものは必ず引数として渡すため、ここで組み立てるのは %s の並びだけである。"""
    if columns == 1:
        return ', '.join(['%s'] * count)
    row = '(' + ', '.join(['%s'] * columns) + ')'
    return ', '.join([row] * count)


def desired_role_ids(member: discord.Member, guild_id: int) -> set[int]:
    """user_roles へ写すべきロール ID。

    @everyone(ID は guild の ID と同じ)を除く。@everyone は Discord のメンバー情報の roles には現れないが discord.Member.roles には現れ、ログイン時に OAuth2 で取得するロールにも含まれない。ここだけが積むと、イベントの反映・照合・ログイン時のロール更新が互いの結果を打ち消し合い、同じ利用者の行が付いたり消えたりし続ける。写す側の判断が 1 箇所に割れないよう、この関数以外で member.roles を user_roles へ写さない。
    """
    return {role.id for role in member.roles if role.id != guild_id}


class GuildMirror:
    """ロール・メンバー・オーナーの写しを保つ。

    イベントのリスナと照合のループの両方をここに置く。両者は同じ行を書き、同じ Lock で直列化されるため、離して置くと不変条件が守られていることを読み取れなくなる。
    """

    def __init__(self, client: discord.Client, db: Database, store: SettingsStore) -> None:
        self._client = client
        self._db = db
        self._store = store
        # イベントの反映と照合を直列化する。詳細はモジュールの docstring を参照。
        self._lock = asyncio.Lock()
        # 照合を要求する合図。set() は何度呼んでも 1 回の照合にまとまるため、要求の重複を呼び出し側が気にしなくてよい。
        self._reconcile_requested = asyncio.Event()

    def request_reconcile(self, reason: str) -> None:
        """次の照合を待たずに照合を促す。イベントの取りこぼしが疑われる場面から呼ぶ。"""
        logger.info('ロールの照合を要求しました(理由: %s)。', reason)
        self._reconcile_requested.set()

    # ------------------------------------------------------------------
    # 照合
    # ------------------------------------------------------------------

    async def run_reconcile_loop(self) -> None:
        """照合を要求されたとき、または guild_reconcile_interval_seconds が過ぎたときに照合する。

        照合を 1 本のタスクが行うことで、要求が重なっても同時に 2 つの照合が走らない。イベントを配送するタスクの中で照合を始める作りにすると、ゲートウェイが短時間に何度も張り直したときに照合が積み重なり、互いの行ロックを奪い合って全て失敗しうる。

        例外はここで握り潰して次の周回へ進む。設定を読めなかっただけでこのループが死ぬと、以降イベントの取りこぼしが二度と正されなくなり、guild を抜けた人が限定記事を読み続け、管理者を外れた人のキーが通り続ける。
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
            # 要求を待ってから照合する。起動直後の 1 回目は on_ready の要求で走るため、ここで待つことが起動を遅らせることはない。
            try:
                await asyncio.wait_for(self._reconcile_requested.wait(), timeout=interval)
            except TimeoutError:
                pass
            # 照合を始める前に下ろす。照合の最中に届いた要求は、その照合が読んだ時点より後の変更を指しているため、次の周回でもう一度照合する必要がある。
            self._reconcile_requested.clear()
            try:
                await self.reconcile(settings)
            except Exception:
                logger.exception('ロールの照合で例外が発生しました。%d 秒後に再試行します。', interval)

    async def reconcile(self, settings: RuntimeSettings) -> None:
        """対象 guild のロール・メンバー・オーナーを丸ごと突き合わせる。"""
        guild = self._client.get_guild(settings.discord.guild_id)
        if guild is None:
            logger.error('bot が対象の guild %s に参加していないため、ロールを照合できません。bot を招待し、設定の discord.guild_id を確認してください。', settings.discord.guild_id)
            return

        if not guild.chunked:
            # 通常 discord.py が接続時に取り寄せ、以後はイベントで最新に保つため、ここへ来るのは取り寄せが間に合わなかった場合だけである。メンバーの一覧が欠けたまま照合すると、居るはずの人を「guild を抜けた」と誤って書いてしまう。
            # Lock の外で待つ。ゲートウェイの往復であり、Lock を持ったまま待つとその間イベントの反映が全て止まる。
            try:
                await asyncio.wait_for(guild.chunk(cache=True), timeout=CHUNK_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.error('guild %s のメンバーを %d 秒以内に取り寄せられなかったため、照合を中止します。次の周回で再試行します。', guild.id, CHUNK_TIMEOUT_SECONDS)
                return

        async with self._lock:
            # guild の状態は必ず Lock の中で読む。外で読んだ一覧を使うと、読んでから書くまでの間に届いたイベントの結果を古い値で上書きしうる(理由はモジュールの docstring を参照)。
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
        """ロールを upsert し、Discord から消えたロールを削除する。削除した件数を返す。"""
        await self._upsert_roles(conn, roles)
        role_ids = [role.id for role in roles]
        # 消えたロールを落とす。user_roles と article_visibility_roles は外部キーで連鎖して消えるが、publishers は role_id が NULL になるだけで行は残る(解散した報道社の過去記事の署名を守るため)。
        sql = f'DELETE FROM discord_roles WHERE id NOT IN ({_placeholders(len(role_ids))})'
        return await self._db.execute(sql, *role_ids, conn=conn)

    async def _reconcile_members(self, conn: Connection, guild: discord.Guild, members: Sequence[discord.Member]) -> None:
        """メンバーを users へ upsert し、user_roles・is_guild_member・is_guild_owner を洗い替える。

        guild の全メンバーを写すのは、未ログインの人も users に居る必要があるため。articles:write:any で他人を著者に指定するとき、その相手が実在しなければ記事を保存できない。
        """
        await self._upsert_members(conn, members)

        member_ids = {member.id for member in members}
        existing = await self._db.fetch_all('SELECT id, discord_user_id, is_guild_member, is_guild_owner FROM users', conn=conn)
        user_id_by_discord_id = {int(row['discord_user_id']): int(row['id']) for row in existing}

        # 「居なくなった人」はイベントでは分からない。Discord は bot が停止している間に起きた離脱を、復帰後に教えてくれるわけではないため、この洗い替えだけが唯一の検出手段である。
        departed = [int(row['id']) for row in existing if row['is_guild_member'] and int(row['discord_user_id']) not in member_ids]
        for chunk in _chunks(departed, _CHUNK_SIZE):
            sql = f'UPDATE users SET is_guild_member = 0 WHERE id IN ({_placeholders(len(chunk))})'
            await self._db.execute(sql, *chunk, conn=conn)
            # 抜けた人のロールも必ず落とす。閲覧範囲の判定は管理者かどうかを is_guild_member より先に見るため、ロールを残すと、抜けた管理者が限定記事を読み続けられる。
            sql = f'DELETE FROM user_roles WHERE user_id IN ({_placeholders(len(chunk))})'
            await self._db.execute(sql, *chunk, conn=conn)
        if departed:
            logger.info('guild を抜けた利用者 %d 人の is_guild_member とロールを落としました。', len(departed))

        await self._reconcile_owner(conn, guild, existing, user_id_by_discord_id)
        await self._reconcile_user_roles(conn, guild, members, user_id_by_discord_id)

    async def _reconcile_owner(self, conn: Connection, guild: discord.Guild, existing: Sequence[dict[str, Any]], user_id_by_discord_id: dict[int, int]) -> None:
        """オーナーの印を洗い替える。

        現在のオーナー以外の is_guild_owner を必ず落とす。オーナーは移譲できるため、立てるだけで落とさないと、譲った側が全権を持ったまま残る。オーナーはロールに依らず管理者として扱われるため、これは権限の取り消しが効かない状態になる。
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
        """user_roles を現在の保有状況へ洗い替える。

        差分だけを適用する。毎回すべて消して入れ直しても結果は同じだが、照合のたびに表を全て書き換えることになり、ログイン時のロール更新と無用に競合する。
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

        # guild を抜けた利用者はここで desired に現れないため、保有していたロールが全て削除される。
        # 残すと、抜けた後もロール限定の記事を読み続けられてしまう。
        to_delete = [(user_id, role_id) for user_id, role_ids in current.items() for role_id in role_ids - desired.get(user_id, frozenset())]
        to_insert = [(user_id, role_id) for user_id, role_ids in desired.items() for role_id in role_ids - current.get(user_id, frozenset())]

        for chunk in _chunks(to_delete, _CHUNK_SIZE):
            params = [value for pair in chunk for value in pair]
            sql = f'DELETE FROM user_roles WHERE (user_id, role_id) IN ({_placeholders(len(chunk), 2)})'
            await self._db.execute(sql, *params, conn=conn)

        for chunk in _chunks(to_insert, _CHUNK_SIZE):
            params = [value for pair in chunk for value in pair]
            # ログイン時のロール更新と同時に走っても衝突しないよう IGNORE を付ける。
            sql = f'INSERT IGNORE INTO user_roles (user_id, role_id) VALUES {_placeholders(len(chunk), 2)}'
            await self._db.execute(sql, *params, conn=conn)

        if to_delete or to_insert:
            logger.info('user_roles を照合しました(付与 %d 件、剥奪 %d 件)。', len(to_insert), len(to_delete))

    # ------------------------------------------------------------------
    # イベントの反映
    #
    # 各リスナは対象 guild のイベントだけを扱う。bot が他の guild にも居る場合、その guild のロールを写すと、閲覧範囲の判定が別の guild のロール ID を根拠にしてしまう。
    # ------------------------------------------------------------------

    async def _target_guild_id(self) -> int:
        return (await self._store.snapshot()).discord.guild_id

    async def apply_role_upsert(self, role: discord.Role) -> None:
        """ロールの作成・更新を写す。permissions の変更もここで届く(Administrator の付け外しは、そのロールを持つ全員の資格を一度に変える)。"""
        if role.guild.id != await self._target_guild_id():
            return
        async with self._lock:
            await self._upsert_roles(None, [role])
        logger.info('ロール %s(%s)を写しました。', role.id, role.name)

    async def apply_role_delete(self, role: discord.Role) -> None:
        """ロールの削除を写す。user_roles と article_visibility_roles は外部キーで連鎖して消え、publishers.role_id は NULL になる(報道社の行は残す)。"""
        if role.guild.id != await self._target_guild_id():
            return
        async with self._lock:
            await self._db.execute('DELETE FROM discord_roles WHERE id = %s', role.id)
        logger.info('ロール %s(%s)の削除を写しました。', role.id, role.name)

    async def apply_member_upsert(self, member: discord.Member) -> None:
        """メンバーの参加・更新を写す。イベントは変更後の全ロールを運ぶため、差分ではなくその状態をそのまま書く。"""
        if member.guild.id != await self._target_guild_id():
            return
        async with self._lock:
            async with self._db.transaction() as conn:
                # 保有ロールより先にロールそのものを写す。新しく作られたロールが同じ瞬間に付与された場合、discord_roles にまだ無いと user_roles の外部キーが通らず、その付与だけが静かに落ちる。
                await self._upsert_roles(conn, member.roles)
                await self._upsert_members(conn, [member])
                user_id = await self._user_id(conn, member.id)
                if user_id is None:
                    logger.warning('Discord ユーザー %s を users へ写せなかったため、ロールを反映できません。', member.id)
                    return
                await self._apply_user_roles(conn, user_id, desired_role_ids(member, member.guild.id))

    async def apply_member_remove(self, user: discord.User | discord.Member, guild_id: int) -> None:
        """メンバーの離脱を写す。is_guild_member を落とすだけでなく、保有ロールも必ず消す。

        閲覧範囲の判定は「管理者かどうか」を is_guild_member より先に見る。ロールを残したまま is_guild_member だけを落とすと、guild を追い出された管理者が全ての下書きを読み続けられる。
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
        """利用者の表示名・アバターの変更を写す。記事の署名に使うため、古いままだと改名前の名前で表示され続ける。

        users に居ない利用者は写さない。guild のメンバーでない誰かの改名まで拾う必要はなく、行を作ると「guild に居ないのに users に居る」行が増えるだけである。
        """
        avatar_hash = user.avatar.key if user.avatar is not None else None
        async with self._lock:
            await self._db.execute(
                'UPDATE users SET username = %s, global_name = %s, avatar_hash = %s WHERE discord_user_id = %s',
                user.name, user.global_name, avatar_hash, user.id,
            )

    async def apply_owner_change(self, guild: discord.Guild) -> None:
        """オーナーの移譲を写す。譲った側を落とし、譲られた側を立てるまでを 1 つのトランザクションで行う。

        分けて書くと、その間にオーナーが 2 人居る状態(あるいは 0 人の状態)が他の接続から見える。オーナーはロールに依らず管理者であるため、前者は権限が消えない瞬間を、後者は正当な管理者が締め出される瞬間を作る。
        """
        if guild.id != await self._target_guild_id():
            return
        async with self._lock:
            async with self._db.transaction() as conn:
                existing = await self._db.fetch_all('SELECT id, discord_user_id, is_guild_member, is_guild_owner FROM users', conn=conn)
                user_id_by_discord_id = {int(row['discord_user_id']): int(row['id']) for row in existing}
                await self._reconcile_owner(conn, guild, existing, user_id_by_discord_id)
        logger.info('guild %s のオーナーが Discord ユーザー %s へ移ったことを写しました。', guild.id, guild.owner_id)

    # ------------------------------------------------------------------
    # 共通の書き込み
    # ------------------------------------------------------------------

    async def _upsert_roles(self, conn: Connection | None, roles: Sequence[discord.Role]) -> None:
        for chunk in _chunks(list(roles), _CHUNK_SIZE):
            params: list[Any] = []
            for role in chunk:
                # permissions を含めるのは必須。管理者資格の判定がこの値だけを根拠にしている。
                params.extend([role.id, role.name, role.colour.value, role.position, role.permissions.value])
            sql = _UPSERT_ROLE_SQL.format(values=_placeholders(len(chunk), 5))
            await self._db.execute(sql, *params, conn=conn)

    async def _upsert_members(self, conn: Connection | None, members: Sequence[discord.Member]) -> None:
        for chunk in _chunks(list(members), _CHUNK_SIZE):
            params: list[Any] = []
            for member in chunk:
                # Member.avatar は guild 固有のアバターではなくアカウントのアバターを指す。users が持つのはアカウント側の情報であるため、こちらを写す。
                avatar_hash = member.avatar.key if member.avatar is not None else None
                params.extend([member.id, member.name, member.global_name, avatar_hash, 1])
            sql = _UPSERT_USER_SQL.format(values=_placeholders(len(chunk), 5))
            await self._db.execute(sql, *params, conn=conn)

    async def _user_id(self, conn: Connection, discord_user_id: int) -> int | None:
        value = await self._db.fetch_val('SELECT id FROM users WHERE discord_user_id = %s', discord_user_id, conn=conn)
        return int(value) if value is not None else None

    async def _apply_user_roles(self, conn: Connection, user_id: int, desired: set[int]) -> None:
        """1 人の保有ロールを desired へ揃える。差分だけを書く(全消し→再投入をしない理由はモジュールの docstring を参照)。"""
        current = {int(row['role_id']) for row in await self._db.fetch_all('SELECT role_id FROM user_roles WHERE user_id = %s', user_id, conn=conn)}
        to_delete = current - desired
        to_insert = desired - current
        if to_delete:
            sql = f'DELETE FROM user_roles WHERE user_id = %s AND role_id IN ({_placeholders(len(to_delete))})'
            await self._db.execute(sql, user_id, *to_delete, conn=conn)
        if to_insert:
            params = [value for role_id in to_insert for value in (user_id, role_id)]
            # ログイン時のロール更新と同時に走っても衝突しないよう IGNORE を付ける。
            sql = f'INSERT IGNORE INTO user_roles (user_id, role_id) VALUES {_placeholders(len(to_insert), 2)}'
            await self._db.execute(sql, *params, conn=conn)
