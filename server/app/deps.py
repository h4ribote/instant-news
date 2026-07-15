"""FastAPI の依存性。要求から認証主体を解決し、操作に必要な資格を検査する。

認証の受け口は 2 つある。ブラウザ向けの Cookie(session)と、機械クライアント向けの Authorization: Bearer。両方があれば Bearer を優先する(明示的に資格情報を添えた側の意図を採る)。

**スコープの検査は API キー経由のときだけ行う。**スコープはキーに貸し出す範囲の上限であって、人が Web UI から自分の権限で行う操作を縛るものではない。一方、管理者資格の検査は経路に依らず、要求のたびに現在のロールから行う。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Final

from fastapi import Depends, Request

from common.config import Config, MediaConfig, effective_media_config
from common.db import Database
from common.settings import RuntimeSettings, SettingsStore

from .auth import SESSION_COOKIE_NAME
from .errors import Forbidden, Unauthorized
from .principals import (
    ADMIN_SCOPES,
    Principal,
    granting_scopes,
    hash_secret,
    is_admin,
    load_principal_from_session,
    load_principal_from_token,
)

BEARER_SCHEME: Final = 'bearer'
# 解決した主体を要求の間だけ覚えておくための場所。1 つの要求で複数の依存性が主体を求めても、DB を引き直さない。
_PRINCIPAL_STATE_KEY: Final = 'instant_news_principal'
# 「まだ解決していない」と「解決した結果が未認証(None)」を区別するための番人。
_UNRESOLVED: Final = object()


async def get_db(request: Request) -> Database:
    """アプリの状態に置いた Database を取り出す。"""
    db = getattr(request.app.state, 'db', None)
    if db is None:
        raise RuntimeError('app.state.db が設定されていません。アプリの起動時に Database を割り当ててください。')
    return db


async def get_config(request: Request) -> Config:
    """アプリの状態に置いた Config を取り出す。

    ここから取れるのは config.yaml が真実であり続ける値(server / database / 画像の保存先)だけである。discord / admin / articles と画像の各上限は settings 表が真実であり、get_settings から取る。
    """
    config = getattr(request.app.state, 'config', None)
    if config is None:
        raise RuntimeError('app.state.config が設定されていません。アプリの起動時に Config を割り当ててください。')
    return config


async def get_settings_store(request: Request) -> SettingsStore:
    """アプリの状態に置いた SettingsStore を取り出す。設定を書き換える経路(管理画面)が使う。"""
    store = getattr(request.app.state, 'settings', None)
    if store is None:
        raise RuntimeError('app.state.settings が設定されていません。アプリの起動時に SettingsStore を割り当ててください。')
    return store


async def get_settings(request: Request) -> RuntimeSettings:
    """settings 表の現在の設定を取り出す。

    要求のたびに読むが、SettingsStore が短い時間だけ値を保持するため DB への問い合わせは毎回は起こらない。設定の変更を反映するのに再起動を要さないのはこのためである。**取得した値を跨いで保持しないこと。**モジュールのグローバルや起動時の変数へ写すと、画面から変更した値がそのプロセスにだけ届かなくなる。
    """
    store = await get_settings_store(request)
    return await store.snapshot()


async def get_media_config(request: Request) -> MediaConfig:
    """画像のモジュールへ渡す実効設定を組み立てる。保存先は config.yaml、上限は settings 表から取る。"""
    config = await get_config(request)
    settings = await get_settings(request)
    return effective_media_config(config, settings)


def _bearer_token(request: Request) -> str | None:
    """Authorization ヘッダから Bearer のキーを取り出す。Bearer 以外のスキームは扱わない。

    前段のプロキシや別の目的で Authorization が付くことがあるため、Bearer 以外は「API キーの提示ではない」とみなして Cookie の判定へ進む。
    """
    header = request.headers.get('Authorization')
    if not header:
        return None
    scheme, _, value = header.partition(' ')
    if scheme.lower() != BEARER_SCHEME:
        return None
    return value.strip()


async def _resolve_principal(request: Request) -> Principal | None:
    db = await get_db(request)
    config = await get_config(request)
    raw_token = _bearer_token(request)
    if raw_token is not None:
        principal = await load_principal_from_token(db, config, raw_token)
        if principal is None:
            # Bearer を名乗った以上、通らないキーは黙って未認証へ落とさない。失効・期限切れのキーが「公開分だけが返る成功応答」を受け取ると、機械のクライアントは失敗に気づけず、書いたつもりの記事が消えたようにしか見えない。
            raise Unauthorized('API キーが無効です。失効・期限切れであるか、所有者が対象の Discord サーバーに参加していません。')
        return principal
    raw_session = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_session:
        return None
    # 期限切れ・破棄済みの Cookie は未認証として扱う。ブラウザは古い Cookie を送り続けるため、ここを 401 にすると公開ページすら開けなくなる。
    return await load_principal_from_session(db, config, hash_secret(raw_session))


async def get_principal_optional(request: Request) -> Principal | None:
    """要求の主体を解決する。未認証なら None。

    公開の一覧や記事の詳細のように、認証の有無で見えるものが変わる経路で使う。
    """
    cached: object = getattr(request.state, _PRINCIPAL_STATE_KEY, _UNRESOLVED)
    if isinstance(cached, Principal):
        return cached
    # 解決の結果が未認証(None)だったことも覚えておく。_UNRESOLVED のときだけ解決へ進む。
    if cached is None:
        return None
    principal = await _resolve_principal(request)
    setattr(request.state, _PRINCIPAL_STATE_KEY, principal)
    return principal


async def get_principal(request: Request) -> Principal:
    """要求の主体を解決する。未認証なら 401。"""
    principal = await get_principal_optional(request)
    if principal is None:
        raise Unauthorized()
    return principal


def _scope_text(scopes: Iterable[str]) -> str:
    """文言に出すスコープの並び。順序を固定しないと、同じ状況の 403 が要求ごとに違う文面で返る。"""
    return ' / '.join(sorted(scopes))


async def ensure_scope(db: Database, settings: RuntimeSettings, principal: Principal, *scopes: str) -> None:
    """列挙したスコープの**いずれか**に相当する操作を、この主体が今の資格で行えるかを検査する。送出は Forbidden。

    検査は 3 段。いずれも要求の時点で判定し、発行時の資格を持ち出さない。
      1. guild のメンバーであること。どのスコープも最低限これを要する。
      2. API キー経由なら、要求を成立させうるスコープ(含意を含む。principals.granting_scopes)を 1 つ以上キーが持つこと(セッション経由では検査しない)。
      3. 要求を実際に成立させたスコープが管理者スコープしかないなら、所有者が今この瞬間に管理者資格を満たすこと。**キーにスコープが付いているだけでは足りない。**満たさなくなった者のキーは、この段で止まる。

    3 段目が見るのは「要求されたスコープ名」ではなく「実際に要求を通したスコープ」である。articles:write:any だけを持つキーが articles:write の要求を通すとき、要求名は管理者スコープではないが、貸し出されている力は管理者スコープのものである。要求名だけを見ると、この経路が管理者資格の検査を丸ごと迂回する。逆に、管理者資格を要さないスコープを 1 つでも実際に持っているなら、それだけで要求は成立するため管理者である必要はない。
    """
    if not scopes:
        raise ValueError('検査するスコープを 1 つ以上指定してください。')
    if not principal.is_guild_member:
        # 認証は通っているため 401 ではなく 403。もう一度ログインしても資格は戻らず、戻すには対象の Discord サーバーへ参加し直すしかない(API キー経由の非メンバーはそもそも認証の段階で通らない)。
        raise Forbidden('対象の Discord サーバーに参加していないため、この操作は行えません。')
    if not principal.via_token:
        # セッション経由の主体はキーの範囲という概念を持たない。要求が管理者スコープでしか成立しないときにだけ、現在の管理者資格を問う。
        if all(scope in ADMIN_SCOPES for scope in scopes) and not await is_admin(db, settings, principal):
            raise Forbidden(f'{_scope_text(scopes)} は管理者のみが行えます。')
        return
    granting: frozenset[str] = frozenset().union(*(granting_scopes(scope) for scope in scopes))
    held = principal.token_scopes & granting
    if not held:
        raise Forbidden(f'この API キーには {_scope_text(granting)} のいずれのスコープもありません。')
    if held - ADMIN_SCOPES:
        # 管理者資格を要さないスコープで既に成立している。
        return
    if not await is_admin(db, settings, principal):
        raise Forbidden(f'{_scope_text(held)} は管理者のみが行えます。')


async def ensure_admin(db: Database, settings: RuntimeSettings, principal: Principal) -> None:
    """管理者資格を要求する。送出は Forbidden。

    API キーによる要求は通さない。キーに貸し出せる権限はスコープが名指ししたものだけであり、どのスコープにも当たらない操作を「所有者が管理者だから」という理由で通すと、articles:write だけを与えたキーが管理操作を行えることになる。スコープでキーを縛る仕組みが、そこで意味を失う。
    """
    if principal.via_token:
        raise Forbidden('この操作は API キーでは行えません。Web UI からログインしてください。')
    if not principal.is_guild_member:
        raise Forbidden('対象の Discord サーバーに参加していないため、この操作は行えません。')
    if not await is_admin(db, settings, principal):
        raise Forbidden('この操作は管理者のみが行えます。')


def require_scope(scope: str) -> Callable[..., Awaitable[Principal]]:
    """そのスコープに相当する操作の資格を要求する依存性を作る。"""

    async def dependency(
        principal: Principal = Depends(get_principal),
        db: Database = Depends(get_db),
        settings: RuntimeSettings = Depends(get_settings),
    ) -> Principal:
        await ensure_scope(db, settings, principal, scope)
        return principal

    return dependency


def require_admin() -> Callable[..., Awaitable[Principal]]:
    """管理者資格を要求する依存性を作る。"""

    async def dependency(
        principal: Principal = Depends(get_principal),
        db: Database = Depends(get_db),
        settings: RuntimeSettings = Depends(get_settings),
    ) -> Principal:
        await ensure_admin(db, settings, principal)
        return principal

    return dependency
