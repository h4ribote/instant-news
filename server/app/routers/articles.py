"""記事のエンドポイント。

閲覧できない記事には 403 ではなく 404 を返す。403 は「そこに何かがある」ことを教えてしまい、存在自体が秘密である限定公開の記事では、それだけで漏洩になる。この規則は詳細・改版履歴・差分のすべてに一様に適用する(記事本体を隠しながら改版履歴からタイトルと本文が読めては意味がない)。

業務上の規則の判定は articles.py に置き、ここは要求の受け取りと応答の組み立てに徹する。同じ保存・公開の手順を予約公開のループからも呼ぶためである。
"""

from __future__ import annotations

import difflib
from typing import Any, Final

from fastapi import APIRouter, Depends, Query

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings
from common.snowflake import to_snowflake

from ..articles import ensure_may_edit, has_write_any, publish_article, save_article
from ..deps import get_config, get_db, get_principal_optional, get_settings, require_scope
from ..errors import BadRequest, NotFound
from ..media.service import IngestFailure
from ..principals import Principal
from ..schemas import (
    ArticleListItem,
    ArticleListOut,
    ArticleOut,
    ArticleSaveOut,
    ArticleWrite,
    CommentAuthorOut,
    CommentListOut,
    CommentOut,
    DiscordPostOut,
    MediaFailure,
    RevisionDiffOut,
    RevisionListItem,
    RevisionOut,
)
from ..visibility import can_view_article, visibility_sql_filter

router = APIRouter(tags=['articles'])

_DEFAULT_PER_PAGE: Final = 20
_MAX_PER_PAGE: Final = 100

# コメントの 1 ページの件数。記事より多めに採るのは、コメントが 1 件あたり小さく、会話として続けて読むものであるため。
_DEFAULT_COMMENTS_PER_PAGE: Final = 50
_MAX_COMMENTS_PER_PAGE: Final = 200

# 賛否とコメント数は記事の全通知メッセージの合計である。1 記事が複数のチャンネルへ通知されうるため、article_discord_posts は記事につき複数行を持ち、行ごとに独立した数を保持する。
# 差し引きを SQL で求めるときは必ず SUM() を経た値どうしで引く。up_count と down_count は符号なしであり、行ごとの引き算(dp.up_count - dp.down_count)は反対が賛成を上回った瞬間に、値が丸まるのではなく文そのものが失敗する。SUM() は DECIMAL を返すため負の値を扱える。
# articles には別名を付けない。visibility_sql_filter が返す条件の断片は articles を表名として参照するため、別名を付けるとその断片が解決できない列を指し、一覧のクエリが実行時に落ちる。
_ARTICLE_COLUMNS: Final = """
    articles.id, articles.slug, articles.title, articles.excerpt, articles.body_md, articles.body_html,
    articles.cover_image_url, articles.author_id, articles.publisher_id, articles.category_id,
    articles.series_id, articles.series_position, articles.visibility, articles.byline_mode,
    articles.is_breaking, articles.is_sponsored, articles.sponsor_name, articles.status,
    articles.view_count, articles.scheduled_at, articles.published_at, articles.created_at, articles.updated_at,
    u.discord_user_id AS author_discord_user_id, u.username AS author_username,
    u.global_name AS author_global_name, u.avatar_hash AS author_avatar_hash,
    p.role_id AS publisher_role_id, p.slug AS publisher_slug, p.name AS publisher_name,
    p.description AS publisher_description, p.logo_url AS publisher_logo_url, p.stance AS publisher_stance,
    c.slug AS category_slug, c.name AS category_name, c.position AS category_position,
    c.notify_channel_id AS category_notify_channel_id, c.include_in_latest AS category_include_in_latest,
    s.slug AS series_slug, s.title AS series_title,
    (SELECT COALESCE(SUM(dp.up_count), 0) FROM article_discord_posts dp WHERE dp.article_id = articles.id) AS up_count,
    (SELECT COALESCE(SUM(dp.down_count), 0) FROM article_discord_posts dp WHERE dp.article_id = articles.id) AS down_count,
    (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = articles.id) AS comment_count
"""

_ARTICLE_FROM: Final = """
    FROM articles
    JOIN users u ON u.id = articles.author_id
    JOIN categories c ON c.id = articles.category_id
    LEFT JOIN publishers p ON p.id = articles.publisher_id
    LEFT JOIN series s ON s.id = articles.series_id
"""

_REVISION_SELECT: Final = """
SELECT r.id, r.revision_number, r.editor_id, r.via_token_id, r.title, r.body_md, r.excerpt, r.note,
       r.is_correction, r.created_at,
       u.discord_user_id AS editor_discord_user_id, u.username AS editor_username,
       u.global_name AS editor_global_name, u.avatar_hash AS editor_avatar_hash
  FROM article_revisions r
  JOIN users u ON u.id = r.editor_id
"""


# --------------------------------------------------------------------------
# 行から応答の形へ
# --------------------------------------------------------------------------


def _author_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['author_id'],
        'discord_user_id': row['author_discord_user_id'],
        'username': row['author_username'],
        'global_name': row['author_global_name'],
        'avatar_hash': row['author_avatar_hash'],
    }


def _publisher_data(row: dict[str, Any]) -> dict[str, Any] | None:
    if row['publisher_id'] is None:
        return None
    return {
        'id': row['publisher_id'],
        'role_id': row['publisher_role_id'],
        'slug': row['publisher_slug'],
        'name': row['publisher_name'],
        'description': row['publisher_description'],
        'logo_url': row['publisher_logo_url'],
        'stance': row['publisher_stance'],
    }


def _category_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['category_id'],
        'slug': row['category_slug'],
        'name': row['category_name'],
        'position': row['category_position'],
        'notify_channel_id': row['category_notify_channel_id'],
        'include_in_latest': bool(row['category_include_in_latest']),
    }


def _series_data(row: dict[str, Any]) -> dict[str, Any] | None:
    if row['series_id'] is None:
        return None
    return {'id': row['series_id'], 'slug': row['series_slug'], 'title': row['series_title']}


def _list_item_data(row: dict[str, Any], tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'slug': row['slug'],
        'title': row['title'],
        'excerpt': row['excerpt'],
        'cover_image_url': row['cover_image_url'],
        'author': _author_data(row),
        'publisher': _publisher_data(row),
        'category': _category_data(row),
        'series': _series_data(row),
        'series_position': row['series_position'],
        'tags': tags,
        'visibility': row['visibility'],
        'byline_mode': row['byline_mode'],
        'is_breaking': bool(row['is_breaking']),
        'is_sponsored': bool(row['is_sponsored']),
        'sponsor_name': row['sponsor_name'],
        'status': row['status'],
        'view_count': int(row['view_count']),
        # SUM() は MySQL から Decimal で返るため、そのまま int の項目へ渡さず明示的に変換する。
        'up_count': int(row['up_count']),
        'down_count': int(row['down_count']),
        'score': int(row['up_count']) - int(row['down_count']),
        'comment_count': int(row['comment_count']),
        'published_at': row['published_at'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


def _revision_item_data(row: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': row['id'],
        'revision_number': row['revision_number'],
        'editor': {
            'id': row['editor_id'],
            'discord_user_id': row['editor_discord_user_id'],
            'username': row['editor_username'],
            'global_name': row['editor_global_name'],
            'avatar_hash': row['editor_avatar_hash'],
        },
        'via_token_id': row['via_token_id'],
        'title': row['title'],
        'excerpt': row['excerpt'],
        'note': row['note'],
        'is_correction': bool(row['is_correction']),
        'created_at': row['created_at'],
    }


async def _fetch_tags(db: Database, article_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """記事ごとのタグを引く。

    記事の一覧と同じクエリで JOIN すると、タグの数だけ記事の行が複製され、頁の件数が狂う。1 度の追加クエリでまとめて引き、記事へ割り当てる。
    """
    if not article_ids:
        return {}
    placeholders = ', '.join(['%s'] * len(article_ids))
    rows = await db.fetch_all(
        f'SELECT at.article_id, t.id, t.slug, t.name, t.description, t.icon_url, t.notify_channel_id '
        f'FROM article_tags at JOIN tags t ON t.id = at.tag_id WHERE at.article_id IN ({placeholders}) ORDER BY t.name',
        *article_ids,
    )
    grouped: dict[int, list[dict[str, Any]]] = {article_id: [] for article_id in article_ids}
    for row in rows:
        grouped[row['article_id']].append(
            {
                'id': row['id'],
                'slug': row['slug'],
                'name': row['name'],
                'description': row['description'],
                'icon_url': row['icon_url'],
                'notify_channel_id': row['notify_channel_id'],
            }
        )
    return grouped


def _thread_url(guild_id: int, thread_id: int | None) -> str | None:
    """スレッドを開く URL。スレッドはそれ自体が 1 つのチャンネルであるため、チャンネルの URL と同じ形で指せる。

    組み立てるのがサーバーであるのは、この URL に対象 guild の ID が要るためである。guild_id はプラットフォームの設定にしかなく、画面はそれを知る手段を持たない。
    """
    if thread_id is None:
        return None
    return f'https://discord.com/channels/{guild_id}/{thread_id}'


async def _fetch_discord_posts(db: Database, settings: RuntimeSettings, article_id: int) -> list[DiscordPostOut]:
    """記事の通知メッセージの内訳を引く。未送信の行も返す(まだ送られていないことも、記事面が知ってよい事実である)。"""
    rows = await db.fetch_all(
        'SELECT dp.channel_id, dp.message_id, dp.thread_id, dp.up_count, dp.down_count, dp.posted_at, '
        '(SELECT COUNT(*) FROM article_comments ac WHERE ac.post_id = dp.id) AS comment_count '
        'FROM article_discord_posts dp WHERE dp.article_id = %s ORDER BY dp.enqueued_at, dp.id',
        article_id,
    )
    return [
        DiscordPostOut(
            channel_id=row['channel_id'],
            message_id=row['message_id'],
            thread_id=row['thread_id'],
            thread_url=_thread_url(settings.discord.guild_id, row['thread_id']),
            up_count=int(row['up_count']),
            down_count=int(row['down_count']),
            # 差し引きはここで求める。行の列どうしを SQL で引くと、符号なしの引き算になって文が失敗する。
            score=int(row['up_count']) - int(row['down_count']),
            comment_count=int(row['comment_count']),
            posted_at=row['posted_at'],
        )
        for row in rows
    ]


async def _article_out(db: Database, settings: RuntimeSettings, row: dict[str, Any]) -> ArticleOut:
    """記事詳細の応答を組み立てる。"""
    tags = (await _fetch_tags(db, [row['id']])).get(row['id'], [])
    role_rows = await db.fetch_all('SELECT role_id FROM article_visibility_roles WHERE article_id = %s', row['id'])
    correction_rows = await db.fetch_all(
        f'{_REVISION_SELECT} WHERE r.article_id = %s AND r.is_correction = 1 ORDER BY r.revision_number',
        row['id'],
    )
    data = _list_item_data(row, tags)
    data.update(
        {
            'body_md': row['body_md'],
            'body_html': row['body_html'],
            'visibility_role_ids': [role_row['role_id'] for role_row in role_rows],
            'scheduled_at': row['scheduled_at'],
            'corrections': [_revision_item_data(correction) for correction in correction_rows],
            'discord_posts': await _fetch_discord_posts(db, settings, int(row['id'])),
        }
    )
    return ArticleOut.model_validate(data)


# --------------------------------------------------------------------------
# 記事の取得と閲覧の判定
# --------------------------------------------------------------------------


def _parse_id(value: str) -> int:
    """パスの ID を int へ。ID として解釈できない値は、存在しない記事と区別する意味が無いため 404 にする。"""
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound('記事が見つかりません。') from exc


async def _load_article(db: Database, *, article_id: int | None = None, slug: str | None = None) -> dict[str, Any] | None:
    if article_id is not None:
        return await db.fetch_one(f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_FROM} WHERE articles.id = %s', article_id)
    return await db.fetch_one(f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_FROM} WHERE articles.slug = %s', slug)


async def _require_viewable(db: Database, settings: RuntimeSettings, principal: Principal | None, slug: str) -> dict[str, Any]:
    """slug から記事を引き、閲覧できなければ 404 にする。settings は管理者の判定(設定で委譲されたロールを含む)に要る。"""
    row = await _load_article(db, slug=slug)
    if row is None or not await can_view_article(db, principal, row, settings=settings):
        raise NotFound('記事が見つかりません。')
    return row


async def _require_editable(db: Database, settings: RuntimeSettings, principal: Principal, article_id: int) -> dict[str, Any]:
    """記事を引き、その主体が編集・公開できることを確かめる。"""
    row = await _load_article(db, article_id=article_id)
    if row is None:
        raise NotFound('記事が見つかりません。')
    ensure_may_edit(principal, row, write_any=await has_write_any(db, settings, principal))
    return row


# --------------------------------------------------------------------------
# 一覧
# --------------------------------------------------------------------------


@router.get('/api/articles', response_model=ArticleListOut)
async def list_articles(
    category: str | None = Query(default=None, description='カテゴリの slug で絞る。'),
    tag: str | None = Query(default=None, description='タグの slug で絞る。'),
    publisher: str | None = Query(default=None, description='報道社の slug で絞る。'),
    series: str | None = Query(default=None, description='連載の slug で絞る。'),
    author: str | None = Query(default=None, description='著者の利用者 ID で絞る。'),
    latest_only: bool = Query(default=False, description='新着一覧に載せるカテゴリの記事だけを返す。'),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=_DEFAULT_PER_PAGE, ge=1, le=_MAX_PER_PAGE),
    db: Database = Depends(get_db),
    principal: Principal | None = Depends(get_principal_optional),
) -> ArticleListOut:
    """公開済みの記事を新しい順に返す。閲覧範囲の絞り込みを必ず通す。"""
    conditions = ["articles.status = 'published'"]
    params: list[Any] = []

    # 断片は既に括弧で閉じられているため、そのまま AND で連結できる。
    visibility_sql, visibility_params = visibility_sql_filter(principal)
    conditions.append(visibility_sql)
    params.extend(visibility_params)

    if category is not None:
        conditions.append('c.slug = %s')
        params.append(category)
    if publisher is not None:
        conditions.append('p.slug = %s')
        params.append(publisher)
    if series is not None:
        conditions.append('s.slug = %s')
        params.append(series)
    if author is not None:
        try:
            author_id = to_snowflake(author)
        except ValueError as exc:
            raise BadRequest(f'author は利用者 ID として解釈できません: {author}') from exc
        conditions.append('articles.author_id = %s')
        params.append(author_id)
    if tag is not None:
        # EXISTS で絞る。JOIN するとタグを複数持つ記事の行が複製され、件数と頁が狂う。
        conditions.append('EXISTS (SELECT 1 FROM article_tags atg JOIN tags t ON t.id = atg.tag_id WHERE atg.article_id = articles.id AND t.slug = %s)')
        params.append(tag)
    if latest_only:
        # 相場のような定期記事が横断的な新着の流れを埋めるのを防ぐ。
        conditions.append('c.include_in_latest = 1')

    where = ' AND '.join(conditions)
    total = int(await db.fetch_val(f'SELECT COUNT(*) {_ARTICLE_FROM} WHERE {where}', *params) or 0)

    # 並びは公開日時の降順。同時刻の記事が頁の境界で重複・欠落しないよう ID で一意に決める。
    rows = await db.fetch_all(
        f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_FROM} WHERE {where} ORDER BY articles.published_at DESC, articles.id DESC LIMIT %s OFFSET %s',
        *params,
        per_page,
        (page - 1) * per_page,
    )
    tags_by_article = await _fetch_tags(db, [row['id'] for row in rows])
    items = [ArticleListItem.model_validate(_list_item_data(row, tags_by_article.get(row['id'], []))) for row in rows]
    return ArticleListOut(items=items, page=page, per_page=per_page, total=total, has_next=page * per_page < total)


# --------------------------------------------------------------------------
# 保存・公開
# --------------------------------------------------------------------------


@router.post('/api/articles', response_model=ArticleSaveOut, status_code=201)
async def create_article(
    payload: ArticleWrite,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal = Depends(require_scope('articles:write')),
) -> ArticleSaveOut:
    """記事を作成する。"""
    article_id, failures = await save_article(db, config, settings, principal, article_id=None, payload=payload)
    return await _save_response(db, settings, article_id, failures)


@router.patch('/api/articles/{article_id}', response_model=ArticleSaveOut)
async def update_article(
    article_id: str,
    payload: ArticleWrite,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal = Depends(require_scope('articles:write')),
) -> ArticleSaveOut:
    """記事を更新する。"""
    saved_id, failures = await save_article(db, config, settings, principal, article_id=_parse_id(article_id), payload=payload)
    return await _save_response(db, settings, saved_id, failures)


async def _save_response(db: Database, settings: RuntimeSettings, article_id: int, failures: list[IngestFailure]) -> ArticleSaveOut:
    """保存の応答を組み立てる。

    取り込めなかった画像は必ず応答へ載せる。取り込めなかった URL は本文にそのまま残り、外部の CDN であればいずれ失効して画像が消えるため、黙って成功として扱うと、朽ちるリンクが誰にも気付かれないまま記事に残る。
    """
    row = await _load_article(db, article_id=article_id)
    if row is None:
        raise NotFound('記事が見つかりません。')
    return ArticleSaveOut(
        article=await _article_out(db, settings, row),
        media_failures=[MediaFailure(source_url=failure.source_url, reason=failure.reason) for failure in failures],
    )


@router.post('/api/articles/{article_id}/publish', response_model=ArticleOut)
async def publish(
    article_id: str,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal = Depends(require_scope('articles:write')),
) -> ArticleOut:
    """記事を公開する。署名を凍結し、同じトランザクションで通知を積む。"""
    parsed = _parse_id(article_id)
    await _require_editable(db, settings, principal, parsed)
    await publish_article(db, settings, parsed)
    row = await _load_article(db, article_id=parsed)
    if row is None:
        raise NotFound('記事が見つかりません。')
    return await _article_out(db, settings, row)


# --------------------------------------------------------------------------
# 詳細・改版履歴
# --------------------------------------------------------------------------


@router.get('/api/articles/{slug}', response_model=ArticleOut)
async def get_article(
    slug: str,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> ArticleOut:
    """記事の詳細を返す。閲覧できなければ 404。"""
    row = await _require_viewable(db, settings, principal, slug)
    # updated_at を自身の値で明示的に更新する。省くと ON UPDATE CURRENT_TIMESTAMP が働き、閲覧のたびに「更新日時」が進んで、記事が編集されたのか読まれただけなのかを区別できなくなる。
    await db.execute('UPDATE articles SET view_count = view_count + 1, updated_at = updated_at WHERE id = %s', row['id'])
    row['view_count'] = int(row['view_count']) + 1
    return await _article_out(db, settings, row)


@router.get('/api/articles/{slug}/comments', response_model=CommentListOut)
async def list_comments(
    slug: str,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=_DEFAULT_COMMENTS_PER_PAGE, ge=1, le=_MAX_COMMENTS_PER_PAGE),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> CommentListOut:
    """記事のコメントを古い順に返す。閲覧できない記事なら 404。

    コメントの閲覧範囲は記事の閲覧範囲そのものである。記事を読めない相手にコメントを返すと、本文を伏せたまま、その記事について何がどれだけ語られたかが漏れる。判定は記事詳細と同じ _require_viewable に委ねる(ここに独自の判定を書くと、記事側の規則が変わったときに片方だけが古くなる)。

    古い順に返すのは、コメントが会話だからである。点数の高い順に並べ替えると、返信が元の発言より前に出て文脈が壊れる。

    この API は読み出しのみで、書き込みを受け付けない。コメントの実体は Discord のスレッドにあり、書く場所はそこだけである(thread_url がその入口)。
    """
    article = await _require_viewable(db, settings, principal, slug)
    total = int(await db.fetch_val('SELECT COUNT(*) FROM article_comments WHERE article_id = %s', article['id']) or 0)
    rows = await db.fetch_all(
        """
        SELECT c.id, c.message_id, c.author_discord_user_id, c.body, c.up_count, c.down_count,
               c.posted_at, c.edited_at,
               u.username AS author_username, u.global_name AS author_global_name,
               u.avatar_hash AS author_avatar_hash
          FROM article_comments c
          LEFT JOIN users u ON u.discord_user_id = c.author_discord_user_id
         WHERE c.article_id = %s
         ORDER BY c.posted_at, c.id
         LIMIT %s OFFSET %s
        """,
        article['id'], per_page, (page - 1) * per_page,
    )
    # スレッドは通知ごとに開くため、1 記事に複数ありうる。入口として示すのは最初に送られた通知のものにする(コメントはどのスレッドのものも同じ記事のコメントとして扱う)。
    thread_row = await db.fetch_one(
        'SELECT thread_id FROM article_discord_posts WHERE article_id = %s AND thread_id IS NOT NULL ORDER BY enqueued_at, id LIMIT 1',
        article['id'],
    )
    return CommentListOut(
        items=[
            CommentOut(
                id=row['id'],
                message_id=row['message_id'],
                author=CommentAuthorOut(
                    discord_user_id=row['author_discord_user_id'],
                    username=row['author_username'],
                    global_name=row['author_global_name'],
                    avatar_hash=row['author_avatar_hash'],
                ),
                body=row['body'],
                up_count=int(row['up_count']),
                down_count=int(row['down_count']),
                # 差し引きはここで求める。行の列どうしを SQL で引くと符号なしの引き算になり、反対が賛成を上回った瞬間に文が失敗する。
                score=int(row['up_count']) - int(row['down_count']),
                posted_at=row['posted_at'],
                edited_at=row['edited_at'],
            )
            for row in rows
        ],
        total=total,
        thread_url=_thread_url(settings.discord.guild_id, thread_row['thread_id']) if thread_row else None,
    )


@router.get('/api/articles/{slug}/revisions', response_model=list[RevisionListItem])
async def list_revisions(
    slug: str,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> list[RevisionListItem]:
    """改版履歴の一覧を返す。"""
    article = await _require_viewable(db, settings, principal, slug)
    rows = await db.fetch_all(f'{_REVISION_SELECT} WHERE r.article_id = %s ORDER BY r.revision_number', article['id'])
    return [RevisionListItem.model_validate(_revision_item_data(row)) for row in rows]


@router.get('/api/articles/{slug}/revisions/{revision_number}', response_model=RevisionOut)
async def get_revision(
    slug: str,
    revision_number: int,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> RevisionOut:
    """過去版のスナップショットを返す。"""
    article = await _require_viewable(db, settings, principal, slug)
    row = await _fetch_revision(db, article['id'], revision_number)
    data = _revision_item_data(row)
    data['body_md'] = row['body_md']
    return RevisionOut.model_validate(data)


@router.get('/api/articles/{slug}/revisions/{revision_number}/diff', response_model=RevisionDiffOut)
async def get_revision_diff(
    slug: str,
    revision_number: int,
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> RevisionDiffOut:
    """直前版との差分を unified diff の行の配列で返す。"""
    article = await _require_viewable(db, settings, principal, slug)
    current = await _fetch_revision(db, article['id'], revision_number)
    # 直前版は revision_number - 1 とは限らない。連番は記事内で 1 から振るが、より小さい番号のうち最大のものを直前版とすれば、履歴に欠けがあっても正しく並ぶ。
    previous = await db.fetch_one(
        f'{_REVISION_SELECT} WHERE r.article_id = %s AND r.revision_number < %s ORDER BY r.revision_number DESC LIMIT 1',
        article['id'],
        revision_number,
    )
    previous_body = previous['body_md'] if previous else ''
    previous_number = previous['revision_number'] if previous else None
    diff = difflib.unified_diff(
        previous_body.splitlines(),
        current['body_md'].splitlines(),
        fromfile=f'revision {previous_number}' if previous_number is not None else '(初版の前)',
        tofile=f'revision {current["revision_number"]}',
        # 行末の改行は splitlines() で落としてあるため、差分の側でも付けない。
        lineterm='',
    )
    return RevisionDiffOut(revision_number=current['revision_number'], previous_revision_number=previous_number, diff=list(diff))


async def _fetch_revision(db: Database, article_id: int, revision_number: int) -> dict[str, Any]:
    row = await db.fetch_one(
        f'{_REVISION_SELECT} WHERE r.article_id = %s AND r.revision_number = %s',
        article_id,
        revision_number,
    )
    if row is None:
        raise NotFound('その改版は存在しません。')
    return row
