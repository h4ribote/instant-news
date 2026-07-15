"""GFM 相当の Markdown のレンダリングと、本文中の画像 URL の抽出・差し替え。

本文の権威は Markdown のソース(articles.body_md)であり、HTML(articles.body_html)は再生成できるキャッシュに過ぎない。ここでの入力は執筆者が書いた任意のテキストであって、信用してよいものは何も無い。

レンダリングは 2 段構えにする。markdown-it-py が Markdown を HTML へ変換し、nh3 がその HTML を許可した要素だけへ削り込む。Markdown は生の HTML をそのまま通す仕様であり、変換器だけでは <script> も onerror も素通りする。サニタイズを外すと、記事を 1 本書ける者が全閲覧者のセッションを取れる。

抽出と差し替えは markdown-it のトークンを通して行う。同じ本文を正規表現で舐めると、コードブロックやインラインコードの中に書かれた記法まで拾ってしまう。「![](...) と書けば画像になります」と説明した記事が、説明のつもりで書いた URL を実際に取得されることになる。
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

import nh3
from markdown_it import MarkdownIt
from markdown_it.token import Token

# --------------------------------------------------------------------------
# レンダリングの構成
# --------------------------------------------------------------------------


def _build_markdown() -> MarkdownIt:
    """GFM 相当の Markdown パーサを組み立てる。

    commonmark を土台に、表・打ち消し線・裸の URL の自動リンクを足す。生の HTML は通す(commonmark の既定)。執筆者が <img> や <div> で組んだ図版が消えないようにするためであり、危険な要素は後段の nh3 が落とす。ここで HTML を止めてしまうと、抽出が拾うべき生の <img src> がトークンとして現れなくなる。
    """
    md = MarkdownIt('commonmark', {'linkify': True})
    md.enable(['table', 'strikethrough', 'linkify'])
    return md


# パーサもサニタイザも状態を持たないため、要求ごとに作らず使い回す。
_MD: Final = _build_markdown()

# 許可する要素。見出し・段落・強調・リスト・表・コード・引用・リンク・画像・水平線・打ち消し。ここに無い要素は開始タグと終了タグが外され、中の文字だけが残る。
_ALLOWED_TAGS: Final[set[str]] = {
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'br', 'hr',
    'em', 'strong', 'del', 's',
    'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'pre', 'code',
    'blockquote',
    'a', 'img',
}

# 許可する属性。ここに挙げた組み合わせ以外の属性は、要素が残っても全て落ちる(nh3 は既定の一覧を置き換える)。style と class を通さないため、表の桁揃えとコードの言語指定は表示に反映されない。属性を 1 つ許すたびに、その属性を経由した注入を検討する必要がある。
_ALLOWED_ATTRIBUTES: Final[dict[str, set[str]]] = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title'},
}

# href と src で許すスキーム。javascript: と data: をここで落とす。nh3 の既定は tel: や magnet: を含む広い一覧であるため、明示的に絞る。
_ALLOWED_URL_SCHEMES: Final[set[str]] = {'http', 'https', 'mailto'}

# 外部リンクへ付ける rel。nofollow は本文に貼られた宣伝リンクへ検索順位を渡さないため、noopener は開いた先から window.opener 経由で元のページを操作されないために要る。
_LINK_REL: Final = 'nofollow noopener'

# リンクは別のタブで開く。読んでいる記事を外部サイトで置き換えない。
_SET_TAG_ATTRIBUTE_VALUES: Final[dict[str, dict[str, str]]] = {'a': {'target': '_blank'}}

_CLEANER: Final = nh3.Cleaner(
    tags=_ALLOWED_TAGS,
    attributes=_ALLOWED_ATTRIBUTES,
    url_schemes=_ALLOWED_URL_SCHEMES,
    link_rel=_LINK_REL,
    set_tag_attribute_values=_SET_TAG_ATTRIBUTE_VALUES,
    strip_comments=True,
)

# 本文の構造を持たず、中身をそのまま文字として扱う要素。トークンを辿る際にここへ降りないことが、コードブロックの中の記法を拾わない仕組みそのものである。
_BLOCK_CODE_TYPES: Final = frozenset({'fence', 'code_block'})
_HTML_TYPES: Final = frozenset({'html_block', 'html_inline'})


@dataclass(frozen=True)
class ExtractedImage:
    """本文から見つかった画像 1 つ。

    raw はその画像が書かれていた記法。生の HTML の <img> はソースの文字列そのものが入る。Markdown の ![alt](url) 記法の場合は、パーサが解釈した結果から組み直した正規形が入る(トークンには元の文字位置が残らないため、書かれたままの文字列は復元できない)。
    """

    url: str
    raw: str


def render_markdown(body_md: str) -> str:
    """Markdown を、そのまま記事面へ差し込める HTML へ変換する。

    戻り値は必ずサニタイズ済みである。この関数を経由しない HTML を body_html へ入れてはならない。
    """
    return _CLEANER.clean(_MD.render(body_md))


def extract_image_urls(body_md: str) -> list[str]:
    """本文が参照している画像の URL を、現れた順に重複を除いて返す。

    Markdown の ![alt](url) と生の HTML の <img src="url"> の両方を拾う。コードブロックとインラインコードの中に書かれたものは拾わない。
    """
    seen: set[str] = set()
    urls: list[str] = []
    for image in _extract_images(body_md):
        if image.url in seen:
            continue
        seen.add(image.url)
        urls.append(image.url)
    return urls


def rewrite_image_urls(body_md: str, mapping: Mapping[str, str]) -> str:
    """本文中の画像 URL を差し替える。

    mapping に載っている URL だけを差し替え、載っていない URL(取得に失敗したもの)は元のまま残す。コードブロックとインラインコードの中身は書き換えない。手順の説明としてコード中に書かれた URL は、記事の内容であって参照ではない。
    """
    if not body_md or not mapping:
        return body_md
    # 長い URL から順に照合する。ある URL が別の URL の接頭辞である場合(クエリ文字列の有無など)に短い方を先に当てると、差し替えた後ろに元の URL の断片が残る。
    keys = sorted((key for key in mapping if key), key=len, reverse=True)
    if not keys:
        return body_md
    protected = _protected_spans(body_md)
    starts = [span[0] for span in protected]
    pieces: list[str] = []
    index = 0
    length = len(body_md)
    while index < length:
        skip_to = _protecting_end(protected, starts, index)
        if skip_to is not None:
            # 保護区間へ入った。中は 1 文字も触らずに書き写す。
            pieces.append(body_md[index:skip_to])
            index = skip_to
            continue
        for key in keys:
            if not body_md.startswith(key, index):
                continue
            end = index + len(key)
            if _crosses_protected(protected, starts, index, end):
                # 一致が保護区間へ食い込んでいる。差し替えるとコードの中身を削ることになる。
                continue
            pieces.append(mapping[key])
            index = end
            break
        else:
            pieces.append(body_md[index])
            index += 1
    return ''.join(pieces)


# --------------------------------------------------------------------------
# 画像の抽出
# --------------------------------------------------------------------------


class _ImgSrcCollector(HTMLParser):
    """生の HTML の断片から <img> の src を、書かれた順に集める。

    生の HTML を正規表現で舐めない。属性の引用符の有無・大文字小文字・タグ内の改行・複数の src らしき文字列といった揺れを、正規表現は必ずどこかで取り違える。
    """

    def __init__(self) -> None:
        # 実体参照は解決する。&amp; を含む URL を、書かれた通りの &amp; のまま取得しに行かないようにする。
        super().__init__(convert_charrefs=True)
        self.found: list[ExtractedImage] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != 'img':
            return
        for name, value in attrs:
            if name == 'src' and value:
                self.found.append(ExtractedImage(url=value, raw=self.get_starttag_text() or ''))
                return

    def take(self) -> list[ExtractedImage]:
        """今までに集めた分を取り出し、内部の控えを空にする。"""
        found = self.found
        self.found = []
        return found


def _extract_images(body_md: str) -> list[ExtractedImage]:
    """本文が参照している画像を、現れた順に(重複も含めて)返す。"""
    images: list[ExtractedImage] = []
    collector = _ImgSrcCollector()
    _collect_images(_MD.parse(body_md), collector, images)
    # 閉じられていないタグが最後のトークンに残っている場合、close() で吐き出される。
    collector.close()
    images.extend(collector.take())
    return images


def _collect_images(tokens: Sequence[Token], collector: _ImgSrcCollector, images: list[ExtractedImage]) -> None:
    """トークンを書かれた順に辿り、画像を積む。

    コードのトークン(fence / code_block / code_inline)は子を持たないため、辿った先に現れない。中に書かれた記法を拾わないのは、この構造による当然の帰結であって、後から除いた結果ではない。
    """
    for token in tokens:
        if token.type == 'image':
            src = token.attrGet('src')
            if src:
                images.append(ExtractedImage(url=str(src), raw=_markdown_image_raw(token)))
            # 代替テキストの中に入れ子の画像を書ける。
            if token.children:
                _collect_images(token.children, collector, images)
            continue
        if token.type in _HTML_TYPES:
            collector.feed(token.content)
            images.extend(collector.take())
            continue
        if token.children:
            _collect_images(token.children, collector, images)


def _markdown_image_raw(token: Token) -> str:
    """image トークンから ![alt](url) 記法を組み直す。"""
    src = token.attrGet('src') or ''
    title = token.attrGet('title')
    if title:
        return f'![{token.content}]({src} "{title}")'
    return f'![{token.content}]({src})'


# --------------------------------------------------------------------------
# 差し替えで触ってはならない範囲
# --------------------------------------------------------------------------


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """書き換えてはならない文字範囲を、重なりを解消して昇順で返す。

    対象はコードブロック(fence / code_block)とインラインコード。前者はトークンが行範囲を持つため正確に求まる。後者は行範囲を持たないため、パーサが見つけたコードスパンの記法を、その行範囲の中から順に探して位置を定める。
    """
    offsets = _line_offsets(text)
    spans: list[tuple[int, int]] = []
    _collect_protected(_MD.parse(text), text, offsets, spans)
    return _merge_spans(spans)


def _collect_protected(tokens: Sequence[Token], text: str, offsets: Sequence[int], spans: list[tuple[int, int]]) -> None:
    for token in tokens:
        if token.type in _BLOCK_CODE_TYPES:
            if token.map:
                spans.append(_line_span(token.map, offsets, len(text)))
            continue
        if token.type == 'inline':
            region = _line_span(token.map, offsets, len(text)) if token.map else (0, len(text))
            _collect_inline_code(token, text, region, spans)
            continue
        if token.children:
            _collect_protected(token.children, text, offsets, spans)


def _collect_inline_code(inline: Token, text: str, region: tuple[int, int], spans: list[tuple[int, int]]) -> None:
    """inline トークンの行範囲の中から、コードスパンの位置を順に求めて積む。"""
    cursor = region[0]
    for child in inline.children or []:
        if child.type != 'code_inline':
            continue
        span = _find_code_span(text, cursor, region[1], child)
        if span is None:
            # 記法を特定できなかった(内容に改行を含むコードスパンなど。コードスパンは改行を空白へ畳むため、トークンの内容とソースの文字列が一致しない)。取り違えて書き換えるより、この行範囲を丸ごと保護する方が害が小さい。差し替えが 1 つ効かないだけで済み、コードは壊れない。
            spans.append(region)
            return
        spans.append(span)
        cursor = span[1]


def _find_code_span(text: str, start: int, limit: int, token: Token) -> tuple[int, int] | None:
    """コードスパン 1 つの文字範囲を求める。見つからなければ None。

    token.markup はバッククォートの並び、token.content は中身。CommonMark は中身の前後に空白が 1 つずつある場合だけ、その 1 つを取り除くため、候補は 2 通りある。
    """
    markup = token.markup
    if not markup:
        return None
    for candidate in (f'{markup}{token.content}{markup}', f'{markup} {token.content} {markup}'):
        position = text.find(candidate, start, limit)
        if position != -1:
            return (position, position + len(candidate))
    return None


def _line_offsets(text: str) -> list[int]:
    """各行の先頭の文字位置。offsets[i] が i 行目の先頭。"""
    offsets = [0]
    position = text.find('\n')
    while position != -1:
        offsets.append(position + 1)
        position = text.find('\n', position + 1)
    return offsets


def _line_span(token_map: Sequence[int], offsets: Sequence[int], text_length: int) -> tuple[int, int]:
    """トークンの行範囲 [開始行, 終了行) を文字範囲へ直す。終了行は含まない。"""
    start_line, end_line = token_map[0], token_map[1]
    start = offsets[start_line] if start_line < len(offsets) else text_length
    end = offsets[end_line] if end_line < len(offsets) else text_length
    return (min(start, text_length), min(end, text_length))


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """範囲を昇順に整列し、重なり合うものを 1 つに畳む。

    以降の探索は範囲が重ならないことを前提に二分探索するため、ここで正規化しておく。
    """
    if not spans:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    return merged


def _protecting_end(spans: Sequence[tuple[int, int]], starts: Sequence[int], index: int) -> int | None:
    """index を含む保護区間があれば、その終端を返す。無ければ None。"""
    position = bisect.bisect_right(starts, index) - 1
    if position < 0:
        return None
    start, end = spans[position]
    return end if start <= index < end else None


def _crosses_protected(spans: Sequence[tuple[int, int]], starts: Sequence[int], start: int, end: int) -> bool:
    """[start, end) が、start より後ろから始まる保護区間へ食い込むかを返す。"""
    position = bisect.bisect_right(starts, start)
    return position < len(spans) and spans[position][0] < end
