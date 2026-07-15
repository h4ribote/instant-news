"""記事の保存・公開のサービス層。

このモジュールは HTTP を知らない。ルータからもバックグラウンドの予約公開ループからも同じ呼び出しで使えるようにするためである。送出する例外は errors.py の AppError 派生であり、HTTP への変換は境界(例外ハンドラ)が行う。

保存と公開で Discord ロールの扱いが正反対である点がこのモジュールの要である。保存時の資格判定(誰の名義で書けるか・速報を立てられるか)は現在のロールを都度参照するが、公開時に決まる署名(articles.publisher_id)は凍結し、以後ロールが変わっても動かさない。誰がいつどの社の名前で報じたかは過去の事実であって、現在の所属の関数ではない。

画像の取り込みは公開時ではなく保存のたびに行う。Discord の CDN URL は約 24 時間で失効するため、公開時まで待つと下書きのまま放置された記事は失効後の取得になり、そのときには元の画像がもう存在しない。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Final

from aiomysql import Connection

from common.config import Config, effective_media_config
from common.db import Database
from common.settings import RuntimeSettings
from common.snowflake import to_snowflake

from .errors import ArticleError, ArticleForbidden, Conflict, NotFound, RateLimited
from .markdown import render_markdown
from .media.service import IngestFailure, ingest_article_images
from .principals import Principal, has_scope, is_admin
from .schemas import ArticleWrite, visible_text

# ArticleError / ArticleForbidden は errors.py で定義したものをそのまま用いる。例外から HTTP への変換を errors.py へ集約する方針のもとでは、同名の別クラスをここで定義すると例外ハンドラの対応表から外れ、業務上の規則違反が 400 や 403 ではなく 500 として返る。
__all__ = [
    'ArticleError',
    'ArticleForbidden',
    'has_write_any',
    'ensure_may_edit',
    'save_article',
    'publish_article',
    'resolve_notify_channels',
    'enqueue_notifications',
]

logger = logging.getLogger(__name__)

# slug に残す文字。Unicode の語構成文字(日本語を含む)と - . のみを残し、それ以外は - へ潰す。URL のパス断片としてそのまま出るため、空白や区切りに化ける文字を残さない。
_SLUG_STRIP_RE: Final = re.compile(r'[^\w.-]+', re.UNICODE)
# 連番の接尾辞(-2 など)を足しても articles.slug の VARCHAR(200) に収まるようにする。
_SLUG_BASE_MAX_LENGTH: Final = 190
# タイトルが記号だけで構成されていて slug が空になった場合の代替。
_SLUG_FALLBACK: Final = 'article'
# 自動生成の slug が衝突し続けた場合に打ち切る上限。
_SLUG_MAX_ATTEMPTS: Final = 1000

# 記事の保存で参照する既存行の列。
_EXISTING_COLUMNS: Final = (
    'id, slug, author_id, publisher_id, category_id, series_id, series_position, title, excerpt, body_md, '
    'cover_image_url, status, visibility, byline_mode, is_breaking, is_sponsored, sponsor_name, scheduled_at, published_at'
)


# --------------------------------------------------------------------------
# 小さな補助
# --------------------------------------------------------------------------


def _specified(payload: ArticleWrite, name: str) -> bool:
    """その項目が要求で明示的に指定されたか。

    ArticleWrite は作成と部分更新の両方で使うため全項目が省略可能であり、「省略された」と「明示的に null を指定した」を値だけでは区別できない。前者は既存値の維持、後者は消去であって意味が正反対になる。
    """
    return name in payload.model_fields_set


def _merged(payload: ArticleWrite, existing: dict[str, Any] | None, name: str, default: Any = None) -> Any:
    """指定されていればその値、されていなければ既存値、既存も無ければ既定値を返す。"""
    if _specified(payload, name):
        return getattr(payload, name)
    if existing is not None:
        return existing[name]
    return default


def _merged_id(payload: ArticleWrite, existing: dict[str, Any] | None, name: str) -> int | None:
    """ID の項目を int へ正規化して合成する。要求では文字列、DB では BIGINT で扱う。"""
    if _specified(payload, name):
        value = getattr(payload, name)
        return None if value is None else to_snowflake(value)
    if existing is not None:
        return existing[name]
    return None


def _flag(value: Any) -> int:
    """TINYINT(1) の列へ渡す 0/1 にする。DB からは int、要求からは bool で来る。"""
    return 1 if value else 0


def _slugify(title: str) -> str:
    """タイトルから slug の素を作る。

    日本語のタイトルを ASCII へ音訳する術は無く、落とすと slug が空になるため、Unicode の語構成文字はそのまま残す(ブラウザとサーバーの双方がパーセント符号化を透過的に扱う)。
    """
    normalized = unicodedata.normalize('NFKC', title).lower()
    slug = _SLUG_STRIP_RE.sub('-', normalized).strip('-.')
    if len(slug) > _SLUG_BASE_MAX_LENGTH:
        slug = slug[:_SLUG_BASE_MAX_LENGTH].rstrip('-.')
    # '.' や '..' はパスの断片として特別な意味を持つため、そのまま slug にしてはならない。
    if not slug or slug in ('.', '..'):
        return _SLUG_FALLBACK
    return slug


# --------------------------------------------------------------------------
# 資格の判定
# --------------------------------------------------------------------------


async def has_write_any(db: Database, settings: RuntimeSettings, principal: Principal) -> bool:
    """著者・報道社を自由に指定できる主体か。

    API キー経由では、スコープが付いているだけでは足りず、要求の時点で所有者が現に管理者資格を満たしていなければならない(発行時の資格を凍結しないため、管理者を外れたユーザーの古いキーはその時点で通らなくなる)。セッション経由では管理者であることがそのまま同じ力になる。

    管理者が所有する articles:write のキーに対してもこの判定を通す必要がある。所有者が管理者であることだけを見ると、著者を所有者に固定するはずのキーが他人の記事へ手を伸ばせてしまい、スコープの区別が消える。
    """
    if principal.via_token:
        return has_scope(principal, 'articles:write:any') and await is_admin(db, settings, principal)
    return await is_admin(db, settings, principal)


def ensure_may_edit(principal: Principal, existing: dict[str, Any], *, write_any: bool) -> None:
    """既存の記事を編集・公開できる主体かを検査する。著者本人か、著者を自由に指定できる主体だけが操作できる。"""
    if write_any:
        return
    if existing['author_id'] != principal.user_id:
        raise ArticleForbidden('他の利用者が著者の記事は編集できません。')


async def _ensure_within_rate_limit(db: Database, settings: RuntimeSettings, principal: Principal, *, conn: Connection | None = None) -> None:
    """API キー 1 本あたりの作成・更新の上限を検査する。

    キー単位であってユーザー単位でもエンドポイント単位でもない。自動投稿は本質的に暴走しうるため、上限に達したキーだけが止まり、同じ人間の他のキーや Web UI からの投稿は影響を受けないようにする。人間の手による投稿はこの意味では暴走しないため、セッション経由は対象外である。

    conn を渡した呼び出しが上限の実際の担保である。キーの行を FOR UPDATE で確保してから数えることで、同じキーからの保存が直列化され、数えた後・改版を挿入する前に他の要求が割り込めなくなる。確保せずに数えると、並行して届いた全ての要求が挿入前の同じ値を読み、上限 60 に対して同時に投げた 500 件が全て通る(上限は直列な要求に対してしか効かなくなる)。トークンの行がキー単位のミューテックスとして働く。

    conn を渡さない呼び出しは、外部ホストからの画像の取得という重い処理を、どのみち弾く要求のために走らせないための早い段階での目安である。ここを通っても上限に達していないことは保証されない。
    """
    if principal.token_id is None:
        return
    if conn is not None:
        await db.fetch_val('SELECT id FROM api_tokens WHERE id = %s FOR UPDATE', principal.token_id, conn=conn)
    limit = settings.articles.api_rate_limit_per_hour
    row = await db.fetch_one(
        'SELECT COUNT(*) AS used, COALESCE(TIMESTAMPDIFF(SECOND, NOW(), MIN(created_at) + INTERVAL 1 HOUR), 0) AS retry_after '
        'FROM article_revisions WHERE via_token_id = %s AND created_at >= NOW() - INTERVAL 1 HOUR',
        principal.token_id,
        conn=conn,
    )
    used = int(row['used']) if row else 0
    if used < limit:
        return
    # 窓から最も古い改版が外れるまでの秒数。既に外れている(境界で 0 以下になった)場合も、即時の再試行を促さないよう 1 秒は空ける。
    retry_after = max(int(row['retry_after']) if row else 1, 1)
    raise RateLimited(
        f'このキーの記事の作成・更新は 1 時間あたり {limit} 件までです。',
        retry_after=retry_after,
        details={'limit': limit, 'used': used},
    )


async def _resolve_author_id(db: Database, principal: Principal, payload: ArticleWrite, existing: dict[str, Any] | None, *, write_any: bool) -> int:
    """著者を決める。

    articles:write のキーは著者をキーの所有者に固定する。このキーで得られる力を「その人が Web UI から手で投稿するのと同じ」に留めるためであり、他人を著者に指定できるのは articles:write:any(および管理者のセッション)だけである。
    """
    current = existing['author_id'] if existing is not None else principal.user_id
    if not _specified(payload, 'author_id') or payload.author_id is None:
        return current
    requested = to_snowflake(payload.author_id)
    if requested == current:
        return current
    if not write_any:
        raise ArticleForbidden('他の利用者を著者に指定する権限がありません。')
    if await db.fetch_val('SELECT id FROM users WHERE id = %s', requested) is None:
        raise ArticleError(f'著者に指定された利用者が存在しません: {requested}')
    return requested


async def _ensure_may_sign_publisher(db: Database, principal: Principal, publisher_id: int, *, write_any: bool) -> None:
    """その報道社を名乗れる主体かを検査する。所属を表すロールを保有していることが条件である。"""
    if write_any:
        return
    role_id = await db.fetch_val('SELECT role_id FROM publishers WHERE id = %s', publisher_id)
    if role_id is None or role_id not in principal.role_ids:
        raise ArticleForbidden('この報道社を名乗る権限がありません。所属を表すロールが必要です。')


def _ensure_byline_unfrozen(existing: dict[str, Any] | None, author_id: int, publisher_id: int | None) -> None:
    """公開済みの記事の署名(著者と報道社)を変える要求を拒む。

    公開の時点で「著者がその報道社のロールを実際に持っているか」を検証して署名を確定させており(_freeze_publisher)、それが成り立つのは以後この対が動かないことを前提にしている。公開後に片方だけでも書き換えられると、検証を経ていない対が「検証済みの署名」として記事に残る。公開してから報道社を付ける、あるいは正当に公開した記事の著者だけを他人に差し替える、という順序で検証を素通りできてはならない。

    誰がいつどの社の名前で報じたかは過去の事実であり、著者を自由に指定できる主体であっても後から書き換える正当な理由が無い。値の変わらない指定(要求元が記事全体を送り返す形の更新)は素通しするため、公開済みの記事の本文や見出しの訂正は妨げない。
    """
    if existing is None or existing['published_at'] is None:
        return
    if publisher_id != existing['publisher_id']:
        raise ArticleForbidden('公開済みの記事の報道社は変更できません。署名は公開の時点で確定します。')
    if author_id != existing['author_id']:
        raise ArticleForbidden('公開済みの記事の著者は変更できません。署名は公開の時点で確定します。')


def _ensure_may_flag_breaking(settings: RuntimeSettings, principal: Principal) -> None:
    """速報を立てられる主体かを検査する。

    設定が空リストであれば誰も立てられない。これは設定の未記入ではなく「速報を使わない」という有効な意思であり、既定で誰でも立てられる側へ倒してはならない。
    """
    allowed = settings.articles.breaking.allowed_role_ids
    if not allowed or principal.role_ids.isdisjoint(allowed):
        raise ArticleForbidden('速報を立てる権限がありません。')


# --------------------------------------------------------------------------
# 参照先の解決
# --------------------------------------------------------------------------


async def _resolve_tag_ids(db: Database, slugs: list[str]) -> list[int]:
    """タグの slug を ID へ解決する。存在しない slug はエラーにする。

    記事の投稿でタグを勝手に増やさない。タグの新設は taxonomy:admin の管轄であり、綴りの誤りが黙って新しいタグを生むと、分類は静かに壊れていく。
    """
    if not slugs:
        return []
    placeholders = ', '.join(['%s'] * len(slugs))
    rows = await db.fetch_all(f'SELECT id, slug FROM tags WHERE slug IN ({placeholders})', *slugs)
    found = {row['slug']: row['id'] for row in rows}
    missing = [slug for slug in slugs if slug not in found]
    if missing:
        raise ArticleError(f'存在しないタグが指定されました: {", ".join(missing)}')
    return [found[slug] for slug in slugs]


async def _resolve_visibility_role_ids(db: Database, role_ids: list[str]) -> list[int]:
    """閲覧を許可するロールを検証して int の一覧にする。同期済みのロールでなければエラーにする。"""
    if not role_ids:
        return []
    ids = [to_snowflake(value) for value in role_ids]
    placeholders = ', '.join(['%s'] * len(ids))
    rows = await db.fetch_all(f'SELECT id FROM discord_roles WHERE id IN ({placeholders})', *ids)
    found = {row['id'] for row in rows}
    missing = [str(value) for value in ids if value not in found]
    if missing:
        raise ArticleError(f'存在しないロールが指定されました: {", ".join(missing)}')
    return ids


async def _unique_slug(db: Database, conn: Connection, base: str, *, exclude_id: int | None) -> str:
    """base をもとに未使用の slug を作る。衝突したら連番を付す。"""
    candidate = base
    for attempt in range(1, _SLUG_MAX_ATTEMPTS + 1):
        taken = await db.fetch_val(
            'SELECT id FROM articles WHERE slug = %s AND (%s IS NULL OR id <> %s)',
            candidate,
            exclude_id,
            exclude_id,
            conn=conn,
        )
        if taken is None:
            return candidate
        candidate = f'{base}-{attempt + 1}'
    raise Conflict('slug を決められませんでした。タイトルを変えるか slug を明示してください。')


async def _ensure_slug_free(db: Database, conn: Connection, slug: str, *, exclude_id: int | None) -> None:
    """明示された slug が空いているかを検査する。自動生成と違い、黙って連番を付けて別の URL にしてはならない。"""
    taken = await db.fetch_val(
        'SELECT id FROM articles WHERE slug = %s AND (%s IS NULL OR id <> %s)',
        slug,
        exclude_id,
        exclude_id,
        conn=conn,
    )
    if taken is not None:
        raise Conflict(f'この slug は既に使われています: {slug}')


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------


async def save_article(
    db: Database,
    config: Config,
    settings: RuntimeSettings,
    principal: Principal,
    *,
    article_id: int | None,
    payload: ArticleWrite,
) -> tuple[int, list[IngestFailure]]:
    """記事を作成または更新する。戻り値は (記事 ID, 取り込めなかった画像の一覧)。

    article_id が None なら作成、そうでなければ更新である。取り込めなかった画像は例外にせず戻り値へ載せる。外部ホストの一時的な不調で記事が出せなくなってはならない一方、取り込めなかった URL は本文にそのまま残り、いずれ失効して画像が消えるため、黙って成功として扱わず必ず呼び出し元へ伝える。

    config と settings の両方を受け取るのは、画像の取り込みに要る設定の出どころが分かれているためである。保存先(storage_dir)は実行環境と一体で config.yaml が持ち、各上限は管理者が変更できる settings 表が持つ。effective_media_config がこの 2 つを 1 つに畳む。
    """
    existing: dict[str, Any] | None = None
    if article_id is not None:
        existing = await db.fetch_one(f'SELECT {_EXISTING_COLUMNS} FROM articles WHERE id = %s', article_id)
        if existing is None:
            raise NotFound('記事が見つかりません。')

    write_any = await has_write_any(db, settings, principal)
    if existing is not None:
        ensure_may_edit(principal, existing, write_any=write_any)
    # 早い段階での目安。上限に達しているキーのために外部ホストからの画像の取得を走らせないためだけのものであり、上限そのものはトランザクション内の検査が担保する。
    await _ensure_within_rate_limit(db, settings, principal)

    author_id = await _resolve_author_id(db, principal, payload, existing, write_any=write_any)

    title = _merged(payload, existing, 'title')
    if not title:
        raise ArticleError('title は必須です。')
    body_md = _merged(payload, existing, 'body_md')
    if body_md is None:
        raise ArticleError('body_md は必須です。')

    category_id = _merged_id(payload, existing, 'category_id')
    if category_id is None:
        raise ArticleError('category_id は必須です。')
    if _specified(payload, 'category_id') or existing is None:
        if await db.fetch_val('SELECT id FROM categories WHERE id = %s', category_id) is None:
            raise ArticleError(f'存在しないカテゴリが指定されました: {category_id}')

    publisher_id = _merged_id(payload, existing, 'publisher_id')
    _ensure_byline_unfrozen(existing, author_id, publisher_id)
    # 資格の検査は署名を実際に変える要求に限る。値の変わらない指定(要求元が記事全体を送り返す形の更新)まで弾くと、記事に触れられる主体が名乗れる報道社の現在の保有者だけになり、解散した報道社の過去記事を管理者が直せなくなる。
    # 著者が変わる場合も署名は変わる。報道社の値が同じでも、その社を名乗る人物が入れ替わるのは新しい署名を作ることであり、素通しすると「報道社を変えず著者だけを差し替える」だけで検査が消える。
    signature_changed = existing is None or publisher_id != existing['publisher_id'] or author_id != existing['author_id']
    if publisher_id is not None and signature_changed:
        if await db.fetch_val('SELECT id FROM publishers WHERE id = %s', publisher_id) is None:
            raise ArticleError(f'存在しない報道社が指定されました: {publisher_id}')
        await _ensure_may_sign_publisher(db, principal, publisher_id, write_any=write_any)

    series_id = _merged_id(payload, existing, 'series_id')
    if series_id is not None and (existing is None or series_id != existing['series_id']):
        if await db.fetch_val('SELECT id FROM series WHERE id = %s', series_id) is None:
            raise ArticleError(f'存在しない連載が指定されました: {series_id}')

    visibility = _merged(payload, existing, 'visibility', 'public')
    status = _merged(payload, existing, 'status', 'draft')
    scheduled_at = _merged(payload, existing, 'scheduled_at')
    if status == 'scheduled' and scheduled_at is None:
        raise ArticleError('status が scheduled のときは scheduled_at が必須です。')

    is_breaking = bool(_merged(payload, existing, 'is_breaking', False))
    # 検査は立てる要求に限る。既に立っている速報の本文を直すたびに権限を問うと、速報を出せる者しか訂正できなくなる。
    if is_breaking and not (existing is not None and existing['is_breaking']):
        _ensure_may_flag_breaking(settings, principal)

    is_sponsored = bool(_merged(payload, existing, 'is_sponsored', False))
    sponsor_name = _merged(payload, existing, 'sponsor_name')
    # 判定は「空白を落とすと空か」ではなく「表示可能な文字を含むか」で行う。str.strip() が落とすのは str.isspace() が真の文字だけであり、U+200B(ゼロ幅空白)や U+FEFF は素通りする。それらを名前にすると、記事面にも embed にも「広告」ラベルだけが出て広告主が誰なのかはどこにも表示されず、開示を要求した意味が消える。
    if is_sponsored and not (sponsor_name and visible_text(sponsor_name)):
        raise ArticleError('is_sponsored が真のときは sponsor_name が必須です。「広告」とだけ書いても開示になりません。')

    excerpt = _merged(payload, existing, 'excerpt')
    cover_image_url = _merged(payload, existing, 'cover_image_url')
    byline_mode = _merged(payload, existing, 'byline_mode', 'author')
    series_position = _merged(payload, existing, 'series_position')

    # tags / visibility_role_ids は指定されたときだけ洗い替える。None は「触れない」を意味する。
    tag_ids: list[int] | None = None
    if _specified(payload, 'tags'):
        tag_ids = await _resolve_tag_ids(db, payload.tags or [])
    elif existing is None:
        tag_ids = []

    visibility_role_ids: list[int] | None = None
    if _specified(payload, 'visibility_role_ids'):
        visibility_role_ids = await _resolve_visibility_role_ids(db, payload.visibility_role_ids or [])
    elif existing is None:
        visibility_role_ids = []

    if visibility == 'restricted':
        effective_roles = visibility_role_ids
        if effective_roles is None:
            rows = await db.fetch_all('SELECT role_id FROM article_visibility_roles WHERE article_id = %s', article_id)
            effective_roles = [row['role_id'] for row in rows]
        if not effective_roles:
            raise ArticleError('visibility が restricted のときは visibility_role_ids に 1 つ以上のロールが必要です。')

    # 画像の取り込みはトランザクションの外で行う。外部ホストへの取得は設定上の総所要時間まで掛かりうるため、その間トランザクションを開いたままにすると記事の行を長時間ロックし続ける。media は内容アドレスで重複排除され記事とは独立に存在するため、記事の保存が後で失敗しても不整合にならない(参照されない実体が残るだけである)。
    # uploaded_by は取り込みを実際に起こした主体にする。著者ではないのは、利用量の上限が資源を使った側に掛かるべきであり、articles:write:any で他人を著者にした投稿の取り込み分が、その他人の枠を削ってはならないためである。
    body_md, media_ids, failures = await ingest_article_images(
        db,
        effective_media_config(config, settings),
        body_md,
        uploaded_by=principal.user_id,
        via_token_id=principal.token_id,
    )
    body_html = render_markdown(body_md)

    async with db.transaction() as conn:
        # 上限の担保はここで行う。キーの行を確保してから数え、同じトランザクションで改版を挿入することで、数えた後に他の要求が割り込めなくなる。記事の行より先に確保するのは、ロックを取る順序をこの経路で常に(キー → 記事)に揃え、逆順で待ち合う組み合わせを作らないためである。
        await _ensure_within_rate_limit(db, settings, principal, conn=conn)

        if article_id is not None:
            # 記事の行をロックしてから改版番号を採る。同じ記事への保存が同時に走ると MAX(revision_number)+1 を同じ値で読み、(article_id, revision_number) の一意制約で片方が落ちる。
            if await db.fetch_val('SELECT id FROM articles WHERE id = %s FOR UPDATE', article_id, conn=conn) is None:
                raise NotFound('記事が見つかりません。')

        if _specified(payload, 'slug') and payload.slug:
            slug = payload.slug
            await _ensure_slug_free(db, conn, slug, exclude_id=article_id)
        elif existing is not None:
            # タイトルを直すたびにパーマリンクが変わると、既に配られた URL が壊れる。slug の変更は明示的な指定でだけ起こす。
            slug = existing['slug']
        else:
            slug = await _unique_slug(db, conn, _slugify(title), exclude_id=None)

        columns = (
            slug,
            author_id,
            publisher_id,
            category_id,
            series_id,
            series_position,
            title,
            excerpt,
            body_md,
            body_html,
            cover_image_url,
            status,
            visibility,
            byline_mode,
            _flag(is_breaking),
            _flag(is_sponsored),
            sponsor_name,
            scheduled_at,
        )
        if article_id is None:
            article_id = await db.execute_insert(
                'INSERT INTO articles (slug, author_id, publisher_id, category_id, series_id, series_position, title, excerpt, '
                'body_md, body_html, cover_image_url, status, visibility, byline_mode, is_breaking, is_sponsored, sponsor_name, scheduled_at) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                *columns,
                conn=conn,
            )
        else:
            await db.execute(
                'UPDATE articles SET slug = %s, author_id = %s, publisher_id = %s, category_id = %s, series_id = %s, '
                'series_position = %s, title = %s, excerpt = %s, body_md = %s, body_html = %s, cover_image_url = %s, '
                'status = %s, visibility = %s, byline_mode = %s, is_breaking = %s, is_sponsored = %s, sponsor_name = %s, '
                'scheduled_at = %s WHERE id = %s',
                *columns,
                article_id,
                conn=conn,
            )

        if tag_ids is not None:
            await db.execute('DELETE FROM article_tags WHERE article_id = %s', article_id, conn=conn)
            for tag_id in tag_ids:
                await db.execute('INSERT INTO article_tags (article_id, tag_id) VALUES (%s, %s)', article_id, tag_id, conn=conn)

        if visibility_role_ids is not None:
            await db.execute('DELETE FROM article_visibility_roles WHERE article_id = %s', article_id, conn=conn)
            for role_id in visibility_role_ids:
                await db.execute('INSERT INTO article_visibility_roles (article_id, role_id) VALUES (%s, %s)', article_id, role_id, conn=conn)

        # article_media は追記のみで、行を消してはならない。編集で本文から画像を外しても過去の改版はその画像を参照し続けており、消すと改版履歴を開いた瞬間に画像が 404 になる。
        for media_id in media_ids:
            await db.execute('INSERT IGNORE INTO article_media (article_id, media_id) VALUES (%s, %s)', article_id, media_id, conn=conn)

        revision_number = int(
            await db.fetch_val(
                'SELECT COALESCE(MAX(revision_number), 0) + 1 FROM article_revisions WHERE article_id = %s',
                article_id,
                conn=conn,
            )
        )
        # editor_id は「誰の名前で書かれたか」であり、その改版時点の署名(著者)を残す。「誰が実際に投稿したか」は via_token_id が担い、両者は別物である。articles:write:any が他人の署名で記事を出せる以上、後者が無いと説明責任が追えない。
        await db.execute(
            'INSERT INTO article_revisions (article_id, revision_number, editor_id, via_token_id, title, body_md, excerpt, note, is_correction) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            article_id,
            revision_number,
            author_id,
            principal.token_id,
            title,
            body_md,
            excerpt,
            payload.revision_note,
            _flag(payload.is_correction),
            conn=conn,
        )

        # status='published' での保存も公開である。行の status だけを書き換えて済ませると、署名の凍結も通知の待ち行列への追加も起こらないまま「公開された記事」が生まれる。公開の手順は 1 つに保つ。
        if status == 'published':
            await publish_article(db, settings, article_id, conn=conn)

    return article_id, failures


# --------------------------------------------------------------------------
# 公開
# --------------------------------------------------------------------------


async def _freeze_publisher(db: Database, conn: Connection, article_id: int, author_id: int, publisher_id: int | None) -> int | None:
    """公開の時点で署名を確定する。著者がその報道社のロールを実際に持っているかを検証する。

    保存時に検査するのは「書いている主体がその社を名乗れるか」であり、ここで検査するのは「署名される著者が実際にその社に所属しているか」である。両者は別であり、著者を自由に指定できる主体であっても、実在しない所属を署名にはできない。
    """
    if publisher_id is None:
        return None
    holds = await db.fetch_val(
        'SELECT p.id FROM publishers p JOIN user_roles ur ON ur.role_id = p.role_id WHERE p.id = %s AND ur.user_id = %s',
        publisher_id,
        author_id,
        conn=conn,
    )
    if holds is not None:
        return publisher_id
    # 公開を止めない。予約公開は背景のループが実行するため、ここで例外にすると記事は永遠に公開されないまま失敗を繰り返す。名乗れない署名を外して公開し、記録はログに残す。
    logger.warning(
        '記事 %s の署名を外しました。著者 %s は報道社 %s の所属ロールを保有していません。',
        article_id,
        author_id,
        publisher_id,
    )
    return None


async def publish_article(db: Database, settings: RuntimeSettings, article_id: int, *, conn: Connection | None = None) -> None:
    """記事を公開し、通知を待ち行列へ積む。

    再公開しても published_at を動かさない(COALESCE)。最初に公開された時刻は過去の事実であり、記事を直すたびに新しい記事として並び直してはならない。同じ理由で署名の凍結も最初の公開でだけ行い、以後は著者のロールが変わっても動かさない。

    通知を待ち行列へ積むのも最初の公開のときだけである。公開済みの記事の編集は新しい通知を生まない。積む宛先は記事のカテゴリとタグから毎回解決されるため、2 度目以降も積むと、公開後にタグを 1 つ足しただけで新しいチャンネルへ「新着記事」が流れ、そのタグの購読者には過去記事の DM が届く。(article_id, channel_id) の一意制約が防げるのは同じ宛先への二重送信だけであり、新しい宛先の行は素通りする。誤字の修正が読者への通知を生んではならない。

    conn を渡すと呼び出し元のトランザクションに乗る。通知の待ち行列への追加を公開と同じトランザクションで行うためにあり、分けると「公開されたのに誰にも通知されない記事」が生まれうる。
    """
    if conn is None:
        async with db.transaction() as owned:
            await publish_article(db, settings, article_id, conn=owned)
            return

    row = await db.fetch_one(
        'SELECT id, author_id, publisher_id, published_at FROM articles WHERE id = %s FOR UPDATE',
        article_id,
        conn=conn,
    )
    if row is None:
        raise NotFound('記事が見つかりません。')

    if row['published_at'] is not None:
        # 2 度目以降の公開。署名にも通知にも触れず、状態だけを戻す(アーカイブから戻す経路がここに当たる)。ここで凍結をやり直すと、記者が移籍した後の編集で過去の署名が遡って書き換わる。
        await db.execute("UPDATE articles SET status = 'published' WHERE id = %s", article_id, conn=conn)
        return

    publisher_id = await _freeze_publisher(db, conn, article_id, row['author_id'], row['publisher_id'])
    await db.execute(
        "UPDATE articles SET status = 'published', publisher_id = %s, published_at = COALESCE(published_at, NOW()) WHERE id = %s",
        publisher_id,
        article_id,
        conn=conn,
    )
    await enqueue_notifications(db, article_id, conn=conn)


# --------------------------------------------------------------------------
# 通知の待ち行列
# --------------------------------------------------------------------------


async def resolve_notify_channels(db: Database, article_id: int, *, conn: Connection | None = None) -> set[int]:
    """記事の通知先チャンネルの集合を返す。

    宛先はカテゴリとタグの notify_channel_id を重複排除した集合である。カテゴリの主チャンネルとタグの追加チャンネルが同じでも、1 チャンネルにつき 1 通しか送らない。

    宛先が 0 件なら空集合を返す。既定チャンネルへのフォールバックは持たない。フォールバックがあると「この欄は通知しない」という意図を表現できなくなり、notify_channel_id を NULL にしても記事が既定のチャンネルへ流れてしまう。
    """
    rows = await db.fetch_all(
        'SELECT c.notify_channel_id AS channel_id FROM articles a JOIN categories c ON c.id = a.category_id '
        'WHERE a.id = %s AND c.notify_channel_id IS NOT NULL '
        'UNION '
        'SELECT t.notify_channel_id AS channel_id FROM article_tags at JOIN tags t ON t.id = at.tag_id '
        'WHERE at.article_id = %s AND t.notify_channel_id IS NOT NULL',
        article_id,
        article_id,
        conn=conn,
    )
    return {int(row['channel_id']) for row in rows}


async def enqueue_notifications(db: Database, article_id: int, *, conn: Connection | None = None) -> None:
    """通知の宛先を待ち行列へ積む。実際の送信は bot が行う。

    server から bot へ直接イベントを送る方式は、bot が落ちている間のイベントを取りこぼす。DB を待ち行列にすれば、bot は復帰後に未送信の行を拾って送れる。

    いずれも INSERT IGNORE であり、同じ宛先への二重送信は (article_id, channel_id) と (article_id, user_id) の一意制約が構造的に防ぐ。一方、宛先そのものが増えた場合(公開後にタグを足した場合)に新しい行を防ぐのは一意制約ではなく、この関数を最初の公開でしか呼ばない publish_article 側の判断である。誤字修正での再通知を避けるためのフラグが要らないのは、この 2 つが揃っているためである。
    """
    for channel_id in sorted(await resolve_notify_channels(db, article_id, conn=conn)):
        await db.execute(
            'INSERT IGNORE INTO article_discord_posts (article_id, channel_id) VALUES (%s, %s)',
            article_id,
            channel_id,
            conn=conn,
        )

    # 購読者への DM はチャンネルへの通知とは独立に積む。宛先チャンネルを持たない記事でも購読は成立する。著者自身は対象から外す(自分が書いた記事の通知を自分が受け取る意味はない)。
    await db.execute(
        'INSERT IGNORE INTO article_subscription_notices (article_id, user_id) '
        'SELECT DISTINCT a.id, ts.user_id FROM articles a '
        'JOIN article_tags at ON at.article_id = a.id '
        'JOIN tag_subscriptions ts ON ts.tag_id = at.tag_id '
        'WHERE a.id = %s AND ts.user_id <> a.author_id',
        article_id,
        conn=conn,
    )
