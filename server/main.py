#!/usr/bin/env python3
"""Assembly and entry point of the FastAPI application.

There are two startup paths, both using the same app.

    python -m server.main            starts uvicorn on config.yaml's server.host / server.port.
    uvicorn server.main:app          is started from an outer process manager; uvicorn's own arguments decide what it listens on.

Config, DB and settings live on app.state.config / app.state.db / app.state.settings, and the route implementations reach them from there through the deps.py dependencies. Holding them in module globals would make the app unswappable in tests.

The source of truth is split in two: config.yaml (app.state.config) holds only server, database and the image storage location; everything else lives in the settings table (app.state.settings). The remaining config.yaml values are used at startup as seed values that fill only the keys the settings table does not have yet.

**A bare import must not read config.** The app is assembled the moment the `app` attribute is first referenced (the module's __getattr__). If import required config, every path that loads this module -- tests, the CLI, an interactive import while developing -- would be forced to have config.yaml present, and even create_app(config=...), which takes the config as an argument, could not be called.
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
from .app.rate_limit import RateLimitHeadersMiddleware
from .app.routers import admin_ads, admin_roles, admin_settings, admin_taxonomy, ads, articles, auth, me, media, rankings, search, taxonomy
from .app.scheduler import run_publish_scheduler
from .app.schemas import ErrorOut

logger = logging.getLogger(__name__)

LOG_FORMAT: Final = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'

# Each router owns its URL prefix: routes outside /api span /auth, /media and /ads, so no single prefix can be applied to all of them here.
_API_ROUTERS: Final = (auth, me, articles, taxonomy, search, rankings, media, ads, admin_ads, admin_taxonomy, admin_settings, admin_roles)

# Every error response, whatever its status, has the one body errors.py builds. Declaring it as the default response states that in the OpenAPI document, and replaces the 422 body FastAPI would otherwise document but this app never sends.
_ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {'default': {'model': ErrorOut, 'description': 'エラー。本体の形は状態コードによらず同じ。'}}


def _configure_logging() -> None:
    """Send the application's logs to stdout.

    uvicorn configures only its own loggers and never touches root. Without configuring root here, anything below WARNING from the application is discarded, leaving the scheduled-publishing poll loop and image ingest failures entirely invisible. basicConfig does nothing when handlers already exist (when logging is configured from outside).
    """
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


async def _bootstrap_settings(store: SettingsStore, config: Config) -> None:
    """Fill the settings table's absent keys with config.yaml's seed values and confirm the settings can be read.

    Seeding targets only keys that are not there yet (SettingsStore.bootstrap does this with INSERT IGNORE). Overwriting from config.yaml on every startup would silently revert values changed from the UI at the next restart. When a setting is added, only the new key gets filled here.

    Settings are re-read right after seeding, and startup is aborted if they cannot be read. Starting up anyway would make every route that needs settings -- auth, saving an article, scheduled publishing -- fail at request time, and because startup itself succeeded the cause would look like nothing more than "it doesn't work".
    """
    inserted = await store.bootstrap(config.bootstrap)
    if inserted:
        logger.info('settings 表へ config.yaml の初期値を投入しました: %s', ', '.join(inserted))
    try:
        await store.snapshot(force=True)
    except MissingSettingsError as exc:
        # Seeding has just finished, so this is normally unreachable. Getting here means settings rows were deleted between the seeding and the re-read (by another process, or by hand).
        raise RuntimeError(f'settings 表の設定を読み込めないため起動を中止します。初期値の投入は完了しているため、投入した行が削除された可能性があります: {exc}') from exc
    except ValueError as exc:
        # The settings table holds a value that fails the registry's validation. The API and the CLI both run it, so this only happens when a row was rewritten with raw SQL.
        raise RuntimeError(f'settings 表に不正な値があるため起動を中止します。CLI から修正してください(python -m server.cli set-setting <key> <json-value>): {exc}') from exc


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Tie the lifetime of the connection pool and the scheduled-publishing scheduler to the app."""
    db: Database = app.state.db
    config: Config = app.state.config
    store: SettingsStore = app.state.settings
    await db.connect()
    try:
        # Settings are read from the DB, so do this right after connecting and before starting the scheduler.
        await _bootstrap_settings(store, config)
        scheduler = asyncio.create_task(run_publish_scheduler(app), name='publish-scheduler')
        app.state.scheduler_task = scheduler
        try:
            yield
        finally:
            scheduler.cancel()
            # Await after the cancel. Closing the connection pool without waiting closes a connection that was mid-publish, leaving an exception that shows up only at shutdown.
            with suppress(asyncio.CancelledError):
                await scheduler
    finally:
        # Close the connection pool even when seeding failed. Raising with it still open leaves the failed process exiting while it still holds connections.
        await db.close()


def create_app(config: Config | None = None) -> FastAPI:
    """Assemble the app. Reads config.yaml when config is omitted."""
    _configure_logging()
    resolved = config if config is not None else load_config()
    app = FastAPI(
        title='instant-news',
        description='Discord コミュニティのためのニュースプラットフォーム。',
        lifespan=_lifespan,
    )
    app.state.config = resolved
    # Connect in lifespan, where the process actually starts running. A bare import must not grab the DB.
    app.state.db = Database(resolved.database)
    # The single access point for reading and writing settings. Only constructed here, with no DB access (seeding and the first read happen in lifespan).
    app.state.settings = SettingsStore(app.state.db)

    register_exception_handlers(app)
    app.add_middleware(RateLimitHeadersMiddleware)

    # Static assets. pages.py serves the HTML itself, so only css and js are exposed here.
    app.mount('/static/css', StaticFiles(directory=pages.WEB_DIR / 'css'), name='static-css')
    app.mount('/static/js', StaticFiles(directory=pages.WEB_DIR / 'js'), name='static-js')

    for module in _API_ROUTERS:
        app.include_router(module.router, responses=_ERROR_RESPONSES)
    # Register the page routes last. Starlette matches in registration order, so a route with a variable part like /articles/{slug} must not come before the API, image and static-asset routes.
    app.include_router(pages.router)
    return app


def __getattr__(name: str) -> Any:
    """Assemble the app the moment it is asked for as a module attribute, as `uvicorn server.main:app` does (PEP 562).

    Assembling here is what keeps import itself from requiring config. Once assembled the app stays as a module attribute, so the second and later references never pass through this function: config is not re-read and no second app is ever created.
    """
    if name == 'app':
        global app
        app = create_app()
        return app
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def main() -> None:
    """Entry point when started with `python -m server.main`. The config file decides what it listens on."""
    application = create_app()
    config: Config = application.state.config
    uvicorn.run(application, host=config.server.host, port=config.server.port)


if __name__ == '__main__':
    main()
