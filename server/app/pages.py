"""Serving the static HTML pages, and injecting og meta into the article page.

The frontend is static HTML and JavaScript that fetches its content from the JSON API and renders it. Social and Discord crawlers do not run JavaScript, so a shared link's card comes out empty. The article page alone is served with og meta already filled in server-side.

**og:image and the body excerpt are emitted only for articles with visibility='public' and status='published'.** Draft, in-review, scheduled and archived articles, and members / restricted ones, emit nothing at all, title included. og meta exists for crawlers and is handed straight to unauthenticated parties, so putting unpublished or limited-visibility content here would make it readable to anyone who knows the URL.

Only the cover image is usable as og:image. The cover image is served without authentication because Discord embeds show it, but images inside the body get the same visibility check as the article, so a crawler fetching one gets a 404.
"""

from __future__ import annotations

import logging
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urljoin

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings

from .articles import resolve_moved_slug
from .deps import get_config, get_db, get_principal_optional, get_settings
from .errors import AppError, NotFound
from .principals import Principal
from .visibility import can_view_article

logger = logging.getLogger(__name__)

router = APIRouter(tags=['pages'])

# Where the static frontend lives: server/web/, relative to server/app/pages.py.
WEB_DIR: Final = Path(__file__).resolve().parent.parent / 'web'

# Marker in the article template that the generated og meta replaces.
OG_PLACEHOLDER: Final = '<!-- og:meta -->'

# Name appended to <title>, so the card title and the tab title match.
SITE_NAME: Final = 'instant-news'

# og:description length. The major social sites cut anything longer, so truncate here rather than leave the cut up to them.
DESCRIPTION_LIMIT: Final = 200

# Drops the <title> the template carries: only one <title> in an HTML document is effective, and a surviving template title comes first and so overrides the article's.
# Comments and <title> are alternatives in one pattern so comments are skipped whole. If a template note mentions <title> or </title> in prose, a search that did not skip comments would treat everything from that mention to the real </title> as a single element and swallow it, taking out the intervening comment's --> and the og marker, after which the following <link> and <script> are eaten by the unclosed comment.
_COMMENT_OR_TITLE: Final = re.compile(r'(?P<comment><!--.*?-->)|(?P<title><title\b[^>]*>.*?</title\s*>)', re.IGNORECASE | re.DOTALL)

# Block elements padded with a space on each side when extracting plain text; without the padding the end of one paragraph and the start of the next join into one word.
_BLOCK_TAGS: Final = frozenset({'p', 'div', 'br', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'table', 'tr', 'td', 'th', 'hr'})


class _TextExtractor(HTMLParser):
    """Pull just the plain text out of rendered body HTML.

    Used to build og:description from the body when the article has no excerpt (articles.excerpt). Interpreting the body Markdown here would pick up code-block contents and markup snippets, so the input is body_html, which has already been rendered and sanitized.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append(' ')

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append(' ')

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return ' '.join(''.join(self._parts).split())


def _plain_text(body_html: str) -> str:
    """Flatten body HTML into a single line of plain text. Fragments that cannot be parsed do not raise; whatever could be extracted is returned."""
    parser = _TextExtractor()
    parser.feed(body_html)
    parser.close()
    return parser.text()


def _truncate(text: str, limit: int) -> str:
    """Truncate a string longer than limit and append an ellipsis."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '…'


def _description(article: dict[str, Any]) -> str | None:
    """The description for og:description: the excerpt if there is one, otherwise built from the body."""
    excerpt = (article.get('excerpt') or '').strip()
    if excerpt:
        return _truncate(' '.join(excerpt.split()), DESCRIPTION_LIMIT)
    body_html = article.get('body_html') or ''
    text = _plain_text(body_html) if body_html else ''
    return _truncate(text, DESCRIPTION_LIMIT) if text else None


def _meta_tag(attribute: str, name: str, content: str) -> str:
    """One meta tag. Attribute values are always escaped: the article title, the excerpt and the cover URL are all strings a writer typed, and embedding them raw would let the writer close the quote and inject arbitrary tags."""
    return f'<meta {attribute}="{escape(name)}" content="{escape(content)}">'


def _is_shareable(article: dict[str, Any]) -> bool:
    """Whether the article's content may be handed to a crawler: only published articles that anyone can view qualify."""
    return article.get('status') == 'published' and article.get('visibility') == 'public'


def _build_meta(config: Config, article: dict[str, Any]) -> str:
    """Build the <title> and the meta tags that go in the article page's head."""
    if not _is_shareable(article):
        # An unpublished draft copy or a limited-visibility article. Only the author and admins can reach this, but nothing is emitted, not even the title, so that no content flows from their page out to a crawler or a share target.
        return f'<title>{escape(SITE_NAME)}</title>\n<meta name="robots" content="noindex">'

    title = str(article.get('title') or '')
    url = f'{config.server.base_url}/articles/{quote(str(article["slug"]), safe="")}'
    tags = [
        f'<title>{escape(title)} | {escape(SITE_NAME)}</title>',
        _meta_tag('property', 'og:title', title),
        _meta_tag('property', 'og:url', url),
        _meta_tag('property', 'og:type', 'article'),
    ]
    description = _description(article)
    if description:
        tags.append(_meta_tag('property', 'og:description', description))
    cover = (article.get('cover_image_url') or '').strip()
    if cover:
        # A delivery URL arrives as an absolute path under /media/..., so prefix the public URL to make it an absolute URL. An absolute URL on a remote host is left as is.
        tags.append(_meta_tag('property', 'og:image', urljoin(f'{config.server.base_url}/', cover)))
    tags.append(_meta_tag('name', 'twitter:card', 'summary_large_image'))
    return '\n'.join(tags)


def _strip_first_title(html: str) -> str:
    """Drop the first <title> element the template carries. Content inside a comment does not count as an element."""
    removed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal removed
        # Return comments untouched: the og marker is itself a comment and must not be removed.
        if match.group('comment') is not None or removed:
            return match.group(0)
        removed = True
        return ''

    return _COMMENT_OR_TITLE.sub(_replace, html)


def _inject_meta(template: str, meta: str) -> str:
    """Replace the template's marker with the generated meta tags."""
    # The template's <title> is always dropped: leaving it puts two <title> elements side by side and the first one wins. Keeping exactly one instead of relying on order gives valid HTML, and even a naive scanner then picks up the article's title.
    stripped = _strip_first_title(template)
    if OG_PLACEHOLDER in stripped:
        return stripped.replace(OG_PLACEHOLDER, meta, 1)
    # A template that has lost the marker still gets its meta. Skipping it silently would leave the share card empty with no one able to notice.
    logger.warning('article.html に %s がありません。</head> の直前へ og メタを挿入します。', OG_PLACEHOLDER)
    if '</head>' in stripped:
        return stripped.replace('</head>', f'{meta}\n</head>', 1)
    logger.error('article.html に </head> がありません。og メタを先頭へ挿入します。')
    return f'{meta}\n{stripped}'


def _page_path(filename: str) -> Path:
    """The file behind a served page. A missing file is a deployment error: the user gets a 500 and the path is recorded in the log, never put in the response."""
    path = WEB_DIR / filename
    if not path.is_file():
        logger.error('ページのテンプレートが見つかりません: %s', path)
        raise AppError()
    return path


def _read_template(filename: str) -> str:
    return _page_path(filename).read_text(encoding='utf-8')


def _serve(filename: str) -> FileResponse:
    """Return a static page as is. JavaScript fetches the content from the API and renders it."""
    return FileResponse(_page_path(filename))


async def render_article_page(db: Database, config: Config, settings: RuntimeSettings, slug: str, principal: Principal | None) -> Response:
    """Build and return the article page.

    An article the viewer cannot see returns 404, not 403. A 403 reveals that something exists at that slug, and for a limited-visibility article whose very existence is secret, that alone is a leak.

    A slug the article used to have answers 301 to its current page, so links already handed out (the Discord embed among them) survive a slug change. The visibility check comes first: the redirect itself would reveal the new slug. Cache-Control: no-cache keeps a cached redirect from looping when the article later takes the old slug back.

    config is used to build public URLs (og:url / og:image), settings to run the visibility check. Both are taken because their sources differ: config.yaml for the former, the settings table for the latter.
    """
    article = await db.fetch_one('SELECT * FROM articles WHERE slug = %s', slug)
    if article is None:
        moved_id = await resolve_moved_slug(db, slug)
        moved = await db.fetch_one('SELECT * FROM articles WHERE id = %s', moved_id) if moved_id is not None else None
        if moved is not None and await can_view_article(db, principal, moved, settings=settings):
            return RedirectResponse(f'/articles/{quote(str(moved["slug"]), safe="")}', status_code=301, headers={'Cache-Control': 'no-cache'})
        raise NotFound('記事が見つかりません。')
    if not await can_view_article(db, principal, article, settings=settings):
        raise NotFound('記事が見つかりません。')
    return HTMLResponse(_inject_meta(_read_template('article.html'), _build_meta(config, article)))


# Routes. Each one carries a fixed prefix so it cannot collide with the API (/api/...), images (/media/...) or static assets (/static/...).


@router.get('/', include_in_schema=False)
async def index_page() -> FileResponse:
    """Home. The latest articles listing."""
    return _serve('index.html')


@router.get('/articles/{slug}', include_in_schema=False)
async def article_page(
    slug: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> Response:
    """The article page. The only one built dynamically, because og meta is injected into it."""
    return await render_article_page(db, config, settings, slug, principal)


@router.get('/tags/{slug}', include_in_schema=False)
async def tag_page(slug: str) -> FileResponse:
    """The tag page: a dedicated hub for a topic."""
    return _serve('tag.html')


@router.get('/publishers/{slug}', include_in_schema=False)
async def publisher_page(slug: str) -> FileResponse:
    """A publisher's profile."""
    return _serve('publisher.html')


@router.get('/categories/{slug}', include_in_schema=False)
async def category_page(slug: str) -> FileResponse:
    """The per-category listing. Uses the same template as the home page; JavaScript reads the filter from the URL."""
    return _serve('index.html')


@router.get('/editor', include_in_schema=False)
async def editor_page() -> FileResponse:
    """The editor: create and edit articles."""
    return _serve('editor.html')


@router.get('/settings', include_in_schema=False)
async def settings_page() -> FileResponse:
    """The user's own settings page (API keys, tag subscriptions)."""
    return _serve('settings.html')


@router.get('/admin', include_in_schema=False)
async def admin_page() -> FileResponse:
    """The platform settings screen (every key in the settings table).

    Only the template is returned here; the setting values are obtainable solely from an API that requires settings:admin. Who they are shown to is decided by that API, not by this route.
    """
    return _serve('admin.html')
