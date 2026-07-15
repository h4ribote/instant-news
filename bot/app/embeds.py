"""記事を Discord へ流すためのメッセージ(embed)の組み立て。

記事広告(is_sponsored)は embed にも必ず「広告」と提供元の名前を出す。記事面にだけ開示を描画して embed に出さないと、Discord で見出しだけを読む人には広告であることが伝わらないまま届く。これは開示の欠落であり、記事面が開示していることを理由に省いてよいものではない。ここでは表題の接頭辞とフィールドの両方に出し、記事の側から消せない形にする。

速報(is_breaking)はメッセージ本文でロールを mention する。mention 先は RuntimeSettings が与え、空なら mention しない(空リストは「速報でも mention しない」という有効な設定であり、既定の mention 先へ勝手に落とすことはしない)。

組み立てに要る値は 2 つの出どころから来る。記事・画像の絶対 URL を作る base_url は実行環境と一体であるため Config が持ち、mention 先は管理者が画面から変更できるため RuntimeSettings が持つ。両方を要する関数には両方を渡す。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

import discord

from common.config import Config
from common.settings import RuntimeSettings

# Discord の制限。超過すると送信そのものが 400 で失敗するため、必ず丸めてから渡す。
EMBED_TITLE_LIMIT: Final = 256
EMBED_DESCRIPTION_LIMIT: Final = 4096
EMBED_FIELD_VALUE_LIMIT: Final = 1024
EMBED_AUTHOR_NAME_LIMIT: Final = 256
THREAD_NAME_LIMIT: Final = 100
MESSAGE_CONTENT_LIMIT: Final = 2000

# 開示・速報のラベル。表題の接頭辞とフィールド名の両方で使う。
SPONSORED_LABEL: Final = '広告'
BREAKING_LABEL: Final = '速報'

COLOR_SPONSORED: Final = 0xC9A227
COLOR_BREAKING: Final = 0xD7263D
COLOR_DEFAULT: Final = 0x2B6CB0

# Discord がアカウントへ割り当てる既定アバターの種類数。アバター未設定のユーザーはこの剰余で決まる絵になる。
_DEFAULT_AVATAR_COUNT: Final = 6


@dataclass(frozen=True)
class ArticleView:
    """embed の組み立てに必要な記事の情報を 1 つにまとめたもの。delivery が DB から読み出して作る。

    表示に使う値だけを持ち、本文(body_md / body_html)は持たない。Discord へ流すのは見出しと抜粋であり、本文そのものではないため。
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
        """記事・カテゴリ・報道社・著者を結合した 1 行と、タグ名の一覧から組み立てる。"""
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
    """送信する 1 通のメッセージ。mention の許可は必ず明示して渡すため、embed と組にして持つ。"""

    content: str | None
    embed: discord.Embed
    allowed_mentions: discord.AllowedMentions


def article_url(config: Config, slug: str) -> str:
    """記事のパーマリンク。base_url は読み込み時に末尾の / を落としてあるため、そのまま連結してよい。"""
    return f'{config.server.base_url}/articles/{slug}'


def absolute_url(config: Config, url: str | None) -> str | None:
    """DB に入っている URL を Discord から到達できる絶対 URL にする。

    取り込んだ画像は /media/{hash}/{variant}.webp のような相対 URL で入っているため、公開 URL を前置する必要がある。外部の絶対 URL がそのまま入っている場合(ロゴを外部に置いた報道社など)は触らない。
    """
    if not url:
        return None
    if url.startswith(('http://', 'https://')):
        return url
    return f'{config.server.base_url}/{url.lstrip("/")}'


def _truncate(text: str, limit: int) -> str:
    """Discord の上限へ丸める。切り詰めた場合は省略記号を付け、途中で切れていることを読み手に示す。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + '…'


def _avatar_url(discord_user_id: int, avatar_hash: str | None) -> str:
    """Discord の CDN 上のアバター URL。未設定のユーザーには既定アバターを返す。

    アバターを設定していないユーザーの既定アバターは、ユーザー ID の上位ビット(スノーフレークのタイムスタンプ部)から決まる。ハッシュが a_ で始まるものはアニメーション付きであり、拡張子が異なる。
    """
    if not avatar_hash:
        return f'https://cdn.discordapp.com/embed/avatars/{(discord_user_id >> 22) % _DEFAULT_AVATAR_COUNT}.png'
    extension = 'gif' if avatar_hash.startswith('a_') else 'png'
    return f'https://cdn.discordapp.com/avatars/{discord_user_id}/{avatar_hash}.{extension}?size=64'


def _publisher_label(article: ArticleView) -> str | None:
    """報道社の表示名。立場が state(国営メディア等)の報道社にはラベルを添える。読み手が発行元の性質を踏まえて読めるようにするため。"""
    if not article.publisher_name:
        return None
    if article.publisher_stance == 'state':
        return f'{article.publisher_name}(国営)'
    return article.publisher_name


def _byline(config: Config, article: ArticleView) -> tuple[str, str | None]:
    """署名(表示名とアイコン URL)。

    byline_mode が publisher の記事は報道社名だけを出し、著者名を出してはならない。著者は監査のために保持されているだけであり、記事面がそう表示しないものを Discord だけが表示すれば、署名を伏せた意図が壊れる。
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
    """embed の色。開示を最優先し、広告は速報より先に色を決める(速報の色に紛れて広告と気付かれないことを避ける)。"""
    if article.is_sponsored:
        return discord.Colour(COLOR_SPONSORED)
    if article.is_breaking:
        return discord.Colour(COLOR_BREAKING)
    return discord.Colour(COLOR_DEFAULT)


def _title(article: ArticleView) -> str:
    """表題。広告と速報は接頭辞で示す。広告の接頭辞を先に置き、一覧で切り詰められても開示が残るようにする。"""
    prefix = ''
    if article.is_sponsored:
        prefix += f'【{SPONSORED_LABEL}】'
    if article.is_breaking:
        prefix += f'【{BREAKING_LABEL}】'
    return _truncate(f'{prefix}{article.title}', EMBED_TITLE_LIMIT)


def _sponsored_disclosure(article: ArticleView) -> str:
    """記事広告の開示文。

    提供元の名前は server 側が is_sponsored=1 の記事に必須として検査するが、万一欠けていても開示自体は落とさない。「広告である」ことの開示は提供元の名前が判明しているかどうかとは独立に必要であり、名前が無いことを理由に黙るのは最悪の選択である。
    """
    if article.sponsor_name:
        return f'この記事は {article.sponsor_name} の提供による記事広告です。'
    return 'この記事は記事広告です。'


def build_article_embed(config: Config, article: ArticleView) -> discord.Embed:
    """記事 1 本の embed。"""
    embed = discord.Embed(
        title=_title(article),
        url=article_url(config, article.slug),
        description=_truncate(article.excerpt, EMBED_DESCRIPTION_LIMIT) if article.excerpt else None,
        colour=_color(article),
    )
    if article.published_at is not None:
        # DATETIME はタイムゾーンを持たず、本システムは日時を UTC で格納する。naive のまま渡すと discord.py が
        # 実行環境のローカル時刻とみなして変換し、表示が時差の分だけずれる。
        published_at = article.published_at
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        embed.timestamp = published_at

    name, icon_url = _byline(config, article)
    embed.set_author(name=name, icon_url=icon_url)

    if article.is_sponsored:
        # 表題の接頭辞に加えて独立したフィールドにも出す。接頭辞だけだと、表題が長い記事で切り詰められた際に開示が消えうる。
        embed.add_field(name=SPONSORED_LABEL, value=_truncate(_sponsored_disclosure(article), EMBED_FIELD_VALUE_LIMIT), inline=False)

    embed.add_field(name='カテゴリ', value=_truncate(article.category_name, EMBED_FIELD_VALUE_LIMIT), inline=True)
    if article.tag_names:
        embed.add_field(name='タグ', value=_truncate(' / '.join(article.tag_names), EMBED_FIELD_VALUE_LIMIT), inline=True)

    cover = absolute_url(config, article.cover_image_url)
    if cover:
        embed.set_image(url=cover)
    return embed


def _mention_content(settings: RuntimeSettings, article: ArticleView) -> tuple[str | None, discord.AllowedMentions]:
    """速報の mention 本文と、その mention だけを許可する設定を返す。

    許可を明示するのは、mention されるロールを設定したものだけに限るため。既定では本文に書いた文字列がそのまま通知になるため、許可を絞らないと将来 content に別の値を混ぜたときに意図しない一斉通知が起きうる。
    """
    role_ids = settings.articles.breaking.mention_role_ids
    if not article.is_breaking or not role_ids:
        return None, discord.AllowedMentions.none()
    content = ' '.join(f'<@&{role_id}>' for role_id in role_ids)
    mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=role_id) for role_id in role_ids], replied_user=False)
    return _truncate(content, MESSAGE_CONTENT_LIMIT), mentions


def build_channel_message(config: Config, settings: RuntimeSettings, article: ArticleView) -> OutgoingMessage:
    """チャンネルへ流す新着記事の通知。mention 先は呼び出し側が周回のたびに読み直した設定から取る。"""
    content, mentions = _mention_content(settings, article)
    return OutgoingMessage(content=content, embed=build_article_embed(config, article), allowed_mentions=mentions)


def build_subscription_dm(config: Config, article: ArticleView, matched_tags: str | None) -> OutgoingMessage:
    """タグの購読者へ送る DM。

    どのタグの購読で届いたのかを本文に書く。読み手が購読を解除する判断を下せるようにするため。購読が既に解除されている等でタグを特定できない場合も、DM 自体は送る(公開の時点で購読していた事実に基づく通知であるため)。DM ではロールの mention は成立しないため、速報であっても mention は付けない。
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
    """コメント用スレッドの名前。記事の表題から作る。"""
    return _truncate(article.title, THREAD_NAME_LIMIT)
