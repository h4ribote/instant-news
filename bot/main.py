#!/usr/bin/env python3
"""Discord bot のエントリポイント。

リポジトリのルートを作業ディレクトリとして `python -m bot.main` で起動する(common パッケージと bot パッケージをルートからの絶対 import で解決するため)。

この bot は Discord への送信と、Discord から届くイベントの反映だけを行い、記事を公開するかどうかは判断しない。公開は server が行い、送るべきものを DB の待ち行列へ積む。したがって bot が停止していても公開は進み、bot は復帰後に未送信の行を拾って送る。

設定の真実は MySQL の settings 表にあり、config.yaml が持つのはそこへ写す初期値でしかない。bot は自分のトークンすら DB からしか読めないため、Discord へ繋ぐ前に DB へ繋ぎ、初期値の投入を済ませてから設定を読む。server と bot のどちらが先に起動するかは決まっておらず、bot が先だと settings 表が空のままトークンを読むことになるため、投入は bot も行う。INSERT IGNORE で冪等であり、両方が呼んでも、どちらが先でも安全である。

トークンだけは起動時にしか読まない。Discord への接続はトークンで確立するものであり、変更へ追随するには繋ぎ直すしかない。設定の登録簿はトークンを restart_required としており、変更を検知して再接続する仕組みは持たない(その複雑さに見合わない)。他の設定は、送信ループが周回のたびに、イベントのリスナがイベントを処理するたびに読み直すため、bot を再起動せずに効く。

bot の実体は bot/app/client.py の NewsBot にある。どのイベントを購読し、何を写すかはそちらを参照。
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
from common.settings import MissingSettingsError, SettingsStore

logger = logging.getLogger('bot')

LOG_FORMAT: Final = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'


async def _run(config: Config) -> int:
    """DB へ繋ぎ、設定の初期値を投入し、settings 表から読んだトークンで Discord へ繋ぐ。

    DB と Discord の接続を 1 つのイベントループの中で開く。接続プールはそれを作ったイベントループに紐づくため、別のループで作ったプールを bot が掴むと最初のクエリで壊れる。
    """
    db = Database(config.database)
    await db.connect()
    try:
        store = SettingsStore(db)
        try:
            inserted = await store.bootstrap(config.bootstrap)
            settings = await store.snapshot()
        except (ValueError, MissingSettingsError) as exc:
            logger.error('settings 表の設定を読み取れませんでした: %s', exc)
            return 1
        if inserted:
            # 投入は未登録のキーだけを埋める。既にある値を config.yaml で上書きすると、管理画面から変更した設定が再起動のたびに黙って元へ戻る。
            logger.info('settings 表に未登録だった設定へ config.yaml の初期値を投入しました: %s。以後はこの表が真実であり、変更は管理画面から行います。', ', '.join(inserted))
        logger.info('対象の guild %s として起動します。', settings.discord.guild_id)

        bot = NewsBot(config, db, store)
        try:
            # discord.Client.run() ではなく start() を使うのは、トークンを読むために先に DB へ繋ぐ必要があり、run() が自前で asyncio.run() を呼んでしまうため。run(log_handler=None) が行っていた discord.py 側のログ設定の抑止は、start() を使えば設定そのものが行われないため不要になる。
            async with bot:
                await bot.start(settings.discord.bot.token)
        except discord.LoginFailure:
            logger.error('Discord への認証に失敗しました。設定の discord.bot.token を確認してください(python -m server.cli get-setting discord.bot.token で確認できます)。')
            return 1
        except discord.PrivilegedIntentsRequired:
            # どちらが欠けていても Discord は同じ切り方をし、discord.py も区別しない。片方だけを案内すると、それを既に有効にしている運用者は「有効なのに起動しない」という手掛かりの無い状態に置かれるため、両方を挙げて何のために要るかまで書く。
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
