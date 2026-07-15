"""aiomysql の薄いラッパ。ORM は使わない。

SQL は必ずプレースホルダ(%s)で書き、値を文字列連結で埋め込まない。連結した瞬間に SQL インジェクションが成立する。IN 句のように個数が可変な場合も、個数分の %s を生成して引数として渡す。

複数のクエリを 1 つのトランザクションで流す場合は transaction() が渡す Connection を各メソッドの conn 引数へ渡す。conn を省略した呼び出しはプールから都度コネクションを借りるため、同じトランザクションには乗らない。
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
# MySQL は wait_timeout(既定 8 時間)を過ぎた接続を切る。切られた接続をプールから掴むと最初のクエリが失敗するため、それより十分短い間隔で作り直す。
POOL_RECYCLE_SECONDS: Final = 3600
CONNECT_TIMEOUT_SECONDS: Final = 10


class Database:
    """MySQL への接続プールと、その上の最小限のクエリ実行。"""

    def __init__(self, config: DatabaseConfig) -> None:
        self._config = config
        self._pool: Pool | None = None

    async def connect(self) -> None:
        """接続プールを生成する。既に生成済みなら何もしない。"""
        if self._pool is not None:
            return
        self._pool = await create_pool(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            db=self._config.name,
            # 既定は自動コミット。明示的なトランザクションは transaction() が BEGIN で開始する。
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
        """接続プールを閉じる。既に閉じているなら何もしない。"""
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
        """プールからコネクションを 1 本借りる。抜けると必ずプールへ返る。"""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """トランザクションを開始し、正常終了で commit、例外で rollback する。

        渡された Connection を各メソッドの conn 引数へ渡すことで、同じトランザクションでクエリを流せる。
        """
        async with self.acquire() as conn:
            await conn.begin()
            try:
                yield conn
            except BaseException:
                # asyncio.CancelledError も BaseException 派生であり、取りこぼすと中途半端な状態のまま接続がプールへ返る。
                await conn.rollback()
                raise
            await conn.commit()

    @asynccontextmanager
    async def _cursor(self, conn: Connection | None) -> AsyncIterator[DictCursor]:
        """conn が与えられていればそのコネクション上に、なければ借りたコネクション上にカーソルを開く。"""
        if conn is not None:
            async with conn.cursor(DictCursor) as cursor:
                yield cursor
            return
        async with self.acquire() as owned:
            async with owned.cursor(DictCursor) as cursor:
                yield cursor

    @staticmethod
    def _params(args: tuple[Any, ...]) -> tuple[Any, ...] | None:
        """引数が無い場合に None を渡すためのもの。

        aiomysql は引数が None 以外のとき SQL を % で書式化するため、引数の無い SQL に空のタプルを渡すと、本文中の % (LIKE のワイルドカードなど)が書式指定子と解釈されて壊れる。
        """
        return args or None

    async def fetch_one(self, sql: str, *args: Any, conn: Connection | None = None) -> dict[str, Any] | None:
        """先頭の 1 行を返す。無ければ None。"""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return await cursor.fetchone()

    async def fetch_all(self, sql: str, *args: Any, conn: Connection | None = None) -> list[dict[str, Any]]:
        """全ての行を返す。"""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return list(await cursor.fetchall())

    async def fetch_val(self, sql: str, *args: Any, conn: Connection | None = None) -> Any:
        """先頭の行の先頭の列を返す。行が無ければ None。"""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            row = await cursor.fetchone()
            if not row:
                return None
            return next(iter(row.values()))

    async def execute(self, sql: str, *args: Any, conn: Connection | None = None) -> int:
        """SQL を実行し、影響を受けた行数を返す。"""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return int(cursor.rowcount)

    async def execute_insert(self, sql: str, *args: Any, conn: Connection | None = None) -> int:
        """INSERT を実行し、生成された AUTO_INCREMENT の値を返す。"""
        async with self._cursor(conn) as cursor:
            await cursor.execute(sql, self._params(args))
            return int(cursor.lastrowid)
