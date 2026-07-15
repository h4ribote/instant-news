"""カテゴリ・タグ・報道社の管理用 API(taxonomy:admin)。

このルーターが要求するスコープは taxonomy:admin ただ 1 つである。管理者が発行できるスコープを 1 つに束ねてはならない。分類の整備を任せた相手が、記事の署名を偽造する力(articles:write:any)や広告を入稿する力(ads:admin)まで併せ持つ理由は無い。

分類の集合はデプロイごとに異なるためコアでは固定せず、ここで編集する。設定ファイルではなく DB に置くのは、articles.category_id が外部キーで参照する実体であり、設定ファイルに置くと参照整合性を保てないためである。
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
from ..schemas import CategoryOut, CategoryWrite, PublisherOut, PublisherWrite, TagOut, TagWrite

# 経路ごとの宣言ではなくルーターへ課す。新しい経路を足したときに検査を書き忘れても、素通りする経路が生まれない。
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
    """パスの ID を 64bit の整数へ。解釈できない値は、存在しない ID と区別する意味が無いため「無い」として扱う。"""
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound(f'{label}がありません。') from exc


def _optional_id(value: str | None) -> int | None:
    """要求の ID(文字列)を DB へ入れる整数へ。null はそのまま null であり、「このカテゴリは通知しない」「この報道社にロールを紐づけない」という意味のある指定である。"""
    return None if value is None else to_snowflake(value)


@contextmanager
def _conflict_on_integrity(message: str) -> Iterator[None]:
    """制約違反を 409 へ写す。

    重複は事前に検査して分かりやすい文言を返すが、検査と書き込みの間に別の要求が割り込むと事前の検査は擦り抜ける。最後に守るのは DB の制約であり、そこで生じた違反も同じ形の応答にする。
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
    """一意であるべき値の重複を 409 で返す。exclude_id を与えると、その行自身は衝突とみなさない(更新で値を変えない場合に自分自身と衝突するため)。"""
    query = sql if exclude_id is None else f'{sql} AND id <> %s'
    args = [value] if exclude_id is None else [value, exclude_id]
    if await db.fetch_val(query, *args) is not None:
        raise Conflict(message)


def _require_on_create(payload: Any, names: tuple[str, ...], label: str) -> None:
    """作成に必須の項目を検査する。

    *Write のモデルは作成と部分更新の両方で使うため全ての項目が省略可能であり、作成時に何が要るかはここでしか表現できない。
    """
    missing = [name for name in names if getattr(payload, name) is None]
    if missing:
        raise BadRequest(f'{label}の作成には次の項目が必要です: {", ".join(missing)}')


def _reject_nulls(payload: Any, names: tuple[str, ...]) -> None:
    """NOT NULL の列へ明示的な null を指定されたら弾く。null が入らないことは DB が守るが、そこで落とすと制約違反の 500 になり、何が悪かったのかが伝わらない。"""
    for name in names:
        if name in payload.model_fields_set and getattr(payload, name) is None:
            raise BadRequest(f'{name} には null を指定できません。')


# --------------------------------------------------------------------------
# カテゴリ
# --------------------------------------------------------------------------


@router.get('/api/admin/categories')
async def list_categories(db: Annotated[Database, Depends(get_db)]) -> list[CategoryOut]:
    rows = await db.fetch_all(f'{_CATEGORY_SELECT} ORDER BY position, id')
    return [CategoryOut.model_validate(row) for row in rows]


@router.post('/api/admin/categories', status_code=status.HTTP_201_CREATED)
async def create_category(payload: CategoryWrite, db: Annotated[Database, Depends(get_db)]) -> CategoryOut:
    """カテゴリを作る。notify_channel_id を省略した(または null にした)カテゴリは、どのチャンネルへも通知しない。"""
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
    """カテゴリを部分更新する。省略された項目は現在の値のまま残す。notify_channel_id への明示的な null は「この欄は通知しない」という指定である。"""
    identifier = _parse_id(category_id, 'カテゴリ')
    current = await _fetch_or_404(db, _CATEGORY_SELECT, identifier, 'カテゴリ')
    fields = payload.model_fields_set
    _reject_nulls(payload, ('slug', 'name', 'position', 'include_in_latest'))
    # 代入する断片はここに書かれた固定の文字列だけであり、値は全て %s で渡す。要求の内容が SQL の構造に触れることはない。
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
    """カテゴリを削除する。記事が 1 件でも属している場合は削除できない。

    articles.category_id は NOT NULL かつ RESTRICT で参照しているため、記事のあるカテゴリを消せば記事の主分類が失われる。これは DB が拒む(制約違反はここで 409 へ写す)が、何が起きたのかを伝えるために属している記事の件数を先に数えて返す。記事を別のカテゴリへ移してから削除すること。
    """
    identifier = _parse_id(category_id, 'カテゴリ')
    await _fetch_or_404(db, _CATEGORY_SELECT, identifier, 'カテゴリ')
    article_count = int(await db.fetch_val('SELECT COUNT(*) FROM articles WHERE category_id = %s', identifier) or 0)
    if article_count:
        raise Conflict(f'このカテゴリには {article_count} 件の記事が属しているため削除できません。記事を別のカテゴリへ移してから削除してください。', details={'article_count': article_count})
    with _conflict_on_integrity('このカテゴリには記事が属しているため削除できません。記事を別のカテゴリへ移してから削除してください。'):
        await db.execute('DELETE FROM categories WHERE id = %s', identifier)


# --------------------------------------------------------------------------
# タグ
# --------------------------------------------------------------------------


@router.get('/api/admin/tags')
async def list_tags(db: Annotated[Database, Depends(get_db)]) -> list[TagOut]:
    rows = await db.fetch_all(f'{_TAG_SELECT} ORDER BY slug')
    return [TagOut.model_validate(row) for row in rows]


@router.post('/api/admin/tags', status_code=status.HTTP_201_CREATED)
async def create_tag(payload: TagWrite, db: Annotated[Database, Depends(get_db)]) -> TagOut:
    """タグを作る。notify_channel_id を設定したタグは、カテゴリが決める主チャンネルに加えて追加の通知先になる。"""
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
    """タグを部分更新する。省略された項目は現在の値のまま残す。notify_channel_id への明示的な null は「このタグは通知の振り分けに関与しない」という指定である。"""
    identifier = _parse_id(tag_id, 'タグ')
    current = await _fetch_or_404(db, _TAG_SELECT, identifier, 'タグ')
    fields = payload.model_fields_set
    _reject_nulls(payload, ('slug', 'name'))
    # 代入する断片はここに書かれた固定の文字列だけであり、値は全て %s で渡す。
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
    """タグを削除する。

    article_tags と tag_subscriptions は CASCADE で消える。記事からこのタグが外れ、購読も解かれるが、記事そのものは残る(タグは主分類ではないため、失っても記事は成立する)。カテゴリと違って引き止める理由が無いのはこのためである。
    """
    identifier = _parse_id(tag_id, 'タグ')
    await _fetch_or_404(db, _TAG_SELECT, identifier, 'タグ')
    await db.execute('DELETE FROM tags WHERE id = %s', identifier)


# --------------------------------------------------------------------------
# 報道社
# --------------------------------------------------------------------------


async def _require_role_exists(db: Database, role_id: int) -> None:
    """紐づけるロールが discord_roles にあることを確かめる。

    このスナップショットは bot がロールの作成イベントを受けて写すため、Discord で作ったばかりのロールがごく短い間まだ現れていないことがある。外部キー違反の 500 ではなく、待てばよいのか ID が誤っているのかが分かる形で返す。
    """
    if await db.fetch_val('SELECT id FROM discord_roles WHERE id = %s', role_id) is None:
        raise BadRequest(f'そのロールは Discord のロールとして取り込まれていません: {role_id}。ロールの一覧は bot がイベントを受けて写すため、作成した直後であれば少し待つこと。')


@router.get('/api/admin/publishers')
async def list_publishers(db: Annotated[Database, Depends(get_db)]) -> list[PublisherOut]:
    rows = await db.fetch_all(f'{_PUBLISHER_SELECT} ORDER BY name, id')
    return [PublisherOut.model_validate(row) for row in rows]


@router.post('/api/admin/publishers', status_code=status.HTTP_201_CREATED)
async def create_publisher(payload: PublisherWrite, db: Annotated[Database, Depends(get_db)]) -> PublisherOut:
    """報道社を作る。role_id は所属を表す Discord ロールであり、記者がその報道社を名乗れるかどうかはこのロールの保有で決まる。"""
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
    """報道社を部分更新する。省略された項目は現在の値のまま残す。

    role_id への明示的な null は「解散した(誰もこの報道社を名乗れない)」という指定である。報道社の行そのものは残るため、過去記事の署名は保たれる。
    """
    identifier = _parse_id(publisher_id, '報道社')
    current = await _fetch_or_404(db, _PUBLISHER_SELECT, identifier, '報道社')
    fields = payload.model_fields_set
    _reject_nulls(payload, ('slug', 'name', 'stance'))
    # 代入する断片はここに書かれた固定の文字列だけであり、値は全て %s で渡す。
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
    """報道社を削除する。記事が 1 件でも紐づいている場合は削除できない。

    articles.publisher_id は RESTRICT で参照している。記事のある報道社を消せば、その報道社が出した過去記事の署名が失われる。解散した報道社の過去記事は残るのが正しく、解散を表すのは削除ではなく role_id を null にすることである(誰もその報道社を名乗れなくなり、署名だけが残る)。
    """
    identifier = _parse_id(publisher_id, '報道社')
    await _fetch_or_404(db, _PUBLISHER_SELECT, identifier, '報道社')
    article_count = int(await db.fetch_val('SELECT COUNT(*) FROM articles WHERE publisher_id = %s', identifier) or 0)
    if article_count:
        raise Conflict(f'この報道社は {article_count} 件の記事の署名になっているため削除できません。解散を表すには削除ではなく role_id を null にしてください(過去記事の署名が残ります)。', details={'article_count': article_count})
    with _conflict_on_integrity('この報道社は記事の署名になっているため削除できません。解散を表すには削除ではなく role_id を null にしてください。'):
        await db.execute('DELETE FROM publishers WHERE id = %s', identifier)
