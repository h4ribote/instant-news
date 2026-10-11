"""Fixtures for the integration tests.

The tests drive the real FastAPI app in-process through httpx against the MySQL of docker-compose.test.yml. Each test starts from emptied tables; the settings table is kept, since the app seeds it at startup from tests/config.test.yaml.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from common.config import load_config
from common.db import Database
from server.app.principals import hash_secret
from server.main import create_app

# Every table except settings, emptied before each test.
_TABLES = (
    'article_slug_history',
    'article_subscription_notices',
    'article_comments',
    'article_discord_posts',
    'article_media',
    'article_revisions',
    'article_visibility_roles',
    'article_tags',
    'articles',
    'series',
    'tag_subscriptions',
    'tags',
    'categories',
    'publishers',
    'media',
    'api_token_scopes',
    'api_tokens',
    'oauth_states',
    'sessions',
    'user_roles',
    'discord_roles',
    'users',
)

_discord_ids = count(200000000000000000)


@dataclass(frozen=True)
class Actor:
    """A user and the Authorization header of an API key they own. An anonymous actor has no user and sends no header."""

    user_id: int | None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class World:
    """The fixtures every article test needs."""

    author: Actor
    other: Actor
    admin: Actor
    anonymous: Actor
    category_id: str
    series_id: str


@pytest.fixture
def anyio_backend() -> str:
    return 'asyncio'


async def _reset(db: Database) -> None:
    async with db.acquire() as conn:
        await db.execute('SET FOREIGN_KEY_CHECKS = 0', conn=conn)
        try:
            for table in _TABLES:
                await db.execute(f'TRUNCATE TABLE {table}', conn=conn)
        finally:
            await db.execute('SET FOREIGN_KEY_CHECKS = 1', conn=conn)


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app(load_config())
    async with application.router.lifespan_context(application):
        await _reset(application.state.db)
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as http:
        yield http


@pytest.fixture
def db(app: FastAPI) -> Database:
    return app.state.db


async def _make_user(db: Database, username: str, *, owner: bool = False) -> int:
    return await db.execute_insert(
        'INSERT INTO users (discord_user_id, username, is_guild_member, is_guild_owner) VALUES (%s, %s, 1, %s)',
        next(_discord_ids),
        username,
        1 if owner else 0,
    )


async def _make_key(db: Database, user_id: int, scope: str) -> dict[str, str]:
    raw = 'ink_' + secrets.token_hex(24)
    token_id = await db.execute_insert(
        'INSERT INTO api_tokens (user_id, token_hash, token_prefix, name) VALUES (%s, %s, %s, %s)',
        user_id,
        hash_secret(raw),
        raw[:12],
        'test',
    )
    await db.execute('INSERT INTO api_token_scopes (token_id, scope) VALUES (%s, %s)', token_id, scope)
    return {'Authorization': f'Bearer {raw}'}


@pytest.fixture
async def world(db: Database) -> World:
    author_id = await _make_user(db, 'author')
    other_id = await _make_user(db, 'other')
    # The guild owner counts as an admin, which articles:write:any requires of the key's owner.
    admin_id = await _make_user(db, 'admin', owner=True)
    category_id = await db.execute_insert("INSERT INTO categories (slug, name) VALUES ('news', 'News')")
    series_id = await db.execute_insert("INSERT INTO series (slug, title, owner_id) VALUES ('serial', 'Serial', %s)", author_id)
    return World(
        author=Actor(author_id, await _make_key(db, author_id, 'articles:write')),
        other=Actor(other_id, await _make_key(db, other_id, 'articles:write')),
        admin=Actor(admin_id, await _make_key(db, admin_id, 'articles:write:any')),
        anonymous=Actor(None),
        category_id=str(category_id),
        series_id=str(series_id),
    )


CreateArticle = Callable[..., Awaitable[httpx.Response]]


@pytest.fixture
def create_article(client: httpx.AsyncClient, world: World) -> CreateArticle:
    """POST an article as the author (or as the given actor) and assert it was created."""

    async def _create(actor: Actor | None = None, **fields: Any) -> httpx.Response:
        body: dict[str, Any] = {'title': 'Headline', 'body_md': 'Body text.', 'category_id': world.category_id}
        body.update(fields)
        response = await client.post('/api/articles', json=body, headers=(actor or world.author).headers)
        assert response.status_code == 201, response.text
        return response

    return _create
