#!/usr/bin/env python3
"""CLI for managing API keys and platform settings.

    python -m server.cli issue-token --discord-user-id <id> --name <name> --scope <scope> [--scope <scope>...] [--expires-days <n>]
    python -m server.cli list-tokens [--discord-user-id <id>]
    python -m server.cli revoke-token --id <token_id>
    python -m server.cli list-settings
    python -m server.cli get-setting <key>
    python -m server.cli set-setting <key> <json-value>

Issuing keys here is what makes the API usable even before a Web UI exists. Writing keys into the config file is deliberately not an option: the raw value would sit in a file, contradicting the rule that the DB stores only the SHA-256, and such a key could be neither revoked nor listed.

A key is always tied to an existing user. There is no standalone account for a machine - the machine borrows a human's permissions - so responsibility always stays with a human. Permissions are not frozen at issue time but decided from the current roles on every request, so the check here only confirms that this person may issue right now.

The settings commands (list-settings / get-setting / set-setting) are **the only recovery path once you are locked out of the admin screen**. The source of truth for settings is the settings table, normally edited from the frontend's admin screen. But mis-set admin roles (admin.role_ids) leave nobody meeting admin eligibility, putting both the admin screen and the settings API out of reach. Since admin eligibility is re-checked on every request, nothing on the HTTP side can undo that state. Editing the config file does not undo it either, because seed values are read only at first boot. Hence a path that reaches the DB directly is required.

This CLI is stronger than the Web API, deliberately dropping two restrictions the API imposes: it prints the raw value of secret settings (get-setting) and writes settings that are not editable (set-setting). Both restrictions exist on the API side because that data must not reach everyone who can open the UI; they were never meant to bind someone who can reach the DB, who can already read and write the settings table directly.

The only config section this CLI needs is database. It ignores the others because a last-resort recovery tool must not presuppose the health of the very thing it came to repair. The remaining config sections are seed values copied into the settings table at first boot, and the source of truth for them already lives in the table. If a flaw there made even the CLI refuse to start, the operator who came to fix the settings would be left with no means.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Final


# load_database_config reads only the database config section, unlike load_config which reads the whole file: this CLI needs nothing but the DB connection target, and a flaw in another config section must not cost us the recovery path.
from common.config import DatabaseConfig, load_database_config
from common.db import Database
from common.settings import (
    CACHE_TTL_SECONDS,
    DEFS_BY_KEY,
    REGISTRY,
    MissingSettingsError,
    RuntimeSettings,
    SettingDef,
    SettingsStore,
    coerce_value,
    to_json_value,
)
from common.snowflake import to_snowflake

from .app.auth import create_api_token
from .app.principals import ADMIN_SCOPES, Principal, is_admin

# Same set as the api_token_scopes.scope ENUM. The source of truth for the values is migrations/0001_initial.sql.
SCOPES: Final = ('articles:write', 'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin')

# Key generation (prefix, raw-value length, SHA-256 digest, row insert) belongs to app.auth.create_api_token, so keys issued from the Web UI and from the CLI share one shape. The two paths differ only in whom eligibility to issue is checked against, and that check is what lives here.


class CliError(Exception):
    """A mistake in what the user specified, or a state that fails a precondition. Printed to stderr as the message for an abnormal exit."""


async def _load_user(db: Database, discord_user_id: int) -> dict[str, Any]:
    """Look the target user up. No user, no key: a key for someone who does not exist would only fail the per-request permission check every time."""
    user = await db.fetch_one(
        'SELECT id, discord_user_id, username, global_name, is_guild_member, is_guild_owner FROM users WHERE discord_user_id = %s',
        discord_user_id,
    )
    if user is None:
        raise CliError(f'Discord ユーザー {discord_user_id} は users に登録されていません。先に本人がログインするか、bot が guild のメンバーを取り込むのを待ってください。')
    return user


async def _principal_for(db: Database, user: dict[str, Any]) -> Principal:
    """Build the target user's current eligibility. Not via an API key, so token_id and token_scopes are empty."""
    rows = await db.fetch_all('SELECT role_id FROM user_roles WHERE user_id = %s', user['id'])
    return Principal(
        user_id=int(user['id']),
        discord_user_id=int(user['discord_user_id']),
        is_guild_member=bool(user['is_guild_member']),
        is_guild_owner=bool(user['is_guild_owner']),
        role_ids=frozenset(int(row['role_id']) for row in rows),
        token_id=None,
        token_scopes=frozenset(),
    )


def _display_name(user: dict[str, Any]) -> str:
    return str(user.get('global_name') or user.get('username') or '')


async def _issue_token(db: Database, settings: RuntimeSettings, *, discord_user_id: int, name: str, scopes: list[str], expires_days: int | None) -> int:
    user = await _load_user(db, discord_user_id)
    principal = await _principal_for(db, user)
    requested_admin = sorted(set(scopes) & ADMIN_SCOPES)
    if requested_admin and not await is_admin(db, settings, principal):
        raise CliError(f'{_display_name(user)}({discord_user_id})は管理者ではないため、{", ".join(requested_admin)} を発行できません。guild のオーナー、Administrator 権限を持つロールの保有者、または設定 admin.role_ids に列挙したロールの保有者だけが発行できます。')
    if 'settings:admin' in requested_admin:
        # settings:admin can rewrite admin.role_ids, i.e. hand out admin eligibility itself, which makes it stronger than any other scope. Unlike the other admin scopes it cannot be narrowed to ads only or taxonomy only, so granting it is effectively delegating admin permissions.
        print('警告: settings:admin はプラットフォームの設定を変更でき、その中には管理ロール(admin.role_ids)が含まれます。このキーの持ち主は自分自身や任意の相手へ管理者資格を与えられるため、実質的に管理者権限を委譲することになります。', file=sys.stderr)
    if not user['is_guild_member']:
        # Issuing is not blocked: the key works as-is once they rejoin. But every request 401s until then, so handing the key over silently would leave the reason it does not work unexplained.
        print(f'警告: {_display_name(user)}({discord_user_id})は現在 guild のメンバーではありません。メンバーに戻るまで、このキーによる要求は認証されません。', file=sys.stderr)

    # Generation and insert go through the same steps as issuance from the Web UI. The raw value exists only in this return value; the DB keeps nothing but the SHA-256.
    raw, row = await create_api_token(db, user_id=int(user['id']), name=name, scopes=scopes, expires_days=expires_days)
    token_id = row['id']

    print(f'API キーを発行しました(id={token_id})。')
    print(f'  所有者: {_display_name(user)}({discord_user_id})')
    print(f'  名前: {name}')
    print(f'  スコープ: {", ".join(scopes)}')
    print(f'  期限: {f"{expires_days} 日後" if expires_days is not None else "無期限"}')
    print('生のキーは DB に保存しないため、この 1 回しか表示されません。この場で控えてください。')
    print(raw)
    return 0


async def _list_tokens(db: Database, *, discord_user_id: int | None) -> int:
    sql = (
        'SELECT t.id, t.token_prefix, t.name, t.created_at, t.last_used_at, t.expires_at, t.revoked_at,'
        # Expiry is judged by the DB's clock: if the clock on the host running the CLI drifts from the DB's, the listing would disagree with what authentication actually accepts.
        ' (t.expires_at IS NOT NULL AND t.expires_at <= NOW()) AS is_expired,'
        ' u.discord_user_id, u.username, u.global_name,'
        " GROUP_CONCAT(s.scope ORDER BY s.scope SEPARATOR ',') AS scopes"
        ' FROM api_tokens t'
        ' JOIN users u ON u.id = t.user_id'
        ' LEFT JOIN api_token_scopes s ON s.token_id = t.id'
    )
    args: list[Any] = []
    if discord_user_id is not None:
        sql += ' WHERE u.discord_user_id = %s'
        args.append(discord_user_id)
    sql += ' GROUP BY t.id, u.discord_user_id, u.username, u.global_name ORDER BY t.id'
    rows = await db.fetch_all(sql, *args)
    if not rows:
        print('該当する API キーはありません。')
        return 0
    for row in rows:
        # The raw value is not in the DB, so it cannot be shown. token_prefix is plaintext kept solely to tell keys apart in the listing.
        print(f'id={row["id"]} {row["token_prefix"]}… [{_token_state(row)}] {row["name"]}')
        print(f'  所有者: {_display_name(row)}({row["discord_user_id"]})')
        print(f'  スコープ: {row["scopes"] or "(なし)"}')
        print(f'  作成: {row["created_at"]}  最終使用: {row["last_used_at"] or "未使用"}  期限: {row["expires_at"] or "無期限"}')
    return 0


def _token_state(row: dict[str, Any]) -> str:
    """The key state shown in the listing. Revoked and expired are kept distinct because the remedy differs: do nothing, versus issue a new key."""
    if row['revoked_at'] is not None:
        return f'失効 {row["revoked_at"]}'
    if row['is_expired']:
        return f'期限切れ {row["expires_at"]}'
    return '有効'


async def _revoke_token(db: Database, *, token_id: int) -> int:
    affected = await db.execute('UPDATE api_tokens SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL', token_id)
    if affected:
        print(f'API キー id={token_id} を失効しました。')
        return 0
    # Report which of the two reasons the update hit. Already revoked means the goal is met, so it is not an abnormal exit.
    existing = await db.fetch_one('SELECT revoked_at FROM api_tokens WHERE id = %s', token_id)
    if existing is None:
        raise CliError(f'API キー id={token_id} は存在しません。')
    print(f'API キー id={token_id} は既に {existing["revoked_at"]} に失効しています。')
    return 0


# Platform settings

# Settings reads and writes bypass SettingsStore.raw_values / describe for two reasons. First, raw_values raises if even one registry key is missing, so this last-resort recovery tool would lose list, get and set alike right after a key is added to the registry, while no process has seeded it yet. Second, recovery needs secret raw values printed, and describe hides them. Writes bypass set_many too: set_many refuses settings that are not editable, but that refusal is a restriction on the UI and does not apply here. Only validation is shared (the registry's coerce_value), so a value entered from the UI and one entered from the CLI cannot end up in different shapes.


async def _load_setting_rows(db: Database) -> dict[str, dict[str, Any]]:
    """Read the settings rows in a form that can be looked up by key. The updater is joined down to a name so it can be displayed."""
    rows = await db.fetch_all(
        'SELECT s.setting_key, s.value, s.updated_at, s.updated_by, u.username, u.global_name'
        ' FROM settings s LEFT JOIN users u ON u.id = s.updated_by'
    )
    return {str(row['setting_key']): row for row in rows}


def _decode_stored(row: dict[str, Any]) -> Any:
    """Turn a JSON column value into a Python value. aiomysql returns JSON columns as strings, so decode here."""
    raw = row['value']
    if isinstance(raw, (str, bytes, bytearray)):
        return json.loads(raw)
    return raw


def _stored_text(row: dict[str, Any] | None) -> str:
    """Return the stored value in JSON notation, in the exact form you can hand straight back to set-setting.

    It is shown without going through the registry's coerce_value because a stored value the registry no longer accepts (after a minimum was tightened, say) is precisely the state this CLI is here to fix. A read-only operation must not fail validation.
    """
    if row is None:
        # A key in the registry but not in the table: seeding (a server or bot start) has not run yet.
        return '(未登録)'
    return json.dumps(_decode_stored(row), ensure_ascii=False)


def _require_setting(key: str) -> SettingDef:
    """Look the setting definition up in the registry. Unknown keys are not written: a key absent from the registry can be put in the table, but nothing ever reads it."""
    defn = DEFS_BY_KEY.get(key)
    if defn is None:
        raise CliError(f'未知の設定です: {key}。指定できるキーは list-settings で確認できます(設定の定義はコード側の登録簿にあり、settings 表に無いキーは読み書きしません)。')
    return defn


def _flags_text(defn: SettingDef) -> str:
    """The properties of a setting that its value alone does not reveal."""
    flags: list[str] = []
    if not defn.editable:
        flags.append('変更不可')
    if defn.restart_required:
        flags.append('要再起動')
    if defn.secret:
        flags.append('秘密')
    return f'  [{", ".join(flags)}]' if flags else ''


def _updated_text(row: dict[str, Any] | None) -> str:
    if row is None:
        return '未登録'
    if row['updated_by'] is None:
        # Either seeding or a change from this CLI. Neither is a change made by a platform user.
        who = '初期値の投入または CLI'
    else:
        who = f'{_display_name(row)}(id={row["updated_by"]})'
    return f'{row["updated_at"]}  更新者: {who}'


async def _list_settings(db: Database) -> int:
    """List the settings, hiding secret values: there is no need to line raw values up in a listing, and get-setting prints a single one when it is needed."""
    stored = await _load_setting_rows(db)
    for defn in REGISTRY:
        row = stored.get(defn.key)
        value = '(設定済み)' if defn.secret and row is not None else _stored_text(row)
        print(f'{defn.key} = {value}{_flags_text(defn)}')
        print(f'  {defn.label}')
        print(f'  更新: {_updated_text(row)}')
    missing = [defn.key for defn in REGISTRY if defn.key not in stored]
    if missing:
        print(f'警告: 次の設定が settings 表にありません: {", ".join(missing)}。初期値の投入は server と bot の起動時に行われるため、まだどちらも起動していない可能性があります。', file=sys.stderr)
    return 0


async def _get_setting(db: Database, *, key: str) -> int:
    """Get one setting, printing the raw value even for a secret setting.

    The raw value is printed because only someone who can reach the DB can run this CLI, and that person can read the settings table directly, so hiding it would protect nothing. The API hides it because its readers are everyone who can open the UI, not because a secret setting is hidden from everyone.
    """
    defn = _require_setting(key)
    stored = await _load_setting_rows(db)
    row = stored.get(key)
    print(f'{defn.key}{_flags_text(defn)}')
    print(f'  {defn.label}')
    print(f'  {defn.description}')
    print(f'  更新: {_updated_text(row)}')
    if defn.secret:
        print('注記: これは秘密の設定であり、API からは生値を返しません(返した時点で、画面を開ける者すべてに渡るためです)。以下の値は端末の履歴や画面共有に残ります。')
    if row is None:
        raise CliError(f'設定 {key} は settings 表にありません。初期値の投入は server と bot の起動時に行われるため、まだどちらも起動していない可能性があります。')
    print(_stored_text(row))
    return 0


def _parse_value(defn: SettingDef, text: str) -> Any:
    """Read a command-line value as that setting's value.

    It is parsed as JSON, so one notation covers arrays (["111","222"]), numbers (60) and strings ("text") alike. Text that does not parse as JSON is taken as a plain string, so there is no need to quote every token or URL. As a consequence an all-digit string, such as an OAuth client ID, is read as a number and has to be quoted. A mistake always fails validation, so nothing is ever silently stored under a different type.
    """
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        raw = text
    try:
        return coerce_value(defn, raw)
    except ValueError as exc:
        raise CliError(f'{exc} 値は JSON として解釈します(文字列は \'"文字列"\'、配列は \'["111","222"]\'、数値は 60 のように書きます)。JSON として解釈できない値は、そのまま文字列として扱います。') from exc


async def _set_setting(db: Database, *, key: str, text: str) -> int:
    """Set one setting, including settings that are not editable.

    editable=False is a flag that stops the UI from rewriting a setting; it does not bind someone who can reach the DB. This is the only sanctioned way to perform an operation amounting to a redeploy, such as moving to a different target guild. The people to lock out are the UI's users, not the operator who came to recover. It is not let through silently, though.
    """
    defn = _require_setting(key)
    value = _parse_value(defn, text)
    stored = await _load_setting_rows(db)
    before = _stored_text(stored.get(key))
    if not defn.editable:
        print(f'警告: {key}({defn.label})は変更できない設定です。管理画面には表示されますが、変更は API からは拒否されます。', file=sys.stderr)
        print(f'警告: {defn.description}', file=sys.stderr)
        print('警告: これは設定の変更ではなく再デプロイに相当する操作です。既存のデータの意味が変わることを理解したうえで行ってください。', file=sys.stderr)
    payload = json.dumps(to_json_value(defn, value), ensure_ascii=False)
    # updated_by is always NULL: whoever ran this CLI is someone who can reach the DB and cannot be identified as a platform user. Leaving the previous updater in place would make it look as though that person made the last change.
    # Writable even when no row exists yet, e.g. right after a setting is added to the registry. A recovery path must not presuppose that you boot first to get the seed values in.
    await db.execute(
        'INSERT INTO settings (setting_key, value, updated_by) VALUES (%s, %s, NULL) AS new '
        'ON DUPLICATE KEY UPDATE value = new.value, updated_by = NULL',
        key,
        payload,
    )
    print(f'設定 {key} を変更しました。')
    print(f'  変更前: {before}')
    print(f'  変更後: {payload}')
    if defn.restart_required:
        print('この設定を読むプロセスは起動時にしか読まないため、反映には再起動が要ります。値を変えただけでは何も起きません。')
    else:
        print(f'server と bot は settings 表を定期的に読み直すため、反映まで最大 {CACHE_TTL_SECONDS:.0f} 秒かかります。')
    return 0


# Entry point


def _load_database_config(path: str | Path | None) -> DatabaseConfig:
    """Read only the database section of the config file.

    The whole file is not validated because all this CLI needs is the MySQL connection target. Validating everything would let a flaw in a seed-value section (discord / admin / articles / media and their max settings), such as an unknown key, a wrong type or an out-of-range value, stop even this CLI from starting, though it uses none of those values. The source of truth for them is already the settings table, the very table this CLI exists to repair. As the last resort for an operator locked out of the admin screen, it does not depend on the seed-value sections being up to date.

    Discovery and validation reuse common.config as-is: explicit argument, then the environment variable, then config.yaml at the repository root. If the config file read or the content accepted diverged from server and bot, a connection target you thought you had fixed would point at a different DB.
    """
    try:
        return load_database_config(path)
    except (FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from exc


def _snowflake_arg(value: str) -> int:
    """Read an ID given as a command-line argument as a 64-bit ID."""
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m server.cli', description='instant-news の API キーとプラットフォーム設定を操作する。設定の操作は、管理画面から締め出された場合の唯一の復旧手段である。')
    parser.add_argument('--config', default=None, help='設定ファイルの場所(既定は環境変数 INSTANT_NEWS_CONFIG、無ければリポジトリルートの config.yaml)。読むのは database 節だけ。')
    subparsers = parser.add_subparsers(dest='command', required=True)

    issue = subparsers.add_parser('issue-token', help='API キーを発行し、生値を 1 度だけ表示する。')
    issue.add_argument('--discord-user-id', type=_snowflake_arg, required=True, help='キーの所有者となる Discord ユーザーの ID。')
    issue.add_argument('--name', required=True, help='用途が分かる名前(例 「相場自動投稿」)。')
    issue.add_argument('--scope', dest='scopes', action='append', choices=SCOPES, required=True, metavar='SCOPE', help=f'キーに与えるスコープ。複数指定できる。選択肢: {", ".join(SCOPES)}')
    issue.add_argument('--expires-days', type=int, default=None, help='発行から何日で期限切れにするか(省略すると無期限)。')

    listing = subparsers.add_parser('list-tokens', help='API キーを一覧する(生値は表示できない)。')
    listing.add_argument('--discord-user-id', type=_snowflake_arg, default=None, help='絞り込む所有者の Discord ユーザー ID(省略すると全件)。')

    revoke = subparsers.add_parser('revoke-token', help='API キーを失効する。')
    revoke.add_argument('--id', dest='token_id', type=_snowflake_arg, required=True, help='失効する API キーの id。')

    subparsers.add_parser('list-settings', help='プラットフォームの設定を一覧する(秘密の値は伏せる)。')

    get_setting = subparsers.add_parser('get-setting', help='プラットフォームの設定を 1 つ取得する(秘密の値も表示する)。')
    get_setting.add_argument('key', help='設定のキー(例 admin.role_ids)。一覧は list-settings で確認できる。')

    set_setting = subparsers.add_parser(
        'set-setting',
        help='プラットフォームの設定を 1 つ変更する。管理画面から締め出された場合の復旧手段。',
        description='プラットフォームの設定を 1 つ変更する。値は JSON として解釈する(JSON にならない値はそのまま文字列として扱う)。変更できない設定もここからは変更できるが、その場合は警告を出す。',
    )
    set_setting.add_argument('key', help='設定のキー(例 admin.role_ids)。一覧は list-settings で確認できる。')
    set_setting.add_argument('value', help='JSON の値(例 \'["000000000000000000"]\'、60、\'"文字列"\')。')
    return parser


async def _run(args: argparse.Namespace) -> int:
    db = Database(_load_database_config(args.config))
    await db.connect()
    try:
        if args.command == 'issue-token':
            if args.expires_days is not None and args.expires_days < 1:
                raise CliError('--expires-days は 1 以上を指定してください(無期限にする場合は指定しないでください)。')
            if args.name.strip() == '':
                raise CliError('--name を空にはできません。')
            # Collapse duplicates: api_token_scopes has primary key (token_id, scope), so passing the same scope twice would be a duplicate key.
            scopes = list(dict.fromkeys(args.scopes))
            # The admin check looks at the admin roles, whose source of truth is the settings table, so they are read from there and not from config.
            settings = await SettingsStore(db).snapshot()
            return await _issue_token(db, settings, discord_user_id=args.discord_user_id, name=args.name, scopes=scopes, expires_days=args.expires_days)
        if args.command == 'list-tokens':
            return await _list_tokens(db, discord_user_id=args.discord_user_id)
        if args.command == 'revoke-token':
            return await _revoke_token(db, token_id=args.token_id)
        if args.command == 'list-settings':
            return await _list_settings(db)
        if args.command == 'get-setting':
            return await _get_setting(db, key=args.key)
        if args.command == 'set-setting':
            return await _set_setting(db, key=args.key, text=args.value)
        raise CliError(f'未知のコマンドです: {args.command}')
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    # MissingSettingsError reports that seeding has not run, naming the keys that are missing. It is an expected state, so print it as a message rather than a traceback.
    except (CliError, FileNotFoundError, ValueError, MissingSettingsError) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
