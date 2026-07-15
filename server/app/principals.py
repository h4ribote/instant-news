"""認証主体(セッション / API キー)の表現と、権限の判定。

セッションと API キーという 2 つの認証経路を Principal という 1 つの型へ畳む。以降の層は「どうやって認証したか」ではなく「誰が、何を持っているか」だけを見ればよくなる。

**API キーの権限を発行時に凍結してはならない。**キーは所有者の権限を機械が借りたものであり、キー自身が権限を持つわけではない。権限は要求のたびに users / user_roles の現在のスナップショットから判定する。凍結すると、管理者を外れた者のキーが管理権限を持ち続け、ロールを外すという最も自然な対処が効かなくなる。この設計のため、ロールの同期(bot が行う)が権限剥奪の実効手段になっている。

秘密(セッショントークン・API キー)の生値は保存しない。DB には SHA-256 だけを置き、照合は生値を同じ手順で要約して突き合わせる。DB が漏れても、そこからセッションやキーを復元して成りすますことはできない。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from aiomysql import Connection

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings

# Discord の権限ビットフィールドにおける Administrator。このビットを立てたロールの保有者は、その guild の全操作を行える。
ADMINISTRATOR_BIT: Final = 1 << 3

# 値の集合の真実は migrations/0001_initial.sql の api_token_scopes.scope にある。
Scope = Literal['articles:write', 'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin']

# 管理者資格を要求するスコープ。キーにこれらが付いているだけでは足りず、要求の時点で所有者が管理者であることを毎回確かめる。
# settings:admin は admin.role_ids を書き換えられる、すなわち管理者の資格そのものを配れるため、他のどのスコープよりも強い。それでも特別扱いを要さないのは、この集合に入れること自体が「所有者が今この瞬間に管理者であること」を要求のたびに問う仕掛けだからである。管理者を外れた者の settings:admin のキーは、その時点で自分に管理ロールを付け直すこともできなくなる。
ADMIN_SCOPES: Final[frozenset[str]] = frozenset({'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin'})

# スコープの含意。鍵が要求されたスコープ、値がその要求を成立させうるスコープの集合である。
# articles:write:any は「著者・報道社を自由に指定した記事の作成・更新」であり、著者を所有者に固定した作成・更新(articles:write)を包含する。含意を持たないと、この 1 つだけを与えたキーが記事を 1 本も書けないという、スコープの定義と正反対の状態になる。
# 含意で要求を通した場合でも、通した側のスコープに応じた管理者資格の検査は必ず行うこと(deps.ensure_scope が担う)。articles:write:any は管理者スコープであり、articles:write 相当の操作に使うからといって、その検査を省いてよいわけではない。
SCOPE_IMPLICATIONS: Final[dict[str, frozenset[str]]] = {
    'articles:write': frozenset({'articles:write', 'articles:write:any'}),
}


def granting_scopes(scope: str) -> frozenset[str]:
    """その要求を成立させうるスコープの集合を返す。含意を持たないスコープでは自分自身だけを返す。"""
    return SCOPE_IMPLICATIONS.get(scope, frozenset({scope}))


def hash_secret(raw: str) -> str:
    """セッショントークン・API キーの生値を、保存と照合に使う SHA-256(16 進 64 桁)へ要約する。

    パスワードと違い、これらは十分な乱雑さを持つ値をこちらが生成したものであるため、総当たりの余地が無く、ストレッチングは要らない。sessions.token_hash と api_tokens.token_hash が CHAR(64) なのはこの出力の桁数に合わせたもの。
    """
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def secret_equal(left: str, right: str) -> bool:
    """秘密の文字列を一定時間で比較する。

    通常の == は不一致の位置で打ち切るため、比較にかかる時間から先頭何文字が一致したかを推測できる。hmac.compare_digest は非 ASCII の str を受け付けないため、先に UTF-8 のバイト列へ落としてから渡す。
    """
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


@dataclass(frozen=True)
class Principal:
    """要求を発している主体。

    role_ids は「今の」スナップショットであり、要求ごとに読み直したものが入る。token_scopes は API キー経由のときだけ意味を持ち、そのキーに与えた制限を表す。スコープは権限そのものではなく、所有者が持つ権限のうちキーへ貸し出す範囲の上限である(所有者が管理者でなくなれば、ads:admin の付いたキーも管理操作を通さない)。
    """

    user_id: int
    discord_user_id: int
    is_guild_member: bool
    is_guild_owner: bool
    role_ids: frozenset[int]
    # API キー経由なら api_tokens.id、セッション経由なら None。記事の改版・画像の取り込みには「どのキーが実際に投稿したか」として記録する。
    token_id: int | None
    token_scopes: frozenset[str]

    @property
    def via_token(self) -> bool:
        """API キーによる要求か。"""
        return self.token_id is not None


async def load_principal_for_user(
    db: Database,
    user_id: int,
    *,
    token_id: int | None = None,
    token_scopes: Iterable[str] = (),
    conn: Connection | None = None,
) -> Principal | None:
    """users.id から主体を組み立てる。行が無ければ None。

    セッション・API キーのどちらの経路も、認証の照合を終えたあとはここへ合流する。権限の材料(guild メンバーシップ・オーナー・保有ロール)を読むのはこの 1 箇所だけであり、経路によって判定の材料が変わることが起こらない。
    """
    row = await db.fetch_one(
        'SELECT id, discord_user_id, is_guild_member, is_guild_owner FROM users WHERE id = %s',
        user_id,
        conn=conn,
    )
    if row is None:
        return None
    role_rows = await db.fetch_all('SELECT role_id FROM user_roles WHERE user_id = %s', user_id, conn=conn)
    return Principal(
        user_id=int(row['id']),
        discord_user_id=int(row['discord_user_id']),
        is_guild_member=bool(row['is_guild_member']),
        is_guild_owner=bool(row['is_guild_owner']),
        role_ids=frozenset(int(role_row['role_id']) for role_row in role_rows),
        token_id=token_id,
        token_scopes=frozenset(token_scopes),
    )


async def load_principal_from_session(db: Database, config: Config, token_hash: str) -> Principal | None:
    """セッショントークンの SHA-256 から主体を解決する。無効なら None。

    config は API キーの経路と対称にするために受ける(セッションの照合そのものは設定に依らない)。
    """
    row = await db.fetch_one(
        'SELECT token_hash, user_id FROM sessions WHERE token_hash = %s AND expires_at > NOW()',
        token_hash,
    )
    # 引き当ては索引のある token_hash で行うが、一致の判断はここで行う。DB の照合順序(utf8mb4_0900_ai_ci)は大文字小文字を区別しないため、SQL の = だけを一致の根拠にしない。
    if row is None or not secret_equal(str(row['token_hash']), token_hash):
        return None
    principal = await load_principal_for_user(db, int(row['user_id']))
    if principal is None:
        return None
    await db.execute('UPDATE sessions SET last_seen_at = NOW() WHERE token_hash = %s', token_hash)
    return principal


async def load_principal_from_token(db: Database, config: Config, raw_token: str) -> Principal | None:
    """API キーの生値から主体を解決する。無効なら None。

    revoked_at が入っている、または expires_at を過ぎたキーは無効。加えて、**所有者が対象の guild のメンバーでなければ、照合が通っても認証を成立させない**。キーは所有者の権限を機械が借りたものであり、所有者が guild を離れた時点で借りる相手が居ない。権限を落として「認証済みだが何もできない主体」にするのではなく、資格情報そのものが通らないようにする。
    """
    token_hash = hash_secret(raw_token)
    row = await db.fetch_one(
        'SELECT id, token_hash, user_id FROM api_tokens '
        'WHERE token_hash = %s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())',
        token_hash,
    )
    if row is None or not secret_equal(str(row['token_hash']), token_hash):
        return None
    token_id = int(row['id'])
    # 資格の判定より先に記録する。last_used_at は「このキーが使われた」という事実であり、その要求が通ったかどうかとは別物である(通らなくなったキーが叩かれ続けている状況こそ、気づきたい事象である)。
    await db.execute('UPDATE api_tokens SET last_used_at = NOW() WHERE id = %s', token_id)
    scope_rows = await db.fetch_all('SELECT scope FROM api_token_scopes WHERE token_id = %s', token_id)
    principal = await load_principal_for_user(
        db,
        int(row['user_id']),
        token_id=token_id,
        token_scopes=(str(scope_row['scope']) for scope_row in scope_rows),
    )
    if principal is None or not principal.is_guild_member:
        return None
    return principal


async def is_admin(db: Database, settings: RuntimeSettings, principal: Principal) -> bool:
    """要求の時点でこの主体が管理者資格を満たすかを判定する。

    次のいずれかを満たせば管理者とする。
      (a) guild のオーナーである(users.is_guild_owner)。オーナーはロールに依らず guild の全権を持つため、ロールだけを見ると必ず取りこぼす。
      (b) 保有ロールのいずれかが Discord の Administrator 権限(discord_roles.permissions の 1 << 3)を持つ。
      (c) 保有ロールのいずれかが settings の admin.role_ids に含まれる。Administrator は guild の全操作を許す極めて強い権限であり、このプラットフォームの管理を任せたいだけの相手に配らせないための逃げ道である。

    **settings をこの関数の中で読み直さない。**呼び出し側が要求ごとに settings 表から解決したものを渡すこと(server では依存性がそれを行う)。起動時に読んだ設定を握って渡すと、管理ロールを画面から外しても、そのロールだけで管理者だった者が再起動まで管理者のままになる。判定の材料のうちロールの側(principal.role_ids)は要求ごとに読み直した現在のスナップショットであり、設定の側だけが凍結されるという歪んだ状態を作ってはならない。
    """
    if principal.is_guild_owner:
        return True
    if not principal.role_ids:
        return False
    if not principal.role_ids.isdisjoint(settings.admin.role_ids):
        return True
    role_ids = tuple(principal.role_ids)
    # IN 句は個数分の %s を組み立てる。埋め込むのは placeholder だけであり、値は必ず引数として渡す。
    placeholders = ', '.join(['%s'] * len(role_ids))
    found = await db.fetch_val(
        f'SELECT 1 FROM discord_roles WHERE id IN ({placeholders}) AND (permissions & %s) <> 0 LIMIT 1',
        *role_ids,
        ADMINISTRATOR_BIT,
    )
    return found is not None


def has_scope(principal: Principal, scope: str) -> bool:
    """このスコープに相当する操作が、主体に与えられた範囲に収まっているか。

    スコープはキーに対する制限であって、人に対する制限ではない。セッション経由(via_token が偽)の主体は Web UI から自分の権限で操作しているのであり、キーの範囲という概念を持たないため常に真を返す。**これは「何でもできる」という意味ではない。**管理操作の可否は is_admin による資格の判定が別途担っており、この関数はその判定を代替しない。

    判定は厳密一致ではなく含意を通す(SCOPE_IMPLICATIONS)。上位のスコープが下位の操作を包含するのは定義そのものであり、その関係を各呼び出し元に書かせると、書き忘れた 1 箇所だけが仕様どおりに発行されたキーを黙って拒む。ただし真を返したことは管理者資格の検査を済ませたことを意味しない。含意で通った場合に何を検査すべきかは deps.ensure_scope が判断する。
    """
    if not principal.via_token:
        return True
    return not principal.token_scopes.isdisjoint(granting_scopes(scope))
