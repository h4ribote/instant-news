"""The logo and the top bar: rendered into every page from the site.* settings, and validated on the way in."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import CreateArticle, MakeKey, World
from server.app.pages import WEB_DIR
from server.app.site_chrome import LOGO_PLACEHOLDER, TOPBAR_PLACEHOLDER

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures('restore_site_settings')]

_PAGES = ('/', '/categories/news', '/tags/anything', '/publishers/anyone', '/editor', '/settings', '/admin')


async def _update(client: httpx.AsyncClient, world: World, make_key: MakeKey, updates: dict[str, Any]) -> httpx.Response:
    assert world.admin.user_id is not None
    headers = await make_key(world.admin.user_id, 'settings:admin')
    return await client.patch('/api/admin/settings', json={'updates': updates}, headers=headers)


async def _configure(client: httpx.AsyncClient, world: World, make_key: MakeKey, updates: dict[str, Any]) -> None:
    response = await _update(client, world, make_key, updates)
    assert response.status_code == 200, response.text


_CONFIGURED = {
    'site.logo.primary': 'Daily',
    'site.logo.secondary': 'Wire',
    'site.logo.tagline': 'All the news',
    'site.promo.messages': ['First line', 'Second line'],
    'site.promo.interval_seconds': 4,
}


def _assert_configured(html: str) -> None:
    assert '<!-- site:' not in html
    assert '<a class="logo" href="/"><span class="mark">Daily<span>Wire</span></span><span class="tag">All the news</span></a>' in html
    assert '<div class="promo" data-promo data-interval="4" aria-live="off"><span class="promo-item is-current">First line</span><span class="promo-item" aria-hidden="true">Second line</span></div>' in html
    assert '<div class="topbar" data-promo-bar>' in html


async def test_every_template_has_one_marker_of_each() -> None:
    templates = sorted(WEB_DIR.glob('*.html'))
    assert templates
    for path in templates:
        text = path.read_text(encoding='utf-8')
        assert text.count(TOPBAR_PLACEHOLDER) == 1, path.name
        assert text.count(LOGO_PLACEHOLDER) == 1, path.name


@pytest.mark.parametrize('path', _PAGES)
async def test_every_page_carries_the_configured_logo_and_top_bar(client: httpx.AsyncClient, world: World, make_key: MakeKey, path: str) -> None:
    await _configure(client, world, make_key, _CONFIGURED)
    page = await client.get(path)
    assert page.status_code == 200
    assert page.headers['Cache-Control'] == 'no-cache'
    _assert_configured(page.text)


async def test_article_page_carries_them_too(client: httpx.AsyncClient, world: World, make_key: MakeKey, create_article: CreateArticle) -> None:
    article = (await create_article(slug='chrome')).json()['article']
    published = await client.post(f'/api/articles/{article["id"]}/publish', headers=world.author.headers)
    assert published.status_code == 200, published.text
    await _configure(client, world, make_key, _CONFIGURED)
    page = await client.get('/articles/chrome')
    assert page.status_code == 200
    _assert_configured(page.text)
    assert '<meta property="og:title" content="Headline">' in page.text


async def test_text_is_escaped(client: httpx.AsyncClient, world: World, make_key: MakeKey) -> None:
    await _configure(client, world, make_key, {'site.logo.primary': '<b>x</b>', 'site.promo.messages': ['<script>alert(1)</script>']})
    html = (await client.get('/')).text
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert '<span class="mark">&lt;b&gt;x&lt;/b&gt;' in html


async def test_colours_become_custom_properties(client: httpx.AsyncClient, world: World, make_key: MakeKey) -> None:
    response = await _update(client, world, make_key, {'site.logo.primary_color_light': '#AABBCC', 'site.logo.secondary_color_dark': '#112233'})
    assert response.status_code == 200, response.text
    items = {item['key']: item for item in response.json()['items']}
    assert items['site.logo.primary_color_light']['value'] == '#aabbcc'
    assert items['site.logo.primary_color_dark']['value'] is None
    assert '<a class="logo" href="/" style="--logo-primary-light:#aabbcc;--logo-secondary-dark:#112233">' in (await client.get('/')).text

    await _configure(client, world, make_key, {'site.logo.primary_color_light': None, 'site.logo.secondary_color_dark': None})
    assert '<a class="logo" href="/">' in (await client.get('/')).text


async def test_blank_secondary_and_tagline_leave_their_elements_out(client: httpx.AsyncClient, world: World, make_key: MakeKey) -> None:
    await _configure(client, world, make_key, {'site.logo.primary': 'Solo', 'site.logo.secondary': '', 'site.logo.tagline': ''})
    assert '<a class="logo" href="/"><span class="mark">Solo</span></a>' in (await client.get('/')).text


async def test_no_message_renders_the_bar_hidden(client: httpx.AsyncClient, world: World, make_key: MakeKey) -> None:
    await _configure(client, world, make_key, {'site.promo.messages': []})
    html = (await client.get('/')).text
    assert '<div class="topbar" data-promo-bar hidden>' in html
    assert 'promo-item' not in html


@pytest.mark.parametrize(
    'updates',
    [
        {'site.logo.primary_color_light': 'red'},
        {'site.logo.primary_color_light': '#12345'},
        {'site.logo.primary': ''},
        {'site.logo.primary': '   '},
        {'site.logo.primary': 'x' * 41},
        {'site.logo.tagline': 'two\nlines'},
        {'site.promo.messages': ['line\nbreak']},
        {'site.promo.messages': ['m'] * 11},
        {'site.promo.messages': ['']},
        {'site.promo.messages': ['x' * 121]},
        {'site.promo.messages': 'not a list'},
        {'site.promo.interval_seconds': 2},
        {'site.promo.interval_seconds': 61},
    ],
)
async def test_invalid_values_are_rejected(client: httpx.AsyncClient, world: World, make_key: MakeKey, updates: dict[str, Any]) -> None:
    response = await _update(client, world, make_key, updates)
    assert response.status_code == 400, response.text


async def test_listing_carries_the_text_limits(client: httpx.AsyncClient, world: World, make_key: MakeKey) -> None:
    assert world.admin.user_id is not None
    headers = await make_key(world.admin.user_id, 'settings:admin')
    items = {item['key']: item for item in (await client.get('/api/admin/settings', headers=headers)).json()['items']}
    assert items['site.promo.messages']['kind'] == 'str_list'
    assert items['site.promo.messages']['max_items'] == 10
    assert items['site.promo.messages']['max_length'] == 120
    assert items['site.logo.secondary']['allow_empty'] is True
    assert items['site.logo.primary']['allow_empty'] is False
    assert items['site.logo.primary_color_light']['kind'] == 'color'
