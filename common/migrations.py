"""Forward-only schema migrations: the files in migrations/, the schema_migrations history table, and the startup check that code and schema agree.

A migration is migrations/NNNN_<name>.sql, numbered from 0001 without gaps. Applied files are never edited; a schema change is always a new file. The history table records each applied version with its checksum, so an edited file, a version the code does not know (a DB newer than the code) and a run that failed half-way (dirty) are all detected instead of being silently skipped.

MySQL commits DDL implicitly, so a file cannot be applied atomically. A version's row is written with finished_at NULL before its SQL runs and completed after, which is how a failed or interrupted run stays visible as dirty until an operator resolves it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

import aiomysql
import pymysql
from aiomysql import Connection, DictCursor
from pymysql.constants import CLIENT

from .config import DatabaseConfig
from .db import CONNECT_TIMEOUT_SECONDS, Database

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent.parent / 'migrations'
HISTORY_TABLE: Final = 'schema_migrations'
LOCK_TIMEOUT_SECONDS: Final = 60

_FILENAME: Final = re.compile(r'^(\d{4})_([a-z0-9_]+)\.sql$')

_CREATE_HISTORY: Final = f"""
CREATE TABLE {HISTORY_TABLE} (
    version      INT UNSIGNED NOT NULL,
    name         VARCHAR(100) NOT NULL,
    checksum     CHAR(64)     NOT NULL,
    started_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  DATETIME     NULL,
    PRIMARY KEY (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

ResolveMode = Literal['retry', 'applied']


class MigrationError(Exception):
    """The migrations cannot be applied or the schema does not match the code. The message says what to do."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    filename: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    started_at: datetime
    finished_at: datetime | None

    @property
    def dirty(self) -> bool:
        return self.finished_at is None


@dataclass(frozen=True)
class SchemaStatus:
    """What the code ships against what the database records.

    legacy_tables is non-empty only when the history table is missing while other tables exist: a database built before this mechanism, which it cannot adopt.
    """

    migrations: tuple[Migration, ...]
    applied: Mapping[int, AppliedMigration]
    legacy_tables: tuple[str, ...]
    has_history: bool

    @property
    def pending(self) -> list[Migration]:
        return [m for m in self.migrations if m.version not in self.applied]

    @property
    def dirty(self) -> list[AppliedMigration]:
        return [row for row in self.applied.values() if row.dirty]

    @property
    def mismatched(self) -> list[Migration]:
        """Completed versions whose file has changed since it was applied."""
        return [
            m for m in self.migrations
            if (row := self.applied.get(m.version)) is not None and not row.dirty and (row.checksum != m.checksum or row.name != m.name)
        ]

    @property
    def unknown(self) -> list[AppliedMigration]:
        """Versions recorded in the database that the code does not ship, i.e. the database is newer than the code."""
        known = {m.version for m in self.migrations}
        return [row for version, row in sorted(self.applied.items()) if version not in known]

    @property
    def out_of_order(self) -> list[Migration]:
        """Pending versions lower than an applied one. Applying them would run a schema change out of the order it was written for."""
        if not self.applied:
            return []
        highest = max(self.applied)
        return [m for m in self.pending if m.version < highest]

    def problems(self) -> list[str]:
        """Every state that blocks both applying and starting up, as messages that say what to do. Pending versions alone are not a problem."""
        found: list[str] = []
        if self.legacy_tables:
            found.append(
                f'履歴表 {HISTORY_TABLE} が無いのに表があります({", ".join(self.legacy_tables[:5])} ほか)。'
                'マイグレーションの仕組みより前に作られた DB であり、引き継ぐ手段はありません。DB を空にして作り直してください。'
            )
        for row in self.dirty:
            found.append(
                f'版 {row.version:04d}({row.name})は {row.started_at} に適用を始めたまま完了していません。'
                f'DB の状態を確かめ、手で直したなら resolve-migration {row.version} applied、版を最初からやり直せる状態に戻したなら resolve-migration {row.version} retry を実行してください。'
            )
        for m in self.mismatched:
            found.append(f'適用済みの版 {m.filename} の内容が適用時から変わっています。適用済みのファイルは書き換えず、変更は新しい版として追加してください。')
        for row in self.unknown:
            found.append(f'DB には版 {row.version:04d}({row.name})が適用されていますが、このコードはその版を持ちません。DB がコードより新しいため、コードを更新してください。')
        for m in self.out_of_order:
            found.append(f'未適用の版 {m.filename} が、適用済みの版より前の番号を持っています。版の番号を振り直したか、履歴表を手で書き換えた可能性があります。')
        return found


def checksum(sql: str) -> str:
    """SHA-256 of the file with line endings normalized to LF, so a CRLF checkout does not read as an edit."""
    return hashlib.sha256(sql.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Read every migration file in version order. Raises MigrationError on a misnamed file, a duplicate or a gap in the numbering."""
    if not directory.is_dir():
        raise MigrationError(f'マイグレーションのディレクトリがありません: {directory}')
    by_version: dict[int, Migration] = {}
    for path in sorted(directory.glob('*.sql')):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(f'マイグレーションのファイル名が規則に合いません: {path.name}(NNNN_<英小文字・数字・_>.sql の形にしてください)')
        version = int(match.group(1))
        if version in by_version:
            raise MigrationError(f'版 {version:04d} のファイルが複数あります: {by_version[version].filename}, {path.name}')
        sql = path.read_text(encoding='utf-8')
        by_version[version] = Migration(version=version, name=match.group(2), filename=path.name, sql=sql, checksum=checksum(sql))
    if not by_version:
        raise MigrationError(f'マイグレーションのファイルがありません: {directory}')
    expected = list(range(1, len(by_version) + 1))
    if sorted(by_version) != expected:
        missing = sorted(set(range(1, max(by_version) + 1)) - set(by_version))
        raise MigrationError(f'版の番号が 0001 から連続していません。欠けている版: {", ".join(f"{v:04d}" for v in missing)}')
    return tuple(by_version[v] for v in expected)


def lock_name(database: str) -> str:
    """The GET_LOCK name for one database. Lock names are server-wide and capped at 64 characters, so the database name is hashed into it."""
    return 'instant_news.migrate.' + hashlib.sha256(database.encode('utf-8')).hexdigest()[:32]


async def _fetch_all(conn: Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    async with conn.cursor(DictCursor) as cursor:
        await cursor.execute(sql, args or None)
        return list(await cursor.fetchall())


async def _execute(conn: Connection, sql: str, *args: Any) -> int:
    async with conn.cursor() as cursor:
        await cursor.execute(sql, args or None)
        return int(cursor.rowcount)


async def inspect(conn: Connection, migrations: tuple[Migration, ...]) -> SchemaStatus:
    """Compare the shipped migrations with the history recorded in the connection's current database."""
    tables = [str(row['name']) for row in await _fetch_all(conn, 'SELECT table_name AS name FROM information_schema.tables WHERE table_schema = DATABASE() ORDER BY table_name')]
    if HISTORY_TABLE not in tables:
        return SchemaStatus(migrations=migrations, applied={}, legacy_tables=tuple(tables), has_history=False)
    rows = await _fetch_all(conn, f'SELECT version, name, checksum, started_at, finished_at FROM {HISTORY_TABLE} ORDER BY version')
    applied = {
        int(row['version']): AppliedMigration(
            version=int(row['version']),
            name=str(row['name']),
            checksum=str(row['checksum']),
            started_at=row['started_at'],
            finished_at=row['finished_at'],
        )
        for row in rows
    }
    return SchemaStatus(migrations=migrations, applied=applied, legacy_tables=(), has_history=True)


@asynccontextmanager
async def _connect(config: DatabaseConfig) -> AsyncIterator[Connection]:
    """A dedicated connection outside any pool. MULTI_STATEMENTS lets a whole file go to the server in one call, so MySQL's own parser splits the statements."""
    conn = await aiomysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
        db=config.name,
        charset='utf8mb4',
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def _locked(conn: Connection, database: str, timeout: float) -> AsyncIterator[None]:
    """Hold the per-database migration lock, so two runners never apply the same version twice."""
    name = lock_name(database)
    rows = await _fetch_all(conn, 'SELECT GET_LOCK(%s, %s) AS got', name, timeout)
    if rows[0]['got'] != 1:
        raise MigrationError(f'別のマイグレーションの実行がロックを保持しているため、{timeout:g} 秒待っても開始できませんでした。実行中の migrate が終わってからやり直してください。')
    try:
        yield
    finally:
        # A connection that broke mid-run has already lost the lock with its session, and the error that broke it is the one worth raising.
        with suppress(pymysql.MySQLError):
            await _execute(conn, 'SELECT RELEASE_LOCK(%s)', name)


async def _run_file(conn: Connection, migration: Migration) -> None:
    """Send the whole file and drain every result, since an error in a later statement only surfaces when its result is read."""
    completed = 0
    try:
        async with conn.cursor() as cursor:
            # No arguments, so the driver does not %-format the SQL and a literal % in it stays as written.
            await cursor.execute(migration.sql)
            completed = 1
            while await cursor.nextset():
                completed += 1
    except pymysql.MySQLError as exc:
        raise MigrationError(
            f'{migration.filename} の {completed + 1} 文目で失敗しました({completed} 文は完了しており、DDL は取り消せません): {exc}。'
            f'版 {migration.version:04d} は未完了として記録されています。DB の状態を確かめ、resolve-migration で解消してください。'
        ) from exc


async def apply_pending(config: DatabaseConfig, directory: Path = MIGRATIONS_DIR, *, lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> list[Migration]:
    """Apply every pending migration in order and return the ones applied. Applies nothing and raises MigrationError when the status has a problem."""
    migrations = discover(directory)
    async with _connect(config) as conn, _locked(conn, config.name, lock_timeout):
        status = await inspect(conn, migrations)
        problems = status.problems()
        if problems:
            raise MigrationError('\n'.join(problems))
        # Created only when absent: IF NOT EXISTS would raise a "table already exists" warning on every later run, which reads like something went wrong.
        if not status.has_history:
            await _execute(conn, _CREATE_HISTORY)
        applied: list[Migration] = []
        for migration in status.pending:
            await _execute(conn, f'INSERT INTO {HISTORY_TABLE} (version, name, checksum) VALUES (%s, %s, %s)', migration.version, migration.name, migration.checksum)
            await _run_file(conn, migration)
            await _execute(conn, f'UPDATE {HISTORY_TABLE} SET finished_at = CURRENT_TIMESTAMP WHERE version = %s', migration.version)
            applied.append(migration)
        return applied


async def status(config: DatabaseConfig, directory: Path = MIGRATIONS_DIR) -> SchemaStatus:
    async with _connect(config) as conn:
        return await inspect(conn, discover(directory))


async def resolve_dirty(config: DatabaseConfig, version: int, mode: ResolveMode, directory: Path = MIGRATIONS_DIR, *, lock_timeout: float = LOCK_TIMEOUT_SECONDS) -> Migration:
    """Clear a dirty version: retry deletes its row so migrate runs the file again, applied completes it against the current file."""
    migrations = {m.version: m for m in discover(directory)}
    migration = migrations.get(version)
    if migration is None:
        raise MigrationError(f'版 {version:04d} のファイルがありません。')
    async with _connect(config) as conn, _locked(conn, config.name, lock_timeout):
        current = await inspect(conn, tuple(migrations.values()))
        row = current.applied.get(version)
        if row is None or not row.dirty:
            raise MigrationError(f'版 {version:04d} は未完了の状態ではありません。解消が要るのは migration-status で未完了と表示される版だけです。')
        if mode == 'retry':
            await _execute(conn, f'DELETE FROM {HISTORY_TABLE} WHERE version = %s', version)
        else:
            await _execute(
                conn,
                f'UPDATE {HISTORY_TABLE} SET name = %s, checksum = %s, finished_at = CURRENT_TIMESTAMP WHERE version = %s',
                migration.name, migration.checksum, version,
            )
        return migration


async def ensure_current(db: Database, directory: Path = MIGRATIONS_DIR) -> SchemaStatus:
    """Refuse to run against a schema that does not match the code: every shipped version applied and completed, nothing unknown, nothing edited."""
    migrations = discover(directory)
    async with db.acquire() as conn:
        current = await inspect(conn, migrations)
    problems = current.problems()
    if problems:
        raise MigrationError('DB のスキーマがコードと一致しないため起動しません。\n' + '\n'.join(problems))
    if current.pending:
        names = ', '.join(m.filename for m in current.pending)
        raise MigrationError(f'未適用のマイグレーションがあるため起動しません: {names}。python -m server.cli migrate(compose では docker compose run --rm migrate)で適用してください。')
    return current
