"""Discord OAuth2 (PKCE) login, server-side sessions, and API key issuance.

The platform has no password of its own: a Discord account is the only identity. Visibility and admin eligibility are both decided by Discord roles, so the authorization scopes need identify (who you are) plus guilds.members.read (roles in the target guild). Without guilds.members.read, login still succeeds but nothing whatsoever can be decided about the user.

Neither sessions nor API keys store the raw value. What rides in the cookie and the Authorization header is an opaque random token; the DB holds only its SHA-256. The raw value exists only at the moment it is created, and reading the DB cannot recover it.

**An API key's raw value is returned exactly once, in the issue response.** Providing any way to retrieve it again would make the decision not to store it meaningless.

The source of truth for the OAuth2 credentials (client_id / client_secret) and the target guild is the settings table, and these functions receive what was read on the current request. Nothing is read at startup and held, so replacing client_secret from the UI means the next login exchanges with that value.

Two things come instead from config.yaml's server.base_url: the URL Discord returns to after authorization (oauth_redirect_uri) and whether cookies carry Secure (_cookies_are_secure). Both are properties of the runtime environment itself — where the public URL is and whether it is https — not settings changeable from the UI. So the functions involved in the authorization flow take both the RuntimeSettings coming from the settings table and the Config coming from config.yaml.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Any, Final
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from aiomysql import Connection
from fastapi import Response

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings
from common.snowflake import to_snowflake

from .errors import AppError, BadRequest, Forbidden
from .principals import ADMIN_SCOPES, Principal, hash_secret, is_admin, secret_equal

logger = logging.getLogger(__name__)

DISCORD_API_BASE: Final = 'https://discord.com/api/v10'
DISCORD_AUTHORIZE_URL: Final = 'https://discord.com/oauth2/authorize'
DISCORD_TOKEN_URL: Final = f'{DISCORD_API_BASE}/oauth2/token'
# identify fetches the identity; guilds.members.read fetches the roles in the target guild. Visibility and API key eligibility are both decided by roles, so no decision is possible until both are in hand.
OAUTH_SCOPES: Final = 'identify guilds.members.read'
DISCORD_TIMEOUT_SECONDS: Final = 10.0

# The route Discord returns the user to after authorization. routers/auth.py's callback declares its route from this same constant, so the redirect_uri sent at authorization can never diverge from the route that actually receives it.
OAUTH_CALLBACK_PATH: Final = '/auth/callback'

SESSION_COOKIE_NAME: Final = 'session'
SESSION_LIFETIME: Final = timedelta(days=30)

OAUTH_STATE_COOKIE_NAME: Final = 'oauth_state'
# The state cookie is used only by the authorization flow. Confining it under /auth keeps it off API and image-delivery requests.
OAUTH_STATE_COOKIE_PATH: Final = '/auth'
OAUTH_STATE_LIFETIME: Final = timedelta(minutes=10)

# oauth_states.state is CHAR(64), which fits 64 hex digits exactly.
OAUTH_STATE_BYTES: Final = 32
# PKCE's code_verifier is 43-128 characters (RFC 7636). token_urlsafe(64) gives 86, which also fits oauth_states.code_verifier's VARCHAR(128).
PKCE_VERIFIER_BYTES: Final = 64

API_TOKEN_PREFIX: Final = 'ink_'
# api_tokens.token_prefix is VARCHAR(12). Kept in plaintext solely so the listing can tell one key from another.
API_TOKEN_PREFIX_LENGTH: Final = 12
SECRET_TOKEN_BYTES: Final = 32

DEFAULT_REDIRECT_TO: Final = '/'
# Matches oauth_states.redirect_to's VARCHAR(512).
MAX_REDIRECT_TO_LENGTH: Final = 512

_TOKEN_COLUMNS: Final = 'id, user_id, token_prefix, name, created_at, last_used_at, expires_at, revoked_at'


class DiscordAuthError(AppError):
    """The call to Discord failed, or its response was not what was expected.

    Not 4xx: the incoming request was not at fault. All the user can do is retry after a while, and the status code says exactly that.
    """

    status_code = 502
    code = 'discord_auth_error'
    default_message = 'Discord との通信に失敗しました。時間をおいてもう一度お試しください。'


# Redirect target validation


def sanitize_redirect_to(value: str | None) -> str:
    """Confine the post-login redirect target to a path inside this site.

    Taking an external URL as given turns this site into a stepping stone that bounces users anywhere merely by getting them to follow our login URL (an open redirect). Phishing works precisely as "I followed a link on a familiar domain and ended up on a site I do not know", so silently dropping such values is the right call here. Doubtful values fall back to home rather than erroring: naming a redirect target is a convenience, and being unable to log in because of one is backwards.
    """
    if value is None:
        return DEFAULT_REDIRECT_TO
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_REDIRECT_TO_LENGTH:
        return DEFAULT_REDIRECT_TO
    # Values containing control characters or whitespace are refused: they are a foothold for injecting a newline into the Location header and for exploiting per-browser differences in interpretation.
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        return DEFAULT_REDIRECT_TO
    # Some browsers read \ as the same separator as /, so /\example.invalid gets treated as //example.invalid — a different host.
    if '\\' in candidate:
        return DEFAULT_REDIRECT_TO
    parsed = urlsplit(candidate)
    # A value carrying a scheme or a netloc is an absolute URL, and need not lead to this site.
    if parsed.scheme or parsed.netloc:
        return DEFAULT_REDIRECT_TO
    if not parsed.path.startswith('/') or parsed.path.startswith('//'):
        return DEFAULT_REDIRECT_TO
    return urlunsplit(('', '', parsed.path, parsed.query, parsed.fragment))


# Starting the authorization flow


def oauth_redirect_uri(config: Config) -> str:
    """The URL Discord returns to after authorization. Not held as a setting; built from the public URL and the route.

    If it were held as an independent setting, a value disagreeing with base_url could be stored, and then login alone would break silently. Exactly one legitimate value can go here, so there is no reason to make it a setting. **Register this value under Redirects in Discord's application settings.** If the registration is missing or disagrees, Discord refuses at the consent screen.

    Both the start of authorization and the code exchange take it from this function. Discord demands that the redirect_uri of the two requests match exactly, so building it in exactly one place leaves no room for one of them to differ. base_url has its trailing / stripped at load time, so it can be concatenated as-is.
    """
    return f'{config.server.base_url}{OAUTH_CALLBACK_PATH}'


def _code_challenge(verifier: str) -> str:
    """Build the PKCE code_challenge (S256). No = padding: RFC 7636 defines its base64url without it."""
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def build_authorize_url(config: Config, settings: RuntimeSettings, *, state: str, code_challenge: str) -> str:
    """Build the URL of Discord's consent screen."""
    params = {
        'client_id': settings.discord.oauth.client_id,
        'redirect_uri': oauth_redirect_uri(config),
        'response_type': 'code',
        'scope': OAUTH_SCOPES,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        # Return without showing the consent screen if authorization was already granted. Discord asks again whenever the requested scopes change, so this does not skip consent.
        'prompt': 'none',
    }
    return f'{DISCORD_AUTHORIZE_URL}?{urlencode(params)}'


async def begin_login(db: Database, config: Config, settings: RuntimeSettings, redirect_to: str | None) -> tuple[str, str]:
    """Start the authorization flow. Returns (consent screen URL, state).

    PKCE is used so that a stolen authorization code still cannot be exchanged: the code_verifier exists only in our DB, so getting hold of the code alone does not assemble what the exchange needs.
    """
    # Do not let expired states pile up. The authorization flow finishes within minutes, and nobody else cleans up abandoned rows.
    await db.execute('DELETE FROM oauth_states WHERE expires_at <= NOW()')
    state = secrets.token_hex(OAUTH_STATE_BYTES)
    verifier = secrets.token_urlsafe(PKCE_VERIFIER_BYTES)
    await db.execute(
        'INSERT INTO oauth_states (state, code_verifier, redirect_to, expires_at) VALUES (%s, %s, %s, NOW() + INTERVAL %s SECOND)',
        state,
        verifier,
        sanitize_redirect_to(redirect_to),
        int(OAUTH_STATE_LIFETIME.total_seconds()),
    )
    return build_authorize_url(config, settings, state=state, code_challenge=_code_challenge(verifier)), state


async def consume_state(db: Database, state: str) -> dict[str, Any] | None:
    """Look a state up exactly once. None if it is invalid, expired, or already used.

    The row found is deleted on the spot, and only the side whose DELETE removed it counts as having looked it up. If two requests arrive at once with the same state only one gets through, so replaying the authorization code cannot log in twice.
    """
    row = await db.fetch_one(
        'SELECT state, code_verifier, redirect_to FROM oauth_states WHERE state = %s AND expires_at > NOW()',
        state,
    )
    if row is None or not secret_equal(str(row['state']), state):
        return None
    if await db.execute('DELETE FROM oauth_states WHERE state = %s', state) == 0:
        return None
    return row


# The Discord API


def _read_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise DiscordAuthError('Discord の応答を解釈できませんでした。') from exc


def _require_ok(response: httpx.Response, what: str) -> Mapping[str, Any]:
    """Check that the response succeeded and return its JSON mapping.

    Discord's response body is never handed straight back to the user: it can carry client_id or internal identifiers, and none of it is information the user should be told. Chasing down causes is done in the logs.
    """
    if response.status_code >= 400:
        logger.warning('Discord への %s に失敗しました: status=%s body=%s', what, response.status_code, response.text[:500])
        raise DiscordAuthError()
    payload = _read_json(response)
    if not isinstance(payload, Mapping):
        raise DiscordAuthError('Discord の応答の形式が想定と異なります。')
    return payload


async def _get_json(client: httpx.AsyncClient, url: str, access_token: str, what: str) -> Mapping[str, Any] | None:
    """GET with the access token attached. 404 returns None, leaving its meaning to the caller."""
    try:
        response = await client.get(url, headers={'Authorization': f'Bearer {access_token}'})
    except httpx.HTTPError as exc:
        raise DiscordAuthError() from exc
    if response.status_code == 404:
        return None
    return _require_ok(response, what)


async def exchange_code(client: httpx.AsyncClient, config: Config, settings: RuntimeSettings, code: str, code_verifier: str) -> str:
    """Exchange the authorization code for an access token.

    The token obtained is used only to fetch identify and the guild member info, and is never stored. Later requests authenticate with our own session, so there is no reason to keep holding Discord credentials — holding them would only widen what leaks when something leaks.
    """
    form = {
        'client_id': settings.discord.oauth.client_id,
        'client_secret': settings.discord.oauth.client_secret,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': oauth_redirect_uri(config),
        'code_verifier': code_verifier,
    }
    try:
        response = await client.post(DISCORD_TOKEN_URL, data=form)
    except httpx.HTTPError as exc:
        raise DiscordAuthError() from exc
    if 400 <= response.status_code < 500:
        # The authorization code is single-use and short-lived, so a reload or the back button always lands here. This is not a Discord outage, so tell the user they can start over.
        logger.info('認可コードの交換を拒否されました: status=%s body=%s', response.status_code, response.text[:500])
        raise BadRequest('Discord の認可コードを交換できませんでした。もう一度ログインしてください。')
    payload = _require_ok(response, 'アクセストークンの取得')
    access_token = payload.get('access_token')
    if not isinstance(access_token, str) or not access_token:
        raise DiscordAuthError('Discord の応答にアクセストークンが含まれていません。')
    return access_token


async def fetch_identity(client: httpx.AsyncClient, access_token: str) -> Mapping[str, Any]:
    """Fetch the caller's own information via the identify scope."""
    payload = await _get_json(client, f'{DISCORD_API_BASE}/users/@me', access_token, 'ユーザー情報の取得')
    if payload is None:
        raise DiscordAuthError('Discord のユーザー情報を取得できませんでした。')
    return payload


async def fetch_guild_member(client: httpx.AsyncClient, settings: RuntimeSettings, access_token: str) -> Mapping[str, Any] | None:
    """Fetch the member info (including held roles) in the target guild. None if not joined.

    Discord answers 404 for non-members. That is not a failure but the answer "not joined", so treat it as a non-member: this site lets non-members read public articles, so login itself still succeeds.
    """
    return await _get_json(
        client,
        f'{DISCORD_API_BASE}/users/@me/guilds/{settings.discord.guild_id}/member',
        access_token,
        'guild メンバー情報の取得',
    )


# User sync


def _clamp(value: Any, length: int) -> str | None:
    """Fit a display string into the column length. None when there is no value.

    If a Discord spec change starts sending values longer than expected, login truncates the display instead of dying with a 500. Everything handled here is for display and never feeds a decision.
    """
    if value is None:
        return None
    text = str(value)
    return text[:length]


def _member_role_ids(member: Mapping[str, Any] | None) -> list[int]:
    """Extract the held role IDs from the member info, collapsing duplicates while preserving order."""
    if member is None:
        return []
    raw_roles = member.get('roles')
    if not isinstance(raw_roles, Sequence) or isinstance(raw_roles, (str, bytes)):
        return []
    role_ids: list[int] = []
    for raw in raw_roles:
        try:
            role_ids.append(to_snowflake(raw))
        except ValueError:
            logger.warning('解釈できないロール ID を無視しました: %r', raw)
    return list(dict.fromkeys(role_ids))


async def sync_user(db: Database, identity: Mapping[str, Any], member: Mapping[str, Any] | None) -> int:
    """Mirror identify and the guild member info into users / user_roles and return users.id.

    **is_guild_owner is not touched here.** Who the owner is can only be known by the bot, which can read the guild itself; this path — reading yourself with the user's own eligibility — cannot decide it. Writing the default here would wipe the owner's admin eligibility on every login.
    """
    raw_id = identity.get('id')
    try:
        discord_user_id = to_snowflake(raw_id) if raw_id is not None else 0
    except ValueError as exc:
        raise DiscordAuthError('Discord のユーザー ID を解釈できませんでした。') from exc
    if not discord_user_id:
        raise DiscordAuthError('Discord の応答にユーザー ID が含まれていません。')
    username = _clamp(identity.get('username'), 32) or ''
    global_name = _clamp(identity.get('global_name'), 32)
    avatar_hash = _clamp(identity.get('avatar'), 64)
    role_ids = _member_role_ids(member)

    async with db.transaction() as conn:
        await db.execute(
            'INSERT INTO users (discord_user_id, username, global_name, avatar_hash, is_guild_member, last_login_at) '
            'VALUES (%s, %s, %s, %s, %s, NOW()) AS new '
            'ON DUPLICATE KEY UPDATE username = new.username, global_name = new.global_name, '
            'avatar_hash = new.avatar_hash, is_guild_member = new.is_guild_member, last_login_at = NOW()',
            discord_user_id,
            username,
            global_name,
            avatar_hash,
            1 if member is not None else 0,
            conn=conn,
        )
        # LAST_INSERT_ID() does not return the existing row's ID on the ON DUPLICATE KEY UPDATE path, so look it up again.
        user_id = int(await db.fetch_val('SELECT id FROM users WHERE discord_user_id = %s', discord_user_id, conn=conn))
        # Refresh the roles to match the current state. Adding and subtracting a delta would leave roles that were removed on Discord's side lingering here, and access restrictions would stop working. Writing only the row-level diff against the full set of roles the user now holds, as the bot's reconcile does (bot/app/roles.py), reaches the same state; applying increments without knowing that full set does not.
        await db.execute('DELETE FROM user_roles WHERE user_id = %s', user_id, conn=conn)
        if role_ids:
            placeholders = ', '.join(['%s'] * len(role_ids))
            # Roles absent from discord_roles are not inserted. user_roles.role_id references discord_roles, so trying to insert a role the bot has not imported yet fails on the foreign key.
            await db.execute(
                f'INSERT IGNORE INTO user_roles (user_id, role_id) SELECT %s, id FROM discord_roles WHERE id IN ({placeholders})',
                user_id,
                *role_ids,
                conn=conn,
            )
        # Clean up expired sessions. Login is a natural occasion to sweep, and it is cheap because it is confined to this one user.
        await db.execute('DELETE FROM sessions WHERE user_id = %s AND expires_at <= NOW()', user_id, conn=conn)
    return user_id


# Sessions


async def create_session(db: Database, user_id: int) -> str:
    """Issue a session and return the raw value to put in the cookie. The raw value exists only as this return value; the DB keeps nothing but its SHA-256."""
    raw_token = secrets.token_urlsafe(SECRET_TOKEN_BYTES)
    await db.execute(
        'INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, NOW() + INTERVAL %s SECOND)',
        hash_secret(raw_token),
        user_id,
        int(SESSION_LIFETIME.total_seconds()),
    )
    return raw_token


async def destroy_session(db: Database, token_hash: str) -> None:
    """Destroy a session. The row is deleted, so a cookie still held by the client never gets through again."""
    await db.execute('DELETE FROM sessions WHERE token_hash = %s', token_hash)


def _cookies_are_secure(config: Config) -> bool:
    """Whether to put Secure on cookies.

    Only when base_url is https. Secure on a development setup served over http makes the browser refuse to store the cookie, producing a baffling "logged in but not logged in" state.
    """
    return config.server.base_url.lower().startswith('https://')


def set_session_cookie(response: Response, config: Config, raw_token: str) -> None:
    """Set the session cookie.

    HttpOnly: unreadable from JavaScript, so an XSS appearing anywhere still cannot carry the session off.
    SameSite=Lax: no cookie on cross-site POSTs, which by itself closes off nearly all CSRF against state-changing requests. The post-login return to the redirect_uri is a top-level navigation, so Lax still sends the cookie.
    """
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path='/',
        httponly=True,
        samesite='lax',
        secure=_cookies_are_secure(config),
    )


def clear_session_cookie(response: Response, config: Config) -> None:
    """Drop the session cookie. The attributes must match those used when setting it: otherwise the browser regards it as a different cookie and the old one survives."""
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path='/',
        httponly=True,
        samesite='lax',
        secure=_cookies_are_secure(config),
    )


def set_state_cookie(response: Response, config: Config, state: str) -> None:
    """Also entrust the authorization flow's state to the browser.

    Keeping state only in the DB cannot confirm that this browser is the one that started that authorization. An attacker who starts an authorization themselves and gets a victim to follow the resulting return URL leaves the victim's browser logged in to the attacker's account, and everything that person writes from then on ends up in the attacker's hands. Checking against a value only the initiating browser holds is what defeats this.
    """
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=int(OAUTH_STATE_LIFETIME.total_seconds()),
        path=OAUTH_STATE_COOKIE_PATH,
        httponly=True,
        samesite='lax',
        secure=_cookies_are_secure(config),
    )


def clear_state_cookie(response: Response, config: Config) -> None:
    """Drop the authorization flow's state cookie."""
    response.delete_cookie(
        OAUTH_STATE_COOKIE_NAME,
        path=OAUTH_STATE_COOKIE_PATH,
        httponly=True,
        samesite='lax',
        secure=_cookies_are_secure(config),
    )


async def complete_login(db: Database, config: Config, settings: RuntimeSettings, *, code: str, state: str, state_cookie: str | None) -> tuple[str, str]:
    """Complete the login from the authorization code. Returns (the session's raw value, the redirect target path)."""
    if not state_cookie or not secret_equal(state_cookie, state):
        raise BadRequest('ログインの状態を確認できませんでした。もう一度ログインしてください。')
    stored = await consume_state(db, state)
    if stored is None:
        raise BadRequest('ログインの状態を確認できませんでした。もう一度ログインしてください。')
    verifier = stored.get('code_verifier')
    if not isinstance(verifier, str) or not verifier:
        raise BadRequest('ログインの状態を確認できませんでした。もう一度ログインしてください。')
    # Run the three requests over one connection. All are short round trips to the same host, so there is no reason to reconnect for each.
    async with httpx.AsyncClient(timeout=DISCORD_TIMEOUT_SECONDS) as client:
        access_token = await exchange_code(client, config, settings, code, verifier)
        identity = await fetch_identity(client, access_token)
        member = await fetch_guild_member(client, settings, access_token)
    user_id = await sync_user(db, identity, member)
    raw_session = await create_session(db, user_id)
    logger.info('ログインしました: user_id=%s is_guild_member=%s', user_id, member is not None)
    return raw_session, sanitize_redirect_to(stored.get('redirect_to'))


# API keys


async def ensure_can_issue_scopes(db: Database, settings: RuntimeSettings, owner: Principal, scopes: Iterable[str]) -> None:
    """Check whether this principal may issue the requested scopes under its eligibility as it stands right now. Raises Forbidden.

    A key's permissions are decided on every request from the current roles and the current settings.admin.role_ids, so even without a check at issue time a non-admin's key never passes an admin operation. The check exists anyway because letting a non-admin hold a key carrying admin scopes creates dormant eligibility that gains admin permission the moment they become an admin. Looking at the key listing only reveals that the key is powerless today; nobody can see when it would come alive.
    """
    if not owner.is_guild_member:
        raise Forbidden('対象の Discord サーバーに参加していないため、API キーを発行できません。')
    requested_admin = sorted({scope for scope in scopes if scope in ADMIN_SCOPES})
    if requested_admin and not await is_admin(db, settings, owner):
        raise Forbidden(f'次のスコープは管理者のみが発行できます: {", ".join(requested_admin)}')


async def _attach_scopes(db: Database, rows: list[dict[str, Any]], *, conn: Connection | None = None) -> list[dict[str, Any]]:
    """Attach the scope listing to the key rows. The scopes are fetched in one query rather than one per row."""
    if not rows:
        return rows
    token_ids = [int(row['id']) for row in rows]
    placeholders = ', '.join(['%s'] * len(token_ids))
    scope_rows = await db.fetch_all(
        f'SELECT token_id, scope FROM api_token_scopes WHERE token_id IN ({placeholders}) ORDER BY scope',
        *token_ids,
        conn=conn,
    )
    by_token: dict[int, list[str]] = {token_id: [] for token_id in token_ids}
    for scope_row in scope_rows:
        by_token[int(scope_row['token_id'])].append(str(scope_row['scope']))
    for row in rows:
        row['scopes'] = by_token[int(row['id'])]
    return rows


async def load_api_token(db: Database, token_id: int, *, user_id: int | None = None, conn: Connection | None = None) -> dict[str, Any] | None:
    """Return one API key row with its scopes. Given user_id, the result is None unless the key belongs to that user."""
    if user_id is None:
        row = await db.fetch_one(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE id = %s', token_id, conn=conn)
    else:
        row = await db.fetch_one(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE id = %s AND user_id = %s', token_id, user_id, conn=conn)
    if row is None:
        return None
    return (await _attach_scopes(db, [row], conn=conn))[0]


async def list_api_tokens(db: Database, user_id: int | None = None) -> list[dict[str, Any]]:
    """Return the key listing with scopes. Omit user_id to return every user's keys.

    Revoked and expired keys are returned too. The times a key was issued, last used, and revoked are audit material, and dropping the key from the listing makes that history untraceable.
    """
    if user_id is None:
        rows = await db.fetch_all(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens ORDER BY created_at DESC, id DESC')
    else:
        rows = await db.fetch_all(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE user_id = %s ORDER BY created_at DESC, id DESC', user_id)
    return await _attach_scopes(db, rows)


async def create_api_token(db: Database, *, user_id: int, name: str, scopes: Sequence[str], expires_days: int | None = None) -> tuple[str, dict[str, Any]]:
    """Issue an API key and return (raw value, the api_tokens row).

    **This is the only place that can return the raw value.** The DB keeps only the SHA-256 and the leading 12 characters used to tell keys apart in the listing. Callers must put the raw value in the issue response and then discard it.

    Eligibility to issue is not checked here (ensure_can_issue_scopes owns that). Some paths, such as issuing from the CLI, have a requesting principal different from the user the key is issued to, so deciding eligibility is kept separate from the mechanics of issuing.
    """
    unique_scopes = list(dict.fromkeys(scopes))
    if not unique_scopes:
        raise BadRequest('スコープを 1 つ以上指定してください。')
    raw_token = f'{API_TOKEN_PREFIX}{secrets.token_urlsafe(SECRET_TOKEN_BYTES)}'
    async with db.transaction() as conn:
        if expires_days is None:
            token_id = await db.execute_insert(
                'INSERT INTO api_tokens (user_id, token_hash, token_prefix, name, expires_at) VALUES (%s, %s, %s, %s, NULL)',
                user_id,
                hash_secret(raw_token),
                raw_token[:API_TOKEN_PREFIX_LENGTH],
                name,
                conn=conn,
            )
        else:
            # The expiry is decided by the DB's clock. Computing it from the app's clock would shift the expiry unintentionally wherever the two drift apart, since what decides valid from invalid is the DB's NOW().
            token_id = await db.execute_insert(
                'INSERT INTO api_tokens (user_id, token_hash, token_prefix, name, expires_at) VALUES (%s, %s, %s, %s, NOW() + INTERVAL %s DAY)',
                user_id,
                hash_secret(raw_token),
                raw_token[:API_TOKEN_PREFIX_LENGTH],
                name,
                expires_days,
                conn=conn,
            )
        for scope in unique_scopes:
            await db.execute('INSERT INTO api_token_scopes (token_id, scope) VALUES (%s, %s)', token_id, scope, conn=conn)
        row = await load_api_token(db, token_id, conn=conn)
    if row is None:
        raise RuntimeError('発行した API キーを読み戻せませんでした。')
    logger.info('API キーを発行しました: token_id=%s user_id=%s scopes=%s', token_id, user_id, ','.join(unique_scopes))
    return raw_token, row


async def revoke_api_token(db: Database, *, token_id: int, user_id: int | None = None) -> bool:
    """Revoke an API key. False if there is no such key.

    Given user_id, a key that does not belong to that user cannot be revoked. If it is already revoked, revoked_at is left alone: the time of the first revocation is the audit fact, and overwriting it on a second request loses when the key was actually stopped.
    """
    if user_id is None:
        exists = await db.fetch_val('SELECT id FROM api_tokens WHERE id = %s', token_id)
    else:
        exists = await db.fetch_val('SELECT id FROM api_tokens WHERE id = %s AND user_id = %s', token_id, user_id)
    if exists is None:
        return False
    await db.execute('UPDATE api_tokens SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL', token_id)
    logger.info('API キーを失効させました: token_id=%s', token_id)
    return True
