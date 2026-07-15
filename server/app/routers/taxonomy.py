"""分類(カテゴリ・タグ・報道社・連載)の読み取り API。

このモジュールは記事一覧の射影(fetch_article_page)も持ち、検索のルータがそれを再利用する。一覧に出す記事の形(ArticleListItem)は絞り込みの条件に依らず同一であり、条件ごとに組み立てを書き分けると、署名・反応数・タグの解決が経路ごとに食い違う。

一覧はすべて閲覧範囲の絞り込み(visibility_sql_filter)を通し、あわせて status='published' を課す。前者は「読んではいけない記事を見せない」ためであり、後者は「まだ公開していない記事を公開の索引に混ぜない」ため。閲覧範囲の絞り込みだけでは、著者本人と管理者に対して自分の下書き(published_at が NULL)が年表や検索結果へ紛れ込む。下書きは執筆者の記事一覧で辿るものであって、話題の索引に並べるものではない。

SQL の組み立てにあたり、f 文字列へ差し込むのはこのモジュール内に書いた定数(結合句・並び順・IN 句の %s の個数)だけであり、要求から来た値は必ずプレースホルダ(%s)で渡す。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from common.db import Database

from ..deps import get_db, get_principal_optional
from ..errors import NotFound
from ..principals import Principal
from ..rankings import ARTICLE_SCORE_SQL, article_totals_sql
from ..schemas import (
    ArticleListItem,
    CategoryOut,
    Page,
    PublisherDetailOut,
    PublisherOut,
    PublisherStats,
    SeriesBrief,
    SeriesDetailOut,
    SeriesOut,
    TagDetailOut,
    TagOut,
    UserBrief,
)
from ..visibility import visibility_sql_filter

router = APIRouter(tags=['taxonomy'])

# 一覧 1 ページあたりの件数。タグ年表と検索はクエリで page しか受けないため、件数はここで固定する。
DEFAULT_PER_PAGE: Final = 20
# 報道社ページ・連載ページに添える記事の上限。詳細ページは「その社・その連載を概観する」ためのものであり、全件は /api/articles の絞り込みで辿れる。上限を設けないと、記事の多い報道社のページだけが際限なく重くなる。
DETAIL_ARTICLES_LIMIT: Final = 50


class CategoryListItem(CategoryOut):
    """カテゴリ一覧の項目。

    article_count は要求元が閲覧できる公開済みの記事だけで数える。全件で数えると、読めない記事の存在と数が件数から漏れる。
    """

    article_count: int


class PublisherPageOut(PublisherDetailOut):
    """報道社ページの応答。プロフィール・所属記者・実績に、その社の記事一覧を添える。"""

    articles: list[ArticleListItem] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 記事一覧の射影(検索のルータからも使う)
# --------------------------------------------------------------------------

# 一覧に出す記事の列。DictCursor は同名の列を後勝ちで潰すため、結合したテーブルの列には必ず接頭辞つきの別名を与える。
_ARTICLE_COLUMNS: Final = '''
    articles.id AS id,
    articles.slug AS slug,
    articles.title AS title,
    articles.excerpt AS excerpt,
    articles.cover_image_url AS cover_image_url,
    articles.series_position AS series_position,
    articles.visibility AS visibility,
    articles.byline_mode AS byline_mode,
    articles.is_breaking AS is_breaking,
    articles.is_sponsored AS is_sponsored,
    articles.sponsor_name AS sponsor_name,
    articles.status AS status,
    articles.view_count AS view_count,
    articles.published_at AS published_at,
    articles.created_at AS created_at,
    articles.updated_at AS updated_at,
    au.id AS author_id,
    au.discord_user_id AS author_discord_user_id,
    au.username AS author_username,
    au.global_name AS author_global_name,
    au.avatar_hash AS author_avatar_hash,
    pb.id AS publisher_id,
    pb.role_id AS publisher_role_id,
    pb.slug AS publisher_slug,
    pb.name AS publisher_name,
    pb.description AS publisher_description,
    pb.logo_url AS publisher_logo_url,
    pb.stance AS publisher_stance,
    ct.id AS category_id,
    ct.slug AS category_slug,
    ct.name AS category_name,
    ct.position AS category_position,
    ct.notify_channel_id AS category_notify_channel_id,
    ct.include_in_latest AS category_include_in_latest,
    sr.id AS series_id,
    sr.slug AS series_slug,
    sr.title AS series_title,
    COALESCE((SELECT SUM(adp.up_count) FROM article_discord_posts adp WHERE adp.article_id = articles.id), 0) AS up_count,
    COALESCE((SELECT SUM(adp.down_count) FROM article_discord_posts adp WHERE adp.article_id = articles.id), 0) AS down_count,
    (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = articles.id) AS comment_count
'''

# 記事の署名・分類を解決する結合。author_id と category_id は NOT NULL の外部キーであるため内部結合でよく、件数を変えない。反応数を結合ではなく相関副問い合わせで求めるのは、結合すると通知先の数だけ行が増えて記事の重複排除が要るうえ、1 ページ分の記事にしか要らない集計のために通知の全行を畳むことになるため。
_ARTICLE_JOINS: Final = '''
    FROM articles
    JOIN users au ON au.id = articles.author_id
    JOIN categories ct ON ct.id = articles.category_id
    LEFT JOIN publishers pb ON pb.id = articles.publisher_id
    LEFT JOIN series sr ON sr.id = articles.series_id
'''


def published_and_visible(principal: Principal | None) -> tuple[str, list[Any]]:
    """公開済みかつ要求元が閲覧できる記事に絞る WHERE 断片と、その引数を返す。"""
    visibility_sql, visibility_args = visibility_sql_filter(principal)
    return f"articles.status = 'published' AND ({visibility_sql})", list(visibility_args)


async def _fetch_tags_for_articles(db: Database, article_ids: Sequence[int]) -> dict[int, list[TagOut]]:
    """記事 ID の集合に対するタグを 1 度に引き、記事 ID ごとにまとめる。

    記事ごとに 1 クエリを投げると 1 ページ分で件数と同じ回数の往復が生じるため、まとめて引く。
    """
    if not article_ids:
        return {}
    placeholders = ', '.join(['%s'] * len(article_ids))
    rows = await db.fetch_all(
        f'''
        SELECT atg.article_id AS article_id, t.id AS id, t.slug AS slug, t.name AS name,
               t.description AS description, t.icon_url AS icon_url, t.notify_channel_id AS notify_channel_id
        FROM article_tags atg
        JOIN tags t ON t.id = atg.tag_id
        WHERE atg.article_id IN ({placeholders})
        ORDER BY t.name
        ''',
        *article_ids,
    )
    grouped: dict[int, list[TagOut]] = {}
    for row in rows:
        grouped.setdefault(int(row['article_id']), []).append(TagOut.model_validate(row))
    return grouped


def _article_list_item(row: Mapping[str, Any], tags: list[TagOut]) -> ArticleListItem:
    """一覧クエリの 1 行を ArticleListItem へ写す。"""
    publisher: PublisherOut | None = None
    if row['publisher_id'] is not None:
        publisher = PublisherOut(
            id=row['publisher_id'],
            role_id=row['publisher_role_id'],
            slug=row['publisher_slug'],
            name=row['publisher_name'],
            description=row['publisher_description'],
            logo_url=row['publisher_logo_url'],
            stance=row['publisher_stance'],
        )
    series: SeriesBrief | None = None
    if row['series_id'] is not None:
        series = SeriesBrief(id=row['series_id'], slug=row['series_slug'], title=row['series_title'])
    return ArticleListItem(
        id=row['id'],
        slug=row['slug'],
        title=row['title'],
        excerpt=row['excerpt'],
        cover_image_url=row['cover_image_url'],
        author=UserBrief(
            id=row['author_id'],
            discord_user_id=row['author_discord_user_id'],
            username=row['author_username'],
            global_name=row['author_global_name'],
            avatar_hash=row['author_avatar_hash'],
        ),
        publisher=publisher,
        category=CategoryOut(
            id=row['category_id'],
            slug=row['category_slug'],
            name=row['category_name'],
            position=row['category_position'],
            notify_channel_id=row['category_notify_channel_id'],
            include_in_latest=bool(row['category_include_in_latest']),
        ),
        series=series,
        series_position=row['series_position'],
        tags=tags,
        visibility=row['visibility'],
        byline_mode=row['byline_mode'],
        is_breaking=bool(row['is_breaking']),
        is_sponsored=bool(row['is_sponsored']),
        sponsor_name=row['sponsor_name'],
        status=row['status'],
        view_count=int(row['view_count']),
        # SUM() は MySQL では DECIMAL を返すため int へ寄せる。
        up_count=int(row['up_count']),
        down_count=int(row['down_count']),
        # 差し引きはここで求める。行の列どうしを SQL で引くと符号なしの引き算になり、反対が賛成を上回った瞬間に文が失敗する。
        score=int(row['up_count']) - int(row['down_count']),
        comment_count=int(row['comment_count']),
        published_at=row['published_at'],
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


async def fetch_article_page(
    db: Database,
    *,
    where: str,
    where_args: Sequence[Any],
    order_by: str,
    order_args: Sequence[Any] = (),
    joins: str = '',
    page: int,
    per_page: int = DEFAULT_PER_PAGE,
) -> Page[ArticleListItem]:
    """条件に合う記事の 1 ページ分を、一覧の表現で返す。

    where / order_by / joins は呼び出し側がモジュール内の定数として書く SQL 断片であり、要求から来た値を混ぜてはならない。値は where_args / order_args としてプレースホルダで渡す。

    総件数は同じ条件の COUNT(*) で数える。件数のクエリに署名・分類の結合は要らない(内部結合は NOT NULL の外部キーが相手で件数を変えず、外部結合はそもそも件数を変えない)ため、絞り込みに使う joins だけを渡す。
    """
    total = int(await db.fetch_val(f'SELECT COUNT(*) FROM articles {joins} WHERE {where}', *where_args) or 0)
    offset = (page - 1) * per_page
    rows: list[dict[str, Any]] = []
    # 総件数を超えた頁は行が無いことが確定しているため、行のクエリ自体を投げない。
    if offset < total:
        rows = await db.fetch_all(
            f'SELECT {_ARTICLE_COLUMNS} {_ARTICLE_JOINS} {joins} WHERE {where} ORDER BY {order_by} LIMIT %s OFFSET %s',
            *where_args,
            *order_args,
            per_page,
            offset,
        )
    tags_by_article = await _fetch_tags_for_articles(db, [int(row['id']) for row in rows])
    items = [_article_list_item(row, tags_by_article.get(int(row['id']), [])) for row in rows]
    return Page[ArticleListItem](
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        has_next=offset + len(items) < total,
    )


# --------------------------------------------------------------------------
# カテゴリ
# --------------------------------------------------------------------------


@router.get('/api/categories', response_model=list[CategoryListItem])
async def list_categories(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> list[CategoryListItem]:
    """カテゴリの一覧。ナビゲーションの並び順(position)で返し、記事数を添える。"""
    where, args = published_and_visible(principal)
    rows = await db.fetch_all(
        f'''
        SELECT c.id AS id, c.slug AS slug, c.name AS name, c.position AS position,
               c.notify_channel_id AS notify_channel_id, c.include_in_latest AS include_in_latest,
               (SELECT COUNT(*) FROM articles WHERE articles.category_id = c.id AND {where}) AS article_count
        FROM categories c
        ORDER BY c.position, c.name
        ''',
        *args,
    )
    return [CategoryListItem.model_validate(row) for row in rows]


# --------------------------------------------------------------------------
# タグ
# --------------------------------------------------------------------------

_TAG_COLUMNS: Final = '''
    t.id AS id, t.slug AS slug, t.name AS name, t.description AS description,
    t.icon_url AS icon_url, t.notify_channel_id AS notify_channel_id
'''


def _tag_article_count(where: str) -> str:
    """タグに紐づく、閲覧できる公開済み記事の件数を数える相関副問い合わせ。"""
    return f'''
        (SELECT COUNT(*) FROM article_tags atg JOIN articles ON articles.id = atg.article_id
         WHERE atg.tag_id = t.id AND {where})
    '''


@router.get('/api/tags', response_model=list[TagDetailOut])
async def list_tags(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> list[TagDetailOut]:
    """タグの一覧。記事数の多い順に返し、同数は名前順で並べる。"""
    where, args = published_and_visible(principal)
    rows = await db.fetch_all(
        f'''
        SELECT {_TAG_COLUMNS}, {_tag_article_count(where)} AS article_count
        FROM tags t
        ORDER BY article_count DESC, t.name
        ''',
        *args,
    )
    return [TagDetailOut.model_validate(row) for row in rows]


@router.get('/api/tags/{slug}', response_model=TagDetailOut)
async def get_tag(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> TagDetailOut:
    """タグ 1 件。タグページ(話題の特設ページ)の見出しに使う。"""
    where, args = published_and_visible(principal)
    row = await db.fetch_one(
        f'''
        SELECT {_TAG_COLUMNS}, {_tag_article_count(where)} AS article_count
        FROM tags t
        WHERE t.slug = %s
        ''',
        *args,
        slug,
    )
    if row is None:
        raise NotFound('そのタグは存在しません。')
    return TagDetailOut.model_validate(row)


@router.get('/api/tags/{slug}/timeline', response_model=Page[ArticleListItem])
async def tag_timeline(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    page: Annotated[int, Query(ge=1)] = 1,
) -> Page[ArticleListItem]:
    """そのタグの記事を published_at の昇順で辿る年表。

    新着一覧と違って古い順に並べるのは、この一覧が「最新の出来事」ではなく「その話題の経緯」を読むためのものだからである。地政学的な話題では、建国・領土変更・条約締結が時系列に並ぶこと自体に価値があり、降順ではその筋道が逆から提示されてしまう。同時刻の記事は ID の昇順で並べ、頁をまたいで順序が揺れないようにする。
    """
    tag_id = await db.fetch_val('SELECT id FROM tags WHERE slug = %s', slug)
    if tag_id is None:
        raise NotFound('そのタグは存在しません。')
    where, args = published_and_visible(principal)
    return await fetch_article_page(
        db,
        joins='JOIN article_tags atg ON atg.article_id = articles.id',
        where=f'atg.tag_id = %s AND {where}',
        where_args=[tag_id, *args],
        order_by='articles.published_at ASC, articles.id ASC',
        page=page,
    )


# --------------------------------------------------------------------------
# 報道社
# --------------------------------------------------------------------------

_PUBLISHER_COLUMNS: Final = '''
    p.id AS id, p.role_id AS role_id, p.slug AS slug, p.name AS name,
    p.description AS description, p.logo_url AS logo_url, p.stance AS stance
'''


@router.get('/api/publishers', response_model=list[PublisherOut])
async def list_publishers(db: Annotated[Database, Depends(get_db)]) -> list[PublisherOut]:
    """報道社の一覧。解散した報道社(role_id が NULL)も過去記事の署名として残るため落とさない。"""
    rows = await db.fetch_all(f'SELECT {_PUBLISHER_COLUMNS} FROM publishers p ORDER BY p.name')
    return [PublisherOut.model_validate(row) for row in rows]


async def _publisher_members(db: Database, role_id: int | None) -> list[UserBrief]:
    """報道社に所属する記者。所属は Discord のロールで表すため、その role_id を保有する利用者を引く。

    guild を抜けた利用者は user_roles の行が残りうるため、現に参加している者だけを名簿とする。role_id が NULL の報道社は解散しており、所属者は居ない。
    """
    if role_id is None:
        return []
    rows = await db.fetch_all(
        '''
        SELECT u.id AS id, u.discord_user_id AS discord_user_id, u.username AS username,
               u.global_name AS global_name, u.avatar_hash AS avatar_hash
        FROM user_roles ur
        JOIN users u ON u.id = ur.user_id
        WHERE ur.role_id = %s AND u.is_guild_member = 1
        ORDER BY u.username
        ''',
        role_id,
    )
    return [UserBrief.model_validate(row) for row in rows]


@router.get('/api/publishers/{slug}', response_model=PublisherPageOut)
async def get_publisher(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> PublisherPageOut:
    """報道社のプロフィール・所属記者・実績・記事一覧。

    実績は要求元が閲覧できる公開済みの記事だけで数える。全件で数えると、読めない記事の存在と反応の大きさが数字として漏れるうえ、同じページに並ぶ記事一覧の件数と実績の記事数が食い違う。
    """
    publisher_row = await db.fetch_one(f'SELECT {_PUBLISHER_COLUMNS} FROM publishers p WHERE p.slug = %s', slug)
    if publisher_row is None:
        raise NotFound('その報道社は存在しません。')
    publisher_id = int(publisher_row['id'])
    where, args = published_and_visible(principal)
    # 点数はランキングと同じ規則で求める。ここだけ別の式にすると、報道社の面に出る点数とランキングでのその報道社の点数が食い違う。式そのものを rankings から借りるのはそのためであり、記事ごとに畳んでから合計する 2 段の形も同じ理由による(規則は rankings のモジュール docstring を参照)。
    # ランキングと違い、対象は閲覧者が読める記事に限り(published_and_visible)、期間で区切らない。報道社の面はその報道社の実績の全体を示す場所である。
    stats_row = await db.fetch_one(
        f'''
        SELECT COUNT(*) AS article_count,
               COALESCE(SUM(a.up_votes), 0) AS up_count,
               COALESCE(SUM(a.down_votes), 0) AS down_count,
               COALESCE(SUM(a.comments), 0) AS comment_count,
               COALESCE(SUM({ARTICLE_SCORE_SQL}), 0) AS score
        FROM ({article_totals_sql(f'articles.publisher_id = %s AND {where}')}) a
        ''',
        publisher_id,
        *args,
    )
    stats = PublisherStats(
        article_count=int(stats_row['article_count']) if stats_row else 0,
        up_count=int(stats_row['up_count']) if stats_row else 0,
        down_count=int(stats_row['down_count']) if stats_row else 0,
        comment_count=int(stats_row['comment_count']) if stats_row else 0,
        score=int(stats_row['score']) if stats_row else 0,
    )
    members = await _publisher_members(db, publisher_row['role_id'])
    articles = await fetch_article_page(
        db,
        where=f'articles.publisher_id = %s AND {where}',
        where_args=[publisher_id, *args],
        order_by='articles.published_at DESC, articles.id DESC',
        page=1,
        per_page=DETAIL_ARTICLES_LIMIT,
    )
    return PublisherPageOut(**publisher_row, members=members, stats=stats, articles=articles.items)


# --------------------------------------------------------------------------
# 連載
# --------------------------------------------------------------------------

_SERIES_COLUMNS: Final = '''
    s.id AS id, s.slug AS slug, s.title AS title, s.description AS description,
    s.cover_image_url AS cover_image_url, s.created_at AS created_at, s.updated_at AS updated_at,
    o.id AS owner_id, o.discord_user_id AS owner_discord_user_id, o.username AS owner_username,
    o.global_name AS owner_global_name, o.avatar_hash AS owner_avatar_hash
'''

_SERIES_JOINS: Final = 'FROM series s JOIN users o ON o.id = s.owner_id'


def _series_out(row: Mapping[str, Any]) -> dict[str, Any]:
    """連載の行を、owner を入れ子にした形へ写す。"""
    return {
        'id': row['id'],
        'slug': row['slug'],
        'title': row['title'],
        'description': row['description'],
        'cover_image_url': row['cover_image_url'],
        'owner': UserBrief(
            id=row['owner_id'],
            discord_user_id=row['owner_discord_user_id'],
            username=row['owner_username'],
            global_name=row['owner_global_name'],
            avatar_hash=row['owner_avatar_hash'],
        ),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
    }


@router.get('/api/series', response_model=list[SeriesOut])
async def list_series(db: Annotated[Database, Depends(get_db)]) -> list[SeriesOut]:
    """連載の一覧。更新の新しい順に返す。"""
    rows = await db.fetch_all(f'SELECT {_SERIES_COLUMNS} {_SERIES_JOINS} ORDER BY s.updated_at DESC, s.id DESC')
    return [SeriesOut.model_validate(_series_out(row)) for row in rows]


@router.get('/api/series/{slug}', response_model=SeriesDetailOut)
async def get_series(
    slug: str,
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
) -> SeriesDetailOut:
    """連載 1 件と、その収録記事。

    連載は順序を持つまとまりであるため、記事は series_position の昇順で並べる。position が未設定の記事は末尾へ回し、その中では公開の古い順とする(連載は第 1 回から読むものであり、新着順に並べると筋が逆になる)。
    """
    row = await db.fetch_one(f'SELECT {_SERIES_COLUMNS} {_SERIES_JOINS} WHERE s.slug = %s', slug)
    if row is None:
        raise NotFound('その連載は存在しません。')
    where, args = published_and_visible(principal)
    articles = await fetch_article_page(
        db,
        where=f'articles.series_id = %s AND {where}',
        where_args=[int(row['id']), *args],
        order_by='articles.series_position IS NULL, articles.series_position ASC, articles.published_at ASC, articles.id ASC',
        page=1,
        per_page=DETAIL_ARTICLES_LIMIT,
    )
    return SeriesDetailOut.model_validate({**_series_out(row), 'articles': articles.items})
