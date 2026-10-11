"""The write contract of articles: what creation requires, and how a PATCH tells an omitted field from an explicit null."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from conftest import CreateArticle, World

pytestmark = pytest.mark.anyio

# Fields whose column cannot be NULL. A PATCH may omit them but never null them.
NON_NULLABLE = ('title', 'body_md', 'category_id', 'tags', 'visibility', 'visibility_role_ids', 'byline_mode', 'is_breaking', 'is_sponsored', 'slug', 'status', 'author_id')
# Fields whose column can be NULL. null in a PATCH erases the value.
NULLABLE = ('excerpt', 'publisher_id', 'series_id', 'series_position', 'cover_image_url', 'sponsor_name', 'scheduled_at')


def _schema(app: FastAPI, name: str) -> dict:
    return app.openapi()['components']['schemas'][name]


async def test_openapi_states_required_and_non_nullable_fields(app: FastAPI) -> None:
    create = _schema(app, 'ArticleCreate')
    assert sorted(create['required']) == ['body_md', 'category_id', 'title']

    update = _schema(app, 'ArticleUpdate')
    assert 'required' not in update
    for name in NON_NULLABLE:
        prop = update['properties'][name]
        assert 'default' not in prop, name
        assert all(option.get('type') != 'null' for option in prop.get('anyOf', [])), name
    for name in NULLABLE:
        assert any(option.get('type') == 'null' for option in update['properties'][name]['anyOf']), name


@pytest.mark.parametrize('missing', ['title', 'body_md', 'category_id'])
async def test_create_without_a_required_field_is_422(client: httpx.AsyncClient, world: World, missing: str) -> None:
    body = {'title': 'Headline', 'body_md': 'Body.', 'category_id': world.category_id}
    del body[missing]
    response = await client.post('/api/articles', json=body, headers=world.author.headers)
    assert response.status_code == 422


async def test_create_with_null_for_a_defaulted_field_is_422(client: httpx.AsyncClient, world: World) -> None:
    body = {'title': 'Headline', 'body_md': 'Body.', 'category_id': world.category_id, 'status': None}
    response = await client.post('/api/articles', json=body, headers=world.author.headers)
    assert response.status_code == 422


async def test_create_applies_the_column_defaults(create_article: CreateArticle) -> None:
    article = (await create_article()).json()['article']
    assert article['status'] == 'draft'
    assert article['visibility'] == 'public'
    assert article['byline_mode'] == 'author'
    assert article['tags'] == []
    assert article['is_breaking'] is False
    assert article['version'] == 1


async def test_patch_keeps_omitted_fields(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = (
        await create_article(excerpt='Lede.', series_id=world.series_id, series_position=3, scheduled_at='2030-01-01T00:00:00Z', body_md='Original body.')
    ).json()['article']

    response = await client.patch(f'/api/articles/{created["id"]}', json={'title': 'New headline'}, headers=world.author.headers)
    assert response.status_code == 200, response.text
    article = response.json()['article']
    assert article['title'] == 'New headline'
    assert article['excerpt'] == 'Lede.'
    assert article['series']['id'] == world.series_id
    assert article['series_position'] == 3
    assert article['scheduled_at'] == '2030-01-01T00:00:00Z'
    assert article['body_md'] == 'Original body.'


async def test_patch_null_erases_nullable_fields(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = (
        await create_article(excerpt='Lede.', series_id=world.series_id, series_position=3, scheduled_at='2030-01-01T00:00:00Z', cover_image_url='https://example.com/c.png')
    ).json()['article']

    response = await client.patch(
        f'/api/articles/{created["id"]}',
        json={'excerpt': None, 'series_id': None, 'series_position': None, 'scheduled_at': None, 'cover_image_url': None},
        headers=world.author.headers,
    )
    assert response.status_code == 200, response.text
    article = response.json()['article']
    assert article['excerpt'] is None
    assert article['series'] is None
    assert article['series_position'] is None
    assert article['scheduled_at'] is None
    assert article['cover_image_url'] is None


@pytest.mark.parametrize('name', NON_NULLABLE)
async def test_patch_null_on_a_non_nullable_field_is_422(client: httpx.AsyncClient, world: World, create_article: CreateArticle, name: str) -> None:
    created = (await create_article()).json()['article']

    response = await client.patch(f'/api/articles/{created["id"]}', json={name: None}, headers=world.author.headers)
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'validation_error'

    after = (await client.get(f'/api/articles/by-id/{created["id"]}', headers=world.author.headers)).json()
    assert after['version'] == created['version']


async def test_patch_empty_list_clears_tags(client: httpx.AsyncClient, world: World, db, create_article: CreateArticle) -> None:
    await db.execute("INSERT INTO tags (slug, name) VALUES ('economy', 'Economy')")
    created = (await create_article(tags=['economy'])).json()['article']
    assert [tag['slug'] for tag in created['tags']] == ['economy']

    kept = await client.patch(f'/api/articles/{created["id"]}', json={'title': 'Retitled'}, headers=world.author.headers)
    assert [tag['slug'] for tag in kept.json()['article']['tags']] == ['economy']

    cleared = await client.patch(f'/api/articles/{created["id"]}', json={'tags': []}, headers=world.author.headers)
    assert cleared.json()['article']['tags'] == []


async def test_unknown_field_is_422(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = (await create_article()).json()['article']
    response = await client.patch(f'/api/articles/{created["id"]}', json={'titel': 'Typo'}, headers=world.author.headers)
    assert response.status_code == 422
