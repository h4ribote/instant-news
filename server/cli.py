#!/usr/bin/env python3
"""API キーとプラットフォーム設定の管理 CLI。

    python -m server.cli issue-token --discord-user-id <id> --name <name> --scope <scope> [--scope <scope>...] [--expires-days <n>]
    python -m server.cli list-tokens [--discord-user-id <id>]
    python -m server.cli revoke-token --id <token_id>
    python -m server.cli list-settings
    python -m server.cli get-setting <key>
    python -m server.cli set-setting <key> <json-value>

キーの発行は、Web UI が無い段階でも API を使えるようにするための手段。設定ファイルにキーを書く方式は採らない。生値がファイルに残ってしまい、DB には SHA-256 しか保存しないという方針と矛盾するうえ、失効も一覧もできなくなる。

キーは必ず既存のユーザーへ紐づける。機械のための独立したアカウントを作らず、人間の権限を機械が借りる形にすることで、責任の所在が必ず人間に残る。権限は発行時に凍結せず、要求のたびに現在のロールから判定するため、ここでの検査は「今この人が発行してよいか」の確認である。

設定の操作(list-settings / get-setting / set-setting)は、**管理画面から締め出された場合の唯一の復旧手段**である。設定の真実は settings 表にあり、通常はフロントエンドの管理画面から変更する。しかし管理ロール(admin.role_ids)を誤って設定すると、誰も管理者資格を満たさなくなり、管理画面にも設定 API にも到達できなくなる。要求のたびに管理者資格を再検査する設計である以上、この状態を HTTP の側から解く方法は無い。設定ファイルを書き換えても、初期値は初回起動時にしか読まれないため元へ戻らない。したがって DB へ直接到達できる経路が要る。

この CLI は Web の API より強い。API が課す 2 つの制限を、ここでは意図的に課さない。秘密の設定の生値を表示し(get-setting)、変更できない設定も変更できる(set-setting)。どちらも「画面を開ける者」に渡してはならないという理由で API 側に設けた制限であり、DB へ到達できる者を縛るためのものではない。DB へ到達できる者は、そもそも settings 表を直接読み書きできる。

この CLI が設定ファイルに求めるのは database 節だけである。他の節を読まないのは、復旧の最後の手段が、復旧しに来た対象そのものの健全さを前提にしてはならないためである。設定ファイルの残りの節は初回起動時に settings 表へ写す初期値であり、その真実は既に表の側にある。そこに不備があるだけで CLI までもが起動を拒むと、まさに設定を直しに来た運用者が手段を失う。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Final


# 設定ファイル全体を読む load_config ではなく database 節だけを読む load_database_config を使う。この CLI が要るのは DB の接続先だけであり、他の節の不備で復旧手段を失ってはならないためである。
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

# api_token_scopes.scope の ENUM と同じ集合。値の真実は migrations/0001_initial.sql にある。
SCOPES: Final = ('articles:write', 'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin')

# キーの生成(接頭辞・生値の長さ・SHA-256 による要約・行の書き込み)は app.auth.create_api_token が担う。ここで同じ手順を書き直すと、片方だけを直したときに、Web UI から出したキーと CLI から出したキーの形が食い違う。発行の資格を誰に問うかだけがこの 2 つの経路で異なり、その判定はここに書く。


class CliError(Exception):
    """利用者の指定の誤りや、前提を満たさない状態。異常終了の文言として stderr へ出す。"""


async def _load_user(db: Database, discord_user_id: int) -> dict[str, Any]:
    """対象のユーザーを引く。居なければ発行しない(存在しない相手のキーを作っても、要求のたびの権限判定が必ず落ちるだけである)。"""
    user = await db.fetch_one(
        'SELECT id, discord_user_id, username, global_name, is_guild_member, is_guild_owner FROM users WHERE discord_user_id = %s',
        discord_user_id,
    )
    if user is None:
        raise CliError(f'Discord ユーザー {discord_user_id} は users に登録されていません。先に本人がログインするか、bot が guild のメンバーを取り込むのを待ってください。')
    return user


async def _principal_for(db: Database, user: dict[str, Any]) -> Principal:
    """対象ユーザーの現在の資格を組み立てる。API キー経由ではないため token_id と token_scopes は空。"""
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
        # settings:admin は admin.role_ids を書き換えられる、すなわち管理者の資格そのものを配れるため、他のどのスコープよりも強い。他の管理者スコープのように「広告だけ」「分類だけ」と対象を限ることができず、これを渡すのは実質的に管理者権限の委譲である。
        print('警告: settings:admin はプラットフォームの設定を変更でき、その中には管理ロール(admin.role_ids)が含まれます。このキーの持ち主は自分自身や任意の相手へ管理者資格を与えられるため、実質的に管理者権限を委譲することになります。', file=sys.stderr)
    if not user['is_guild_member']:
        # 発行自体は妨げない(再参加すればそのまま使える)。ただし現状ではどの要求も 401 になるため、黙って渡すと使えない理由が分からない。
        print(f'警告: {_display_name(user)}({discord_user_id})は現在 guild のメンバーではありません。メンバーに戻るまで、このキーによる要求は認証されません。', file=sys.stderr)

    # 生成と書き込みは Web UI からの発行と同じ手順を通す。生値が存在するのはこの戻り値だけで、DB には SHA-256 しか残らない。
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
        # 期限切れかどうかは DB の時計で判定する。CLI を動かすホストの時計と DB の時計がずれていると、一覧の表示と実際の認証の可否が食い違う。
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
        # 生値は DB に無いため出せない。token_prefix は一覧でキーを見分けるためだけの平文である。
        print(f'id={row["id"]} {row["token_prefix"]}… [{_token_state(row)}] {row["name"]}')
        print(f'  所有者: {_display_name(row)}({row["discord_user_id"]})')
        print(f'  スコープ: {row["scopes"] or "(なし)"}')
        print(f'  作成: {row["created_at"]}  最終使用: {row["last_used_at"] or "未使用"}  期限: {row["expires_at"] or "無期限"}')
    return 0


def _token_state(row: dict[str, Any]) -> str:
    """一覧に出すキーの状態。失効と期限切れは、取るべき対処(何もしない / 発行し直す)が異なるため区別する。"""
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
    # 更新できなかった理由を区別して伝える。既に失効済みなら目的は達成されているため、異常終了にはしない。
    existing = await db.fetch_one('SELECT revoked_at FROM api_tokens WHERE id = %s', token_id)
    if existing is None:
        raise CliError(f'API キー id={token_id} は存在しません。')
    print(f'API キー id={token_id} は既に {existing["revoked_at"]} に失効しています。')
    return 0


# --------------------------------------------------------------------------
# プラットフォームの設定
# --------------------------------------------------------------------------

# 設定の読み書きに SettingsStore.raw_values / describe を通さないのは 2 つの理由による。1 つは、この CLI が復旧の最後の手段であり、登録簿のキーが 1 つでも欠けていれば送出する raw_values に依ると、キーを 1 つ足した直後(まだどのプロセスも初期値を投入していない状態)に一覧も取得も変更もできなくなること。もう 1 つは、秘密の生値を出す必要があり、生値を伏せる describe では復旧の役に立たないこと。書き込みも set_many を通さない(set_many は変更できない設定を拒むが、その拒否は画面に対する制限であってここには当てはまらない)。検証だけは登録簿の coerce_value を共有し、画面から入れた値と CLI から入れた値の形が食い違わないようにする。


async def _load_setting_rows(db: Database) -> dict[str, dict[str, Any]]:
    """settings 表の行をキーで引ける形で読む。更新者は表示のために名前まで引く。"""
    rows = await db.fetch_all(
        'SELECT s.setting_key, s.value, s.updated_at, s.updated_by, u.username, u.global_name'
        ' FROM settings s LEFT JOIN users u ON u.id = s.updated_by'
    )
    return {str(row['setting_key']): row for row in rows}


def _decode_stored(row: dict[str, Any]) -> Any:
    """JSON 列の値を Python の値へ。aiomysql は JSON 列を文字列で返すため、ここで解く。"""
    raw = row['value']
    if isinstance(raw, (str, bytes, bytearray)):
        return json.loads(raw)
    return raw


def _stored_text(row: dict[str, Any] | None) -> str:
    """保存されている値を JSON の表記で返す。そのまま set-setting へ渡し直せる形である。

    登録簿の coerce_value を通さずに表示するのは、値の下限を後から厳しくした場合など、登録簿がもはや受け付けない値が保存されている状態こそ、この CLI で直したい状態だからである。読むだけの操作が検証で落ちてはならない。
    """
    if row is None:
        # 登録簿にはあるが表に無いキー。初期値の投入(server または bot の起動)がまだ済んでいない。
        return '(未登録)'
    return json.dumps(_decode_stored(row), ensure_ascii=False)


def _require_setting(key: str) -> SettingDef:
    """登録簿から設定の定義を引く。無いキーは書かせない(表に書けても、登録簿に無いキーは誰も読まない)。"""
    defn = DEFS_BY_KEY.get(key)
    if defn is None:
        raise CliError(f'未知の設定です: {key}。指定できるキーは list-settings で確認できます(設定の定義はコード側の登録簿にあり、settings 表に無いキーは読み書きしません)。')
    return defn


def _flags_text(defn: SettingDef) -> str:
    """設定の性質のうち、値だけを見ても分からないもの。"""
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
        # 初期値の投入か、この CLI からの変更。どちらもプラットフォームの利用者による変更ではない。
        who = '初期値の投入または CLI'
    else:
        who = f'{_display_name(row)}(id={row["updated_by"]})'
    return f'{row["updated_at"]}  更新者: {who}'


async def _list_settings(db: Database) -> int:
    """設定を一覧する。秘密の値は伏せる(一覧に生値を並べる必要は無い。要るなら get-setting で 1 つだけ出す)。"""
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
    """設定を 1 つ取得する。秘密の設定も生値を出す。

    生値を出すのは、この CLI を実行できるのは DB へ到達できる者だけであり、その者は settings 表を直接読める以上、伏せても何も守れないためである。API 側で伏せるのは、そちらの読み手が「画面を開ける者すべて」だからであって、秘密の設定は誰にも見せないという意味ではない。
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
    """コマンドラインの値を、その設定の値として読む。

    JSON として解釈する。配列(["111","222"])・数値(60)・文字列("文字列")のいずれも 1 つの記法で書けるためである。JSON として解釈できない文字列は素の文字列として扱う(トークンや URL を毎回引用符で囲ませる意味は無い)。この結果、数字だけの文字列(OAuth のクライアント ID など)は数値として読まれるため、引用符で囲む必要がある。誤りは検証で必ず落ちるため、黙って別の型で保存されることはない。
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
    """設定を 1 つ変更する。変更できない設定も変更できる。

    editable=False は画面から書き換えさせないための印であって、DB へ到達できる者を縛るものではない。対象の guild を移すような、再デプロイに相当する操作を行う唯一の正規手段がここである。締め出すべきは画面の利用者であって、復旧に来た運用者ではない。ただし黙って通しはしない。
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
    # 更新者は必ず NULL にする。この CLI を実行したのは DB へ到達できる者であり、プラットフォームの利用者として特定できない。前の更新者を残すと、その人が最後に変更したように見える。
    # 行が無い場合も書けるようにする(登録簿へ設定を足した直後など)。復旧の手段が「先に起動して初期値を投入せよ」を前提にしてはならない。
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


# --------------------------------------------------------------------------
# 起動
# --------------------------------------------------------------------------


def _load_database_config(path: str | Path | None) -> DatabaseConfig:
    """設定ファイルから database 節だけを読む。

    設定ファイル全体を検証しないのは、この CLI が要るのが MySQL の接続先だけだからである。全体を通すと、初期値の節(discord / admin / articles / media の各上限)にキーが 1 つ欠けているだけで、その値を一切使わないこの CLI までもが起動を拒む。それらの値の真実は既に settings 表にあり、この CLI はその表を直しに来ている。管理画面から締め出された運用者にとっての最後の手段が、初期値の節が最新であることを前提にしてはならない。

    探索と検証は common.config が持つものをそのまま使う(明示した引数 → 環境変数 → リポジトリルートの config.yaml)。読む設定ファイルや受け付ける内容が server・bot と食い違うと、直したはずの接続先で別の DB を見に行くことになる。
    """
    try:
        return load_database_config(path)
    except (FileNotFoundError, ValueError) as exc:
        raise CliError(str(exc)) from exc


def _snowflake_arg(value: str) -> int:
    """コマンドライン引数の ID を 64bit の ID として読む。"""
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
            # 重複を畳む。api_token_scopes の主キーは (token_id, scope) であり、同じスコープを 2 度渡すと重複キーになる。
            scopes = list(dict.fromkeys(args.scopes))
            # 管理者かどうかの判定は管理ロールを見る。その真実は settings 表にあるため、config ではなくここから読む。
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
    # MissingSettingsError は初期値の投入が済んでいないことを、どのキーが欠けているかとともに伝える。想定内の状態であるため、traceback ではなく文言として出す。
    except (CliError, FileNotFoundError, ValueError, MissingSettingsError) as exc:
        print(f'エラー: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
