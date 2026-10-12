"""Deciding which emoji count as votes.

Exactly two reactions are meaningful: :arrow_up: is an up-vote, :arrow_down: is a down-vote, and no other emoji counts however many are pressed. Where readers can press whatever they like, the total is neither the up/down votes nor a measure of interest, so it is worthless both as an article's rating and as input to the publisher ranking.

The decision lives in one place because the same emoji arrives as two different types: an event's RawReactionActionEvent.emoji is a PartialEmoji, while Reaction.emoji off a re-fetched message is a plain str for unicode emoji. PartialEmoji.__eq__ also returns False for every comparison against a str, so a comparison written for only one of the two silently reports "not a single vote" and raises nothing. Passing both through str() first puts them on the same footing.

The variation selector (U+FE0F) is stripped before comparing for the same reason. Neither Discord nor discord.py normalizes emoji: the bytes the gateway sent become PartialEmoji.name verbatim. The up arrow emoji can be written as U+2B06 alone or as U+2B06 U+FE0F, and which one arrives is up to the sender's implementation, so we even them out on this side. unicodedata.normalize does not strip U+FE0F, so it is no help here.

Reactions the bot adds itself go the other way: sent in the complete form, variation selector included. The REST path is the emoji URL-encoded as-is, and U+FE0F's presence makes it a different emoji, so anything but the form a reader's emoji picker sends produces two separate reaction stacks of the same emoji.
"""

from __future__ import annotations

import logging
from typing import Final

import discord

logger = logging.getLogger(__name__)

# What the bot adds: the complete form, variation selector included (see the module docstring for why).
VOTE_UP: Final = '\N{UPWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}'
VOTE_DOWN: Final = '\N{DOWNWARDS BLACK ARROW}\N{VARIATION SELECTOR-16}'

# What comparison runs against: the bare arrows, variation selector stripped.
_UP_BARE: Final = '\N{UPWARDS BLACK ARROW}'
_DOWN_BARE: Final = '\N{DOWNWARDS BLACK ARROW}'

# Strips both variation selectors: U+FE0F (emoji presentation) and U+FE0E (text presentation).
_STRIP_VARIATION: Final = str.maketrans('', '', '\N{VARIATION SELECTOR-16}\N{VARIATION SELECTOR-15}')


def vote_of(emoji: discord.PartialEmoji | discord.Emoji | str) -> int:
    """Map an emoji to a vote: 1 for an up-vote, -1 for a down-vote, 0 for an emoji that is not used for voting.

    Gives the same answer for a PartialEmoji and for Reaction.emoji's str. A custom emoji's str() is <:name:id>, so it never becomes a vote even when its name is an arrow.
    """
    key = str(emoji).translate(_STRIP_VARIATION)
    if key == _UP_BARE:
        return 1
    if key == _DOWN_BARE:
        return -1
    return 0


def tally(message: discord.Message) -> tuple[int, int]:
    """Count (up-votes, down-votes) from a re-fetched message.

    The one reaction the bot added itself to invite voting is not counted. Reaction.count includes it, so failing to subtract it would give every article and every comment a free vote. Reaction.me is "has this bot reacted", and on a re-fetched message it holds Discord's response as-is.

    If someone has removed the bot's emoji, me is False and the count is exactly the readers' votes.
    """
    up = down = 0
    for reaction in message.reactions:
        vote = vote_of(reaction.emoji)
        if vote == 0:
            continue
        count = reaction.count - (1 if reaction.me else 0)
        # The count cannot go negative, but hold the floor rather than reason about how Discord counts.
        count = max(count, 0)
        if vote > 0:
            up = count
        else:
            down = count
    return up, down


async def seed(message: discord.Message) -> bool:
    """Add the vote reactions. True once both are on, False if they could not be added.

    This lets a reader vote with one press without opening the emoji picker; without it, the very first vote costs effort of its own. Up-vote first, then down-vote: Discord orders reactions by when each was first added, so reversing the two puts the down-vote in front.

    A failure does not stop the caller. Readers can add the emoji themselves, and both the article and the comment stand without them. A row that was not seeded stays in the queue and is picked up again on the next pass.
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
