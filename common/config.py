"""Load config.yaml and copy it into frozen config objects.

This file handles two kinds of settings.

config.yaml is always the source of truth for exactly three values: server, database, and the image storage directory (storage_dir). The first two are needed before settings can be read from the DB (the DB's own config cannot be read out of the DB). storage_dir is inseparable from the named volume's mount point and is decided outside the container: change only this value without moving the mount and no images are found, and every existing image becomes unreachable the moment it changes, so there is no point letting the UI touch it. These three are required.

Everything else (the discord / admin / articles / site sections and the media limits) is only a seed value copied into the settings table on first boot and never read again. The source of truth is the DB, and admins change it from the frontend. See settings.py.

The seed-value sections are **optional**. Keys left out fall back to the registry's defaults (REGISTRY in settings.py). They are optional for two reasons. First, once the seed values are in, those keys need not stay in config.yaml, and requiring them would contradict the "never read again" description. Second, requiring them would mean every added setting stops existing deployments from booting until their config.yaml is edited. Only settings that can have no default (values determined solely by the environment, such as the guild ID or credentials) must be supplied at first boot; if one is missing, settings.py's bootstrap fails and lists which keys are missing.

A missing required key, a wrong type, or an out-of-range value raises ValueError at startup instead of being silently filled in with a default. Papering over a config mistake with a default yields "for some reason notifications never fire" or "for some reason nobody becomes an admin": startup succeeds while behaviour alone is quietly wrong, and the cause is hard to track down.

The registry's coerce_value validates the type and range of seed values, keeping a single gate so that seeding from the config file and changes from the API cannot validate differently.

IDs (guild_id / role_ids / channel_id) must be written as strings in YAML. Written as YAML integers, a 64-bit ID risks losing precision somewhere along the paths that handle it.
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

# Config file lookup order: argument, then environment variable, then config.yaml at the repo root.
CONFIG_ENV_VAR: Final = 'INSTANT_NEWS_CONFIG'
DEFAULT_CONFIG_FILENAME: Final = 'config.yaml'
_REPO_ROOT: Final = Path(__file__).resolve().parent.parent
_ROOT_LABEL: Final = '(トップレベル)'

# Sections allowed at the top level.
_TOP_LEVEL: Final = ('server', 'database', 'discord', 'admin', 'articles', 'media', 'site')
# Sections that may carry seed values. Every leaf appearing here must be a registry key, except media.storage_dir.
_BOOTSTRAP_SECTIONS: Final = ('discord', 'admin', 'articles', 'media', 'site')
# The media section is the only one where a runtime-environment value (storage_dir) sits alongside seed values (the ingest limits).
_MEDIA_RUNTIME_KEY: Final = 'media.storage_dir'


@dataclass(frozen=True)
class ServerConfig:
    """Where the HTTP server listens, and the externally reachable public URL."""

    host: str
    port: int
    # Used to build article URLs, image URLs, and the redirect_uri Discord returns to after OAuth2 authorization. A trailing / is stripped at load time, so base_url + '/path' is safe to concatenate.
    base_url: str


@dataclass(frozen=True)
class DatabaseConfig:
    """The MySQL connection target."""

    host: str
    port: int
    name: str
    user: str
    password: str


@dataclass(frozen=True)
class MediaConfig:
    """The values image ingest actually uses: the storage directory from config.yaml and the limits from the settings table, bundled into one despite the differing origins.

    effective_media_config() folds them together here so the media modules never have to care that the values come from two places.
    """

    storage_dir: Path
    fetch: MediaFetchConfig
    max_pixels: int
    quota_bytes_per_user: int


@dataclass(frozen=True)
class Config:
    """The contents of config.yaml."""

    server: ServerConfig
    database: DatabaseConfig
    # Directory holding the image blobs. Must match the named volume's mount point.
    storage_dir: Path
    # Seed values copied into the settings table on first boot. A flat dict keyed by registry key, holding only what config.yaml wrote (keys it omits fall back to the registry's defaults). Keys that already have a value are not overwritten, so later boots do nothing.
    bootstrap: Mapping[str, Any]


def effective_media_config(config: Config, settings: RuntimeSettings) -> MediaConfig:
    """Assemble the effective config handed to the media modules: storage directory from config.yaml, limits from the settings table."""
    return MediaConfig(
        storage_dir=config.storage_dir,
        fetch=settings.media.fetch,
        max_pixels=settings.media.max_pixels,
        quota_bytes_per_user=settings.media.quota_bytes_per_user,
    )


# Loader helpers. Each raises a ValueError naming which key is wrong and why.


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
    """Reject keys config.example.yaml does not have, so that a misspelling or a misplaced nesting level cannot lurk as a silently ignored key that leaves the default in force."""
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
    """Read an absolute http or https URL. A trailing / is stripped so that base_url + '/path' does not concatenate into //."""
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
    # bool derives from int, so unless it is rejected first, true would pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'設定 {full}: 整数である必要があります(実際は {_type_name(value)})。')
    if minimum is not None and value < minimum:
        raise ValueError(f'設定 {full}: {minimum} 以上である必要があります(実際は {value})。')
    if maximum is not None and value > maximum:
        raise ValueError(f'設定 {full}: {maximum} 以下である必要があります(実際は {value})。')
    return value


# Building each config section.


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
    """Walk a mapping and yield each leaf (non-mapping) value with its dotted path. A list of IDs counts as a leaf."""
    for key, value in node.items():
        path = _child(prefix, str(key))
        if isinstance(value, Mapping):
            yield from _walk_leaves(value, path)
        else:
            yield path, value


def _collect_bootstrap(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Build a flat dict keyed by registry key out of the seed-value sections.

    Only keys actually written are picked up; the registry holds the defaults for missing keys, so nothing is filled in here. Value validation is delegated to the registry's coerce_value, keeping one gate for both seeding from the config file and changes from the API.
    """
    found: dict[str, Any] = {}
    for section in _BOOTSTRAP_SECTIONS:
        if section not in raw:
            continue
        node = raw[section]
        if not isinstance(node, Mapping):
            raise ValueError(f'設定 {section}: マッピングである必要があります(実際は {_type_name(node)})。')
        for path, value in _walk_leaves(node, section):
            # The media section is the only one carrying a runtime-environment value alongside seed values; this one is not a seed value.
            if path == _MEDIA_RUNTIME_KEY:
                continue
            defn = DEFS_BY_KEY.get(path)
            if defn is None:
                raise ValueError(f'設定 {path}: 未知のキーです。config.example.yaml を参照してください。')
            found[path] = _coerce_bootstrap_value(defn.key, value)
    return found


def _coerce_bootstrap_value(key: str, value: Any) -> Any:
    """Validate and normalize a single seed value. Reuses the registry's validation as-is, adding a pointer to the policy that IDs are written as strings."""
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
        # safe_load, so the file cannot have arbitrary Python objects constructed from it.
        raw = yaml.safe_load(stream)
    if raw is None:
        raise ValueError(f'設定ファイルが空です: {resolved}')
    if not isinstance(raw, Mapping):
        raise ValueError(f'設定ファイルの最上位はマッピングである必要があります(実際は {_type_name(raw)}): {resolved}')
    return resolved, raw


def load_config(path: str | Path | None = None) -> Config:
    """Load the config file. Lookup order: argument, then the INSTANT_NEWS_CONFIG environment variable, then config.yaml at the repo root.

    Raises FileNotFoundError if the file is missing, ValueError if its contents are invalid.
    """
    resolved, raw = _read_raw(path)
    try:
        return _build_config(raw)
    except ValueError as exc:
        raise ValueError(f'{resolved}: {exc}') from exc


def load_database_config(path: str | Path | None = None) -> DatabaseConfig:
    """Read only the database section of the config file.

    This exists for the CLI. The CLI is the only recovery path once a setting changed from the UI by mistake has locked you out, and that recovery path must not depend on the config file's other sections being up to date. Since reaching the DB is all it takes to do the job, the connection target is all this requires. Lookup and validation are load_config's, so the two cannot disagree about which config files are acceptable.
    """
    resolved, raw = _read_raw(path)
    try:
        return _build_database(raw, 'database', '')
    except ValueError as exc:
        raise ValueError(f'{resolved}: {exc}') from exc
