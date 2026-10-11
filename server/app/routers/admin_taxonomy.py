"""Admin API for categories, tags and publishers (taxonomy:admin).

This router requires exactly one scope: taxonomy:admin. Never bundle the scopes an admin can issue into one — someone trusted to maintain the taxonomy has no reason to also hold the power to forge an article's byline (articles:write:any) or to submit ads (ads:admin).

The taxonomy set differs per deployment, so the core does not fix it; it is edited here. It lives in the DB rather than the config file because articles.category_id references these rows by foreign key, and referential integrity cannot be kept against rows in a config file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Final

from aiomysql import IntegrityError
from fastapi import APIRouter, Depends, status

from common.db import Database
from common.snowflake import to_snowflake

from ..deps import get_db, require_scope
from ..errors import BadRequest, Conflict, NotFound
from ..schemas import CategoryOut, CategoryWrite, ItemList, PublisherOut, PublisherWrite, TagOut, TagWrite

# Imposed on the router rather than declared per route: forgetting the check when adding a route cannot create a route that passes straight through.
router = APIRouter(tags=['admin:taxonomy'], dependencies=[Depends(require_scope('taxonomy:admin'))])

_CATEGORY_SELECT: Final = 'SELECT id, slug, name, position, notify_channel_id, include_in_latest FROM categories'
_TAG_SELECT: Final = 'SELECT id, slug, name, description, icon_url, notify_channel_id FROM tags'
_PUBLISHER_SELECT: Final = 'SELECT id, role_id, slug, name, description, logo_url, stance FROM publishers'

_SLUG_TAKEN: Final = {
    'categories': 'SELECT id FROM categories WHERE slug = %s',
    'tags': 'SELECT id FROM tags WHERE slug = %s',
    'publishers': 'SELECT id FROM publishers WHERE slug = %s',
}
_PUBLISHER_ROLE_TAKEN: Final = 'SELECT id FROM publishers WHERE role_id = %s'


def _parse_id(value: str, label: str) -> int:
    """Parse a path ID into a 64-bit integer. An unparseable value is reported as not found: there is no point distinguishing it from an ID that does not exist."""
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound(f'{label}がありません。') from exc


def _optional_id(value: str | None) -> int | None:
    """Convert a request's ID (a string) into the integer the DB stores. null stays null, and it is a meaningful choice: "this category notifies nowhere", "this publisher has no role attached"."""
    return None if value is None else to_snowflake(value)


@contextmanager
def _conflict_on_integrity(message: str) -> Iterator[None]:
    """Map a constraint violation to a 409.

    Duplicates are checked up front so the message can be clear, but another request slipping in between the check and the write gets past that check. The DB constraint is what guards last, and a violation raised there returns the same shape of response.
    """
    try:
        yield
    except IntegrityError as exc:
        raise Conflict(message) from exc


async def _fetch_or_404(db: Database, select_sql: str, identifier: int, label: str) -> dict[str, Any]:
    row = await db.fetch_one(f'{select_sql} WHERE id = %s', identifier)
    if row is None:
        raise NotFound(f'{label}がありません。')
    return row


async def _ensure_unique(db: Database, sql: str, value: Any, exclude_id: int | None, message: str) -> None:
    """Return 409 when a value that must be unique is already taken. exclude_id spares that row itself, since an update that leaves the value unchanged would otherwise collide with itself."""
    query = sql if exclude_id is None else f'{sql} AND id <> %s'
    args = [value] if exclude_id is None else [value, exclude_id]
    if await db.fetch_val(query, *args) is not None:
        raise Conflict(message)


def _require_on_create(payload: Any, names: tuple[str, ...], label: str) -> None:
    """Check the fields a create requires.

    The *Write models serve both create and partial update, so every field is optional; what a create actually needs can only be expressed here.
    """
    missing = [name for name in names if getattr(payload, name) is None]
    if missing:
        raise BadRequest(f'{label}の作成には次の項目が必要です: {", ".join(missing)}')


def _reject_nulls(payload: Any, names: tuple[str, ...]) -> None:
    """Reject an explicit null aimed at a NOT NULL column. The DB does keep nulls out, but failing there is a constraint-violation 500 that says nothing about what was wrong."""
    for name in names:
        if name in payload.model_fields_set and getattr(payload, name) is None:
            raise BadRequest(f'{name} には null を指定できません。')


# Categories


@router.get('/api/admin/categories')
async def list_categories(db: Annotated[Database, Depends(get_db)]) -> ItemList[CategoryOut]:
    rows = await db.fetch_all(f'{_CATEGORY_SELECT} ORDER BY position, id')
    return ItemList[CategoryOut](items=[CategoryOut.model_validate(row) for row in rows])


@router.post('/api/admin/categories', status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryWrite, db: Annotated[Database, Depends(get_db)]) -> CategoryOut:
    """Create a category. A category whose notify_channel_id is omitted (or null) notifies no channel at all."""
    _require_on_create(payload, ('slug', 'name'), 'カテゴリ')
    await _ensure_unique(db, _SLUG_TAKEN['categories'], payload.slug, None, f'その slug は既に別のカテゴリが使っています: {payload.slug}')
    with _conflict_on_integrity(f'その slug は既に別のカテゴリが使っています: {payload.slug}'):
        category_id = await db.execute_insert(
            'INSERT INTO categories (slug, name, position, notify_channel_id, include_in_latest) VALUES (%s, %s, %s, %s, %s)',
            payload.slug,
            payload.name,
            0 if payload.position is None else payload.position,
            _optional_id(payload.notify_channel_id),
            1 if payload.include_in_latest is None else int(payload.include_in_latest),
        )
    return CategoryOut.model_validate(await _fetch_or_404(db, _CATEGORY_SELECT, category_id, 'カテゴリ'))


@router.patch('/api/admin/categories/{category_id}')
async def update_category(category_id: str, payload: CategoryWrite, db: Annotated[Database, Depends(get_db)]) -> CategoryOut:
    """Partially update a category. Omitted fields keep their current value. An explicit null for notify_channel_id says this section notifies nowhere."""
    identifier = _parse_id(category_id, 'カテゴリ')
    current = await _fetch_or_404(db, _CATEGORY_SELECT, identifier, 'カテゴリ')
    fields = payload.model_fields_set
    _reject_nulls(payload, ('slug', 'name', 'position', 'include_in_latest'))
    # The assignment fragments are only the fixed strings written here; every value goes through %s. Nothing from the request touches the structure of the SQL.
    assignments: list[str] = []
    args: list[Any] = []
    if 'slug' in fields:
        await _ensure_unique(db, _SLUG_TAKEN['categories'], payload.slug, identifier, f'その slug は既に別のカテゴリが使っています: {payload.slug}')
        assignments.append('slug = %s')
        args.append(payload.slug)
    if 'name' in fields:
        assignments.append('name = %s')
        args.append(payload.name)
    if 'position' in fields:
        assignments.append('position = %s')
        args.append(payload.position)
    if 'notify_channel_id' in fields:
        assignments.append('notify_channel_id = %s')
        args.append(_optional_id(payload.notify_channel_id))
    if 'include_in_latest' in fields:
        assignments.append('include_in_latest = %s')
        args.append(int(payload.include_in_latest))
    if not assignments:
        return CategoryOut.model_validate(current)
    args.append(identifier)
    with _conflict_on_integrity('その slug は既に別のカテゴリが使っています。'):
        await db.execute(f'UPDATE categories SET {", ".join(assignments)} WHERE id = %s', *args)
    return CategoryOut.model_validate(await _fetch_or_404(db, _CATEGORY_SELECT, identifier, 'カテゴリ'))


@router.delete('/api/admin/categories/{category_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(category_id: str, db: Annotated[Database, Depends(get_db)]) -> None:
    """Delete a category. Refused while even one article belongs to it.

    articles.category_id references it NOT NULL and RESTRICT, so deleting a category that still has articles would strip those articles of their primary classification. The DB refuses this (the constraint violation is mapped to 409 here), but the articles are counted first so the response can say what happened. Move the articles to another category, then delete.
    """
    identifier = _parse_id(category_id, 'カテゴリ')
    await _fetch_or_404(db, _CATEGORY_SELECT, identifier, 'カテゴリ')
    article_count = int(await db.fetch_val('SELECT COUNT(*) FROM articles WHERE category_id = %s', identifier) or 0)
    if article_count:
        raise Conflict(f'このカテゴリには {article_count} 件の記事が属しているため削除できません。記事を別のカテゴリへ移してから削除してください。', details={'article_count': article_count})
    with _conflict_on_integrity('このカテゴリには記事が属しているため削除できません。記事を別のカテゴリへ移してから削除してください。'):
        await db.execute('DELETE FROM categories WHERE id = %s', identifier)


# Tags


@router.get('/api/admin/tags')
async def list_tags(db: Annotated[Database, Depends(get_db)]) -> ItemList[TagOut]:
    rows = await db.fetch_all(f'{_TAG_SELECT} ORDER BY slug')
    return ItemList[TagOut](items=[TagOut.model_validate(row) for row in rows])


@router.post('/api/admin/tags', status_code=status.HTTP_201_CREATED)
async def create_tag(payload: TagWrite, db: Annotated[Database, Depends(get_db)]) -> TagOut:
    """Create a tag. A tag with a notify_channel_id becomes an additional notify channel on top of the primary channel the category decides."""
    _require_on_create(payload, ('slug', 'name'), 'タグ')
    await _ensure_unique(db, _SLUG_TAKEN['tags'], payload.slug, None, f'その slug は既に別のタグが使っています: {payload.slug}')
    with _conflict_on_integrity(f'その slug は既に別のタグが使っています: {payload.slug}'):
        tag_id = await db.execute_insert(
            'INSERT INTO tags (slug, name, description, icon_url, notify_channel_id) VALUES (%s, %s, %s, %s, %s)',
            payload.slug,
            payload.name,
            payload.description,
            payload.icon_url,
            _optional_id(payload.notify_channel_id),
        )
    return TagOut.model_validate(await _fetch_or_404(db, _TAG_SELECT, tag_id, 'タグ'))


@router.patch('/api/admin/tags/{tag_id}')
async def update_tag(tag_id: str, payload: TagWrite, db: Annotated[Database, Depends(get_db)]) -> TagOut:
    """Partially update a tag. Omitted fields keep their current value. An explicit null for notify_channel_id says this tag takes no part in routing notifications."""
    identifier = _parse_id(tag_id, 'タグ')
    current = await _fetch_or_404(db, _TAG_SELECT, identifier, 'タグ')
    fields = payload.model_fields_set
    _reject_nulls(payload, ('slug', 'name'))
    # The assignment fragments are only the fixed strings written here; every value goes through %s.
    assignments: list[str] = []
    args: list[Any] = []
    if 'slug' in fields:
        await _ensure_unique(db, _SLUG_TAKEN['tags'], payload.slug, identifier, f'その slug は既に別のタグが使っています: {payload.slug}')
        assignments.append('slug = %s')
        args.append(payload.slug)
    if 'name' in fields:
        assignments.append('name = %s')
        args.append(payload.name)
    if 'description' in fields:
        assignments.append('description = %s')
        args.append(payload.description)
    if 'icon_url' in fields:
        assignments.append('icon_url = %s')
        args.append(payload.icon_url)
    if 'notify_channel_id' in fields:
        assignments.append('notify_channel_id = %s')
        args.append(_optional_id(payload.notify_channel_id))
    if not assignments:
        return TagOut.model_validate(current)
    args.append(identifier)
    with _conflict_on_integrity('その slug は既に別のタグが使っています。'):
        await db.execute(f'UPDATE tags SET {", ".join(assignments)} WHERE id = %s', *args)
    return TagOut.model_validate(await _fetch_or_404(db, _TAG_SELECT, identifier, 'タグ'))


@router.delete('/api/admin/tags/{tag_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: str, db: Annotated[Database, Depends(get_db)]) -> None:
    """Delete a tag.

    article_tags and tag_subscriptions go with it via CASCADE: the tag comes off the articles and the subscriptions are dropped, but the articles themselves remain — a tag is not a primary classification, so an article still stands without it. That is why, unlike a category, nothing holds the deletion back.
    """
    identifier = _parse_id(tag_id, 'タグ')
    await _fetch_or_404(db, _TAG_SELECT, identifier, 'タグ')
    await db.execute('DELETE FROM tags WHERE id = %s', identifier)


# Publishers


async def _require_role_exists(db: Database, role_id: int) -> None:
    """Check that the role being attached exists in discord_roles.

    The bot mirrors this snapshot from role-create events, so a role just made in Discord can be briefly absent. Answer in a form that tells the caller whether to wait or whether the ID is wrong, rather than a 500 from a foreign-key violation.
    """
    if await db.fetch_val('SELECT id FROM discord_roles WHERE id = %s', role_id) is None:
        raise BadRequest(f'そのロールは Discord のロールとして取り込まれていません: {role_id}。ロールの一覧は bot がイベントを受けて写すため、作成した直後であれば少し待つこと。')


@router.get('/api/admin/publishers')
async def list_publishers(db: Annotated[Database, Depends(get_db)]) -> ItemList[PublisherOut]:
    rows = await db.fetch_all(f'{_PUBLISHER_SELECT} ORDER BY name, id')
    return ItemList[PublisherOut](items=[PublisherOut.model_validate(row) for row in rows])


@router.post('/api/admin/publishers', status_code=status.HTTP_201_CREATED)
async def create_publisher(payload: PublisherWrite, db: Annotated[Database, Depends(get_db)]) -> PublisherOut:
    """Create a publisher. role_id is the Discord role standing for affiliation: holding it is what decides whether a reporter may publish under that publisher."""
    _require_on_create(payload, ('slug', 'name'), '報道社')
    await _ensure_unique(db, _SLUG_TAKEN['publishers'], payload.slug, None, f'その slug は既に別の報道社が使っています: {payload.slug}')
    role_id = _optional_id(payload.role_id)
    if role_id is not None:
        await _require_role_exists(db, role_id)
        await _ensure_unique(db, _PUBLISHER_ROLE_TAKEN, role_id, None, f'そのロールは既に別の報道社が使っています: {role_id}。1 つのロールを 2 つの報道社の所属にはできません。')
    with _conflict_on_integrity('その slug またはロールは既に別の報道社が使っています。'):
        publisher_id = await db.execute_insert(
            'INSERT INTO publishers (role_id, slug, name, description, logo_url, stance) VALUES (%s, %s, %s, %s, %s, %s)',
            role_id,
            payload.slug,
            payload.name,
            payload.description,
            payload.logo_url,
            'independent' if payload.stance is None else payload.stance,
        )
    return PublisherOut.model_validate(await _fetch_or_404(db, _PUBLISHER_SELECT, publisher_id, '報道社'))


@router.patch('/api/admin/publishers/{publisher_id}')
async def update_publisher(publisher_id: str, payload: PublisherWrite, db: Annotated[Database, Depends(get_db)]) -> PublisherOut:
    """Partially update a publisher. Omitted fields keep their current value.

    An explicit null for role_id says the publisher is dissolved: nobody can publish under it any more. The publisher row itself remains, so the bylines on past articles are preserved.
    """
    identifier = _parse_id(publisher_id, '報道社')
    current = await _fetch_or_404(db, _PUBLISHER_SELECT, identifier, '報道社')
    fields = payload.model_fields_set
    _reject_nulls(payload, ('slug', 'name', 'stance'))
    # The assignment fragments are only the fixed strings written here; every value goes through %s.
    assignments: list[str] = []
    args: list[Any] = []
    if 'role_id' in fields:
        role_id = _optional_id(payload.role_id)
        if role_id is not None:
            await _require_role_exists(db, role_id)
            await _ensure_unique(db, _PUBLISHER_ROLE_TAKEN, role_id, identifier, f'そのロールは既に別の報道社が使っています: {role_id}。1 つのロールを 2 つの報道社の所属にはできません。')
        assignments.append('role_id = %s')
        args.append(role_id)
    if 'slug' in fields:
        await _ensure_unique(db, _SLUG_TAKEN['publishers'], payload.slug, identifier, f'その slug は既に別の報道社が使っています: {payload.slug}')
        assignments.append('slug = %s')
        args.append(payload.slug)
    if 'name' in fields:
        assignments.append('name = %s')
        args.append(payload.name)
    if 'description' in fields:
        assignments.append('description = %s')
        args.append(payload.description)
    if 'logo_url' in fields:
        assignments.append('logo_url = %s')
        args.append(payload.logo_url)
    if 'stance' in fields:
        assignments.append('stance = %s')
        args.append(payload.stance)
    if not assignments:
        return PublisherOut.model_validate(current)
    args.append(identifier)
    with _conflict_on_integrity('その slug またはロールは既に別の報道社が使っています。'):
        await db.execute(f'UPDATE publishers SET {", ".join(assignments)} WHERE id = %s', *args)
    return PublisherOut.model_validate(await _fetch_or_404(db, _PUBLISHER_SELECT, identifier, '報道社'))


@router.delete('/api/admin/publishers/{publisher_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_publisher(publisher_id: str, db: Annotated[Database, Depends(get_db)]) -> None:
    """Delete a publisher. Refused while even one article is tied to it.

    articles.publisher_id references it with RESTRICT. Deleting a publisher that still has articles would lose the byline on every past article it published. Past articles of a dissolved publisher are meant to remain, and dissolution is expressed by setting role_id to null rather than by deleting: nobody can publish under it any more, and the byline is all that is left.
    """
    identifier = _parse_id(publisher_id, '報道社')
    await _fetch_or_404(db, _PUBLISHER_SELECT, identifier, '報道社')
    article_count = int(await db.fetch_val('SELECT COUNT(*) FROM articles WHERE publisher_id = %s', identifier) or 0)
    if article_count:
        raise Conflict(f'この報道社は {article_count} 件の記事の署名になっているため削除できません。解散を表すには削除ではなく role_id を null にしてください(過去記事の署名が残ります)。', details={'article_count': article_count})
    with _conflict_on_integrity('この報道社は記事の署名になっているため削除できません。解散を表すには削除ではなく role_id を null にしてください。'):
        await db.execute('DELETE FROM publishers WHERE id = %s', identifier)
