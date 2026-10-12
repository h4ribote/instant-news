"""Platform settings. The source of truth is the MySQL settings table; the matching values in config.yaml are used only as seed values on first boot.

config.yaml stays the source of truth only for what is needed before settings can be read from the DB (server, database) and for what is decided outside the container (the image storage directory, which is meaningless unless it matches the named volume's mount point). Everything else must be changeable by an admin from the frontend, and there is no reason to require editing the config file and restarting the container for it.

The config file does not overwrite the DB on every boot, since values changed from the UI would silently revert on the next restart, producing the hardest state of all to diagnose: the value you thought you set is not taking effect. One of the two has to be authoritative, and here it is the DB.

Kind, default, validation, secrecy and whether a change needs a restart are defined in code by this registry (REGISTRY). The settings table holds values only. Adding a setting means adding it to the registry, with no table change and no UI change (the UI reads the registry and generates itself).

The JSON representation renders Discord IDs as strings: JavaScript's Number is exact only to 2^53, so snowflakes break silently.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

from .snowflake import to_snowflake

if TYPE_CHECKING:
    from .db import Database

# Settings reload interval, in seconds. server and bot are separate processes, and re-reading is the only way one learns of a setting the other changed. A shorter interval propagates changes sooner but queries the DB more often; changes are rare and a few seconds of lag does no real harm. Whoever made the change sees it immediately, since set_many drops its own process's cache.
CACHE_TTL_SECONDS: Final = 10.0

SettingKind = Literal['id', 'id_list', 'int', 'number', 'str', 'secret']

# Marker for which entity an ID points at. kind only states the shape of the value (id or id_list), so without this there is no way to tell the UI "this setting is an array of roles" and it would have to branch on the key name. Branching on key names breaks the registry's design of never hardcoding keys in the UI, and the day an id_list pointing at some other entity is added, that field gets the list of roles too.
# Only entities the DB holds rows for can be expressed here. Adding a channel would require the bot to import channels the way it imports discord_roles (there is no such table today, so notify channels are entered as raw IDs).
SettingRef = Literal['role']


class _Required:
    """Marker for a setting with no default: it must be given in config.yaml on first boot."""

    def __repr__(self) -> str:
        return '<必須>'


# For settings that cannot have a default: values determined solely by the environment, such as the guild ID or credentials, have no sensible default.
REQUIRED: Final = _Required()


@dataclass(frozen=True)
class SettingDef:
    """One setting's definition. UI generation, validation and secret handling all follow from it."""

    key: str
    kind: SettingKind
    label: str
    description: str
    # Not editable: the value is shown on the admin screen but cannot be written.
    editable: bool = True
    # The process does not re-read this after a change; the admin screen says so.
    restart_required: bool = False
    # Which entity the ID points at. Meaningful only when kind is id / id_list. The UI reads this and offers an entity picker instead of raw ID entry.
    ref: SettingRef | None = None
    minimum: float | None = None
    # Treat minimum as "greater than" rather than "at least". For settings where 0 is meaningless, such as timeouts.
    exclusive_minimum: bool = False
    maximum: float | None = None
    # Default used when config.yaml omits this setting on first boot. REQUIRED means config.yaml must supply it. Giving a setting a default is what lets one be added without existing deployments having to edit their config.yaml.
    default: Any = REQUIRED

    @property
    def secret(self) -> bool:
        """Whether this setting's raw value must never be returned from the API; only whether it is set is returned."""
        return self.kind == 'secret'

    @property
    def has_default(self) -> bool:
        return self.default is not REQUIRED


# The settings registry. Keys absent from here are never read or written; a stale row left in the settings table is ignored.
REGISTRY: Final[tuple[SettingDef, ...]] = (
    SettingDef(
        key='discord.guild_id', kind='id', editable=False,
        label='対象の Discord サーバー',
        description='このデプロイが対応する唯一の guild。変更できない。変更すると、ロールのスナップショット・報道社のロール紐付け・閲覧範囲の判定がすべて別の guild を指すことになり、既存のデータが無意味になる。他の guild へ移すのは再デプロイであって設定変更ではない。',
    ),
    SettingDef(
        key='discord.oauth.client_id', kind='str',
        label='OAuth2 クライアント ID',
        description='利用者認証に使う Discord アプリケーションの ID。',
    ),
    SettingDef(
        key='discord.oauth.client_secret', kind='secret',
        label='OAuth2 クライアントシークレット',
        description='利用者認証に使う Discord アプリケーションのシークレット。設定済みかどうかだけを表示し、生値は返さない。',
    ),
    SettingDef(
        key='discord.bot.token', kind='secret', restart_required=True,
        label='bot のトークン',
        description='新着記事を通知する bot の資格情報。bot は起動時にのみ読むため、変更を反映するには bot の再起動が要る。',
    ),
    SettingDef(
        key='discord.bot.publish_poll_interval_seconds', kind='int', minimum=1, maximum=3600, default=15,
        label='通知の送信間隔(秒)',
        description='公開された記事の通知の待ち行列を bot が確認する間隔。server が予約公開の時刻到来を確かめる間隔でもある。この 2 つは Discord から届くイベントでは駆動できない(公開を決めるのは server であり、Discord は記事が公開されたことを知らない)ため、待ち行列を周期的に見に行くしかない。',
    ),
    SettingDef(
        key='discord.bot.guild_reconcile_interval_seconds', kind='int', minimum=1, maximum=3600, default=3600,
        label='ロールの照合の間隔(秒)',
        description='guild のロール・メンバー・オーナーを Discord と突き合わせて補正する間隔。ロールの変更は通常この間隔を待たずにゲートウェイのイベントで即座に反映されるため、これは反映の速さを決める設定ではなく、イベントを取りこぼした場合に必ず正される時刻の上限である。上限があるのは安全のためである。管理者の資格とロール別の閲覧範囲は user_roles・users.is_guild_member・users.is_guild_owner・discord_roles.permissions のスナップショットに基づいて要求のたびに判定され、それを書くのはイベントの反映とこの照合だけである。上限が無ければ、極端に長い間隔を設定することで、取りこぼしたイベントが永久に正されない状態を作れてしまう。',
    ),
    SettingDef(
        key='discord.bot.count_reconcile_interval_seconds', kind='int', minimum=1, maximum=86400, default=3600,
        label='投票とコメントの照合の間隔(秒)',
        description='通知メッセージとコメントに付いた投票の数、およびコメントそのものを Discord と突き合わせて補正する間隔。投票もコメントも通常この間隔を待たずにゲートウェイのイベントで即座に反映されるため、これはイベントを取りこぼした場合にそれが正される時刻の上限である。',
    ),
    SettingDef(
        key='discord.bot.count_reconcile_window_days', kind='int', minimum=0, maximum=365, default=14,
        label='投票とコメントの照合の対象日数',
        description='投票の数とコメントを照合し直す、公開からの日数。照合は対象期間の通知とスレッドを 1 件残らず読み直すため、際限なく遡ると、反応がとうに止んだ記事まで含めて問い合わせが増え続ける。ここを過ぎた通知にもイベントは届き、投票もコメントもそのまま更新される(イベントが読むのは実際に変化した投稿だけであるため、打ち切る必要がない)。この日数は、送信は済んだがスレッドを開けなかった・投票用の絵文字を付けられなかった通知を bot が拾い直す期間も兼ねる。0 にすると、取りこぼしの補正だけでなくそれらの再試行も止まる。',
    ),
    SettingDef(
        key='admin.role_ids', kind='id_list', ref='role', default=(),
        label='管理ロール',
        description='プラットフォーム管理の資格を与えるロール。空でも、Discord の Administrator 権限を持つロールの保有者と guild オーナーは管理者として扱われる。Administrator はサーバーの全操作を許す極めて強い権限であり、広告の入稿だけを任せたい相手に与えるのは筋が悪いため、Administrator を配らずに委譲する手段としてここを使う。この設定を誤って自分を締め出した場合は CLI から復旧できる。',
    ),
    SettingDef(
        key='articles.breaking.allowed_role_ids', kind='id_list', ref='role', default=(),
        label='速報を立てられるロール',
        description='記事に速報フラグを立てられるロール。空なら誰も立てられない。乱発を防ぐために絞る。',
    ),
    SettingDef(
        key='articles.breaking.mention_role_ids', kind='id_list', ref='role', default=(),
        label='速報で mention するロール',
        description='速報の通知時に mention するロール。空なら mention しない。',
    ),
    SettingDef(
        key='articles.api_rate_limit_per_hour', kind='int', minimum=0, default=60,
        label='API キーの投稿上限(1 時間あたり)',
        description='API キー 1 本あたりの記事の作成・更新の上限。自動投稿は本質的に暴走しうるため、キー単位で上限を設ける。0 は「このキーでは 1 本も書かせない」という有効な設定。',
    ),
    SettingDef(
        key='media.fetch.max_images_per_article', kind='int', minimum=0, default=20,
        label='1 記事あたりの画像取り込み数',
        description='記事の保存時に取得を試みる本文中の画像の最大数。超過分は元の URL のまま残る。0 なら取り込まない。',
    ),
    SettingDef(
        key='media.fetch.max_bytes', kind='int', minimum=1, default=10485760,
        label='画像 1 枚あたりの最大バイト数',
        description='取得を打ち切るバイト数。Content-Length は信用せず、ストリームしながら超過で中断する。',
    ),
    SettingDef(
        key='media.fetch.timeout_seconds', kind='number', minimum=0, exclusive_minimum=True, default=10.0,
        label='画像 1 枚あたりのタイムアウト(秒)',
        description='1 枚の取得に費やす上限。0 は許さない。取り込まないことを表すなら 1 記事あたりの取り込み数を 0 にするのが正しく、0 秒にすると全件が「0 秒以内に取得できませんでした」という原因の分かりにくい失敗になる。',
    ),
    SettingDef(
        key='media.fetch.total_timeout_seconds', kind='number', minimum=0, exclusive_minimum=True, default=60.0,
        label='1 記事あたりの取り込みの総時間(秒)',
        description='記事の保存 1 回で画像の取得に費やす総時間の上限。超過したものは失敗として扱い、元の URL のまま残す。0 は許さない(理由は 1 枚あたりのタイムアウトと同じ)。',
    ),
    SettingDef(
        key='media.fetch.max_redirects', kind='int', minimum=0, maximum=10, default=3,
        label='リダイレクトの最大ホップ数',
        description='画像の取得で追うリダイレクトの上限。各ホップで宛先 IP を検査する。0 ならリダイレクトを追わない。',
    ),
    SettingDef(
        key='media.max_pixels', kind='int', minimum=1, default=50000000,
        label='画像の最大画素数',
        description='デコード後の画素数の上限。展開爆弾(数 KB の画像が展開時に数十 GB の画素になる)への対策はバイト数では効かず、画素数で縛る必要がある。',
    ),
    SettingDef(
        key='media.quota_bytes_per_user', kind='int', minimum=0, default=1073741824,
        label='1 利用者あたりの画像の総容量',
        description='1 人が取り込める画像の合計バイト数の上限。',
    ),
)

DEFS_BY_KEY: Final[dict[str, SettingDef]] = {d.key: d for d in REGISTRY}


# The shape of the settings. Building from config.yaml and building from the settings table yield the same types.


@dataclass(frozen=True)
class OAuthConfig:
    """Discord OAuth2 application credentials. The scopes required are identify and guilds.members.read.

    The redirect_uri is not a setting. Our own router owns the callback route (OAUTH_CALLBACK_PATH in server/app/auth.py) and the origin is server.base_url from config.yaml, so exactly one legitimate value exists. Holding it as an independent setting would allow storing a value that drifts from base_url, and then login alone breaks silently, because Discord returns the user to a place with no route to receive the authorization result. oauth_redirect_uri in server/app/auth.py derives it.
    """

    client_id: str
    client_secret: str


@dataclass(frozen=True)
class BotConfig:
    """The bot's credentials, the interval at which it checks the queue, and the intervals at which it reconciles missed events.

    Role changes, votes and comments all apply immediately from gateway events, so nothing here decides how fast a change takes effect. The intervals set only the upper bound on how long a missed event can go uncorrected.
    """

    token: str
    publish_poll_interval_seconds: int
    guild_reconcile_interval_seconds: int
    count_reconcile_interval_seconds: int
    count_reconcile_window_days: int


@dataclass(frozen=True)
class DiscordConfig:
    """The single target guild, and the auth and bot settings for it."""

    guild_id: int
    oauth: OAuthConfig
    bot: BotConfig


@dataclass(frozen=True)
class AdminConfig:
    """Settings for delegating platform admin eligibility to roles.

    Even with role_ids empty, holders of a role carrying Discord's Administrator permission and the guild owner are treated as admins (the guild owner never appears in settings, because the bot imports it as users.is_guild_owner).
    """

    role_ids: tuple[int, ...]


@dataclass(frozen=True)
class BreakingConfig:
    """Breaking-news permission and mention targets. An empty list is a valid setting for both: nobody may flag an article as breaking, and no role is mentioned."""

    allowed_role_ids: tuple[int, ...]
    mention_role_ids: tuple[int, ...]


@dataclass(frozen=True)
class ArticlesConfig:
    """Constraints on posting articles."""

    breaking: BreakingConfig
    # Rate limit on article creates and updates per API key, per hour. Automated posting can inherently run away, so the limit is per key.
    api_rate_limit_per_hour: int


@dataclass(frozen=True)
class MediaFetchConfig:
    """Limits on fetching from image URLs found in the article body. Since the feature makes the server fetch arbitrary URLs, these limits are also a safety requirement."""

    max_images_per_article: int
    max_bytes: int
    timeout_seconds: float
    total_timeout_seconds: float
    max_redirects: int


@dataclass(frozen=True)
class MediaLimits:
    """Limits on image ingest. The storage directory (storage_dir) is inseparable from the runtime environment, so it is not here; config.yaml holds it."""

    fetch: MediaFetchConfig
    # Max pixels after decoding. A byte limit is no defence against a decompression bomb: a few-KB PNG that expands to tens of GB of pixels.
    max_pixels: int
    quota_bytes_per_user: int


@dataclass(frozen=True)
class RuntimeSettings:
    """The full set of settings held in the settings table. The same type is built from config.yaml's seed values."""

    discord: DiscordConfig
    admin: AdminConfig
    articles: ArticlesConfig
    media: MediaLimits


# Validating and normalizing values. API input and config.yaml seed values pass through the same gate.


def _type_name(value: Any) -> str:
    return 'null' if value is None else type(value).__name__


def coerce_value(defn: SettingDef, raw: Any) -> Any:
    """Validate and normalize a value arriving from outside; raise ValueError with the reason when it is invalid.

    The canonical form is int for id and tuple[int, ...] for id_list. Both the config file and the API deliver them as strings, and this is the one place they are converted to int.
    """
    if defn.kind == 'id':
        try:
            return to_snowflake(raw)
        except ValueError as exc:
            raise ValueError(f'設定 {defn.key}: Discord の ID として解釈できません({exc})。') from exc

    if defn.kind == 'id_list':
        if not isinstance(raw, (list, tuple)):
            raise ValueError(f'設定 {defn.key}: ID の配列である必要があります(実際は {_type_name(raw)})。指定しない場合は空の配列にしてください。')
        ids: list[int] = []
        for index, item in enumerate(raw):
            try:
                ids.append(to_snowflake(item))
            except ValueError as exc:
                raise ValueError(f'設定 {defn.key}[{index}]: Discord の ID として解釈できません({exc})。') from exc
        # Duplicates carry no meaning, so drop them. Keep the input order, so the ordering in the UI does not rearrange itself.
        seen: dict[int, None] = {}
        for value in ids:
            seen.setdefault(value, None)
        return tuple(seen)

    if defn.kind == 'int':
        # bool derives from int, so unless it is rejected first, true would pass as 1.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f'設定 {defn.key}: 整数である必要があります(実際は {_type_name(raw)})。')
        return _check_range(defn, raw)

    if defn.kind == 'number':
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f'設定 {defn.key}: 数値である必要があります(実際は {_type_name(raw)})。')
        return float(_check_range(defn, float(raw)))

    if defn.kind in ('str', 'secret'):
        if not isinstance(raw, str):
            raise ValueError(f'設定 {defn.key}: 文字列である必要があります(実際は {_type_name(raw)})。')
        if not raw.strip():
            raise ValueError(f'設定 {defn.key}: 空にできません。')
        return raw

    raise ValueError(f'設定 {defn.key}: 未知の型 {defn.kind} です。')


def _check_range(defn: SettingDef, value: float) -> Any:
    if defn.minimum is not None:
        if defn.exclusive_minimum and value <= defn.minimum:
            raise ValueError(f'設定 {defn.key}: {defn.minimum} より大きい必要があります(実際は {value})。')
        if not defn.exclusive_minimum and value < defn.minimum:
            raise ValueError(f'設定 {defn.key}: {defn.minimum} 以上である必要があります(実際は {value})。')
    if defn.maximum is not None and value > defn.maximum:
        raise ValueError(f'設定 {defn.key}: {defn.maximum} 以下である必要があります(実際は {value})。')
    return value


def to_json_value(defn: SettingDef, value: Any) -> Any:
    """Render the canonical form as JSON. IDs become strings, because JavaScript's Number corrupts a 64-bit ID."""
    if defn.kind == 'id':
        return str(value)
    if defn.kind == 'id_list':
        return [str(item) for item in value]
    return value


# Building RuntimeSettings from flat keys.


def build(values: Mapping[str, Any]) -> RuntimeSettings:
    """Build RuntimeSettings from a flat dict. A missing key raises KeyError."""
    return RuntimeSettings(
        discord=DiscordConfig(
            guild_id=values['discord.guild_id'],
            oauth=OAuthConfig(
                client_id=values['discord.oauth.client_id'],
                client_secret=values['discord.oauth.client_secret'],
            ),
            bot=BotConfig(
                token=values['discord.bot.token'],
                publish_poll_interval_seconds=values['discord.bot.publish_poll_interval_seconds'],
                guild_reconcile_interval_seconds=values['discord.bot.guild_reconcile_interval_seconds'],
                count_reconcile_interval_seconds=values['discord.bot.count_reconcile_interval_seconds'],
                count_reconcile_window_days=values['discord.bot.count_reconcile_window_days'],
            ),
        ),
        admin=AdminConfig(role_ids=values['admin.role_ids']),
        articles=ArticlesConfig(
            breaking=BreakingConfig(
                allowed_role_ids=values['articles.breaking.allowed_role_ids'],
                mention_role_ids=values['articles.breaking.mention_role_ids'],
            ),
            api_rate_limit_per_hour=values['articles.api_rate_limit_per_hour'],
        ),
        media=MediaLimits(
            fetch=MediaFetchConfig(
                max_images_per_article=values['media.fetch.max_images_per_article'],
                max_bytes=values['media.fetch.max_bytes'],
                timeout_seconds=values['media.fetch.timeout_seconds'],
                total_timeout_seconds=values['media.fetch.total_timeout_seconds'],
                max_redirects=values['media.fetch.max_redirects'],
            ),
            max_pixels=values['media.max_pixels'],
            quota_bytes_per_user=values['media.quota_bytes_per_user'],
        ),
    )


class MissingSettingsError(RuntimeError):
    """The settings table does not have all the required keys. Happens when the seed values have not been bootstrapped."""


# Reading and writing settings.


class SettingsStore:
    """Reads and writes the settings table, holding the result for a short time.

    server and bot are separate processes, so the DB is the only path by which one tells the other about a setting it changed. The re-read interval (CACHE_TTL_SECONDS) is exactly the propagation lag.
    """

    def __init__(self, db: Database, *, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._db = db
        self._ttl = ttl
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def bootstrap(self, initial: Mapping[str, Any]) -> list[str]:
        """Seed the keys that have no row yet, and return the keys that were filled in.

        A value comes from config.yaml if it is written there, otherwise from the registry's default. When a setting with no default (one determined solely by the environment, such as the guild ID or credentials) is absent from config.yaml too, fail and list which keys are missing.

        Rows that already exist are left alone. Overwriting from config.yaml on every boot would silently revert values changed from the UI at the next restart. When a setting is added, only the new key is seeded here (a setting with a default needs no config.yaml edit).

        The missing keys are read before writing so that a normal restart INSERTs nothing. Running INSERT IGNORE over every key makes MySQL emit a duplicate-key warning for each existing row, which aiomysql relays into a Python warning. A wall of duplicate-key warnings against the settings primary key on every boot reads exactly like the config file overwriting the DB, the opposite of what this design intends.
        """
        rows = await self._db.fetch_all('SELECT setting_key FROM settings')
        existing = {str(row['setting_key']) for row in rows}
        missing = [defn for defn in REGISTRY if defn.key not in existing]
        if not missing:
            return []

        required_but_absent = [defn.key for defn in missing if defn.key not in initial and not defn.has_default]
        if required_but_absent:
            raise MissingSettingsError(
                f'初期値がありません: {", ".join(required_but_absent)}。これらの設定には既定値が無いため、初回起動時に config.yaml で与える必要があります(config.example.yaml を参照してください)。'
            )

        inserted: list[str] = []
        for defn in missing:
            raw = initial[defn.key] if defn.key in initial else defn.default
            value = coerce_value(defn, raw)
            payload = json.dumps(to_json_value(defn, value), ensure_ascii=False)
            # If server and bot first-boot at the same time, the other one can insert between our read and our write. IGNORE lets that pass: the first writer wins.
            affected = await self._db.execute(
                'INSERT IGNORE INTO settings (setting_key, value, updated_by) VALUES (%s, %s, NULL)',
                defn.key, payload,
            )
            if affected:
                inserted.append(defn.key)
        await self.refresh()
        return inserted

    async def refresh(self) -> None:
        """Drop the held values so the next read goes back to the DB."""
        async with self._lock:
            self._cache = None
            self._cached_at = 0.0

    async def raw_values(self, *, force: bool = False) -> dict[str, Any]:
        """Return the canonical form of each key in the registry."""
        async with self._lock:
            fresh = self._cache is not None and (time.monotonic() - self._cached_at) < self._ttl
            if fresh and not force:
                assert self._cache is not None
                return dict(self._cache)
            rows = await self._db.fetch_all('SELECT setting_key, value FROM settings')
            stored = {str(row['setting_key']): row['value'] for row in rows}
            values: dict[str, Any] = {}
            missing: list[str] = []
            for defn in REGISTRY:
                if defn.key not in stored:
                    missing.append(defn.key)
                    continue
                raw = stored[defn.key]
                # aiomysql returns JSON columns as strings, so decode here.
                if isinstance(raw, (str, bytes, bytearray)):
                    raw = json.loads(raw)
                values[defn.key] = coerce_value(defn, raw)
            if missing:
                raise MissingSettingsError(
                    f'settings 表に設定がありません: {", ".join(missing)}。config.yaml の初期値からの投入(起動時の bootstrap)が済んでいない可能性があります。'
                )
            self._cache = values
            self._cached_at = time.monotonic()
            return dict(values)

    async def snapshot(self, *, force: bool = False) -> RuntimeSettings:
        """Assemble and return the full set of settings."""
        return build(await self.raw_values(force=force))

    async def set_many(self, updates: Mapping[str, Any], *, updated_by: int | None) -> list[str]:
        """Partially update settings; return the keys that changed.

        Everything is validated up front, together. If even one value is invalid, nothing is written, so no half-applied settings state can be created.
        """
        if not updates:
            return []
        prepared: list[tuple[str, str]] = []
        for key, raw in updates.items():
            defn = DEFS_BY_KEY.get(key)
            if defn is None:
                raise ValueError(f'設定 {key}: 未知のキーです。')
            if not defn.editable:
                raise ValueError(f'設定 {key}: この設定は変更できません。')
            value = coerce_value(defn, raw)
            prepared.append((key, json.dumps(to_json_value(defn, value), ensure_ascii=False)))

        async with self._db.transaction() as conn:
            for key, payload in prepared:
                await self._db.execute(
                    'UPDATE settings SET value = %s, updated_by = %s WHERE setting_key = %s',
                    payload, updated_by, key, conn=conn,
                )
        # Whoever made the change sees it immediately; the other processes catch up within CACHE_TTL_SECONDS.
        await self.refresh()
        return [key for key, _ in prepared]

    async def describe(self) -> list[dict[str, Any]]:
        """The definitions and current values from which the UI generates itself.

        A secret setting returns no raw value, only whether it is set. Returning one from the API would hand the bot token to everyone who can open the UI.

        **Everything needed to build an input field is passed from here, down to the range (minimum / maximum / exclusive_minimum) and the ref.** Whatever is not passed, the UI has no way to know, so it would end up duplicating the registry's knowledge, and every duplicated piece drifts from the registry. For the range, that surfaces as learning the max from a 400 only after pressing save; for the ref, as a picker appearing only for the settings the UI branched on by key name.
        """
        values = await self.raw_values()
        rows = await self._db.fetch_all('SELECT setting_key, updated_at, updated_by FROM settings')
        meta = {str(row['setting_key']): row for row in rows}
        result: list[dict[str, Any]] = []
        for defn in REGISTRY:
            row = meta.get(defn.key)
            entry: dict[str, Any] = {
                'key': defn.key,
                'kind': defn.kind,
                'label': defn.label,
                'description': defn.description,
                'editable': defn.editable,
                'restart_required': defn.restart_required,
                'secret': defn.secret,
                'ref': defn.ref,
                'minimum': defn.minimum,
                'exclusive_minimum': defn.exclusive_minimum,
                'maximum': defn.maximum,
                'updated_at': row['updated_at'] if row else None,
                'updated_by': str(row['updated_by']) if row and row['updated_by'] is not None else None,
            }
            if defn.secret:
                entry['value'] = None
                entry['is_set'] = bool(values.get(defn.key))
            else:
                entry['value'] = to_json_value(defn, values[defn.key])
                entry['is_set'] = True
            result.append(entry)
        return result


def known_keys() -> Iterable[str]:
    """The keys in the registry. Used for the CLI's input completion and validation."""
    return (defn.key for defn in REGISTRY)
