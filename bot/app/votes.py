"""投票に使う絵文字の判定。

リアクションは 2 つだけを意味のあるものとして扱う。:arrow_up: が賛成、:arrow_down: が反対であり、それ以外の絵文字はいくつ付いても数えない。読者が好きな絵文字を押せる場では、その総数は賛否でも関心の大きさでもない何かになってしまい、記事の評価としても報道社ランキングの材料としても意味を持たないためである。

この判定を 1 箇所に集めるのは、同じ絵文字が 2 つの異なる型で届くためである。イベントで届く RawReactionActionEvent.emoji は PartialEmoji であり、メッセージを取得し直したときの Reaction.emoji は unicode の絵文字なら素の str である。しかも PartialEmoji.__eq__ は str との比較に対して常に False を返すため、どちらか一方だけを想定した比較は、例外も出ないまま静かに「1 票も入っていない」という結果になる。str() を通してから比較すれば両方を同じ土俵に乗せられる。

異体字セレクタ(U+FE0F)を落としてから比べるのも同じ理由による。Discord も discord.py も絵文字を正規化せず、ゲートウェイが送ってきたバイト列がそのまま PartialEmoji.name になる。⬆️ は U+2B06 単体でも U+2B06 U+FE0F でも表現でき、どちらが届くかを送り手の実装に委ねることになるため、こちら側で揃える。unicodedata.normalize は U+FE0F を落とさないため使えない。

bot が自分で付ける側は、逆に異体字セレクタを含む完全な形で送る。REST のパスは絵文字をそのまま URL エンコードしたものであり、U+FE0F の有無で別の絵文字として扱われるため、読者が絵文字パレットから押したものと同じ形でなければ、同じ絵文字の 2 つの山ができてしまう。
"""

from __future__ import annotations

import logging
from typing import Final

import discord

logger = logging.getLogger(__name__)

# bot が付ける側。異体字セレクタまで含めた完全な形で送る(理由はモジュールの docstring を参照)。
VOTE_UP: Final = '\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}'
VOTE_DOWN: Final = '\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}'

# 比較する側。異体字セレクタを落とした素の矢印。
_UP_BARE: Final = '\N{UPWARDS BLACK ARROW}'
_DOWN_BARE: Final = '\N{DOWNWARDS BLACK ARROW}'

# 異体字セレクタ(絵文字表現・文字表現)を落とす。
_STRIP_VARIATION: Final = str.maketrans('', '', '\N{VARIATION SELECTOR-16}\N{VARIATION SELECTOR-15}')


def vote_of(emoji: discord.PartialEmoji | discord.Emoji | str) -> int:
    """絵文字を票へ写す。賛成なら 1、反対なら -1、投票に使わない絵文字なら 0。

    PartialEmoji でも Reaction.emoji の str でも同じ答えを返す。カスタム絵文字は str() が <:name:id> の形になるため、名前が矢印であっても票にはならない。
    """
    key = str(emoji).translate(_STRIP_VARIATION)
    if key == _UP_BARE:
        return 1
    if key == _DOWN_BARE:
        return -1
    return 0


def tally(message: discord.Message) -> tuple[int, int]:
    """取得し直したメッセージから (賛成, 反対) を数える。

    bot 自身が投票を促すために付けた 1 件は数えない。Reaction.count はそれを含むため、引かなければ全ての記事とコメントに下駄を履かせることになる。Reaction.me は「この bot が押しているか」であり、取得し直したメッセージでは Discord の応答がそのまま入る。

    誰かが bot の付けた絵文字を剥がしていれば me は False になり、そのときは数がそのまま読者の票である。
    """
    up = down = 0
    for reaction in message.reactions:
        vote = vote_of(reaction.emoji)
        if vote == 0:
            continue
        count = reaction.count - (1 if reaction.me else 0)
        # 数が負になることはないが、Discord の数え方に踏み込まずに下限だけ守る。
        count = max(count, 0)
        if vote > 0:
            up = count
        else:
            down = count
    return up, down


async def seed(message: discord.Message) -> bool:
    """投票用の絵文字を付ける。付け終えたら True、付けられなければ False。

    読者が絵文字パレットを開かずに 1 押しで投票できるようにするためであり、これが無いと最初の 1 票のためだけに手間を要求することになる。賛成・反対の順に付ける(Discord はリアクションを最初に付いた順に並べるため、逆にすると反対が先に並ぶ)。

    失敗しても呼び出し側の処理を止めない。絵文字が付いていなくても読者は自分で付けられ、記事もコメントも成立する。付け損ねた行は待ち行列に残り、次の周回で拾い直される。
    """
    try:
        await message.add_reaction(VOTE_UP)
        await message.add_reaction(VOTE_DOWN)
    except discord.Forbidden:
        logger.warning('メッセージ %s に投票用の絵文字を付ける権限が bot にありません(チャンネル %s)。読者は自分で絵文字を付けられますが、1 押しでの投票はできません。', message.id, message.channel.id)
        return False
    except discord.HTTPException:
        logger.exception('メッセージ %s に投票用の絵文字を付けられませんでした。次の周回で再試行します。', message.id)
        return False
    return True
