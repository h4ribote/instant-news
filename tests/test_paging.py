"""The listing conventions: cursor pages for listings that grow, {items} for complete collections."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from common.db import Database
from conftest import World

pytestmark = pytest.mark.anyio


async def _publish(client: httpx.AsyncClient, world: World, db: Database, minute: int, **fields: Any) -> str:
    """Publish an article and pin its published_at, so the order under test does not hang on the clock."""
    body = {'title': f'Article {minute}', 'body_md': 'Body.', 'category_id': world.category_id, 'status': 'published', **fields}
    response = await client.post('/api/articles', json=body, headers=world.author.headers)
    assert response.status_code == 201, response.text
    article_id = response.json()['article']['id']
    await db.execute("UPDATE articles SET published_at = '2026-01-01 00:00:00' + INTERVAL %s MINUTE WHERE id = %s", minute, article_id)
    return article_id


async def _walk(client: httpx.AsyncClient, path: str, **params: Any) -> list[list[str]]:
    """Follow next_cursor from the first page to the last and return the ids page by page."""
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        query = {**params, **({'cursor': cursor} if cursor else {})}
        response = await client.get(path, params=query)
        assert response.status_code == 200, response.text
        data = response.json()
        pages.append([item['id'] for item in data['items']])
        cursor = data['next_cursor']
        if cursor is None:
            return pages


async def test_cursor_walks_every_article_once_newest_first(client: httpx.AsyncClient, world: World, db: Database) -> None:
    ids = [await _publish(client, world, db, minute) for minute in range(7)]
    pages = await _walk(client, '/api/articles', per_page=3)
    assert [len(page) for page in pages] == [3, 3, 1]
    assert [article_id for page in pages for article_id in page] == list(reversed(ids))


async def test_articles_sharing_a_timestamp_are_ordered_by_id(client: httpx.AsyncClient, world: World, db: Database) -> None:
    ids = [await _publish(client, world, db, 0) for _ in range(5)]
    pages = await _walk(client, '/api/articles', per_page=2)
    assert [article_id for page in pages for article_id in page] == sorted(ids, key=int, reverse=True)


async def test_new_articles_do_not_shift_later_pages(client: httpx.AsyncClient, world: World, db: Database) -> None:
    ids = [await _publish(client, world, db, minute) for minute in range(6)]
    first = (await client.get('/api/articles', params={'per_page': 3})).json()
    await _publish(client, world, db, 100)
    second = (await client.get('/api/articles', params={'per_page': 3, 'cursor': first['next_cursor']})).json()
    assert [item['id'] for item in second['items']] == list(reversed(ids))[3:]


async def test_prev_cursor_returns_the_previous_page(client: httpx.AsyncClient, world: World, db: Database) -> None:
    for minute in range(7):
        await _publish(client, world, db, minute)
    first = (await client.get('/api/articles', params={'per_page': 3})).json()
    assert first['prev_cursor'] is None
    second = (await client.get('/api/articles', params={'per_page': 3, 'cursor': first['next_cursor']})).json()
    back = (await client.get('/api/articles', params={'per_page': 3, 'cursor': second['prev_cursor']})).json()
    assert [item['id'] for item in back['items']] == [item['id'] for item in first['items']]
    assert back['prev_cursor'] is None
    assert back['next_cursor'] is not None


async def test_total_only_when_asked(client: httpx.AsyncClient, world: World, db: Database) -> None:
    for minute in range(3):
        await _publish(client, world, db, minute)
    assert (await client.get('/api/articles', params={'per_page': 2})).json()['total'] is None
    counted = (await client.get('/api/articles', params={'per_page': 2, 'include_total': 'true'})).json()
    assert counted['total'] == 3
    assert counted['per_page'] == 2


async def test_listing_shows_only_what_the_caller_may_read(client: httpx.AsyncClient, world: World, db: Database) -> None:
    public = await _publish(client, world, db, 0)
    members = await _publish(client, world, db, 1, visibility='members')
    anonymous = (await client.get('/api/articles', params={'include_total': 'true'})).json()
    assert [item['id'] for item in anonymous['items']] == [public]
    assert anonymous['total'] == 1
    member = (await client.get('/api/articles', headers=world.other.headers)).json()
    assert [item['id'] for item in member['items']] == [members, public]


@pytest.mark.parametrize('cursor', ['garbage', 'e30', '!!!'])
async def test_malformed_cursor_is_400(client: httpx.AsyncClient, cursor: str) -> None:
    response = await client.get('/api/articles', params={'cursor': cursor})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'invalid_cursor'


async def test_cursor_is_bound_to_its_listing_and_filters(client: httpx.AsyncClient, world: World, db: Database) -> None:
    for minute in range(3):
        await _publish(client, world, db, minute, tags=[])
    cursor = (await client.get('/api/articles', params={'per_page': 1})).json()['next_cursor']
    other_filter = await client.get('/api/articles', params={'per_page': 1, 'latest_only': 'true', 'cursor': cursor})
    assert other_filter.status_code == 400
    other_listing = await client.get('/api/series/serial/articles', params={'per_page': 1, 'cursor': cursor})
    assert other_listing.status_code == 400


async def test_tag_timeline_pages_oldest_first(client: httpx.AsyncClient, world: World, db: Database) -> None:
    await db.execute("INSERT INTO tags (slug, name) VALUES ('topic', 'Topic')")
    ids = [await _publish(client, world, db, minute, tags=['topic']) for minute in range(5)]
    pages = await _walk(client, '/api/tags/topic/timeline', per_page=2)
    assert [len(page) for page in pages] == [2, 2, 1]
    assert [article_id for page in pages for article_id in page] == ids


async def test_series_articles_go_in_installment_order(client: httpx.AsyncClient, world: World, db: Database) -> None:
    second = await _publish(client, world, db, 0, series_id=world.series_id, series_position=2)
    unnumbered_old = await _publish(client, world, db, 1, series_id=world.series_id)
    first = await _publish(client, world, db, 2, series_id=world.series_id, series_position=1)
    unnumbered_new = await _publish(client, world, db, 3, series_id=world.series_id)
    pages = await _walk(client, '/api/series/serial/articles', per_page=1)
    assert [article_id for page in pages for article_id in page] == [first, second, unnumbered_old, unnumbered_new]


async def test_search_pages_by_position(client: httpx.AsyncClient, world: World, db: Database) -> None:
    ids = {await _publish(client, world, db, minute, body_md='zebra crossing') for minute in range(5)}
    pages = await _walk(client, '/api/search', q='zebra', per_page=2)
    assert [len(page) for page in pages] == [2, 2, 1]
    assert {article_id for page in pages for article_id in page} == ids
    counted = (await client.get('/api/search', params={'q': 'zebra', 'include_total': 'true'})).json()
    assert counted['total'] == 5


async def test_comments_page_oldest_first(client: httpx.AsyncClient, world: World, db: Database) -> None:
    article_id = await _publish(client, world, db, 0, slug='discussed')
    post_id = await db.execute_insert('INSERT INTO article_discord_posts (article_id, channel_id, message_id, thread_id) VALUES (%s, 1, 2, 3)', article_id)
    comment_ids = [
        str(await db.execute_insert(
            "INSERT INTO article_comments (post_id, article_id, message_id, author_discord_user_id, body, posted_at) VALUES (%s, %s, %s, 9, 'hi', '2026-01-01 00:00:00' + INTERVAL %s MINUTE)",
            post_id,
            article_id,
            100 + minute,
            minute,
        ))
        for minute in range(5)
    ]
    first = (await client.get('/api/articles/discussed/comments', params={'per_page': 2, 'include_total': 'true'})).json()
    assert first['total'] == 5
    assert first['thread_url'] is not None
    pages = await _walk(client, '/api/articles/discussed/comments', per_page=2)
    assert [comment_id for page in pages for comment_id in page] == comment_ids


async def test_revisions_page_newest_first(client: httpx.AsyncClient, world: World, db: Database) -> None:
    article_id = await _publish(client, world, db, 0, slug='edited')
    for number in range(2, 5):
        await client.patch(f'/api/articles/{article_id}', json={'title': f'Edit {number}'}, headers=world.author.headers)
    response = await client.get('/api/articles/edited/revisions', params={'per_page': 3})
    data = response.json()
    assert [item['revision_number'] for item in data['items']] == [4, 3, 2]
    rest = (await client.get('/api/articles/edited/revisions', params={'per_page': 3, 'cursor': data['next_cursor']})).json()
    assert [item['revision_number'] for item in rest['items']] == [1]
    assert rest['next_cursor'] is None


@pytest.mark.parametrize('path', ['/api/categories', '/api/tags', '/api/publishers', '/api/series', '/api/rankings/publishers', '/api/rankings/articles'])
async def test_collections_are_wrapped_in_items(client: httpx.AsyncClient, world: World, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert isinstance(response.json(), dict)
    assert isinstance(response.json()['items'], list)


async def test_detail_pages_leave_their_articles_to_the_listings(client: httpx.AsyncClient, world: World, db: Database) -> None:
    await db.execute("INSERT INTO publishers (slug, name) VALUES ('press', 'Press')")
    publisher = (await client.get('/api/publishers/press')).json()
    series = (await client.get('/api/series/serial')).json()
    assert 'articles' not in publisher
    assert 'articles' not in series
