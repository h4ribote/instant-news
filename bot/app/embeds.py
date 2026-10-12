"""Assembly of the Discord messages (embeds) that carry articles.

A sponsored article (is_sponsored) always carries the "Sponsored" label and the sponsor name in the embed too. Rendering the disclosure only on the article page would let anyone who reads just the headline in Discord receive the article without learning it is an ad: that is a missing disclosure, and the article page disclosing it is no excuse to omit it here. So it goes in both the title prefix and a field, where the article side cannot remove it.

Breaking news (is_breaking) mentions roles in the message content. The mention targets come from RuntimeSettings; empty means no mention, because an empty list is a valid setting meaning "do not mention even for breaking news" and must not be silently replaced by some default target.

The values needed for assembly come from two sources. base_url, which builds the absolute article and image URLs, is bound to the runtime environment and so lives in Config; the mention targets can be changed by an admin from the admin screen and so live in RuntimeSettings. Functions that need both are passed both.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

import discord

from common.config import Config
from common.settings import RuntimeSettings

# Discord's limits. Exceeding one fails the send itself with 400, so always truncate before passing.
EMBED_TITLE_LIMIT: Final = 256
EMBED_DESCRIPTION_LIMIT: Final = 4096
EMBED_FIELD_VALUE_LIMIT: Final = 1024
EMBED_AUTHOR_NAME_LIMIT: Final = 256
THREAD_NAME_LIMIT: Final = 100
MESSAGE_CONTENT_LIMIT: Final = 2000

# Disclosure and breaking-news labels. Used both as the title prefix and as field names.
SPONSORED_LABEL: Final = '広告'
BREAKING_LABEL: Final = '速報'

COLOR_SPONSORED: Final = 0xC9A227
COLOR_BREAKING: Final = 0xD7263D
COLOR_DEFAULT: Final = 0x2B6CB0

# How many default avatars Discord assigns across accounts. A user with no avatar set gets the one this modulus lands on.
_DEFAULT_AVATAR_COUNT: Final = 6


@dataclass(frozen=True)
class ArticleView:
    """Every article field the embed needs, gathered into one object. delivery reads the row from the DB and builds it.

    Holds display values only, never the body (body_md / body_html): what goes to Discord is the headline and the excerpt, not the body itself.
    """

    id: int
    slug: str
    title: str
    excerpt: str | None
    cover_image_url: str | None
    is_breaking: bool
    is_sponsored: bool
    sponsor_name: str | None
    byline_mode: str
    category_name: str
    tag_names: tuple[str, ...]
    publisher_name: str | None
    publisher_logo_url: str | None
    publisher_stance: str | None
    author_discord_user_id: int
    author_username: str
    author_global_name: str | None
    author_avatar_hash: str | None
    published_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any], tag_names: Sequence[str]) -> ArticleView:
        """Build from one row joining article, category, publisher and author, plus the list of tag names."""
        return cls(
            id=int(row['id']),
            slug=str(row['slug']),
            title=str(row['title']),
            excerpt=row['excerpt'],
            cover_image_url=row['cover_image_url'],
            is_breaking=bool(row['is_breaking']),
            is_sponsored=bool(row['is_sponsored']),
            sponsor_name=row['sponsor_name'],
            byline_mode=str(row['byline_mode']),
            category_name=str(row['category_name']),
            tag_names=tuple(tag_names),
            publisher_name=row['publisher_name'],
            publisher_logo_url=row['publisher_logo_url'],
            publisher_stance=row['publisher_stance'],
            author_discord_user_id=int(row['author_discord_user_id']),
            author_username=str(row['author_username']),
            author_global_name=row['author_global_name'],
            author_avatar_hash=row['author_avatar_hash'],
            published_at=row['published_at'],
        )


@dataclass(frozen=True)
class OutgoingMessage:
    """One message to send. Allowed mentions are always passed explicitly, so they are held together with the embed."""

    content: str | None
    embed: discord.Embed
    allowed_mentions: discord.AllowedMentions


def article_url(config: Config, slug: str) -> str:
    """The article permalink. base_url has its trailing / stripped at load time, so it can be concatenated as is."""
    return f'{config.server.base_url}/articles/{slug}'


def absolute_url(config: Config, url: str | None) -> str | None:
    """Turn a URL held in the DB into an absolute URL Discord can reach.

    Ingested images are stored as relative URLs like /media/{hash}/{variant}.webp, so the public URL (base_url) has to be prefixed. A URL already stored as an external absolute URL (a publisher hosting its logo elsewhere, say) is left untouched.
    """
    if not url:
        return None
    if url.startswith(('http://', 'https://')):
        return url
    return f'{config.server.base_url}/{url.lstrip("/")}'


def _truncate(text: str, limit: int) -> str:
    """Truncate to a Discord limit. A truncated string gets an ellipsis so the reader can see it was cut off mid-way."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + '…'


def _avatar_url(discord_user_id: int, avatar_hash: str | None) -> str:
    """The avatar URL on Discord's CDN; users with no avatar set get a default avatar.

    Which default avatar a user gets is derived from the high bits of the user ID (the snowflake's timestamp part). A hash beginning with a_ is animated and takes a different extension.
    """
    if not avatar_hash:
        return f'https://cdn.discordapp.com/embed/avatars/{(discord_user_id >> 22) % _DEFAULT_AVATAR_COUNT}.png'
    extension = 'gif' if avatar_hash.startswith('a_') else 'png'
    return f'https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.{extension}?size=64'


def _publisher_label(article: ArticleView) -> str | None:
    """The publisher's display name, with a label attached when its stance is state (state media and the like), so the reader can weigh what kind of publisher it came from."""
    if not article.publisher_name:
        return None
    if article.publisher_stance == 'state':
        return f'{article.publisher_name}(国営)'
    return article.publisher_name


def _byline(config: Config, article: ArticleView) -> tuple[str, str | None]:
    """The byline (display name and icon URL).

    A byline_mode='publisher' article shows the publisher name only and must never show the author name: the author is retained purely for audit, and if Discord alone showed what the article page deliberately does not, withholding the byline would be defeated.
    """
    publisher = _publisher_label(article)
    if article.byline_mode == 'publisher' and publisher:
        return _truncate(publisher, EMBED_AUTHOR_NAME_LIMIT), absolute_url(config, article.publisher_logo_url)
    author = article.author_global_name or article.author_username
    avatar = _avatar_url(article.author_discord_user_id, article.author_avatar_hash)
    if publisher:
        return _truncate(f'{author} / {publisher}', EMBED_AUTHOR_NAME_LIMIT), avatar
    return _truncate(author, EMBED_AUTHOR_NAME_LIMIT), avatar


def _color(article: ArticleView) -> discord.Colour:
    """The embed colour. Disclosure wins: sponsored is decided before breaking, so that an ad is never lost in the breaking colour and read as ordinary news."""
    if article.is_sponsored:
        return discord.Colour(COLOR_SPONSORED)
    if article.is_breaking:
        return discord.Colour(COLOR_BREAKING)
    return discord.Colour(COLOR_DEFAULT)


def _title(article: ArticleView) -> str:
    """The title. Sponsored and breaking are marked with prefixes, and the sponsored prefix goes first so the disclosure survives truncation in a listing."""
    prefix = ''
    if article.is_sponsored:
        prefix += f'【{SPONSORED_LABEL}】'
    if article.is_breaking:
        prefix += f'【{BREAKING_LABEL}】'
    return _truncate(f'{prefix}{article.title}', EMBED_TITLE_LIMIT)


def _sponsored_disclosure(article: ArticleView) -> str:
    """The sponsored-article disclosure sentence.

    server requires the sponsor name on every is_sponsored=1 article, but if it is somehow missing the disclosure itself is still not dropped: disclosing that this is an ad is required independently of whether the sponsor is known, and staying silent because the name is absent is the worst of the options.
    """
    if article.sponsor_name:
        return f'この記事は {article.sponsor_name} の提供による記事広告です。'
    return 'この記事は記事広告です。'


def build_article_embed(config: Config, article: ArticleView) -> discord.Embed:
    """The embed for a single article."""
    embed = discord.Embed(
        title=_title(article),
        url=article_url(config, article.slug),
        description=_truncate(article.excerpt, EMBED_DESCRIPTION_LIMIT) if article.excerpt else None,
        colour=_color(article),
    )
    if article.published_at is not None:
        # DATETIME carries no timezone and this system stores timestamps in UTC, so passing a naive value through would have discord.py take it for the host's local time and convert it, shifting the displayed time by the offset.
        published_at = article.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        embed.timestamp = published_at

    name, icon_url = _byline(config, article)
    embed.set_author(name=name, icon_url=icon_url)

    if article.is_sponsored:
        # Shown in a field of its own as well as in the title prefix: with the prefix alone, an article whose title is long enough to be truncated can lose the disclosure.
        embed.add_field(name=SPONSORED_LABEL, value=_truncate(_sponsored_disclosure(article), EMBED_FIELD_VALUE_LIMIT), inline=False)

    embed.add_field(name='カテゴリ', value=_truncate(article.category_name, EMBED_FIELD_VALUE_LIMIT), inline=True)
    if article.tag_names:
        embed.add_field(name='タグ', value=_truncate(' / '.join(article.tag_names), EMBED_FIELD_VALUE_LIMIT), inline=True)

    cover = absolute_url(config, article.cover_image_url)
    if cover:
        embed.set_image(url=cover)
    return embed


def _mention_content(settings: RuntimeSettings, article: ArticleView) -> tuple[str | None, discord.AllowedMentions]:
    """The breaking-news mention content, and an AllowedMentions permitting only those mentions.

    The allowance is explicit so that the only roles ever mentioned are the configured ones. By default any mention string written into the content pings for real, so without narrowing the allowance, mixing some other value into content later could fire an unintended mass ping.
    """
    role_ids = settings.articles.breaking.mention_role_ids
    if not article.is_breaking or not role_ids:
        return None, discord.AllowedMentions.none()
    content = ' '.join(f'<@&{role_id}>' for role_id in role_ids)
    mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=role_id) for role_id in role_ids], replied_user=False)
    return _truncate(content, MESSAGE_CONTENT_LIMIT), mentions


def build_channel_message(config: Config, settings: RuntimeSettings, article: ArticleView) -> OutgoingMessage:
    """The notification post for a newly published article, sent to a channel. The mention targets come from the settings the caller re-reads on every pass."""
    content, mentions = _mention_content(settings, article)
    return OutgoingMessage(content=content, embed=build_article_embed(config, article), allowed_mentions=mentions)


def build_subscription_dm(config: Config, article: ArticleView, matched_tags: str | None) -> OutgoingMessage:
    """The DM sent to a tag's subscribers.

    The content names which tag's subscription delivered it, so the reader can decide whether to unsubscribe. When the tag cannot be identified (the subscription has since been cancelled, say), the DM is still sent, because the notification rests on the fact that they were subscribed at publish time. Role mentions do not work in a DM, so nothing is mentioned even for breaking news.
    """
    if matched_tags:
        content = f'購読中のタグ「{_truncate(matched_tags, 200)}」に新しい記事が公開されました。'
    else:
        content = '購読中のタグに新しい記事が公開されました。'
    return OutgoingMessage(
        content=_truncate(content, MESSAGE_CONTENT_LIMIT),
        embed=build_article_embed(config, article),
        allowed_mentions=discord.AllowedMentions.none(),
    )


def thread_name(article: ArticleView) -> str:
    """The name of the comment thread, built from the article title."""
    return _truncate(article.title, THREAD_NAME_LIMIT)
