"""Thin wrapper over aiomysql. No ORM.

Always write SQL with placeholders (%s); never embed values by string concatenation, which is SQL injection the moment it happens. Where the count is variable, as in an IN clause, generate that many %s and pass the values as arguments.

To run several queries in one transaction, pass the Connection yielded by transaction() to each method's conn argument. A call that omits conn borrows its own connection from the pool, so it does not join the transaction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from aiomysql import Connection, DictCursor, Pool, create_pool

from .config import DatabaseConfig

logger = logging.getLogger(__name__)

POOL_MIN_SIZE: Final = 1
POOL_MAX_SIZE: Final = 10
# MySQL closes connections idle past wait_timeout (8 hours by default), and taking a closed one out of the pool fails on the first query, so recycle at a comfortably shorter interval.
POOL_RECYCLE_SECONDS: Final = 3600
CONNECT_TIMEOUT_SECONDS: Final = 10


class Database:
    """A MySQL connection pool and the minimal query execution on top of it."""

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self._pool: Pool | None = None

    async def connect(self) -> None:
        """Create the connection pool. No-op if it already exists."""
        if self._pool is not None:
            return
        self._pool = await create_pool(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            db=self._config.name,
            # Autocommit by default; transaction() opens explicit transactions with BEGIN.
            autocommit=True,
            charset='utf8mb4',
            cursorclass=DictCursor,
            minsize=POOL_MIN_SIZE,
            maxsize=POOL_MAX_SIZE,
            pool_recycle=POOL_RECYCLE_SECONDS,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
        logger.info('データベースへ接続しました: %s:%s/%s', self._config.host, self._config.port, self._config.name)

    async def close(self) -> None:
        """Close the connection pool. No-op if it is already closed."""
        if self._pool is None:
            return
        pool = self._pool
        self._pool = None
        pool.close()
        await pool.wait_closed()
        logger.info('データベースの接続を閉じました。')

    def _require_pool(self) -> Pool:
        if self._pool is None:
            raise RuntimeError('データベースへ接続していません。先に Database.connect() を呼んでください。')
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Connection]:
        """Borrow one connection from the pool. It always goes back on exit."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """Begin a transaction; commit on normal exit, roll back on exception.

        Pass the yielded Connection to each method's conn argument to run those queries in this transaction.
        """
        async with self.acquire() as conn:
            await conn.begin()
            try:
                yield conn
            except BaseException:
                # asyncio.CancelledError derives from BaseException too, and missing it would return the connection to the pool half-finished.
                await conn.rollback()
                raise
            await conn.commit()

    @asynccontextmanager
    async def _cursor(self, conn: Connection | None) -> AsyncIterator[DictCursor]:
        """Open a cursor on conn when given, otherwise on a connection borrowed for the call."""
        if conn is not None:
            async with conn.cursor(DictCursor) as cursor:
                yield cursor
            return
        async with self.acquire() as owned:
            async with owned.cursor(DictCursor) as cursor:
                yield cursor

    @staticmethod
    def _params(args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        """Pass None, not an empty tuple, when there are no arguments.

        aiomysql %-formats the SQL whenever the arguments are not None, so an empty tuple makes a literal % in the statement (a LIKE wildcard, say) get read as a format specifier and break.
        """
        return args or None

    async def fetch_one(self, sql: str, *args: Any, conn: Connection | None = None) -> dict[str, Any] | None:
        """Return the first row, or None if there is none."""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, *args: Any, conn: Connection | None = None) -> list[dict[str, Any]]:
        """Return every row."""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return list(await cursor.fetchall())

    async def fetch_val(self, sql: str, *args: Any, conn: Connection | None = None) -> Any:
        """Return the first column of the first row, or None if there is no row."""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            row = await cursor.fetchone()
            if not row:
                return None
            return next(iter(row.values()))

    async def execute(self, sql: str, *args: Any, conn: Connection | None = None) -> int:
        """Execute the SQL and return the number of affected rows."""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return int(cursor.rowcount)

    async def execute_insert(self, sql: str, *args: Any, conn: Connection | None = None) -> int:
        """Execute the INSERT and return the generated AUTO_INCREMENT value."""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return int(cursor.lastrowid)
