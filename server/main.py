#!/usr/bin/env python3
"""FastAPI アプリの組み立てとエントリポイント。

起動の経路は 2 つあり、どちらも同じアプリを使う。

    python -m server.main            … config.yaml の server.host / server.port で uvicorn を起動する。
    uvicorn server.main:app          … 外側のプロセス管理から起動する。待ち受け先は uvicorn の引数が決める。

設定と DB は app.state.config / app.state.db / app.state.settings に置き、経路の実装は deps.py の依存性を通してそこから取る。モジュールのグローバル変数に持たせると、試験でアプリを差し替えられなくなる。

設定の真実は 2 箇所に分かれている。config.yaml(app.state.config)が持つのは server・database・画像の保存先だけであり、それ以外は settings 表(app.state.settings)が持つ。config.yaml の残りの値は、起動時に settings 表の未登録のキーだけを埋める初期値として使う。

**import しただけで設定を読みに行かない。**アプリは `app` 属性が初めて参照された時点で組み立てる(モジュールの __getattr__)。import が設定を要求すると、このモジュールを読み込むあらゆる経路 -- 試験、CLI、開発時の対話的な import -- が config.yaml の存在を強いられ、設定を引数で渡して組み立てる create_app(config=...) すら呼べなくなる。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Final

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from common.config import Config, load_config
from common.db import Database
from common.settings import MissingSettingsError, SettingsStore

from .app import pages
from .app.errors import register_exception_handlers
from .app.routers import admin_ads, admin_roles, admin_settings, admin_taxonomy, ads, articles, auth, me, media, rankings, search, taxonomy
from .app.scheduler import run_publish_scheduler

logger = logging.getLogger(__name__)

LOG_FORMAT: Final = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'

# 経路を持つルータ。URL の接頭辞は各ルータが自分で持つ(/api の下に無い経路が /auth /media /ads に跨がるため、ここで一律に前置できない)。
_API_ROUTERS: Final = (auth, me, articles, taxonomy, search, rankings, media, ads, admin_ads, admin_taxonomy, admin_settings, admin_roles)


def _configure_logging() -> None:
    """アプリのログを標準出力へ出す。

    uvicorn は自分のロガーだけを設定し、root には手を触れない。ここで root を設定しないと、アプリ側のログは警告未満が捨てられ、予約公開の巡回や画像の取り込み失敗が何も見えなくなる。既にハンドラがある場合(外側でログを設定している場合)basicConfig は何もしない。
    """
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


async def _bootstrap_settings(store: SettingsStore, config: Config) -> None:
    """settings 表の未登録のキーを config.yaml の初期値で埋め、設定を読めることを確かめる。

    投入は未登録のキーだけを対象とする(SettingsStore.bootstrap が INSERT IGNORE で行う)。起動のたびに config.yaml で上書きすると、画面から変更した値が次の再起動で黙って元へ戻る。設定を増やした場合、新しいキーだけがここで埋まる。

    投入の直後に設定を読み直し、読めなければ起動を中止する。読めないまま起動すると、認証・記事の保存・予約公開といった設定を要する経路がすべて要求の時点で失敗し、起動には成功しているために原因が「動かない」としか見えなくなる。
    """
    inserted = await store.bootstrap(config.bootstrap)
    if inserted:
        logger.info('settings 表へ config.yaml の初期値を投入しました: %s', ', '.join(inserted))
    try:
        await store.snapshot(force=True)
    except MissingSettingsError as exc:
        # 初期値の投入を済ませた直後であり、本来ここへは来ない。来るとすれば、投入と読み直しの間に settings 表の行が消されている(別のプロセスや手作業による削除)。
        raise RuntimeError(f'settings 表の設定を読み込めないため起動を中止します。初期値の投入は完了しているため、投入した行が削除された可能性があります: {exc}') from exc
    except ValueError as exc:
        # 登録簿の検証を通らない値が settings 表に入っている。API と CLI は検証を通すため、直接 SQL で書き換えた場合に起こる。
        raise RuntimeError(f'settings 表に不正な値があるため起動を中止します。CLI から修正してください(python -m server.cli set-setting <key> <json-value>): {exc}') from exc


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """接続プールと予約公開のスケジューラの寿命をアプリに合わせる。"""
    db: Database = app.state.db
    config: Config = app.state.config
    store: SettingsStore = app.state.settings
    await db.connect()
    try:
        # 設定は DB から読むため、接続の直後・スケジューラの起動より前に済ませる。
        await _bootstrap_settings(store, config)
        scheduler = asyncio.create_task(run_publish_scheduler(app), name='publish-scheduler')
        app.state.scheduler_task = scheduler
        try:
            yield
        finally:
            scheduler.cancel()
            # cancel 後に待ち合わせる。待たずに接続プールを閉じると、公開の途中だったコネクションが閉じられ、終了時にだけ出る例外が残る。
            with suppress(asyncio.CancelledError):
                await scheduler
    finally:
        # 初期値の投入が失敗した場合も接続プールを閉じる。開いたまま例外を投げると、起動に失敗したプロセスが接続を掴んだまま終わる。
        await db.close()


def create_app(config: Config | None = None) -> FastAPI:
    """アプリを組み立てる。config を省略すると config.yaml を読む。"""
    _configure_logging()
    resolved = config if config is not None else load_config()
    app = FastAPI(
        title='instant-news',
        description='Discord コミュニティのためのニュースプラットフォーム。',
        lifespan=_lifespan,
    )
    app.state.config = resolved
    # 接続はプロセスが実際に走り出す lifespan で行う。import しただけの状態で DB を掴まない。
    app.state.db = Database(resolved.database)
    # 設定の読み書きの窓口。ここでは組み立てるだけで DB へは触れない(初期値の投入と最初の読み取りは lifespan で行う)。
    app.state.settings = SettingsStore(app.state.db)

    register_exception_handlers(app)

    # 静的資産。HTML そのものは pages.py が配信するため、ここで公開するのは css と js だけにする。
    app.mount('/static/css', StaticFiles(directory=pages.WEB_DIR / 'css'), name='static-css')
    app.mount('/static/js', StaticFiles(directory=pages.WEB_DIR / 'js'), name='static-js')

    for module in _API_ROUTERS:
        app.include_router(module.router)
    # ページの経路は最後に登録する。Starlette は登録順に照合するため、/articles/{slug} のような可変部分を持つ経路が API・画像・静的資産の経路より先に並ばないようにする。
    app.include_router(pages.router)
    return app


def __getattr__(name: str) -> Any:
    """`uvicorn server.main:app` のように、モジュールの属性としてアプリを求められた時点で組み立てる(PEP 562)。

    ここで組み立てることで、import そのものは設定を要求しない。一度組み立てたアプリはモジュールの属性として残るため、二度目以降の参照はこの関数を通らず、設定を読み直すことも複数のアプリが生まれることもない。
    """
    if name == 'app':
        global app
        app = create_app()
        return app
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def main() -> None:
    """python -m server.main で起動したときの入口。待ち受け先は設定ファイルが決める。"""
    application = create_app()
    config: Config = application.state.config
    uvicorn.run(application, host=config.server.host, port=config.server.port)


if __name__ == '__main__':
    main()
