"""The rate limit standing reported on article writes made with an API key."""

from __future__ import annotations

import httpx
import pytest

from conftest import CreateArticle, SetRateLimit, World

pytestmark = pytest.mark.anyio

_HEADERS = ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset', 'RateLimit-Policy', 'RateLimit')


def _standing(response: httpx.Response) -> tuple[int, int, int]:
    limit = int(response.headers['X-RateLimit-Limit'])
    remaining = int(response.headers['X-RateLimit-Remaining'])
    reset = int(response.headers['X-RateLimit-Reset'])
    assert response.headers['RateLimit-Policy'] == f'"articles-write";q={limit};w=3600'
    assert response.headers['RateLimit'] == f'"articles-write";r={remaining};t={reset}'
    return limit, remaining, reset


async def test_saves_report_the_remaining_budget(client: httpx.AsyncClient, world: World, set_rate_limit: SetRateLimit) -> None:
    await set_rate_limit(5)
    created = await client.post('/api/articles', json={'title': 'One', 'body_md': 'x', 'category_id': world.category_id}, headers=world.author.headers)
    assert created.status_code == 201
    limit, remaining, reset = _standing(created)
    assert (limit, remaining) == (5, 4)
    assert 0 < reset <= 3600

    article_id = created.json()['article']['id']
    patched = await client.patch(f'/api/articles/{article_id}', json={'title': 'Two'}, headers=world.author.headers)
    assert _standing(patched)[1] == 3


async def test_429_reports_zero_remaining(client: httpx.AsyncClient, world: World, set_rate_limit: SetRateLimit) -> None:
    await set_rate_limit(1)
    body = {'title': 'Only one', 'body_md': 'x', 'category_id': world.category_id}
    assert (await client.post('/api/articles', json=body, headers=world.author.headers)).status_code == 201
    refused = await client.post('/api/articles', json=body, headers=world.author.headers)
    assert refused.status_code == 429
    limit, remaining, reset = _standing(refused)
    assert (limit, remaining) == (1, 0)
    assert int(refused.headers['Retry-After']) >= 1
    assert reset >= 1


async def test_error_responses_carry_the_standing(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = await create_article()
    article_id = created.json()['article']['id']
    await client.patch(f'/api/articles/{article_id}', json={'title': 'Moved on'}, headers=world.author.headers)

    stale = await client.patch(f'/api/articles/{article_id}', json={'title': 'Mine'}, headers={**world.author.headers, 'If-Match': created.headers['ETag']})
    assert stale.status_code == 412
    assert all(name in stale.headers for name in _HEADERS)

    invalid = await client.post('/api/articles', json={'title': 'No body'}, headers=world.author.headers)
    assert invalid.status_code == 422
    assert all(name in invalid.headers for name in _HEADERS)


async def test_each_key_reports_its_own_budget(client: httpx.AsyncClient, world: World, set_rate_limit: SetRateLimit) -> None:
    await set_rate_limit(10)
    body = {'title': 'Mine', 'body_md': 'x', 'category_id': world.category_id}
    await client.post('/api/articles', json=body, headers=world.author.headers)
    theirs = await client.post('/api/articles', json=body, headers=world.other.headers)
    assert _standing(theirs)[1] == 9


async def test_session_writes_carry_no_headers(client: httpx.AsyncClient, world: World, author_session: dict[str, str]) -> None:
    response = await client.post('/api/articles', json={'title': 'By hand', 'body_md': 'x', 'category_id': world.category_id}, headers=author_session)
    assert response.status_code == 201, response.text
    assert not any(name in response.headers for name in _HEADERS)


async def test_reads_carry_no_headers(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = (await create_article()).json()['article']['id']
    response = await client.get(f'/api/articles/by-id/{article_id}', headers=world.author.headers)
    assert not any(name in response.headers for name in _HEADERS)
