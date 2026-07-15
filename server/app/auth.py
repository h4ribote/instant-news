"""Discord OAuth2(PKCE)によるログイン、サーバー側セッション、API キーの発行。

このプラットフォームは独自のパスワードを持たず、Discord アカウントだけを身元とする。閲覧範囲も管理者資格も Discord のロールで決まるため、認可のスコープには identify(誰であるか)に加えて guilds.members.read(対象 guild でのロール)が要る。後者が無いとログインは成立しても、その利用者について何一つ判定できない。

セッションと API キーは、どちらも生値を保存しない。Cookie や Authorization ヘッダに載るのは乱雑な不透明トークンで、DB にはその SHA-256 だけを置く。生値はそれを作った瞬間にしか存在せず、DB を読めても復元できない。

**API キーの生値は発行の応答で一度だけ返す。**再取得の手段を用意すると、保存しないという設計そのものが無意味になる。

OAuth2 の資格情報(client_id / client_secret)と対象 guild は settings 表が真実であり、要求のたびに読んだものを受け取る。起動時に読んで握り込まないため、画面から client_secret を差し替えれば次のログインからその値で交換される。

一方、config.yaml の server.base_url から決まるものが 2 つある。認可後に戻る URL(oauth_redirect_uri)と、Cookie に Secure を付けるかどうか(_cookies_are_secure)である。どちらも公開 URL がどこにあり https かどうかという実行環境の性質そのものであって、画面から変えられる設定ではない。したがって認可フローに関わる関数は、settings 表から来る RuntimeSettings と config.yaml から来る Config の両方を受ける。
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
# identify は身元の取得、guilds.members.read は対象 guild でのロールの取得に要る。閲覧範囲も API キーの資格もロールで決まるため、両方が揃って初めて判定が成立する。
OAUTH_SCOPES: Final = 'identify guilds.members.read'
DISCORD_TIMEOUT_SECONDS: Final = 10.0

# 認可後に Discord が利用者を戻す先の経路。routers/auth.py の callback がこの定数をそのまま経路の宣言に使うため、認可で申告する戻り先と、実際にそれを受け取る経路が食い違うことがない。
OAUTH_CALLBACK_PATH: Final = '/auth/callback'

SESSION_COOKIE_NAME: Final = 'session'
SESSION_LIFETIME: Final = timedelta(days=30)

OAUTH_STATE_COOKIE_NAME: Final = 'oauth_state'
# state の Cookie は認可フローでしか使わない。/auth 配下に限ることで、API や画像の配信の要求には載らない。
OAUTH_STATE_COOKIE_PATH: Final = '/auth'
OAUTH_STATE_LIFETIME: Final = timedelta(minutes=10)

# oauth_states.state は CHAR(64)。16 進 64 桁がちょうど収まる。
OAUTH_STATE_BYTES: Final = 32
# PKCE の code_verifier は 43〜128 文字(RFC 7636)。token_urlsafe(64) は 86 文字で、oauth_states.code_verifier の VARCHAR(128) にも収まる。
PKCE_VERIFIER_BYTES: Final = 64

API_TOKEN_PREFIX: Final = 'ink_'
# api_tokens.token_prefix は VARCHAR(12)。一覧でどのキーがどれかを見分けるためだけに平文で持つ。
API_TOKEN_PREFIX_LENGTH: Final = 12
SECRET_TOKEN_BYTES: Final = 32

DEFAULT_REDIRECT_TO: Final = '/'
# oauth_states.redirect_to の VARCHAR(512) に合わせる。
MAX_REDIRECT_TO_LENGTH: Final = 512

_TOKEN_COLUMNS: Final = 'id, user_id, token_prefix, name, created_at, last_used_at, expires_at, revoked_at'


class DiscordAuthError(AppError):
    """Discord 側との通信や応答が期待通りでない。

    こちらの要求の誤りではないため 4xx にはしない。利用者にできることは時間をおいて再試行することだけであり、それを状態コードで伝える。
    """

    status_code = 502
    code = 'discord_auth_error'
    default_message = 'Discord との通信に失敗しました。時間をおいてもう一度お試しください。'


# --------------------------------------------------------------------------
# 遷移先の検証
# --------------------------------------------------------------------------


def sanitize_redirect_to(value: str | None) -> str:
    """ログイン後の遷移先を、このサイト内のパスへ限定する。

    外部の URL をそのまま受けると、このサイトのログイン URL を踏ませるだけで任意のサイトへ利用者を飛ばせる踏み台になる(オープンリダイレクト)。フィッシングは「見慣れたドメインのリンクを踏んだのに知らないサイトに居る」という形で成立するため、ここは黙って落とすのが正しい。判断に迷う値はエラーにせずトップへ寄せる(遷移先の指定は利便性のためのものであり、それが理由でログインできなくなるのは筋が悪い)。
    """
    if value is None:
        return DEFAULT_REDIRECT_TO
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_REDIRECT_TO_LENGTH:
        return DEFAULT_REDIRECT_TO
    # 制御文字と空白を含む値は受けない。Location ヘッダへの改行の注入や、ブラウザごとの解釈の揺れの足がかりになる。
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        return DEFAULT_REDIRECT_TO
    # ブラウザによっては \ を / と同じ区切りとして解釈するため、/\example.invalid が //example.invalid(別ホスト)として扱われる。
    if '\\' in candidate:
        return DEFAULT_REDIRECT_TO
    parsed = urlsplit(candidate)
    # scheme や netloc を持つ値は絶対 URL であり、行き先がこのサイトとは限らない。
    if parsed.scheme or parsed.netloc:
        return DEFAULT_REDIRECT_TO
    if not parsed.path.startswith('/') or parsed.path.startswith('//'):
        return DEFAULT_REDIRECT_TO
    return urlunsplit(('', '', parsed.path, parsed.query, parsed.fragment))


# --------------------------------------------------------------------------
# 認可フローの開始
# --------------------------------------------------------------------------


def oauth_redirect_uri(config: Config) -> str:
    """認可後に Discord が戻す先の URL。設定としては持たず、公開 URL と経路から組み立てる。

    設定として独立に持つと base_url と食い違う値を保存でき、そのときログインだけが静かに壊れる。入りうる正当な値は 1 つしかない以上、それを設定にする理由が無い。**この値を Discord のアプリケーション設定の Redirects へ登録すること。**登録が無い、または食い違う場合、Discord は認可画面の時点で断る。

    認可の開始とコードの交換の両方でこの関数から取る。Discord は 2 つの要求の redirect_uri が完全に一致することを求めるため、組み立てる場所を 1 つに限って、片方だけが違う値になる余地を無くす。base_url は読み込み時に末尾の / を落としてあるため、そのまま連結してよい。
    """
    return f'{config.server.base_url}{OAUTH_CALLBACK_PATH}'


def _code_challenge(verifier: str) -> str:
    """PKCE の code_challenge(S256)を作る。パディングの = は付けない(RFC 7636 の base64url は付けない形で定義されている)。"""
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')


def build_authorize_url(config: Config, settings: RuntimeSettings, *, state: str, code_challenge: str) -> str:
    """Discord の認可画面の URL を組み立てる。"""
    params = {
        'client_id': settings.discord.oauth.client_id,
        'redirect_uri': oauth_redirect_uri(config),
        'response_type': 'code',
        'scope': OAUTH_SCOPES,
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        # 既に認可済みなら確認画面を出さずに戻す。求めるスコープが変われば Discord 側が改めて確認を求めるため、同意を省いてしまうことにはならない。
        'prompt': 'none',
    }
    return f'{DISCORD_AUTHORIZE_URL}?{urlencode(params)}'


async def begin_login(db: Database, config: Config, settings: RuntimeSettings, redirect_to: str | None) -> tuple[str, str]:
    """認可フローを開始する。戻り値は (認可画面の URL, state)。

    PKCE を使うのは、認可コードを盗まれても交換させないため。code_verifier はこちらの DB にしか無く、コードだけを手に入れても交換に必要な材料が揃わない。
    """
    # 期限切れの state を溜め込まない。認可フローは数分で終わるものであり、放置された行を掃除する者は他に居ない。
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
    """state を 1 回だけ引き当てる。無効・期限切れ・使用済みなら None。

    引き当てた行はその場で消し、削除できた側だけが「引き当てた」ことにする。同じ state で同時に 2 つの要求が来ても通るのは片方だけであり、認可コードの再送で二重にログインが成立することがない。
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


# --------------------------------------------------------------------------
# Discord の API
# --------------------------------------------------------------------------


def _read_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise DiscordAuthError('Discord の応答を解釈できませんでした。') from exc


def _require_ok(response: httpx.Response, what: str) -> Mapping[str, Any]:
    """応答が成功であることを確かめ、JSON のマッピングを返す。

    Discord の応答本文をそのまま利用者へ返さない。client_id や内部の識別子が混ざることがあり、伝えるべき情報でもない。原因の追跡はログで行う。
    """
    if response.status_code >= 400:
        logger.warning('Discord への %s に失敗しました: status=%s body=%s', what, response.status_code, response.text[:500])
        raise DiscordAuthError()
    payload = _read_json(response)
    if not isinstance(payload, Mapping):
        raise DiscordAuthError('Discord の応答の形式が想定と異なります。')
    return payload


async def _get_json(client: httpx.AsyncClient, url: str, access_token: str, what: str) -> Mapping[str, Any] | None:
    """アクセストークンを添えて GET する。404 は None を返し、意味づけは呼び出し元に任せる。"""
    try:
        response = await client.get(url, headers={'Authorization': f'Bearer {access_token}'})
    except httpx.HTTPError as exc:
        raise DiscordAuthError() from exc
    if response.status_code == 404:
        return None
    return _require_ok(response, what)


async def exchange_code(client: httpx.AsyncClient, config: Config, settings: RuntimeSettings, code: str, code_verifier: str) -> str:
    """認可コードをアクセストークンへ交換する。

    得たトークンは identify と guild メンバー情報の取得にだけ使い、保存しない。以後の要求は自前のセッションで認証するため、Discord の資格情報を持ち続ける理由が無い(持てば、漏れたときに漏れるものが増えるだけになる)。
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
        # 認可コードは 1 回きりで短命であり、再読み込みや戻るボタンで必ずここへ来る。Discord 側の障害ではないため、やり直せることを伝える。
        logger.info('認可コードの交換を拒否されました: status=%s body=%s', response.status_code, response.text[:500])
        raise BadRequest('Discord の認可コードを交換できませんでした。もう一度ログインしてください。')
    payload = _require_ok(response, 'アクセストークンの取得')
    access_token = payload.get('access_token')
    if not isinstance(access_token, str) or not access_token:
        raise DiscordAuthError('Discord の応答にアクセストークンが含まれていません。')
    return access_token


async def fetch_identity(client: httpx.AsyncClient, access_token: str) -> Mapping[str, Any]:
    """identify スコープで自分自身の情報を取得する。"""
    payload = await _get_json(client, f'{DISCORD_API_BASE}/users/@me', access_token, 'ユーザー情報の取得')
    if payload is None:
        raise DiscordAuthError('Discord のユーザー情報を取得できませんでした。')
    return payload


async def fetch_guild_member(client: httpx.AsyncClient, settings: RuntimeSettings, access_token: str) -> Mapping[str, Any] | None:
    """対象 guild でのメンバー情報(保有ロールを含む)を取得する。参加していなければ None。

    Discord は非メンバーに対して 404 を返す。これは失敗ではなく「参加していない」という答えであり、非メンバーとして扱う(このサイトは未参加でも公開記事を読めるため、ログイン自体は成立させる)。
    """
    return await _get_json(
        client,
        f'{DISCORD_API_BASE}/users/@me/guilds/{settings.discord.guild_id}/member',
        access_token,
        'guild メンバー情報の取得',
    )


# --------------------------------------------------------------------------
# 利用者の同期
# --------------------------------------------------------------------------


def _clamp(value: Any, length: int) -> str | None:
    """表示用の文字列を列の長さへ収める。値が無ければ None。

    Discord 側の仕様変更で想定より長い値が来たときに、ログインが 500 で落ちるのではなく表示が切り詰められるだけで済むようにする。ここで扱うのはいずれも表示のための項目であり、判定には使わない。
    """
    if value is None:
        return None
    text = str(value)
    return text[:length]


def _member_role_ids(member: Mapping[str, Any] | None) -> list[int]:
    """メンバー情報から保有ロールの ID を取り出す。順序を保ったまま重複を畳む。"""
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
    """identify と guild メンバー情報を users / user_roles へ写し、users.id を返す。

    **is_guild_owner はここでは触らない。**オーナーであるかは guild 自体を読める bot だけが知りうる情報であり、この経路(利用者の資格で自分自身を読む)からは判定できない。ここで既定値を書くと、ログインするたびにオーナーの管理者資格が消える。
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
        # LAST_INSERT_ID() は ON DUPLICATE KEY UPDATE の経路では既存行の ID を返さないため、改めて引く。
        user_id = int(await db.fetch_val('SELECT id FROM users WHERE discord_user_id = %s', discord_user_id, conn=conn))
        # ロールは洗い替える。差分で足し引きすると、Discord 側で外されたロールがこちらに残り続け、閲覧制限が効かなくなる。
        await db.execute('DELETE FROM user_roles WHERE user_id = %s', user_id, conn=conn)
        if role_ids:
            placeholders = ', '.join(['%s'] * len(role_ids))
            # discord_roles に無いロールは入れない。user_roles.role_id が discord_roles を参照しており、bot がまだ取り込んでいないロールを入れようとすると外部キーで落ちる。
            await db.execute(
                f'INSERT IGNORE INTO user_roles (user_id, role_id) SELECT %s, id FROM discord_roles WHERE id IN ({placeholders})',
                user_id,
                *role_ids,
                conn=conn,
            )
        # 期限切れのセッションを掃除する。ログインは掃除を掛ける自然な機会であり、対象がこの利用者に限られるため安い。
        await db.execute('DELETE FROM sessions WHERE user_id = %s AND expires_at <= NOW()', user_id, conn=conn)
    return user_id


# --------------------------------------------------------------------------
# セッション
# --------------------------------------------------------------------------


async def create_session(db: Database, user_id: int) -> str:
    """セッションを発行し、Cookie に載せる生値を返す。生値が存在するのはこの戻り値だけで、DB には SHA-256 しか残らない。"""
    raw_token = secrets.token_urlsafe(SECRET_TOKEN_BYTES)
    await db.execute(
        'INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, NOW() + INTERVAL %s SECOND)',
        hash_secret(raw_token),
        user_id,
        int(SESSION_LIFETIME.total_seconds()),
    )
    return raw_token


async def destroy_session(db: Database, token_hash: str) -> None:
    """セッションを破棄する。行を消すため、Cookie が手元に残っていても二度と通らない。"""
    await db.execute('DELETE FROM sessions WHERE token_hash = %s', token_hash)


def _cookies_are_secure(config: Config) -> bool:
    """Cookie に Secure を付けるか。

    base_url が https のときだけ付ける。http で動かす開発構成に Secure を付けると、ブラウザが Cookie を保存せず、原因の分かりにくい「ログインしたのにログインしていない」状態になる。
    """
    return config.server.base_url.lower().startswith('https://')


def set_session_cookie(response: Response, config: Config, raw_token: str) -> None:
    """セッションの Cookie を載せる。

    HttpOnly … JavaScript から読めなくする。万一どこかに XSS が生まれても、セッションを持ち出せない。
    SameSite=Lax … 他サイトからの POST に Cookie を載せない。これだけで、状態を変える要求に対する CSRF がほぼ塞がる。ログイン後の戻り先はトップレベルの遷移であるため Lax でも Cookie は載る。
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
    """セッションの Cookie を落とす。属性は設定時と揃える(揃っていないとブラウザは別の Cookie とみなし、古いものが残る)。"""
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path='/',
        httponly=True,
        samesite='lax',
        secure=_cookies_are_secure(config),
    )


def set_state_cookie(response: Response, config: Config, state: str) -> None:
    """認可フローの state をブラウザにも預ける。

    state を DB に置くだけでは「その認可を開始したのがこのブラウザか」を確かめられない。攻撃者が自分で認可を開始し、その戻り先 URL を被害者に踏ませると、被害者のブラウザが攻撃者のアカウントでログインした状態になる(以後その人が書いたものは攻撃者の手元に残る)。開始したブラウザにしか無い値と突き合わせることで、これが成立しなくなる。
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
    """認可フローの state の Cookie を落とす。"""
    response.delete_cookie(
        OAUTH_STATE_COOKIE_NAME,
        path=OAUTH_STATE_COOKIE_PATH,
        httponly=True,
        samesite='lax',
        secure=_cookies_are_secure(config),
    )


async def complete_login(db: Database, config: Config, settings: RuntimeSettings, *, code: str, state: str, state_cookie: str | None) -> tuple[str, str]:
    """認可コードからログインを完成させる。戻り値は (セッションの生値, 遷移先のパス)。"""
    if not state_cookie or not secret_equal(state_cookie, state):
        raise BadRequest('ログインの状態を確認できませんでした。もう一度ログインしてください。')
    stored = await consume_state(db, state)
    if stored is None:
        raise BadRequest('ログインの状態を確認できませんでした。もう一度ログインしてください。')
    verifier = stored.get('code_verifier')
    if not isinstance(verifier, str) or not verifier:
        raise BadRequest('ログインの状態を確認できませんでした。もう一度ログインしてください。')
    # 3 回の要求を 1 本の接続で流す。いずれも同じ相手への短い往復であり、都度つなぎ直す理由が無い。
    async with httpx.AsyncClient(timeout=DISCORD_TIMEOUT_SECONDS) as client:
        access_token = await exchange_code(client, config, settings, code, verifier)
        identity = await fetch_identity(client, access_token)
        member = await fetch_guild_member(client, settings, access_token)
    user_id = await sync_user(db, identity, member)
    raw_session = await create_session(db, user_id)
    logger.info('ログインしました: user_id=%s is_guild_member=%s', user_id, member is not None)
    return raw_session, sanitize_redirect_to(stored.get('redirect_to'))


# --------------------------------------------------------------------------
# API キー
# --------------------------------------------------------------------------


async def ensure_can_issue_scopes(db: Database, settings: RuntimeSettings, owner: Principal, scopes: Iterable[str]) -> None:
    """要求されたスコープを、この主体が「今の」資格で発行できるかを検査する。送出は Forbidden。

    キーの権限は要求のたびに現在のロールと現在の settings.admin.role_ids から判定されるため、発行時の検査を省いても管理者でない者のキーが管理操作を通すことはない。それでも検査するのは、管理者でない者に管理者スコープ付きのキーを持たせると、それが「後で管理者になった瞬間に管理権限を得る」休眠状態の資格になるため。キーの一覧を眺めても、そのキーが今は無力であることしか分からず、いつ有効化されるかは誰にも見えない。
    """
    if not owner.is_guild_member:
        raise Forbidden('対象の Discord サーバーに参加していないため、API キーを発行できません。')
    requested_admin = sorted({scope for scope in scopes if scope in ADMIN_SCOPES})
    if requested_admin and not await is_admin(db, settings, owner):
        raise Forbidden(f'次のスコープは管理者のみが発行できます: {", ".join(requested_admin)}')


async def _attach_scopes(db: Database, rows: list[dict[str, Any]], *, conn: Connection | None = None) -> list[dict[str, Any]]:
    """キーの行にスコープの一覧を添える。行ごとに引かず 1 回でまとめて引く。"""
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
    """API キーの 1 行をスコープ付きで返す。user_id を与えると、その利用者のキーでなければ None になる。"""
    if user_id is None:
        row = await db.fetch_one(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE id = %s', token_id, conn=conn)
    else:
        row = await db.fetch_one(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE id = %s AND user_id = %s', token_id, user_id, conn=conn)
    if row is None:
        return None
    return (await _attach_scopes(db, [row], conn=conn))[0]


async def list_api_tokens(db: Database, user_id: int | None = None) -> list[dict[str, Any]]:
    """キーの一覧をスコープ付きで返す。user_id を省くと全利用者のキーを返す。

    失効済み・期限切れのキーも返す。いつ発行され、いつ最後に使われ、いつ失効したのかは監査の材料であり、一覧から消すとその履歴が辿れなくなる。
    """
    if user_id is None:
        rows = await db.fetch_all(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens ORDER BY created_at DESC, id DESC')
    else:
        rows = await db.fetch_all(f'SELECT {_TOKEN_COLUMNS} FROM api_tokens WHERE user_id = %s ORDER BY created_at DESC, id DESC', user_id)
    return await _attach_scopes(db, rows)


async def create_api_token(db: Database, *, user_id: int, name: str, scopes: Sequence[str], expires_days: int | None = None) -> tuple[str, dict[str, Any]]:
    """API キーを発行し、(生値, api_tokens の行) を返す。

    **生値を返せるのはここだけである。**DB には SHA-256 と、一覧で見分けるための先頭 12 文字しか残らない。呼び出し元は発行の応答へ載せたあと生値を捨てること。

    発行の資格の検査はここでは行わない(ensure_can_issue_scopes が担う)。CLI からの発行のように、要求元の主体と発行先の利用者が別である経路があるため、資格の判定と発行の手続きを分けている。
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
            # 期限は DB の時計で決める。アプリ側の時計で計算すると、両者がずれている環境で期限が意図せず前後する(有効・無効を判定するのは DB の NOW() である)。
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
    """API キーを失効させる。対象が無ければ False。

    user_id を与えると、その利用者のキーでなければ失効できない。既に失効済みなら revoked_at は動かさない(最初に失効させた時刻こそが監査の事実であり、二度目の要求で上書きすると、いつ止めたのかが分からなくなる)。
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
