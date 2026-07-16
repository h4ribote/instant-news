"""プラットフォームの設定。真実は MySQL の settings 表にあり、config.yaml の対応する値は初回起動時の初期値としてのみ使う。

config.yaml が常に真実であり続けるのは、設定を DB から読むために先に要る値(server・database)と、コンテナの外で決まる値(画像の保存先。名前付きボリュームのマウント先と一致していなければ意味を成さない)だけである。それ以外は管理者がフロントエンドから変更できる必要があり、設定ファイルの書き換えとコンテナの再起動を要求する理由がない。

起動のたびに設定ファイルで DB を上書きしてはならない。画面から変更した値が次の再起動で黙って元へ戻り、「設定したはずの値が効いていない」という最も原因を追いにくい状態になる。どちらが真実かは一方に決める必要があり、ここでは DB を真実とする。

型・既定値・検証・秘密かどうか・変更に再起動を要するかは、この登録簿(REGISTRY)がコード側で定義する。settings 表は値だけを持つ。設定を増やすときは登録簿に足すだけでよく、表の変更も画面の変更も要らない(画面は登録簿を読んで生成する)。

値の JSON 表現では Discord の ID を文字列にする。JavaScript の Number は 2^53 までしか正確に表せず、スノーフレークは黙って壊れる。
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

# 設定の再読み込み間隔(秒)。server と bot は別のプロセスであり、一方が変更した設定を他方が知る手段はこの読み直ししかない。短くすると変更が早く伝わるが DB への問い合わせが増える。設定の変更は稀であり、数秒の遅れは実害にならない。変更した本人には即座に反映される(set_many が自プロセスの caches を捨てるため)。
CACHE_TTL_SECONDS: Final = 10.0

SettingKind = Literal['id', 'id_list', 'int', 'number', 'str', 'secret']

# ID がどの実体を指すかを表す標識。kind は値の形(id か id_list か)しか語らないため、これが無いと「この設定はロールの配列である」ことを画面へ伝える手段が無く、画面はキー名を直に見て分岐するしかない。キー名で分岐させると「画面にキーを直書きしない」という登録簿の設計が崩れ、後から別の実体を指す id_list を足したときに、その欄までロールの選択肢が並ぶ。
# 参照先の実体を DB に持つものだけを表せる。チャンネル(channel)を足すには、bot が discord_roles と同じようにチャンネルを取り込む必要がある(現在その表は無く、通知先チャンネルは ID の直接入力である)。
SettingRef = Literal['role']


class _Required:
    """既定値を持たない設定であることを表す標識。この設定は初回起動時に config.yaml で与える必要がある。"""

    def __repr__(self) -> str:
        return '<必須>'


# 既定値を持てない設定に使う。guild の ID や資格情報のように、その環境でしか決まらない値には妥当な既定が存在しない。
REQUIRED: Final = _Required()


@dataclass(frozen=True)
class SettingDef:
    """設定 1 つの定義。画面の生成・検証・秘密の扱いのすべてがこの定義から決まる。"""

    key: str
    kind: SettingKind
    label: str
    description: str
    # 変更できない設定。値は画面に表示するが書き換えさせない。
    editable: bool = True
    # 変更してもプロセスが読み直さない設定。画面はその旨を示す。
    restart_required: bool = False
    # ID がどの実体を指すか。kind が id / id_list の設定にだけ意味がある。画面はこれを見て、ID の直接入力の代わりに実体の選択 UI を出す。
    ref: SettingRef | None = None
    minimum: float | None = None
    # minimum を「以上」ではなく「より大きい」として扱う。0 に意味が無い設定(タイムアウト等)に使う。
    exclusive_minimum: bool = False
    maximum: float | None = None
    # 初回起動時に config.yaml がこの設定を持たない場合に使う既定値。REQUIRED なら config.yaml で与える必要がある。既定値を持たせることで、設定を増やしても既存のデプロイの config.yaml を編集せずに済む。
    default: Any = REQUIRED

    @property
    def secret(self) -> bool:
        """生値を API から返してはならない設定か。設定済みかどうかだけを返す。"""
        return self.kind == 'secret'

    @property
    def has_default(self) -> bool:
        return self.default is not REQUIRED


# 設定の登録簿。ここに無いキーは読み書きしない(settings 表に古い行が残っていても無視される)。
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


# --------------------------------------------------------------------------
# 設定値の形。config.yaml から組み立てても settings 表から組み立てても同じ型になる。
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthConfig:
    """Discord OAuth2 のアプリケーション資格情報。必要なスコープは identify と guilds.members.read。

    認可後に戻る URL(redirect_uri)は設定として持たない。戻り先の経路はこちらのルーターが決めており(server/app/auth.py の OAUTH_CALLBACK_PATH)、オリジンは config.yaml の server.base_url であるため、入りうる正当な値は 1 つしかない。設定として独立に持つと、base_url と食い違った値を保存できてしまい、そのときログインだけが静かに壊れる(Discord が、認可の結果を受け取る経路が無い場所へ戻す)。導出は server/app/auth.py の oauth_redirect_uri が行う。
    """

    client_id: str
    client_secret: str


@dataclass(frozen=True)
class BotConfig:
    """bot の資格情報と、待ち行列を見に行く間隔・取りこぼしを照合する間隔。

    ロールの変更も投票もコメントもゲートウェイのイベントで即座に反映されるため、ここに「反映の速さ」を決める設定は無い。間隔が決めるのは、イベントを取りこぼした場合にそれが正されるまでの上限だけである。
    """

    token: str
    publish_poll_interval_seconds: int
    guild_reconcile_interval_seconds: int
    count_reconcile_interval_seconds: int
    count_reconcile_window_days: int


@dataclass(frozen=True)
class DiscordConfig:
    """対象とする単一 guild と、その guild に対する認証・bot の設定。"""

    guild_id: int
    oauth: OAuthConfig
    bot: BotConfig


@dataclass(frozen=True)
class AdminConfig:
    """プラットフォーム管理の資格をロールへ委譲するための設定。

    role_ids が空でも、Discord の Administrator 権限を持つロールの保有者と guild オーナーは管理者として扱われる(guild オーナーは users.is_guild_owner として bot が取り込むため、設定には現れない)。
    """

    role_ids: tuple[int, ...]


@dataclass(frozen=True)
class BreakingConfig:
    """速報の権限と mention 先。どちらも空のリストが「誰も立てられない」「mention しない」という有効な設定である。"""

    allowed_role_ids: tuple[int, ...]
    mention_role_ids: tuple[int, ...]


@dataclass(frozen=True)
class ArticlesConfig:
    """記事の投稿に関する制約。"""

    breaking: BreakingConfig
    # API キー 1 本あたりの記事の作成・更新の上限(1 時間あたり)。自動投稿は本質的に暴走しうるため、キー単位で上限を設ける。
    api_rate_limit_per_hour: int


@dataclass(frozen=True)
class MediaFetchConfig:
    """本文中の画像 URL からの取得に関する上限。任意の URL をサーバーに取得させる機能であるため、上限は安全側の要件でもある。"""

    max_images_per_article: int
    max_bytes: int
    timeout_seconds: float
    total_timeout_seconds: float
    max_redirects: int


@dataclass(frozen=True)
class MediaLimits:
    """画像の取り込みの上限。保存先(storage_dir)は実行環境と一体であるため、ここには含めず config.yaml が持つ。"""

    fetch: MediaFetchConfig
    # デコード後の最大画素数。展開爆弾(数 KB の PNG が展開時に数十 GB の画素になる)への対策はバイト数では効かない。
    max_pixels: int
    quota_bytes_per_user: int


@dataclass(frozen=True)
class RuntimeSettings:
    """settings 表が持つ設定の一式。config.yaml の初期値からも同じ型を組み立てる。"""

    discord: DiscordConfig
    admin: AdminConfig
    articles: ArticlesConfig
    media: MediaLimits


# --------------------------------------------------------------------------
# 値の検証と正規化。API からの入力も config.yaml の初期値も同じ関門を通す。
# --------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    return 'null' if value is None else type(value).__name__


def coerce_value(defn: SettingDef, raw: Any) -> Any:
    """外部から来た値を検証して正規化する。不正なら理由を含む ValueError を送出する。

    id は int、id_list は tuple[int, ...] を正規形とする。設定ファイルからも API からも文字列で受け取り、ここで一度だけ int へ寄せる。
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
        # 重複は意味を持たないため落とす。順序は入力のまま保つ(画面での並びが勝手に変わらないようにするため)。
        seen: dict[int, None] = {}
        for value in ids:
            seen.setdefault(value, None)
        return tuple(seen)

    if defn.kind == 'int':
        # bool は int の派生であるため、先に弾かないと true が 1 として通ってしまう。
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
    """正規形を JSON へ落とす。ID は文字列にする(JavaScript の Number では 64bit の ID が壊れるため)。"""
    if defn.kind == 'id':
        return str(value)
    if defn.kind == 'id_list':
        return [str(item) for item in value]
    return value


# --------------------------------------------------------------------------
# 平坦なキーから RuntimeSettings を組み立てる
# --------------------------------------------------------------------------


def build(values: Mapping[str, Any]) -> RuntimeSettings:
    """平坦な辞書から RuntimeSettings を組み立てる。キーの欠落は KeyError。"""
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
    """settings 表に必要なキーが揃っていない。初期値の投入(bootstrap)が済んでいない場合に起こる。"""


# --------------------------------------------------------------------------
# 設定の読み書き
# --------------------------------------------------------------------------


class SettingsStore:
    """settings 表を読み書きし、短い時間だけ結果を保持する。

    server と bot は別のプロセスであるため、一方が変更した設定を他方へ伝える経路は DB しかない。読み直しの間隔(CACHE_TTL_SECONDS)がそのまま伝播の遅れになる。
    """

    def __init__(self, db: Database, *, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._db = db
        self._ttl = ttl
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def bootstrap(self, initial: Mapping[str, Any]) -> list[str]:
        """未登録のキーを初期値で埋め、埋めたキーを返す。

        値の出どころは、config.yaml に書かれていればその値、無ければ登録簿の既定値。既定値を持たない設定(guild の ID や資格情報のように、その環境でしか決まらない値)が config.yaml にも無い場合は、どのキーが足りないのかを挙げて落とす。

        既にある行は触らない。起動のたびに config.yaml で上書きすると、画面から変更した値が次の再起動で黙って元へ戻る。設定を増やした場合、新しいキーだけがここで埋まる(既定値を持つ設定なら config.yaml を編集せずに済む)。

        欠けているキーを先に読み出してから書くのは、通常の再起動で 1 件も INSERT しないためである。全キーを INSERT IGNORE で流すと、MySQL が既存の行ごとに重複キーの警告を出し、aiomysql がそれを Python の警告へ写す。起動のたびに settings の主キーに対する重複の警告が並ぶと、まさに設定ファイルが DB を上書きしているように読め、この設計の意図と正反対の印象を与える。
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
            # server と bot が同時に初回起動した場合、読み出しから書き込みまでの間に相手が入れうる。先に入れた方を正とするため IGNORE で受け流す。
            affected = await self._db.execute(
                'INSERT IGNORE INTO settings (setting_key, value, updated_by) VALUES (%s, %s, NULL)',
                defn.key, payload,
            )
            if affected:
                inserted.append(defn.key)
        await self.refresh()
        return inserted

    async def refresh(self) -> None:
        """保持している値を捨て、次の読み取りで DB から読み直させる。"""
        async with self._lock:
            self._cache = None
            self._cached_at = 0.0

    async def raw_values(self, *, force: bool = False) -> dict[str, Any]:
        """登録簿のキーごとの正規形を返す。"""
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
                # aiomysql は JSON 列を文字列で返すため、ここで解く。
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
        """設定の一式を組み立てて返す。"""
        return build(await self.raw_values(force=force))

    async def set_many(self, updates: Mapping[str, Any], *, updated_by: int | None) -> list[str]:
        """設定を部分更新する。変更したキーを返す。

        検証は全件まとめて先に行う。1 つでも不正なら何も書かない(一部だけ適用された中途半端な設定を作らないため)。
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
        # 変更した本人には即座に反映する。他のプロセスは CACHE_TTL_SECONDS 以内に追いつく。
        await self.refresh()
        return [key for key, _ in prepared]

    async def describe(self) -> list[dict[str, Any]]:
        """画面を生成するための、定義と現在値の一覧。

        秘密の設定は生値を返さず、設定済みかどうかだけを返す。API から返した時点で、画面を開ける者すべてに bot のトークンが渡ることになる。

        **入力欄の生成に要る記述は、値域(minimum / maximum / exclusive_minimum)と参照先(ref)まで含めてここが全て渡す。**渡さない項目は画面が知る術を持たず、画面側に同じ知識を書き写すことになる。書き写した分だけ登録簿と食い違い、値域なら「保存を押して初めて 400 で上限を知る」形で、参照先なら「キー名で分岐した設定にだけ選択 UI が出る」形で現れる。
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
    """登録簿にあるキー。CLI の入力補助と検証に使う。"""
    return (defn.key for defn in REGISTRY)
