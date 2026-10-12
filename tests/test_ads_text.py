"""Text ads: the promo slot takes one line of body_text, an image slot takes image_url, and the two never mix."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from common.db import Database
from conftest import MakeKey, World

pytestmark = pytest.mark.anyio


async def _ads_admin(world: World, make_key: MakeKey) -> dict[str, str]:
    assert world.admin.user_id is not None
    return await make_key(world.admin.user_id, 'ads:admin')


async def _slot_id(db: Database, slug: str) -> str:
    return str(await db.fetch_val('SELECT id FROM ad_slots WHERE slug = %s', slug))


async def _public_media_url(db: Database, user_id: int) -> str:
    content_hash = 'b' * 64
    await db.execute(
        'INSERT INTO media (content_hash, width, height, byte_size, is_public, uploaded_by) VALUES (%s, 1, 1, 1, 1, %s)',
        content_hash,
        user_id,
    )
    return f'/media/{content_hash}/orig.webp'


def _text_ad(slot_id: str, **fields: Any) -> dict[str, Any]:
    return {'slot_id': slot_id, 'advertiser_name': 'Sponsor', 'body_text': 'Try our product', 'link_url': 'https://example.com/', **fields}


async def test_promo_slot_is_listed_as_a_text_slot(client: httpx.AsyncClient, world: World, make_key: MakeKey) -> None:
    response = await client.get('/api/admin/ad-slots', headers=await _ads_admin(world, make_key))
    slots = {slot['slug']: slot for slot in response.json()['items']}
    assert slots['promo']['format'] == 'text'
    assert slots['promo']['max_text_length'] == 80
    assert slots['promo']['recommended_width'] is None
    assert slots['sidebar']['format'] == 'image'
    assert slots['sidebar']['max_text_length'] is None


async def test_text_ad_is_submitted_and_served(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database) -> None:
    created = await client.post('/api/admin/ads', json=_text_ad(await _slot_id(db, 'promo')), headers=await _ads_admin(world, make_key))
    assert created.status_code == 201, created.text
    ad = created.json()
    assert ad['body_text'] == 'Try our product'
    assert ad['image_url'] is None
    assert ad['slot']['format'] == 'text'

    served = (await client.get('/api/ads/serve', params={'slot': 'promo'})).json()['ad']
    assert served['format'] == 'text'
    assert served['body_text'] == 'Try our product'
    assert served['image_url'] is None
    assert served['click_url'] == f'/ads/{ad["id"]}/click'
    assert int(await db.fetch_val('SELECT impressions FROM ad_stats_daily WHERE ad_id = %s', int(ad['id']))) == 1


async def test_empty_promo_slot_serves_null(client: httpx.AsyncClient) -> None:
    response = await client.get('/api/ads/serve', params={'slot': 'promo'})
    assert response.status_code == 200
    assert response.json() == {'ad': None}


@pytest.mark.parametrize(
    'fields',
    [
        {'body_text': None},
        {'body_text': 'two\nlines'},
        {'body_text': '   '},
        {'body_text': 'x' * 81},
        {'alt_text': 'alt'},
    ],
)
async def test_bad_text_creative_is_400(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database, fields: dict[str, Any]) -> None:
    body = {key: value for key, value in _text_ad(await _slot_id(db, 'promo'), **fields).items() if value is not None}
    response = await client.post('/api/admin/ads', json=body, headers=await _ads_admin(world, make_key))
    assert response.status_code == 400, response.text
    assert int(await db.fetch_val('SELECT COUNT(*) FROM ads')) == 0


async def test_image_url_in_a_text_slot_is_400(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database) -> None:
    assert world.admin.user_id is not None
    body = _text_ad(await _slot_id(db, 'promo'), image_url=await _public_media_url(db, world.admin.user_id))
    response = await client.post('/api/admin/ads', json=body, headers=await _ads_admin(world, make_key))
    assert response.status_code == 400, response.text


async def test_body_text_in_an_image_slot_is_400(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database) -> None:
    assert world.admin.user_id is not None
    body = _text_ad(await _slot_id(db, 'sidebar'), image_url=await _public_media_url(db, world.admin.user_id))
    response = await client.post('/api/admin/ads', json=body, headers=await _ads_admin(world, make_key))
    assert response.status_code == 400, response.text


async def test_image_slot_still_requires_an_image(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database) -> None:
    body = {'slot_id': await _slot_id(db, 'sidebar'), 'advertiser_name': 'Sponsor', 'link_url': 'https://example.com/'}
    response = await client.post('/api/admin/ads', json=body, headers=await _ads_admin(world, make_key))
    assert response.status_code == 400, response.text


async def test_moving_across_formats_requires_swapping_the_creative(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database) -> None:
    assert world.admin.user_id is not None
    headers = await _ads_admin(world, make_key)
    ad_id = (await client.post('/api/admin/ads', json=_text_ad(await _slot_id(db, 'promo')), headers=headers)).json()['id']
    sidebar = await _slot_id(db, 'sidebar')

    kept_text = await client.patch(f'/api/admin/ads/{ad_id}', json={'slot_id': sidebar}, headers=headers)
    assert kept_text.status_code == 400, kept_text.text

    image_url = await _public_media_url(db, world.admin.user_id)
    swapped = await client.patch(f'/api/admin/ads/{ad_id}', json={'slot_id': sidebar, 'image_url': image_url, 'body_text': None}, headers=headers)
    assert swapped.status_code == 200, swapped.text
    assert swapped.json()['slot']['slug'] == 'sidebar'
    assert swapped.json()['image_url'] == image_url
    assert swapped.json()['body_text'] is None


async def test_text_ad_body_can_be_edited(client: httpx.AsyncClient, world: World, make_key: MakeKey, db: Database) -> None:
    headers = await _ads_admin(world, make_key)
    ad_id = (await client.post('/api/admin/ads', json=_text_ad(await _slot_id(db, 'promo')), headers=headers)).json()['id']
    edited = await client.patch(f'/api/admin/ads/{ad_id}', json={'body_text': 'New copy'}, headers=headers)
    assert edited.status_code == 200, edited.text
    assert edited.json()['body_text'] == 'New copy'
    dropped = await client.patch(f'/api/admin/ads/{ad_id}', json={'body_text': None}, headers=headers)
    assert dropped.status_code == 400, dropped.text
