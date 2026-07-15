"""静的な HTML ページの配信と、記事ページへの og メタの注入。

フロントエンドは静的な HTML と JavaScript であり、内容は JSON API から取得して描画する。しかし SNS や Discord のクローラは JavaScript を実行しないため、共有時のカードに何も出ない。記事ページだけはサーバー側で og メタを埋めてから返す。

**og:image と本文の抜粋を出すのは visibility='public' かつ status='published' の記事に限る。**下書き・レビュー中・予約・アーカイブや、members / restricted の記事は、タイトルを含めて一切出さない。og メタはクローラのために存在するものであり、認証を経ない相手へそのまま渡る。ここに未公開・限定記事の内容を載せると、URL を知るだけで中身が読めてしまう。

og:image に使えるのはカバー画像だけである。カバー画像は Discord embed に出すため認証なしで配信されるが、本文中の画像は記事と同じ閲覧判定を受けるため、クローラが取得すると 404 になる。
"""

from __future__ import annotations

import logging
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urljoin

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings

from .deps import get_config, get_db, get_principal_optional, get_settings
from .errors import AppError, NotFound
from .principals import Principal
from .visibility import can_view_article

logger = logging.getLogger(__name__)

router = APIRouter(tags=['pages'])

# 静的フロントエンドの置き場。server/app/pages.py から見て server/web/。
WEB_DIR: Final = Path(__file__).resolve().parent.parent / 'web'

# 記事ページのテンプレート内で、生成した og メタ群と差し替える目印。
OG_PLACEHOLDER: Final = '<!-- og:meta -->'

# <title> に添える名前。カードの表題とタブの表題を揃えるために使う。
SITE_NAME: Final = 'instant-news'

# og:description の長さ。これより長い説明は主要な SNS が途中で切るため、切られ方を相手任せにせずこちらで丸める。
DESCRIPTION_LIMIT: Final = 200

# テンプレートが持つ <title> を落とすためのもの。HTML の <title> は 1 つだけが有効であり、テンプレート側の表題が残ると、そちらが先に現れて記事の表題を上書きしてしまう。
# コメントと <title> を 1 つの選択肢として並べ、コメントは丸ごと読み飛ばす。テンプレートの注記が散文として <title> や </title> に言及していると、コメントを読み飛ばさない検索は、その言及から本物の </title> までを 1 つの要素と見なして巻き込む。挟まれたコメントの終わり(-->)や og の目印まで消え、以降の <link> や <script> が閉じ損ねたコメントに飲まれる。
_COMMENT_OR_TITLE: Final = re.compile(r'(?P<comment><!--.*?-->)|(?P<title><title\b[^>]*>.*?</title\s*>)', re.IGNORECASE | re.DOTALL)

# 地の文を取り出す際に、前後を空白で区切るブロック要素。区切らないと段落の末尾と次の段落の先頭が 1 語に繋がる。
_BLOCK_TAGS: Final = frozenset({'p', 'div', 'br', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'table', 'tr', 'td', 'th', 'hr'})


class _TextExtractor(HTMLParser):
    """レンダリング済みの本文 HTML から地の文だけを取り出す。

    抜粋(articles.excerpt)が無い記事の og:description を本文から作るために使う。本文の Markdown を自前で解釈すると、コードブロックの中身や記法の断片を拾ってしまうため、既にレンダリングとサニタイズを経た body_html を入力にする。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append(' ')

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append(' ')

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return ' '.join(''.join(self._parts).split())


def _plain_text(body_html: str) -> str:
    """本文 HTML を 1 行の地の文へ均す。解釈できない断片が混ざっていても例外にせず、取り出せた分を返す。"""
    parser = _TextExtractor()
    parser.feed(body_html)
    parser.close()
    return parser.text()


def _truncate(text: str, limit: int) -> str:
    """limit を超える文字列を丸め、省略記号を添える。"""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '…'


def _description(article: dict[str, Any]) -> str | None:
    """og:description に出す説明文。抜粋があればそれを使い、無ければ本文から作る。"""
    excerpt = (article.get('excerpt') or '').strip()
    if excerpt:
        return _truncate(' '.join(excerpt.split()), DESCRIPTION_LIMIT)
    body_html = article.get('body_html') or ''
    text = _plain_text(body_html) if body_html else ''
    return _truncate(text, DESCRIPTION_LIMIT) if text else None


def _meta_tag(attribute: str, name: str, content: str) -> str:
    """メタタグ 1 つ。属性値は必ずエスケープする。記事の表題も抜粋もカバーの URL も執筆者が書いた文字列であり、そのまま埋めると引用符を閉じて任意のタグを差し込める。"""
    return f'<meta {attribute}="{escape(name)}" content="{escape(content)}">'


def _is_shareable(article: dict[str, Any]) -> bool:
    """クローラへ内容を渡してよい記事か。誰でも閲覧できる公開済みの記事だけが該当する。"""
    return article.get('status') == 'published' and article.get('visibility') == 'public'


def _build_meta(config: Config, article: dict[str, Any]) -> str:
    """記事ページの head に入れる表題とメタタグ群を組み立てる。"""
    if not _is_shareable(article):
        # 未公開・限定記事。ここへ到達できるのは著者本人と管理者だけだが、その画面から内容がクローラや共有先へ流れないよう、表題も含めて何も出さない。
        return f'<title>{escape(SITE_NAME)}</title>\n<meta name="robots" content="noindex">'

    title = str(article.get('title') or '')
    url = f'{config.server.base_url}/articles/{quote(str(article["slug"]), safe="")}'
    tags = [
        f'<title>{escape(title)} | {escape(SITE_NAME)}</title>',
        _meta_tag('property', 'og:title', title),
        _meta_tag('property', 'og:url', url),
        _meta_tag('property', 'og:type', 'article'),
    ]
    description = _description(article)
    if description:
        tags.append(_meta_tag('property', 'og:description', description))
    cover = (article.get('cover_image_url') or '').strip()
    if cover:
        # 配信 URL は /media/... の絶対パスで入るため、公開 URL を前置して絶対 URL にする。外部ホストの絶対 URL はそのまま残る。
        tags.append(_meta_tag('property', 'og:image', urljoin(f'{config.server.base_url}/', cover)))
    tags.append(_meta_tag('name', 'twitter:card', 'summary_large_image'))
    return '\n'.join(tags)


def _strip_first_title(html: str) -> str:
    """テンプレートが持つ最初の <title> 要素を落とす。コメントの中身は要素と見なさない。"""
    removed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal removed
        # コメントはそのまま返す(og の目印もコメントであり、消してはならない)。
        if match.group('comment') is not None or removed:
            return match.group(0)
        removed = True
        return ''

    return _COMMENT_OR_TITLE.sub(_replace, html)


def _inject_meta(template: str, meta: str) -> str:
    """テンプレートの目印を生成したメタタグ群で置き換える。"""
    # テンプレート側の <title> は必ず落とす。残すと 2 つの <title> が並び、先に現れた方が採用される。順序に頼らず 1 つだけにしておけば、正しい HTML になり、素朴な走査を行う相手も記事の表題を拾える。
    stripped = _strip_first_title(template)
    if OG_PLACEHOLDER in stripped:
        return stripped.replace(OG_PLACEHOLDER, meta, 1)
    # 目印を失ったテンプレートでもメタを落とさない。黙って省くと、共有カードが空になったことに誰も気付けない。
    logger.warning('article.html に %s がありません。</head> の直前へ og メタを挿入します。', OG_PLACEHOLDER)
    if '</head>' in stripped:
        return stripped.replace('</head>', f'{meta}\n</head>', 1)
    logger.error('article.html に </head> がありません。og メタを先頭へ挿入します。')
    return f'{meta}\n{stripped}'


def _page_path(filename: str) -> Path:
    """配信するページの実体。存在しない場合は配備の誤りであり、利用者には 500 を返してログへ場所を残す(応答にファイルの場所を出さない)。"""
    path = WEB_DIR / filename
    if not path.is_file():
        logger.error('ページのテンプレートが見つかりません: %s', path)
        raise AppError()
    return path


def _read_template(filename: str) -> str:
    return _page_path(filename).read_text(encoding='utf-8')


def _serve(filename: str) -> FileResponse:
    """静的なページをそのまま返す。内容は JavaScript が API から取得して描画する。"""
    return FileResponse(_page_path(filename))


async def render_article_page(db: Database, config: Config, settings: RuntimeSettings, slug: str, principal: Principal | None) -> HTMLResponse:
    """記事ページを組み立てて返す。

    閲覧できない記事は 403 ではなく 404 を返す。403 は「その slug に何かがある」ことを教えてしまい、存在自体が秘密である限定公開の記事では、それだけで漏洩になる。

    config は公開 URL の組み立て(og:url / og:image)に、settings は閲覧範囲の判定に使う。前者は config.yaml、後者は settings 表と出どころが異なるため、両方を受け取る。
    """
    article = await db.fetch_one('SELECT * FROM articles WHERE slug = %s', slug)
    if article is None or not await can_view_article(db, principal, article, settings=settings):
        raise NotFound('記事が見つかりません。')
    return HTMLResponse(_inject_meta(_read_template('article.html'), _build_meta(config, article)))


# --------------------------------------------------------------------------
# 経路。API(/api/...)・画像(/media/...)・静的資産(/static/...)と衝突しないよう、いずれも固定の接頭辞を持つ。
# --------------------------------------------------------------------------


@router.get('/', include_in_schema=False)
async def index_page() -> FileResponse:
    """トップ。新着記事の一覧。"""
    return _serve('index.html')


@router.get('/articles/{slug}', include_in_schema=False)
async def article_page(
    slug: str,
    db: Database = Depends(get_db),
    config: Config = Depends(get_config),
    settings: RuntimeSettings = Depends(get_settings),
    principal: Principal | None = Depends(get_principal_optional),
) -> HTMLResponse:
    """記事面。og メタを注入するためここだけ動的に組み立てる。"""
    return await render_article_page(db, config, settings, slug, principal)


@router.get('/tags/{slug}', include_in_schema=False)
async def tag_page(slug: str) -> FileResponse:
    """タグページ(話題の特設ページ)。"""
    return _serve('tag.html')


@router.get('/publishers/{slug}', include_in_schema=False)
async def publisher_page(slug: str) -> FileResponse:
    """報道社のプロフィール。"""
    return _serve('publisher.html')


@router.get('/categories/{slug}', include_in_schema=False)
async def category_page(slug: str) -> FileResponse:
    """カテゴリ別の一覧。トップと同じ雛形を使い、絞り込みは JavaScript が URL から読む。"""
    return _serve('index.html')


@router.get('/editor', include_in_schema=False)
async def editor_page() -> FileResponse:
    """記事の作成・編集画面。"""
    return _serve('editor.html')


@router.get('/settings', include_in_schema=False)
async def settings_page() -> FileResponse:
    """利用者の設定画面(API キー・タグ購読)。"""
    return _serve('settings.html')


@router.get('/admin', include_in_schema=False)
async def admin_page() -> FileResponse:
    """プラットフォームの設定画面(settings 表の全キー)。

    ここで返すのは雛形だけであり、設定の値は settings:admin を要求する API からしか取れない。誰に見せるかを決めるのはこの経路ではなく、その API である。
    """
    return _serve('admin.html')
