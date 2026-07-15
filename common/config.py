"""config.yaml を読み込み、凍結した設定オブジェクトへ写す。

本ファイルが扱う設定は 2 種類ある。

常に config.yaml が真実である値は、server・database と、画像の保存先(storage_dir)だけである。前の 2 つは設定を DB から読むために先に要る(DB へ繋ぐ前に DB の設定は読めない)。storage_dir は名前付きボリュームのマウント先と一体であり、コンテナの外で決まる。マウントを変えずにこの値だけを変えても画像は見つからず、変えた瞬間に既存の全画像が参照できなくなるため、画面から触らせる意味がない。これらは必須である。

それ以外(discord / admin / articles / media の各上限)は初回起動時に settings 表へ写す初期値でしかなく、以後は参照されない。真実は DB 側にあり、管理者はフロントエンドから変更する。詳細は settings.py を参照。

初期値の節は**任意**である。書かれていないキーは登録簿(settings.py の REGISTRY)の既定値で埋まる。任意にしているのは 2 つの理由による。第 1 に、初期値が投入された後の config.yaml にそれらのキーが残っている必要はなく、必須にすると「以後は参照されない」という説明と実際の挙動が食い違う。第 2 に、必須にすると設定を 1 つ増やすたびに既存のデプロイが config.yaml を編集するまで起動しなくなる。既定値を持てない設定(guild の ID や資格情報のように、その環境でしか決まらない値)だけは初回起動時に与える必要があり、欠けていれば settings.py の bootstrap がどのキーが足りないのかを挙げて落とす。

必須キーの欠落・型不正・値域外は起動時に ValueError で落とし、既定値で黙って埋めない。設定の誤りを既定値で補うと「なぜか通知が飛ばない」「なぜか誰も管理者にならない」といった、起動には成功するが挙動だけが静かに間違っている状態になり、原因の特定が難しくなる。

初期値の型と値域の検証は登録簿の coerce_value が行う。設定ファイルからの投入と API からの変更で検証が食い違わないよう、関門を 1 つにしている。

ID(guild_id / role_ids / channel_id)は YAML では文字列として書かせる。YAML の整数として書かせると、64bit の ID を扱う経路のどこかで精度を落とす余地が残る。
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml

from .settings import DEFS_BY_KEY, MediaFetchConfig, RuntimeSettings, coerce_value

# 設定ファイルの探索は、引数 → 環境変数 → リポジトリルートの config.yaml の順。
CONFIG_ENV_VAR: Final = 'INSTANT_NEWS_CONFIG'
DEFAULT_CONFIG_FILENAME: Final = 'config.yaml'
_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
_ROOT_LABEL: Final = '(トップレベル)'

# 最上位に置ける節。
_TOP_LEVEL: Final = ('server', 'database', 'discord', 'admin', 'articles', 'media')
# 初期値を書ける節。ここに現れる葉は、media.storage_dir を除いてすべて登録簿のキーでなければならない。
_BOOTSTRAP_SECTIONS: Final = ('discord', 'admin', 'articles', 'media')
# media の節だけは実行環境の値(storage_dir)と初期値(取り込みの上限)が同居する。
_MEDIA_RUNTIME_KEY: Final = 'media.storage_dir'


@dataclass(frozen=True)
class ServerConfig:
    """HTTP サーバーの待ち受けと、外部から到達可能な公開 URL。"""

    host: str
    port: int
    # 記事 URL・画像 URL・OAuth2 の認可後に戻る URL の組み立てに使う。末尾の / は読み込み時に落とすため、base_url + '/path' で連結してよい。
    base_url: str


@dataclass(frozen=True)
class DatabaseConfig:
    """MySQL の接続先。"""

    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class MediaConfig:
    """画像の取り込みが実際に使う設定。保存先は config.yaml、上限は settings 表と、出どころが異なる値を 1 つに束ねたもの。

    出どころが違うことを画像の各モジュールへ持ち込まないために、effective_media_config() がここへ畳んでから渡す。
    """

    storage_dir: Path
    fetch: MediaFetchConfig
    max_pixels: int
    quota_bytes_per_user: int


@dataclass(frozen=True)
class Config:
    """config.yaml の内容。"""

    server: ServerConfig
    database: DatabaseConfig
    # 画像の実体を置くディレクトリ。名前付きボリュームのマウント先と一致させること。
    storage_dir: Path
    # 初回起動時に settings 表へ写す初期値。登録簿のキーで引ける平坦な辞書で、config.yaml に書かれたものだけを含む(書かれていないキーは登録簿の既定値で埋まる)。既に値のあるキーは上書きしないため、2 回目以降の起動では何も起きない。
    bootstrap: Mapping[str, Any]


def effective_media_config(config: Config, settings: RuntimeSettings) -> MediaConfig:
    """画像のモジュールへ渡す実効設定を組み立てる。保存先は config.yaml、上限は settings 表から取る。"""
    return MediaConfig(
        storage_dir=config.storage_dir,
        fetch=settings.media.fetch,
        max_pixels=settings.media.max_pixels,
        quota_bytes_per_user=settings.media.quota_bytes_per_user,
    )


# --------------------------------------------------------------------------
# 読み込みの補助。いずれも「どのキーが」「なぜ」不正なのかを含む ValueError を投げる。
# --------------------------------------------------------------------------


def _child(path: str, key: str) -> str:
    return f'{path}.{key}' if path else key


def _label(path: str) -> str:
    return path or _ROOT_LABEL


def _type_name(value: Any) -> str:
    return 'null' if value is None else type(value).__name__


def _get(node: Mapping[str, Any], key: str, path: str) -> tuple[Any, str]:
    full = _child(path, key)
    if key not in node:
        raise ValueError(f'設定 {full}: 必須キーがありません。config.example.yaml を参照してください。')
    return node[key], full


def _reject_unknown(node: Mapping[str, Any], allowed: tuple[str, ...], path: str) -> None:
    """config.example.yaml に無いキーを拒否する。綴りの誤りや階層の取り違えが「無視されて既定のまま動く」形で潜伏するのを防ぐ。"""
    unknown = sorted(str(key) for key in node if key not in allowed)
    if unknown:
        raise ValueError(f'設定 {_label(path)}: 未知のキーがあります: {", ".join(unknown)}。指定できるのは {", ".join(allowed)} です。')


def _section(parent: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value, full = _get(parent, key, path)
    if not isinstance(value, Mapping):
        raise ValueError(f'設定 {full}: マッピングである必要があります(実際は {_type_name(value)})。')
    return value


def _str(node: Mapping[str, Any], key: str, path: str) -> str:
    value, full = _get(node, key, path)
    if not isinstance(value, str):
        raise ValueError(f'設定 {full}: 文字列である必要があります(実際は {_type_name(value)})。')
    if not value.strip():
        raise ValueError(f'設定 {full}: 空にできません。')
    return value


def _url(node: Mapping[str, Any], key: str, path: str) -> str:
    """http または https の絶対 URL を読む。末尾の / は落とす(base_url + '/path' の連結で // にならないようにするため)。"""
    value = _str(node, key, path).strip()
    full = _child(path, key)
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f'設定 {full}: http または https で始まる URL である必要があります: {value!r}')
    if not parsed.netloc:
        raise ValueError(f'設定 {full}: ホストを含む絶対 URL である必要があります: {value!r}')
    return value.rstrip('/')


def _int(node: Mapping[str, Any], key: str, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    value, full = _get(node, key, path)
    # bool は int の派生であるため、先に弾かないと true が 1 として通ってしまう。
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'設定 {full}: 整数である必要があります(実際は {_type_name(value)})。')
    if minimum is not None and value < minimum:
        raise ValueError(f'設定 {full}: {minimum} 以上である必要があります(実際は {value})。')
    if maximum is not None and value > maximum:
        raise ValueError(f'設定 {full}: {maximum} 以下である必要があります(実際は {value})。')
    return value


# --------------------------------------------------------------------------
# 各節の組み立て
# --------------------------------------------------------------------------


def _build_server(parent: Mapping[str, Any], key: str, path: str) -> ServerConfig:
    node = _section(parent, key, path)
    here = _child(path, key)
    _reject_unknown(node, ('host', 'port', 'base_url'), here)
    return ServerConfig(
        host=_str(node, 'host', here),
        port=_int(node, 'port', here, minimum=1, maximum=65535),
        base_url=_url(node, 'base_url', here),
    )


def _build_database(parent: Mapping[str, Any], key: str, path: str) -> DatabaseConfig:
    node = _section(parent, key, path)
    here = _child(path, key)
    _reject_unknown(node, ('host', 'port', 'name', 'user', 'password'), here)
    return DatabaseConfig(
        host=_str(node, 'host', here),
        port=_int(node, 'port', here, minimum=1, maximum=65535),
        name=_str(node, 'name', here),
        user=_str(node, 'user', here),
        password=_str(node, 'password', here),
    )


def _walk_leaves(node: Mapping[str, Any], prefix: str) -> Iterator[tuple[str, Any]]:
    """マッピングを辿り、葉(マッピング以外)の値をドット区切りのパスとともに返す。ID の配列は葉として扱う。"""
    for key, value in node.items():
        path = _child(prefix, str(key))
        if isinstance(value, Mapping):
            yield from _walk_leaves(value, path)
        else:
            yield path, value


def _collect_bootstrap(raw: Mapping[str, Any]) -> dict[str, Any]:
    """初期値の節から、登録簿のキーで引ける平坦な辞書を作る。

    書かれているキーだけを拾う。欠けているキーの既定値は登録簿が持つため、ここでは埋めない。値の検証は登録簿の coerce_value に委ね、設定ファイルからの投入と API からの変更で関門を 1 つにする。
    """
    found: dict[str, Any] = {}
    for section in _BOOTSTRAP_SECTIONS:
        if section not in raw:
            continue
        node = raw[section]
        if not isinstance(node, Mapping):
            raise ValueError(f'設定 {section}: マッピングである必要があります(実際は {_type_name(node)})。')
        for path, value in _walk_leaves(node, section):
            # media の節にだけは実行環境の値が同居する。これは初期値ではない。
            if path == _MEDIA_RUNTIME_KEY:
                continue
            defn = DEFS_BY_KEY.get(path)
            if defn is None:
                raise ValueError(f'設定 {path}: 未知のキーです。config.example.yaml を参照してください。')
            found[path] = _coerce_bootstrap_value(defn.key, value)
    return found


def _coerce_bootstrap_value(key: str, value: Any) -> Any:
    """初期値 1 つを検証して正規化する。登録簿の検証をそのまま使い、ID は文字列で書かせる方針の案内を添える。"""
    defn = DEFS_BY_KEY[key]
    try:
        return coerce_value(defn, value)
    except ValueError as exc:
        if defn.kind in ('id', 'id_list'):
            raise ValueError(f'{exc} ID は文字列として引用符で囲んで書いてください。') from exc
        raise


def _build_config(raw: Mapping[str, Any]) -> Config:
    _reject_unknown(raw, _TOP_LEVEL, '')
    media_node = _section(raw, 'media', '')
    return Config(
        server=_build_server(raw, 'server', ''),
        database=_build_database(raw, 'database', ''),
        storage_dir=Path(_str(media_node, 'storage_dir', 'media')).expanduser(),
        bootstrap=_collect_bootstrap(raw),
    )


def _resolve_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    return _REPO_ROOT / DEFAULT_CONFIG_FILENAME


def _read_raw(path: str | Path | None) -> tuple[Path, Mapping[str, Any]]:
    resolved = _resolve_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f'設定ファイルが見つかりません: {resolved}。config.example.yaml をコピーして作成してください({CONFIG_ENV_VAR} で場所を指定することもできます)。')
    with resolved.open('r', encoding='utf-8') as stream:
        # 任意の Python オブジェクトを構築させないため safe_load を使う。
        raw = yaml.safe_load(stream)
    if raw is None:
        raise ValueError(f'設定ファイルが空です: {resolved}')
    if not isinstance(raw, Mapping):
        raise ValueError(f'設定ファイルの最上位はマッピングである必要があります(実際は {_type_name(raw)}): {resolved}')
    return resolved, raw


def load_config(path: str | Path | None = None) -> Config:
    """設定ファイルを読み込む。探索順は 引数 → 環境変数 INSTANT_NEWS_CONFIG → リポジトリルートの config.yaml。

    ファイルが見つからない場合は FileNotFoundError、内容が不正な場合は ValueError を送出する。
    """
    resolved, raw = _read_raw(path)
    try:
        return _build_config(raw)
    except ValueError as exc:
        raise ValueError(f'{resolved}: {exc}') from exc


def load_database_config(path: str | Path | None = None) -> DatabaseConfig:
    """設定ファイルから database 節だけを読む。

    CLI のためにある。CLI は設定を画面から誤って変更して締め出された場合の唯一の復旧手段であり、その復旧手段が「設定ファイルの他の節が最新であること」に依存してはならない。DB へ繋げれば仕事ができる以上、要求するのも接続先だけにする。探索と検証は load_config と同じものを使い、受け付ける設定ファイルが両者で食い違わないようにする。
    """
    resolved, raw = _read_raw(path)
    try:
        return _build_database(raw, 'database', '')
    except ValueError as exc:
        raise ValueError(f'{resolved}: {exc}') from exc
