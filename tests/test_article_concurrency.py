"""Optimistic concurrency on articles: the ETag every article response carries, and If-Match on PATCH and publish."""

from __future__ import annotations

import httpx
import pytest

from conftest import CreateArticle, World

pytestmark = pytest.mark.anyio


def _etag(article_id: str, version: int) -> str:
    return f'"{article_id}-{version}"'


async def _get(client: httpx.AsyncClient, world: World, article_id: str) -> dict:
    response = await client.get(f'/api/articles/by-id/{article_id}', headers=world.author.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_responses_carry_the_etag(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = await create_article(slug='etag-article')
    article = created.json()['article']
    assert created.headers['ETag'] == _etag(article['id'], 1)

    by_slug = await client.get('/api/articles/etag-article', headers=world.author.headers)
    assert by_slug.headers['ETag'] == _etag(article['id'], 1)
    by_id = await client.get(f'/api/articles/by-id/{article["id"]}', headers=world.author.headers)
    assert by_id.headers['ETag'] == _etag(article['id'], 1)

    patched = await client.patch(f'/api/articles/{article["id"]}', json={'title': 'Two'}, headers=world.author.headers)
    assert patched.headers['ETag'] == _etag(article['id'], 2)
    assert patched.json()['article']['version'] == 2


async def test_matching_if_match_saves(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = await create_article()
    article_id = created.json()['article']['id']

    response = await client.patch(
        f'/api/articles/{article_id}',
        json={'title': 'Updated'},
        headers={**world.author.headers, 'If-Match': created.headers['ETag']},
    )
    assert response.status_code == 200, response.text
    assert response.json()['article']['title'] == 'Updated'


async def test_stale_if_match_is_412_and_changes_nothing(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = await create_article(title='Original')
    stale = created.headers['ETag']
    article_id = created.json()['article']['id']
    # Someone else's edit lands first.
    await client.patch(f'/api/articles/{article_id}', json={'body_md': 'Their edit.'}, headers=world.author.headers)

    response = await client.patch(
        f'/api/articles/{article_id}',
        json={'title': 'Mine'},
        headers={**world.author.headers, 'If-Match': stale},
    )
    assert response.status_code == 412
    error = response.json()['error']
    assert error['code'] == 'precondition_failed'
    assert error['details']['etag'] == _etag(article_id, 2)

    current = await _get(client, world, article_id)
    assert current['title'] == 'Original'
    assert current['body_md'] == 'Their edit.'
    assert current['version'] == 2


@pytest.mark.parametrize(
    ('if_match', 'expected'),
    [
        ('*', 200),
        ('W/"{id}-1"', 412),
        ('"{id}-1"', 200),
        ('"{id}-9", "{id}-1"', 200),
        ('"{id}-9"', 412),
        ('"999999-1"', 412),
        ('not-an-etag', 412),
    ],
)
async def test_if_match_forms(client: httpx.AsyncClient, world: World, create_article: CreateArticle, if_match: str, expected: int) -> None:
    article_id = (await create_article()).json()['article']['id']
    response = await client.patch(
        f'/api/articles/{article_id}',
        json={'title': 'Changed'},
        headers={**world.author.headers, 'If-Match': if_match.replace('{id}', article_id)},
    )
    assert response.status_code == expected, response.text


async def test_without_if_match_the_save_is_unconditional(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = (await create_article()).json()['article']['id']
    await client.patch(f'/api/articles/{article_id}', json={'title': 'First'}, headers=world.author.headers)
    response = await client.patch(f'/api/articles/{article_id}', json={'title': 'Second'}, headers=world.author.headers)
    assert response.status_code == 200
    assert response.json()['article']['version'] == 3


async def test_publish_honours_if_match(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = await create_article()
    stale = created.headers['ETag']
    article_id = created.json()['article']['id']
    edited = await client.patch(f'/api/articles/{article_id}', json={'title': 'Edited'}, headers=world.author.headers)

    rejected = await client.post(f'/api/articles/{article_id}/publish', headers={**world.author.headers, 'If-Match': stale})
    assert rejected.status_code == 412
    assert (await _get(client, world, article_id))['status'] == 'draft'

    published = await client.post(f'/api/articles/{article_id}/publish', headers={**world.author.headers, 'If-Match': edited.headers['ETag']})
    assert published.status_code == 200, published.text
    assert published.json()['status'] == 'published'
    assert published.json()['version'] == 3
    assert published.headers['ETag'] == _etag(article_id, 3)


async def test_version_moves_only_when_the_article_changes(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = (await create_article(slug='viewed')).json()['article']
    article_id = created['id']
    await client.post(f'/api/articles/{article_id}/publish', headers=world.author.headers)
    assert (await _get(client, world, article_id))['version'] == 2

    # Publishing an article that is already published changes nothing.
    again = await client.post(f'/api/articles/{article_id}/publish', headers=world.author.headers)
    assert again.json()['version'] == 2

    # A view counts, but is not an edit.
    first = await client.get('/api/articles/viewed')
    second = await client.get('/api/articles/viewed')
    assert second.json()['view_count'] == first.json()['view_count'] + 1
    assert second.json()['version'] == 2
    assert second.headers['ETag'] == _etag(article_id, 2)


async def test_archived_article_republish_moves_the_version(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = (await create_article()).json()['article']['id']
    await client.post(f'/api/articles/{article_id}/publish', headers=world.author.headers)
    archived = await client.patch(f'/api/articles/{article_id}', json={'status': 'archived'}, headers=world.author.headers)
    assert archived.json()['article']['version'] == 3

    restored = await client.post(f'/api/articles/{article_id}/publish', headers={**world.author.headers, 'If-Match': archived.headers['ETag']})
    assert restored.status_code == 200
    assert restored.json()['status'] == 'published'
    assert restored.json()['version'] == 4


async def test_permission_is_decided_before_the_precondition(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    article_id = (await create_article()).json()['article']['id']
    stale = _etag(article_id, 99)

    patch = await client.patch(f'/api/articles/{article_id}', json={'title': 'Hijack'}, headers={**world.other.headers, 'If-Match': stale})
    assert patch.status_code == 403
    publish = await client.post(f'/api/articles/{article_id}/publish', headers={**world.other.headers, 'If-Match': stale})
    assert publish.status_code == 403
    assert 'etag' not in patch.text


async def test_save_interrupted_by_another_save_is_refused(
    client: httpx.AsyncClient, world: World, create_article: CreateArticle, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.app.articles as service

    created = await create_article(title='Original', body_md='Original body.')
    article_id = created.json()['article']['id']
    ingest = service.ingest_article_images

    # Another save commits while this one is ingesting images, after it read the row and before it locks it.
    async def interrupted(*args, **kwargs):
        await db.execute('UPDATE articles SET title = %s, version = version + 1 WHERE id = %s', 'Concurrent', article_id)
        return await ingest(*args, **kwargs)

    monkeypatch.setattr(service, 'ingest_article_images', interrupted)

    unconditional = await client.patch(f'/api/articles/{article_id}', json={'body_md': 'Mine.'}, headers=world.author.headers)
    assert unconditional.status_code == 409
    current = await _get(client, world, article_id)
    assert current['title'] == 'Concurrent'
    assert current['body_md'] == 'Original body.'

    conditional = await client.patch(f'/api/articles/{article_id}', json={'body_md': 'Mine.'}, headers={**world.author.headers, 'If-Match': _etag(article_id, current['version'])})
    assert conditional.status_code == 412
    assert conditional.json()['error']['details']['etag'] == _etag(article_id, current['version'] + 1)


async def test_admin_with_write_any_is_held_to_if_match_too(client: httpx.AsyncClient, world: World, create_article: CreateArticle) -> None:
    created = await create_article()
    article_id = created.json()['article']['id']
    await client.patch(f'/api/articles/{article_id}', json={'title': 'Author edit'}, headers=world.author.headers)

    response = await client.patch(f'/api/articles/{article_id}', json={'title': 'Admin edit'}, headers={**world.admin.headers, 'If-Match': created.headers['ETag']})
    assert response.status_code == 412
