"""通知・DM の送信ループ。

server は記事を公開した時点で、宛先チャンネルごとに article_discord_posts へ message_id = NULL の行を、タグの購読者ごとに article_subscription_notices へ sent_at = NULL の行を積む。このループはその 2 つの待ち行列を拾って送り、送信できたものに印を付ける。送信できなかった行は印が付かないまま残り、次の周回で再び拾われる。

ここでは記事を公開してよいかどうかを判断しない。行が積まれていることが「server が公開を確定させた」という事実であり、bot はそれを送るだけである。状態を見て送信を取りやめる判断を bot に持たせると、公開の権威が 2 箇所に分かれる。

送信の間隔・スレッドを開き直す対象の日数・速報の mention 先は settings 表から周回のたびに読み直す。いずれも管理者が画面から変更でき、変更のたびに bot の再起動を要求しないため。記事 URL とカバー画像の絶対 URL に使う base_url だけは実行環境と一体であるため Config から取る。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Final

import discord

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings, SettingsStore

from . import resolve_channel, votes
from .embeds import ArticleView, OutgoingMessage, build_channel_message, build_subscription_dm, thread_name

logger = logging.getLogger(__name__)

# 1 周回で処理する行数の上限。1 回の周回を短く保ち、大量の滞留があっても他方の待ち行列やループ全体を待たせないための区切りであって、処理しきれなかった行は次の周回で続きから拾われる。
POST_BATCH_LIMIT: Final = 50
DM_BATCH_LIMIT: Final = 100

# スレッドが自動アーカイブされるまでの分数。7 日は Discord が許す最長で、コメントの受け皿である以上できるだけ開けておく(アーカイブされても履歴は読めるが、新たなコメントは付かなくなる)。
THREAD_AUTO_ARCHIVE_MINUTES: Final = 10080

# 設定そのものを読み取れなかったときに次の周回まで待つ秒数。本来の間隔は設定の中にあるため、それが読めない間は本来の間隔を知る術がない。DB の瞬断や初期値の未投入で読めない状態は復旧しうるので、短めの一定間隔で読み直す。
SETTINGS_RETRY_SECONDS: Final = 30

_ARTICLE_SQL: Final = """
    SELECT a.id, a.slug, a.title, a.excerpt, a.cover_image_url, a.is_breaking, a.is_sponsored,
           a.sponsor_name, a.byline_mode, a.published_at,
           c.name AS category_name,
           p.name AS publisher_name, p.logo_url AS publisher_logo_url, p.stance AS publisher_stance,
           u.discord_user_id AS author_discord_user_id, u.username AS author_username,
           u.global_name AS author_global_name, u.avatar_hash AS author_avatar_hash
      FROM articles a
      JOIN categories c ON c.id = a.category_id
      JOIN users u      ON u.id = a.author_id
      LEFT JOIN publishers p ON p.id = a.publisher_id
     WHERE a.id = %s
"""

_ARTICLE_TAGS_SQL: Final = """
    SELECT t.name
      FROM article_tags atg
      JOIN tags t ON t.id = atg.tag_id
     WHERE atg.article_id = %s
     ORDER BY t.name
"""

_PENDING_POSTS_SQL: Final = """
    SELECT id, article_id, channel_id
      FROM article_discord_posts
     WHERE message_id IS NULL
     ORDER BY enqueued_at, id
     LIMIT %s
"""

_MARK_POSTED_SQL: Final = """
    UPDATE article_discord_posts
       SET message_id = %s, posted_at = NOW()
     WHERE id = %s AND message_id IS NULL
"""

_SET_THREAD_SQL: Final = 'UPDATE article_discord_posts SET thread_id = %s WHERE id = %s'

_MARK_SEEDED_SQL: Final = 'UPDATE article_discord_posts SET reactions_seeded_at = NOW() WHERE id = %s'

# 送信は済んだが投票用の絵文字を付け損ねた行。付け損ねたままだと、読者はその記事にだけ絵文字パレットから投票することになる。スレッドの作り直しと同じく、送信済みの行は未送信の待ち行列に現れないため、ここで拾い直さないと永久に付かない。
_PENDING_REACTIONS_SQL: Final = """
    SELECT id, channel_id, message_id
      FROM article_discord_posts
     WHERE message_id IS NOT NULL
       AND reactions_seeded_at IS NULL
       AND posted_at >= NOW() - INTERVAL %s DAY
     ORDER BY posted_at, id
     LIMIT %s
"""

# 送信は済んだがスレッドの作成だけが失敗した行。message_id が埋まった行は未送信の待ち行列に現れないため、ここで拾い直さないとコメント欄が永久に開かない。投票とコメントを照合する期間を過ぎた行は対象から外す(いつまでも開こうとし続けないための打ち切りであり、その頃には記事も読まれていない)。
_PENDING_THREADS_SQL: Final = """
    SELECT id, article_id, channel_id, message_id
      FROM article_discord_posts
     WHERE message_id IS NOT NULL
       AND thread_id IS NULL
       AND posted_at >= NOW() - INTERVAL %s DAY
     ORDER BY posted_at, id
     LIMIT %s
"""

# 未送信の購読 DM。どのタグの購読で届いたのかを本文に書くため、記事のタグと購読者の購読タグの交わりを併せて引く。
_PENDING_NOTICES_SQL: Final = """
    SELECT n.article_id, n.user_id, u.discord_user_id,
           MIN(n.enqueued_at) AS first_enqueued_at,
           GROUP_CONCAT(DISTINCT t.name ORDER BY t.name SEPARATOR ', ') AS matched_tags
      FROM article_subscription_notices n
      JOIN users u ON u.id = n.user_id
      LEFT JOIN article_tags atg      ON atg.article_id = n.article_id
      LEFT JOIN tag_subscriptions ts  ON ts.user_id = n.user_id AND ts.tag_id = atg.tag_id
      LEFT JOIN tags t                ON t.id = ts.tag_id
     WHERE n.sent_at IS NULL
     GROUP BY n.article_id, n.user_id, u.discord_user_id
     ORDER BY first_enqueued_at, n.article_id, n.user_id
     LIMIT %s
"""

_MARK_NOTICE_SENT_SQL: Final = """
    UPDATE article_subscription_notices
       SET sent_at = NOW()
     WHERE article_id = %s AND user_id = %s AND sent_at IS NULL
"""


async def run_delivery_loop(client: discord.Client, db: Database, config: Config, store: SettingsStore) -> None:
    """publish_poll_interval_seconds ごとに 2 つの待ち行列を掃き出す。

    設定は周回のたびに読み直す。例外はここで握り潰して次の周回へ進む。設定を読めなかっただけでこのループが死ぬと、以降どの記事の通知も DM も二度と送られない(server は公開を続けるため、待ち行列だけが伸び続ける)。
    """
    await client.wait_until_ready()
    while True:
        try:
            settings = await store.snapshot()
        except Exception:
            logger.exception('設定を読み取れないため、通知の送信を %d 秒後に再試行します。', SETTINGS_RETRY_SECONDS)
            await asyncio.sleep(SETTINGS_RETRY_SECONDS)
            continue
        interval = settings.discord.bot.publish_poll_interval_seconds
        try:
            await deliver_pending(client, db, config, settings)
        except Exception:
            logger.exception('通知の送信で例外が発生しました。%d 秒後に再試行します。', interval)
        await asyncio.sleep(interval)


async def deliver_pending(client: discord.Client, db: Database, config: Config, settings: RuntimeSettings) -> None:
    """未送信の通知・スレッド・購読 DM を 1 周回分だけ処理する。"""
    # 1 つの記事が複数のチャンネルへ通知されるため、同じ周回では記事を引き直さない。
    cache: dict[int, ArticleView | None] = {}
    await _post_pending_notifications(client, db, config, settings, cache)
    await _open_pending_threads(client, db, settings, cache)
    await _seed_pending_votes(client, db, settings)
    await _send_pending_subscription_dms(client, db, config, cache)


async def _load_article(db: Database, article_id: int) -> ArticleView | None:
    row = await db.fetch_one(_ARTICLE_SQL, article_id)
    if row is None:
        return None
    tags = await db.fetch_all(_ARTICLE_TAGS_SQL, article_id)
    return ArticleView.from_row(row, [str(tag['name']) for tag in tags])


async def _article(db: Database, cache: dict[int, ArticleView | None], article_id: int) -> ArticleView | None:
    if article_id not in cache:
        cache[article_id] = await _load_article(db, article_id)
    return cache[article_id]


async def _text_channel(client: discord.Client, channel_id: int) -> discord.TextChannel | None:
    """通知の宛先として使えるテキストチャンネルを引く。

    通知メッセージにはコメント用のスレッドを開くため、メッセージを送れるだけでなくメッセージからスレッドを作れる種類でなければならない。アナウンスチャンネルもここに含まれる。
    """
    channel = await resolve_channel(client, channel_id)
    if channel is None:
        return None
    if not isinstance(channel, discord.TextChannel):
        logger.warning('チャンネル %s はテキストチャンネルではないため通知できません(種類: %s)。通知先にはメッセージとスレッドを作れるテキストチャンネルを設定してください。', channel_id, type(channel).__name__)
        return None
    return channel


async def _post_pending_notifications(client: discord.Client, db: Database, config: Config, settings: RuntimeSettings, cache: dict[int, ArticleView | None]) -> None:
    rows = await db.fetch_all(_PENDING_POSTS_SQL, POST_BATCH_LIMIT)
    for row in rows:
        try:
            await _post_one(client, db, config, settings, cache, row)
        except Exception:
            logger.exception('記事 %s のチャンネル %s への通知に失敗しました。次の周回で再試行します。', row['article_id'], row['channel_id'])


async def _post_one(client: discord.Client, db: Database, config: Config, settings: RuntimeSettings, cache: dict[int, ArticleView | None], row: Mapping[str, Any]) -> None:
    article = await _article(db, cache, int(row['article_id']))
    if article is None:
        logger.warning('記事 %s が見つからないため通知を保留します。', row['article_id'])
        return
    channel = await _text_channel(client, int(row['channel_id']))
    if channel is None:
        return

    message = await channel.send(**_send_kwargs(build_channel_message(config, settings, article)))
    # 送信の直後に message_id を確定させる。スレッドの作成や絵文字の付与を待ってから書くと、その間に落ちた場合に次の周回で同じ記事をもう一度送ってしまう。読者にとって二重投稿は取り返しがつかないが、スレッドが開かれない・絵文字が付かない状態はいずれも次の周回で回復できる。ここから下で例外が出ても送信済みの記録は残る、という順序に意味がある。
    updated = await db.execute(_MARK_POSTED_SQL, message.id, row['id'])
    if updated == 0:
        logger.error('記事 %s のチャンネル %s への通知を送信しましたが、既に送信済みとして記録されていました。メッセージ %s が重複している可能性があります。', row['article_id'], row['channel_id'], message.id)
        return
    logger.info('記事 %s をチャンネル %s へ通知しました(メッセージ %s)。', row['article_id'], row['channel_id'], message.id)
    await _seed_votes(db, message, int(row['id']))
    await _open_thread(db, message, article, int(row['id']))


def _send_kwargs(message: OutgoingMessage) -> dict[str, Any]:
    """OutgoingMessage を discord.py の送信引数へ写す。mention の許可は常に明示して渡す。"""
    return {'content': message.content, 'embed': message.embed, 'allowed_mentions': message.allowed_mentions}


async def _seed_votes(db: Database, message: discord.Message, post_id: int) -> None:
    """通知メッセージへ投票用の絵文字を付け、付け終えたことを記録する。

    付けられなければ記録しないまま戻る。その行は待ち行列に残り、次の周回で拾い直される。絵文字が無くても読者は自分で付けられるため、ここで失敗しても通知そのものは成立する。
    """
    if await votes.seed(message):
        await db.execute(_MARK_SEEDED_SQL, post_id)


async def _open_thread(db: Database, message: discord.Message, article: ArticleView, post_id: int) -> None:
    """通知メッセージにコメント用のスレッドを開き、thread_id を記録する。

    コメントは Discord のスレッドで受け、DB が持つのはその写しであるため、ここで開かないとその記事にコメントする場所が無くなる。既にスレッドが付いているメッセージには作り直さず、そのスレッドを記録する(前の周回でスレッドの作成には成功し、記録の前に落ちた場合に二重に開くのを避ける)。
    """
    thread = message.thread
    if thread is None:
        thread = await message.create_thread(name=thread_name(article), auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES)
    await db.execute(_SET_THREAD_SQL, thread.id, post_id)


async def _open_pending_threads(client: discord.Client, db: Database, settings: RuntimeSettings, cache: dict[int, ArticleView | None]) -> None:
    rows = await db.fetch_all(_PENDING_THREADS_SQL, settings.discord.bot.count_reconcile_window_days, POST_BATCH_LIMIT)
    for row in rows:
        try:
            await _open_thread_for_posted(client, db, cache, row)
        except Exception:
            logger.exception('記事 %s の通知メッセージ %s のスレッド作成に失敗しました。次の周回で再試行します。', row['article_id'], row['message_id'])


async def _open_thread_for_posted(client: discord.Client, db: Database, cache: dict[int, ArticleView | None], row: Mapping[str, Any]) -> None:
    article = await _article(db, cache, int(row['article_id']))
    if article is None:
        return
    channel = await _text_channel(client, int(row['channel_id']))
    if channel is None:
        return
    try:
        message = await channel.fetch_message(int(row['message_id']))
    except discord.NotFound:
        logger.info('通知メッセージ %s は存在しないため、スレッドを開けません(削除された可能性があります)。', row['message_id'])
        return
    await _open_thread(db, message, article, int(row['id']))


async def _seed_pending_votes(client: discord.Client, db: Database, settings: RuntimeSettings) -> None:
    """投票用の絵文字を付け損ねた通知を拾い直す。"""
    rows = await db.fetch_all(_PENDING_REACTIONS_SQL, settings.discord.bot.count_reconcile_window_days, POST_BATCH_LIMIT)
    for row in rows:
        try:
            channel = await _text_channel(client, int(row['channel_id']))
            if channel is None:
                continue
            try:
                message = await channel.fetch_message(int(row['message_id']))
            except discord.NotFound:
                # 通知そのものが消された。付ける先が無いため、この行を待ち行列から外す(次の周回で拾い直しても同じ結果になる)。
                await db.execute(_MARK_SEEDED_SQL, row['id'])
                continue
            await _seed_votes(db, message, int(row['id']))
        except Exception:
            logger.exception('通知メッセージ %s への投票用の絵文字の付与に失敗しました。次の周回で再試行します。', row['message_id'])


async def _send_pending_subscription_dms(client: discord.Client, db: Database, config: Config, cache: dict[int, ArticleView | None]) -> None:
    rows = await db.fetch_all(_PENDING_NOTICES_SQL, DM_BATCH_LIMIT)
    for row in rows:
        try:
            await _send_one_dm(client, db, config, cache, row)
        except Exception:
            logger.exception('記事 %s の購読 DM(ユーザー %s)の送信に失敗しました。次の周回で再試行します。', row['article_id'], row['discord_user_id'])


async def _send_one_dm(client: discord.Client, db: Database, config: Config, cache: dict[int, ArticleView | None], row: Mapping[str, Any]) -> None:
    article = await _article(db, cache, int(row['article_id']))
    if article is None:
        logger.warning('記事 %s が見つからないため購読 DM を保留します。', row['article_id'])
        return
    discord_user_id = int(row['discord_user_id'])

    user = client.get_user(discord_user_id)
    if user is None:
        try:
            user = await client.fetch_user(discord_user_id)
        except discord.NotFound:
            # 退会したアカウントは以後も取得できない。再試行しても永久に成功しないため送信済みとして畳む。
            logger.warning('Discord ユーザー %s が存在しないため、記事 %s の購読 DM を送信済みとして扱います。', discord_user_id, row['article_id'])
            await db.execute(_MARK_NOTICE_SENT_SQL, row['article_id'], row['user_id'])
            return

    try:
        await user.send(**_send_kwargs(build_subscription_dm(config, article, row['matched_tags'])))
    except discord.Forbidden:
        # DM を受け付けない設定・ブロック・共通のサーバーが無い場合。相手の設定である以上、待っても解消しない。
        # ここで送信済みにしないと同じ相手へ永久に再試行し続け、待ち行列が詰まる。
        logger.info('Discord ユーザー %s は DM を受け付けないため、記事 %s の購読 DM を送信済みとして扱います。', discord_user_id, row['article_id'])
    else:
        logger.info('記事 %s の購読 DM を Discord ユーザー %s へ送信しました。', row['article_id'], discord_user_id)
    await db.execute(_MARK_NOTICE_SENT_SQL, row['article_id'], row['user_id'])
