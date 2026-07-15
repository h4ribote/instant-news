"""Discord bot の実装。記事の通知・DM の送信と、ゲートウェイのイベントによるロール・メンバー・投票・コメントの反映を担う。

この bot は Discord への送信と、Discord から届くイベントの反映のみを担い、記事を公開するかどうかの判断は一切行わない。公開は server が行い、その時点で article_discord_posts へ message_id = NULL の行を、article_subscription_notices へ sent_at = NULL の行を積む。bot はその待ち行列を拾って送るだけである。server から bot へ直接イベントを渡す方式にすると、bot が停止している間に公開された記事の通知が失われるが、DB を待ち行列にすれば bot は復帰後に未送信の行を拾い直せる。

これは Discord から bot へ届くゲートウェイのイベントとは別の話である。そちらは Discord 側で起きた出来事の唯一の知らせであり、購読する(client.py を参照)。取りこぼしうるという性質は同じであるため、写しを保つ各部品は取りこぼしを前提に照合を持つ。

本パッケージには、送信ループとイベントのリスナが共有する部品だけを置く。
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


async def resolve_channel(client: discord.Client, channel_id: int) -> discord.abc.GuildChannel | discord.Thread | discord.abc.PrivateChannel | None:
    """チャンネルをキャッシュから引き、無ければ API から取得する。存在しない・アクセスできない場合は None。

    見つからない場合に例外を投げないのは、宛先チャンネルの設定誤りや削除が 1 件あっても、他の宛先への送信・数え直しを止めてはならないため。

    キャッシュ上のオブジェクトが持つ数(Thread.message_count など)を読む用途には使わない。discord.py はキャッシュ上のスレッドの返信数を更新しないため、古い値をそのまま返す。
    """
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(channel_id)
    except discord.NotFound:
        logger.warning('チャンネル %s が見つかりません。削除されたか、ID の設定が誤っている可能性があります。', channel_id)
    except discord.Forbidden:
        logger.warning('チャンネル %s へアクセスする権限が bot にありません。', channel_id)
    return None
