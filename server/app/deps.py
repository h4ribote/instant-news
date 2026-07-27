"""FastAPI dependencies. Resolve the authenticated principal from the request and check the eligibility an operation needs.

There are two ways in: the Cookie (session) for browsers, and Authorization: Bearer for machine clients. When both are present Bearer wins, taking the intent of the side that attached credentials explicitly.

**Scope is checked only on the API key path.** A scope is a ceiling on what the key may borrow, not a limit on what a person does from the Web UI with their own permissions. Admin eligibility, by contrast, is checked regardless of path and decided from the current roles on every request.
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
# Holds the resolved principal for the life of one request, so that several dependencies asking for it do not each re-query the DB.
_PRINCIPAL_STATE_KEY: Final = 'instant_news_principal'
# Sentinel separating "not resolved yet" from "resolved, and the result is unauthenticated (None)".
_UNRESOLVED: Final = object()


async def get_db(request: Request) -> Database:
    """Return the Database placed on the app state."""
    db = getattr(request.app.state, 'db', None)
    if db is None:
        raise RuntimeError('app.state.db が設定されていません。アプリの起動時に Database を割り当ててください。')
    return db


async def get_config(request: Request) -> Config:
    """Return the Config placed on the app state.

    Only the values config.yaml remains the source of truth for are available here: server / database / the image storage location. For discord / admin / articles and the image limits the settings table is the source of truth, so take those from get_settings.
    """
    config = getattr(request.app.state, 'config', None)
    if config is None:
        raise RuntimeError('app.state.config が設定されていません。アプリの起動時に Config を割り当ててください。')
    return config


async def get_settings_store(request: Request) -> SettingsStore:
    """Return the SettingsStore placed on the app state. Used by the path that rewrites settings (the admin screen)."""
    store = getattr(request.app.state, 'settings', None)
    if store is None:
        raise RuntimeError('app.state.settings が設定されていません。アプリの起動時に SettingsStore を割り当ててください。')
    return store


async def get_settings(request: Request) -> RuntimeSettings:
    """Return the current settings from the settings table.

    Read on every request, but SettingsStore holds the values for a short while, so this does not query the DB every time. That is why a settings change takes effect without a restart. **Never keep the returned value beyond the current request.** Copying it into a module global or a variable set at startup means a value changed from the UI never reaches that process.
    """
    store = await get_settings_store(request)
    return await store.snapshot()


async def get_media_config(request: Request) -> MediaConfig:
    """Assemble the effective config handed to the image modules: the storage location from config.yaml, the limits from the settings table."""
    config = await get_config(request)
    settings = await get_settings(request)
    return effective_media_config(config, settings)


def _bearer_token(request: Request) -> str | None:
    """Take the Bearer API key out of the Authorization header. No other scheme is handled.

    An upstream proxy or some unrelated purpose may set Authorization, so anything that is not Bearer counts as "no API key presented" and falls through to the cookie check.
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
            # Having presented itself as Bearer, a key that does not check out must not be dropped silently to unauthenticated. If a revoked or expired key got back a success response carrying only the public subset, a machine client could not tell it had failed, and the article it believed it wrote would simply look as if it had vanished.
            raise Unauthorized('API キーが無効です。失効・期限切れであるか、所有者が対象の Discord サーバーに参加していません。')
        return principal
    raw_session = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_session:
        return None
    # An expired or already destroyed cookie counts as unauthenticated. Browsers keep sending a stale cookie, so raising 401 here would make even public pages impossible to open.
    return await load_principal_from_session(db, config, hash_secret(raw_session))


async def get_principal_optional(request: Request) -> Principal | None:
    """Resolve the requesting principal, or None when unauthenticated.

    For routes where what is visible depends on whether the caller is authenticated, such as the public listing and the article detail.
    """
    cached: object = getattr(request.state, _PRINCIPAL_STATE_KEY, _UNRESOLVED)
    if isinstance(cached, Principal):
        return cached
    # A resolution that came out unauthenticated (None) is remembered as well; only _UNRESOLVED falls through to resolving.
    if cached is None:
        return None
    principal = await _resolve_principal(request)
    setattr(request.state, _PRINCIPAL_STATE_KEY, principal)
    return principal


async def get_principal(request: Request) -> Principal:
    """Resolve the requesting principal; 401 when unauthenticated."""
    principal = await get_principal_optional(request)
    if principal is None:
        raise Unauthorized()
    return principal


def _scope_text(scopes: Iterable[str]) -> str:
    """Order the scopes named in a message. Without a fixed order, the same 403 comes back worded differently from one request to the next."""
    return ' / '.join(sorted(scopes))


async def ensure_scope(db: Database, settings: RuntimeSettings, principal: Principal, *scopes: str) -> None:
    """Check whether this principal, with the eligibility it holds now, may perform an operation matching **any one** of the listed scopes. Raises Forbidden.

    Three stages. Each is decided as of the request; the eligibility held at issue time is never carried forward.
      1. Guild membership. Every scope requires at least this.
      2. On the API key path, the key must hold at least one scope that can satisfy the request, implications included (principals.granting_scopes). Not checked on the session path.
      3. If the scopes that actually satisfied the request are admin scopes and nothing else, the owner must meet admin eligibility at this very moment. **Carrying the scope on the key is not enough.** A key whose owner no longer qualifies stops at this stage.

    Stage 3 looks at the scope that actually let the request through, not at the requested scope name. When a key holding only articles:write:any satisfies an articles:write request, the requested name is not an admin scope, yet the power being lent is an admin scope's. Going by the requested name alone would let this path bypass the admin eligibility check entirely. Conversely, actually holding even one scope that does not require admin eligibility satisfies the request on its own, so being an admin is not required.
    """
    if not scopes:
        raise ValueError('検査するスコープを 1 つ以上指定してください。')
    if not principal.is_guild_member:
        # Authentication succeeded, so 403 rather than 401. Logging in again does not bring the eligibility back; only rejoining the target Discord server does. A non-member on the API key path never gets past authentication in the first place.
        raise Forbidden('対象の Discord サーバーに参加していないため、この操作は行えません。')
    if not principal.via_token:
        # A principal on the session path has no notion of a key's range. Ask for current admin eligibility only when the request can be satisfied by admin scopes alone.
        if all(scope in ADMIN_SCOPES for scope in scopes) and not await is_admin(db, settings, principal):
            raise Forbidden(f'{_scope_text(scopes)} は管理者のみが行えます。')
        return
    granting: frozenset[str] = frozenset().union(*(granting_scopes(scope) for scope in scopes))
    held = principal.token_scopes & granting
    if not held:
        raise Forbidden(f'この API キーには {_scope_text(granting)} のいずれのスコープもありません。')
    if held - ADMIN_SCOPES:
        # Already satisfied by a scope that does not require admin eligibility.
        return
    if not await is_admin(db, settings, principal):
        raise Forbidden(f'{_scope_text(held)} は管理者のみが行えます。')


async def ensure_admin(db: Database, settings: RuntimeSettings, principal: Principal) -> None:
    """Require admin eligibility. Raises Forbidden.

    A request made with an API key is never allowed through. The only permissions a key may borrow are the ones its scope names, and letting an operation that matches no scope through on the grounds that "the owner is an admin" would mean a key granted only articles:write could perform admin operations. That is the point at which bounding keys by scope stops meaning anything.
    """
    if principal.via_token:
        raise Forbidden('この操作は API キーでは行えません。Web UI からログインしてください。')
    if not principal.is_guild_member:
        raise Forbidden('対象の Discord サーバーに参加していないため、この操作は行えません。')
    if not await is_admin(db, settings, principal):
        raise Forbidden('この操作は管理者のみが行えます。')


def require_scope(scope: str) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency requiring the eligibility for an operation matching that scope."""

    async def dependency(
        principal: Principal = Depends(get_principal),
        db: Database = Depends(get_db),
        settings: RuntimeSettings = Depends(get_settings),
    ) -> Principal:
        await ensure_scope(db, settings, principal, scope)
        return principal

    return dependency


def require_admin() -> Callable[..., Awaitable[Principal]]:
    """Build a dependency requiring admin eligibility."""

    async def dependency(
        principal: Principal = Depends(get_principal),
        db: Database = Depends(get_db),
        settings: RuntimeSettings = Depends(get_settings),
    ) -> Principal:
        await ensure_admin(db, settings, principal)
        return principal

    return dependency
