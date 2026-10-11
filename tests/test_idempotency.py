"""Idempotency-Key on the POSTs that create a resource: an article, an ad."""

from __future__ import annotations

import anyio
import httpx
import pytest

from common.db import Database
from conftest import MakeKey, World

pytestmark = pytest.mark.anyio


def _article(world: World, **fields: object) -> dict[str, object]:
    return {'title': 'Market close', 'body_md': 'Prices.', 'category_id': world.category_id, **fields}


async def _article_count(db: Database) -> int:
    return int(await db.fetch_val('SELECT COUNT(*) FROM articles'))


async def test_resend_replays_the_first_article(client: httpx.AsyncClient, world: World, db: Database) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'key-1'}
    first = await client.post('/api/articles', json=_article(world), headers=headers)
    assert first.status_code == 201, first.text
    assert 'Idempotent-Replayed' not in first.headers

    again = await client.post('/api/articles', json=_article(world), headers=headers)
    assert again.status_code == 201
    assert again.headers['Idempotent-Replayed'] == 'true'
    assert again.json()['article']['id'] == first.json()['article']['id']
    assert again.headers['ETag'] == first.headers['ETag']
    assert await _article_count(db) == 1


async def test_body_is_compared_after_normalization(client: httpx.AsyncClient, world: World, db: Database) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'key-order', 'Content-Type': 'application/json'}
    body = f'{{"title": "Market close", "body_md": "Prices.", "category_id": "{world.category_id}"}}'
    reordered = f'{{"category_id":"{world.category_id}","body_md":"Prices.","title":"Market close"}}'
    assert (await client.post('/api/articles', content=body, headers=headers)).status_code == 201
    assert (await client.post('/api/articles', content=reordered, headers=headers)).headers.get('Idempotent-Replayed') == 'true'
    assert await _article_count(db) == 1


async def test_same_key_with_another_body_is_refused(client: httpx.AsyncClient, world: World, db: Database) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'key-2'}
    assert (await client.post('/api/articles', json=_article(world), headers=headers)).status_code == 201
    response = await client.post('/api/articles', json=_article(world, title='Another'), headers=headers)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'idempotency_key_reused'
    assert await _article_count(db) == 1


async def test_keys_are_separate_per_key_and_per_user(client: httpx.AsyncClient, world: World, db: Database) -> None:
    assert (await client.post('/api/articles', json=_article(world), headers={**world.author.headers, 'Idempotency-Key': 'a'})).status_code == 201
    assert (await client.post('/api/articles', json=_article(world), headers={**world.author.headers, 'Idempotency-Key': 'b'})).status_code == 201
    other = await client.post('/api/articles', json=_article(world), headers={**world.other.headers, 'Idempotency-Key': 'a'})
    assert other.status_code == 201
    assert 'Idempotent-Replayed' not in other.headers
    assert await _article_count(db) == 3


async def test_without_a_key_every_post_creates(client: httpx.AsyncClient, world: World, db: Database) -> None:
    for _ in range(2):
        assert (await client.post('/api/articles', json=_article(world), headers=world.author.headers)).status_code == 201
    assert await _article_count(db) == 2


@pytest.mark.parametrize('key', ['', 'has space', 'x' * 256, 'tab\tkey'])
async def test_malformed_key_is_400(client: httpx.AsyncClient, world: World, key: str) -> None:
    response = await client.post('/api/articles', json=_article(world), headers={**world.author.headers, 'Idempotency-Key': key})
    assert response.status_code == 400, response.text


async def test_concurrent_resends_create_one_article(client: httpx.AsyncClient, world: World, db: Database) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'race'}
    responses: list[httpx.Response] = []

    async def post() -> None:
        responses.append(await client.post('/api/articles', json=_article(world), headers=headers))

    async with anyio.create_task_group() as group:
        for _ in range(4):
            group.start_soon(post)

    assert [response.status_code for response in responses] == [201] * 4
    assert len({response.json()['article']['id'] for response in responses}) == 1
    assert sum(response.headers.get('Idempotent-Replayed') == 'true' for response in responses) == 3
    assert await _article_count(db) == 1


async def test_replay_does_not_consume_the_rate_limit(client: httpx.AsyncClient, world: World) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'counted-once'}
    first = await client.post('/api/articles', json=_article(world), headers=headers)
    again = await client.post('/api/articles', json=_article(world), headers=headers)
    assert first.headers['X-RateLimit-Remaining'] == again.headers['X-RateLimit-Remaining']


async def test_expired_key_starts_afresh(client: httpx.AsyncClient, world: World, db: Database) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'old'}
    first = await client.post('/api/articles', json=_article(world), headers=headers)
    await db.execute('UPDATE idempotency_keys SET created_at = NOW() - INTERVAL 25 HOUR')

    again = await client.post('/api/articles', json=_article(world, title='Next day'), headers=headers)
    assert again.status_code == 201
    assert 'Idempotent-Replayed' not in again.headers
    assert again.json()['article']['id'] != first.json()['article']['id']
    assert int(await db.fetch_val('SELECT COUNT(*) FROM idempotency_keys')) == 1


async def test_failed_creation_leaves_the_key_free(client: httpx.AsyncClient, world: World, db: Database) -> None:
    headers = {**world.author.headers, 'Idempotency-Key': 'retry-me'}
    failed = await client.post('/api/articles', json=_article(world, tags=['no-such-tag']), headers=headers)
    assert failed.status_code == 400
    assert int(await db.fetch_val('SELECT COUNT(*) FROM idempotency_keys')) == 0


async def _public_media_url(db: Database, user_id: int) -> str:
    content_hash = 'a' * 64
    await db.execute(
        'INSERT INTO media (content_hash, width, height, byte_size, is_public, uploaded_by) VALUES (%s, 1, 1, 1, 1, %s)',
        content_hash,
        user_id,
    )
    return f'/media/{content_hash}/orig.webp'


async def test_ad_submission_is_idempotent(client: httpx.AsyncClient, world: World, db: Database, make_key: MakeKey) -> None:
    assert world.admin.user_id is not None
    headers = {**await make_key(world.admin.user_id, 'ads:admin'), 'Idempotency-Key': 'ad-1'}
    slot_id = str(await db.fetch_val("SELECT id FROM ad_slots WHERE slug = 'sidebar'"))
    body = {
        'slot_id': slot_id,
        'advertiser_name': 'Sponsor',
        'image_url': await _public_media_url(db, world.admin.user_id),
        'link_url': 'https://example.com/',
    }
    first = await client.post('/api/admin/ads', json=body, headers=headers)
    assert first.status_code == 201, first.text
    again = await client.post('/api/admin/ads', json=body, headers=headers)
    assert again.status_code == 201
    assert again.headers['Idempotent-Replayed'] == 'true'
    assert again.json()['id'] == first.json()['id']
    reused = await client.post('/api/admin/ads', json={**body, 'advertiser_name': 'Someone else'}, headers=headers)
    assert reused.status_code == 422
    assert int(await db.fetch_val('SELECT COUNT(*) FROM ads')) == 1
