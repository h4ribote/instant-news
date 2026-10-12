#!/usr/bin/env python3
"""Discord bot entry point.

Start with `python -m bot.main` from the repository root as the working directory, so that the `common` and `bot` packages resolve as absolute imports from the root.

The bot only sends to Discord and applies the events Discord delivers; it never decides whether an article is published. The server publishes and enqueues what has to be sent onto the DB queue, so publishing proceeds while the bot is down and the bot picks up the unsent rows once it is back.

The source of truth for settings is the MySQL settings table; config.yaml only holds the seed values copied into it at first boot. The bot cannot read even its own token from anywhere but the DB, so it connects to the DB and finishes seeding before it reads settings and connects to Discord. The startup order of server and bot is not fixed, and a bot that starts first would read the token from an empty settings table, so the bot seeds too. Seeding is idempotent via INSERT IGNORE, so it is safe for both to call it, in either order.

Only the token is read at startup and never again. The Discord connection is established with the token, so following a change would mean reconnecting; the settings registry marks the token restart_required and there is no detect-and-reconnect mechanism (not worth the complexity). Every other setting takes effect without a bot restart, because the send loop re-reads on each pass and the event listeners re-read each time they handle an event.

The bot's implementation is NewsBot in bot/app/client.py; see there for which events it subscribes to and what it mirrors.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Final

import discord

from bot.app.client import NewsBot
from common.config import Config, load_config
from common.db import Database
from common.migrations import MigrationError, ensure_current
from common.settings import MissingSettingsError, SettingsStore

logger = logging.getLogger('bot')

LOG_FORMAT: Final = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'


async def _run(config: Config) -> int:
    """Connect to the DB, seed the settings, and connect to Discord with the token read from the settings table.

    Both connections are opened inside a single event loop: a connection pool is bound to the loop that created it, so a pool built on another loop breaks on the bot's first query.
    """
    db = Database(config.database)
    await db.connect()
    try:
        try:
            await ensure_current(db)
        except MigrationError as exc:
            logger.error('%s', exc)
            return 1
        store = SettingsStore(db)
        try:
            inserted = await store.bootstrap(config.bootstrap)
            settings = await store.snapshot()
        except (ValueError, MissingSettingsError) as exc:
            logger.error('settings 表の設定を読み取れませんでした: %s', exc)
            return 1
        if inserted:
            # Seeding fills only the keys that are not there yet: overwriting existing values from config.yaml would silently revert settings changed from the admin screen on every restart.
            logger.info('settings 表に未登録だった設定へ config.yaml の初期値を投入しました: %s。以後はこの表が真実であり、変更は管理画面から行います。', ', '.join(inserted))
        logger.info('対象の guild %s として起動します。', settings.discord.guild_id)

        bot = NewsBot(config, db, store)
        try:
            # start() rather than discord.Client.run(), because reading the token requires connecting to the DB first and run() calls asyncio.run() itself. start() never performs discord.py's own logging setup, so no log_handler override is involved.
            async with bot:
                await bot.start(settings.discord.bot.token)
        except discord.LoginFailure:
            logger.error('Discord への認証に失敗しました。設定の discord.bot.token を確認してください(python -m server.cli get-setting discord.bot.token で確認できます)。')
            return 1
        except discord.PrivilegedIntentsRequired:
            # Discord cuts the connection the same way whichever of the two is missing, and discord.py does not tell them apart either. Naming only one would leave an operator who has already enabled that one with no clue why it still will not start, so list both and say what each is needed for.
            logger.error(
                '特権 intent が有効になっていません。Discord の開発者ポータルで次の 2 つを有効にしてください。'
                'SERVER MEMBERS INTENT(guild のメンバーの取り込みと、参加・離脱・ロール変更のイベントの購読に必要)。'
                'MESSAGE CONTENT INTENT(記事のコメントの本文の取得に必要)。'
            )
            return 1
    finally:
        await db.close()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        logger.error('設定の読み込みに失敗しました: %s', exc)
        return 1
    try:
        return asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
