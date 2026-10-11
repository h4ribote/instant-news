"""Article identifiers: lookup by id, and the redirect from a slug the article used to have."""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest

from conftest import CreateArticle, World

pytestmark = pytest.mark.anyio


async def _rename(client: httpx.AsyncClient, world: World, article_id: str, slug: str) -> httpx.Response:
    response = await client.patch(f'/api/articles/{article_id}', json={'slug': slug}, headers=world.author.headers)
    assert response.status_code == 200, response.text
    return response


async def _published(client: httpx.AsyncClient, world: World, create_article: CreateArticle, slug: str) -> str:
    article_id = (await create_article(slug=slug)).json()['article']['id']
    response = await client.post(f'/api/articles/{article_id}/publish', headers=world.author.headers)
    assert response.status_code == 200, response.text
    return article_id


async def test_by_id_returns_the_article_without_counting_a_view(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = await _published(client, world, create_article, 'by-id-target')

    by_slug = (await client.get('/api/articles/by-id-target')).json()
    by_id = await client.get(f'/api/articles/by-id/{article_id}')
    assert by_id.status_code == 200
    assert by_id.json()['id'] == article_id == by_slug['id']
    assert by_id.json()['slug'] == 'by-id-target'

    again = await client.get(f'/api/articles/by-id/{article_id}')
    assert again.json()['view_count'] == by_slug['view_count']


async def test_by_id_hides_what_the_viewer_cannot_see(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    draft_id = (await create_article()).json()['article']['id']
    assert (await client.get(f'/api/articles/by-id/{draft_id}')).status_code == 404
    assert (await client.get(f'/api/articles/by-id/{draft_id}', headers=world.other.headers)).status_code == 404
    assert (await client.get(f'/api/articles/by-id/{draft_id}', headers=world.author.headers)).status_code == 200
    assert (await client.get('/api/articles/by-id/not-an-id')).status_code == 404
    assert (await client.get('/api/articles/by-id/999999')).status_code == 404


async def test_slug_is_kept_unless_given(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = (await create_article(title='Stable permalink')).json()['article']
    retitled = await client.patch(f'/api/articles/{created["id"]}', json={'title': 'A different headline'}, headers=world.author.headers)
    assert retitled.json()['article']['slug'] == created['slug']


@pytest.mark.parametrize(
    ('old_path', 'new_path'),
    [
        ('/api/articles/old-slug', '/api/articles/new-slug'),
        ('/api/articles/old-slug/comments?page=1&per_page=10', '/api/articles/new-slug/comments?page=1&per_page=10'),
        ('/api/articles/old-slug/revisions', '/api/articles/new-slug/revisions'),
        ('/api/articles/old-slug/revisions/1', '/api/articles/new-slug/revisions/1'),
        ('/api/articles/old-slug/revisions/1/diff', '/api/articles/new-slug/revisions/1/diff'),
        ('/articles/old-slug', '/articles/new-slug'),
    ],
)
async def test_old_slug_redirects_to_the_current_one(client: httpx.AsyncClient, world: World, create_article: CreateArticle, old_path: str, new_path: str) -> None:
    article_id = await _published(client, world, create_article, 'old-slug')
    await _rename(client, world, article_id, 'new-slug')

    response = await client.get(old_path)
    assert response.status_code == 301
    assert response.headers['Location'] == new_path
    assert response.headers['Cache-Control'] == 'no-cache'
    assert (await client.get(new_path)).status_code == 200


async def test_redirect_percent_encodes_the_new_slug(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = await _published(client, world, create_article, 'ascii-slug')
    await _rename(client, world, article_id, '日本語の記事')

    response = await client.get('/api/articles/ascii-slug')
    assert response.status_code == 301
    assert response.headers['Location'] == '/api/articles/' + quote('日本語の記事', safe='')


async def test_old_slug_of_an_invisible_article_is_404(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    draft_id = (await create_article(slug='draft-old')).json()['article']['id']
    await _rename(client, world, draft_id, 'draft-new')

    assert (await client.get('/api/articles/draft-old')).status_code == 404
    assert (await client.get('/articles/draft-old')).status_code == 404
    assert (await client.get('/api/articles/draft-old', headers=world.other.headers)).status_code == 404
    assert (await client.get('/api/articles/draft-old', headers=world.author.headers)).status_code == 301


async def test_another_article_cannot_take_a_former_slug(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = await _published(client, world, create_article, 'taken-once')
    await _rename(client, world, article_id, 'taken-now')

    body = {'title': 'Other', 'body_md': 'Body.', 'category_id': world.category_id, 'slug': 'taken-once'}
    assert (await client.post('/api/articles', json=body, headers=world.author.headers)).status_code == 409

    other_id = (await create_article(slug='innocent')).json()['article']['id']
    rename = await client.patch(f'/api/articles/{other_id}', json={'slug': 'taken-once'}, headers=world.author.headers)
    assert rename.status_code == 409


async def test_generated_slug_avoids_former_slugs(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    first = (await create_article(title='Hello')).json()['article']
    assert first['slug'] == 'hello'
    await _rename(client, world, first['id'], 'hello-renamed')

    second = (await create_article(title='Hello')).json()['article']
    assert second['slug'] == 'hello-2'


async def test_an_article_can_take_back_its_former_slug(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = await _published(client, world, create_article, 'first-name')
    await _rename(client, world, article_id, 'second-name')
    await _rename(client, world, article_id, 'first-name')

    assert (await client.get('/api/articles/first-name')).status_code == 200
    back = await client.get('/api/articles/second-name')
    assert back.status_code == 301
    assert back.headers['Location'] == '/api/articles/first-name'


async def test_resending_the_current_slug_is_not_a_rename(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = await _published(client, world, create_article, 'same-slug')
    response = await client.patch(f'/api/articles/{article_id}', json={'slug': 'same-slug', 'title': 'Retitled'}, headers=world.author.headers)
    assert response.status_code == 200
    assert (await client.get('/api/articles/same-slug')).status_code == 200


async def test_by_id_is_reserved(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    body = {'title': 'Reserved', 'body_md': 'Body.', 'category_id': world.category_id, 'slug': 'by-id'}
    response = await client.post('/api/articles', json=body, headers=world.author.headers)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'article_error'

    generated = (await create_article(title='By ID')).json()['article']
    assert generated['slug'] == 'by-id-2'
