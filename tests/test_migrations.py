"""The schema migration runner: discovery, applying, the history it records, the states it refuses, and the startup check.

The runner tests apply throwaway migrations from tmp_path to the scratch database tests/initdb creates, emptied before each test. The last tests check the repository's own migrations/ and the test database the migrate service built from it.
"""

from __future__ import annotations

import dataclasses
import warnings
from collections.abc import AsyncIterator
from pathlib import Path

import aiomysql
import pytest

from common.config import DatabaseConfig, load_database_config
from common.db import Database
from common.migrations import MigrationError, apply_pending, discover, ensure_current, lock_name, resolve_dirty, status

pytestmark = pytest.mark.anyio

SCRATCH_DATABASE = 'instant_news_scratch'


async def _connect(config: DatabaseConfig) -> aiomysql.Connection:
    return await aiomysql.connect(host=config.host, port=config.port, user=config.user, password=config.password, db=config.name, autocommit=True)


async def _query(config: DatabaseConfig, sql: str, *args: object) -> list[tuple]:
    conn = await _connect(config)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, args or None)
            return list(await cursor.fetchall())
    finally:
        conn.close()


@pytest.fixture
async def scratch() -> AsyncIterator[DatabaseConfig]:
    config = dataclasses.replace(load_database_config(), name=SCRATCH_DATABASE)
    conn = await _connect(config)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute('SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()')
            tables = [row[0] for row in await cursor.fetchall()]
            await cursor.execute('SET FOREIGN_KEY_CHECKS = 0')
            for table in tables:
                await cursor.execute(f'DROP TABLE `{table}`')
            await cursor.execute('SET FOREIGN_KEY_CHECKS = 1')
    finally:
        conn.close()
    yield config


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    directory = tmp_path / 'migrations'
    directory.mkdir()
    return directory


def _write(directory: Path, filename: str, sql: str) -> None:
    (directory / filename).write_bytes(sql.encode('utf-8'))


# Discovery


def test_discover_orders_by_version(migrations_dir: Path) -> None:
    _write(migrations_dir, '0002_second.sql', 'SELECT 2;')
    _write(migrations_dir, '0001_first.sql', 'SELECT 1;')
    (migrations_dir / 'README.md').write_text('not a migration')
    assert [m.filename for m in discover(migrations_dir)] == ['0001_first.sql', '0002_second.sql']


@pytest.mark.parametrize(
    'filenames',
    [
        ['0001_a.sql', '0003_c.sql'],
        ['0002_b.sql'],
        ['0001_a.sql', '0001_b.sql'],
        ['001_a.sql'],
        ['0001-a.sql'],
        ['0001_Upper.sql'],
        [],
    ],
)
def test_discover_rejects_malformed_sets(migrations_dir: Path, filenames: list[str]) -> None:
    for filename in filenames:
        _write(migrations_dir, filename, 'SELECT 1;')
    with pytest.raises(MigrationError):
        discover(migrations_dir)


def test_checksum_ignores_line_endings(migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_a.sql', 'CREATE TABLE a (id INT);\n')
    lf = discover(migrations_dir)[0].checksum
    _write(migrations_dir, '0001_a.sql', 'CREATE TABLE a (id INT);\r\n')
    assert discover(migrations_dir)[0].checksum == lf


# Applying


async def test_applies_in_order_once(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT NOT NULL PRIMARY KEY);')
    _write(migrations_dir, '0002_alter.sql', 'ALTER TABLE a ADD COLUMN note VARCHAR(10) NULL;')
    applied = await apply_pending(scratch, migrations_dir)
    assert [m.version for m in applied] == [1, 2]
    # A run with nothing to do is silent: no MySQL warning relayed from re-creating the history table.
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        assert await apply_pending(scratch, migrations_dir) == []
    assert await _query(scratch, "SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'a' ORDER BY ordinal_position") == [('id',), ('note',)]
    current = await status(scratch, migrations_dir)
    assert current.pending == [] and current.problems() == []
    assert all(not row.dirty for row in current.applied.values())


async def test_new_version_is_applied_on_top(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT NOT NULL PRIMARY KEY);')
    await apply_pending(scratch, migrations_dir)
    _write(migrations_dir, '0002_seed.sql', 'INSERT INTO a (id) VALUES (7);')
    assert [m.version for m in (await status(scratch, migrations_dir)).pending] == [2]
    assert [m.version for m in await apply_pending(scratch, migrations_dir)] == [2]
    assert await _query(scratch, 'SELECT id FROM a') == [(7,)]


async def test_multi_statement_file_with_comments_and_literals(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    sql = (
        '-- a comment; with a semicolon\n'
        'SET NAMES utf8mb4;\n'
        '/* block; comment */\n'
        'CREATE TABLE a (id INT NOT NULL PRIMARY KEY, body VARCHAR(50) NOT NULL);\n'
        "INSERT INTO a VALUES (1, 'semi;colon'), (2, '100% -- not a comment'), (3, '日本語');\n"
        '-- trailing comment\n'
    )
    _write(migrations_dir, '0001_create.sql', sql)
    await apply_pending(scratch, migrations_dir)
    assert await _query(scratch, 'SELECT id, body FROM a ORDER BY id') == [(1, 'semi;colon'), (2, '100% -- not a comment'), (3, '日本語')]


# States that block applying and startup


async def test_failure_leaves_the_version_dirty(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT NOT NULL PRIMARY KEY);')
    _write(migrations_dir, '0002_broken.sql', 'CREATE TABLE b (id INT);\nCREATE TABLE broken (;')
    with pytest.raises(MigrationError, match='2 文目'):
        await apply_pending(scratch, migrations_dir)
    current = await status(scratch, migrations_dir)
    assert [row.version for row in current.dirty] == [2]
    assert current.applied[1].dirty is False
    with pytest.raises(MigrationError, match='完了していません'):
        await apply_pending(scratch, migrations_dir)


async def test_retry_reruns_a_dirty_version(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_broken.sql', 'CREATE TABLE broken (;')
    with pytest.raises(MigrationError):
        await apply_pending(scratch, migrations_dir)
    _write(migrations_dir, '0001_broken.sql', 'CREATE TABLE fixed (id INT);')
    await resolve_dirty(scratch, 1, 'retry', migrations_dir)
    assert [m.version for m in await apply_pending(scratch, migrations_dir)] == [1]
    assert (await status(scratch, migrations_dir)).problems() == []


async def test_applied_completes_a_dirty_version(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_partial.sql', 'CREATE TABLE a (id INT);\nCREATE TABLE broken (;')
    with pytest.raises(MigrationError):
        await apply_pending(scratch, migrations_dir)
    # The operator finishes the version by hand and records the file that describes the result.
    _write(migrations_dir, '0001_partial.sql', 'CREATE TABLE a (id INT);\n')
    await resolve_dirty(scratch, 1, 'applied', migrations_dir)
    current = await status(scratch, migrations_dir)
    assert current.problems() == [] and current.pending == []


async def test_resolve_refuses_a_version_that_is_not_dirty(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    await apply_pending(scratch, migrations_dir)
    with pytest.raises(MigrationError, match='未完了の状態ではありません'):
        await resolve_dirty(scratch, 1, 'retry', migrations_dir)
    with pytest.raises(MigrationError, match='ファイルがありません'):
        await resolve_dirty(scratch, 9, 'applied', migrations_dir)


async def test_edited_applied_file_is_refused(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    await apply_pending(scratch, migrations_dir)
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id BIGINT);')
    _write(migrations_dir, '0002_more.sql', 'CREATE TABLE b (id INT);')
    with pytest.raises(MigrationError, match='変わっています'):
        await apply_pending(scratch, migrations_dir)
    assert await _query(scratch, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'b'") == [(0,)]


async def test_crlf_checkout_is_not_an_edit(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);\n')
    await apply_pending(scratch, migrations_dir)
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);\r\n')
    assert (await status(scratch, migrations_dir)).problems() == []


async def test_database_newer_than_code_is_refused(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    _write(migrations_dir, '0002_more.sql', 'CREATE TABLE b (id INT);')
    await apply_pending(scratch, migrations_dir)
    (migrations_dir / '0002_more.sql').unlink()
    current = await status(scratch, migrations_dir)
    assert [row.version for row in current.unknown] == [2]
    with pytest.raises(MigrationError, match='コードより新しい'):
        await apply_pending(scratch, migrations_dir)


async def test_out_of_order_history_is_refused(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    _write(migrations_dir, '0002_more.sql', 'CREATE TABLE b (id INT);')
    await apply_pending(scratch, migrations_dir)
    await _query(scratch, 'DELETE FROM schema_migrations WHERE version = 1')
    with pytest.raises(MigrationError, match='前の番号'):
        await apply_pending(scratch, migrations_dir)


async def test_legacy_database_is_refused(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    await _query(scratch, 'CREATE TABLE users (id INT)')
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    with pytest.raises(MigrationError, match='履歴表'):
        await apply_pending(scratch, migrations_dir)
    assert await _query(scratch, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'schema_migrations'") == [(0,)]


async def test_held_lock_blocks_a_second_runner(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    holder = await _connect(scratch)
    try:
        async with holder.cursor() as cursor:
            await cursor.execute('SELECT GET_LOCK(%s, 0)', (lock_name(scratch.name),))
            assert (await cursor.fetchone())[0] == 1
        with pytest.raises(MigrationError, match='ロック'):
            await apply_pending(scratch, migrations_dir, lock_timeout=0)
    finally:
        holder.close()
    assert [m.version for m in await apply_pending(scratch, migrations_dir, lock_timeout=0)] == [1]


# The startup check


async def _ensure(config: DatabaseConfig, directory: Path) -> None:
    db = Database(config)
    await db.connect()
    try:
        await ensure_current(db, directory)
    finally:
        await db.close()


async def test_ensure_current_refuses_an_unmigrated_database(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    with pytest.raises(MigrationError, match='未適用'):
        await _ensure(scratch, migrations_dir)


async def test_ensure_current_refuses_pending_and_problems(scratch: DatabaseConfig, migrations_dir: Path) -> None:
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id INT);')
    await apply_pending(scratch, migrations_dir)
    await _ensure(scratch, migrations_dir)
    _write(migrations_dir, '0002_more.sql', 'CREATE TABLE b (id INT);')
    with pytest.raises(MigrationError, match='0002_more.sql'):
        await _ensure(scratch, migrations_dir)
    (migrations_dir / '0002_more.sql').unlink()
    _write(migrations_dir, '0001_create.sql', 'CREATE TABLE a (id BIGINT);')
    with pytest.raises(MigrationError, match='一致しない'):
        await _ensure(scratch, migrations_dir)


# The repository's own migrations


def test_repository_migrations_are_well_formed() -> None:
    migrations = discover()
    assert migrations[0].filename == '0001_initial.sql'


async def test_test_database_is_current(db: Database) -> None:
    current = await ensure_current(db)
    assert len(current.applied) == len(current.migrations)
