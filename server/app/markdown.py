"""Render GFM-equivalent Markdown, and extract and rewrite the image URLs in a body.

The Markdown source (articles.body_md) is authoritative; the HTML (articles.body_html) is only a cache that can be regenerated. The input here is arbitrary text a writer typed, and none of it may be trusted.

Rendering is two stages: markdown-it-py converts Markdown to HTML, then nh3 cuts that HTML down to the allowed elements. Markdown passes raw HTML through by spec, so the converter alone lets <script> and onerror straight through. Remove the sanitizer and anyone who can write a single article can take every viewer's session.

Extraction and rewriting go through markdown-it tokens. Scanning the same body with a regex would also pick up notation written inside code blocks and inline code, so an article explaining "write ![](...) to get an image" would have the URL it wrote as an explanation actually fetched.
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

# Rendering setup


def _build_markdown() -> MarkdownIt:
    """Build the GFM-equivalent Markdown parser.

    commonmark as the base, plus tables, strikethrough and autolinking of bare URLs. Raw HTML is passed through (the commonmark default) so that a figure a writer built out of <img> or <div> does not disappear; nh3 downstream drops the dangerous elements. Blocking HTML here would also stop the raw <img src> that extraction has to find from ever appearing as a token.
    """
    md = MarkdownIt('commonmark', {'linkify': True})
    md.enable(['table', 'strikethrough', 'linkify'])
    return md


# Parser and sanitizer are both stateless, so they are reused instead of built per request.
_MD: Final = _build_markdown()

# Allowed elements: headings, paragraphs, emphasis, lists, tables, code, blockquotes, links, images, horizontal rules, strikethrough. An element not listed here loses its start and end tags; only the text inside survives.
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

# Allowed attributes. Any attribute outside these pairs is dropped even when the element survives (nh3 replaces its default list rather than adding to it). style and class are not allowed, so table column alignment and code language hints do not reach the rendered page. Every additional attribute allowed here demands a fresh look at injection through that attribute.
_ALLOWED_ATTRIBUTES: Final[dict[str, set[str]]] = {
    'a': {'href', 'title'},
    'img': {'src', 'alt', 'title'},
}

# Schemes allowed in href and src: this is where javascript: and data: are dropped. nh3's default list is a wide one that includes tel: and magnet:, so narrow it explicitly.
_ALLOWED_URL_SCHEMES: Final[set[str]] = {'http', 'https', 'mailto'}

# rel for outbound links. nofollow so a promotional link pasted into a body passes no search ranking; noopener so the opened page cannot drive the original through window.opener.
_LINK_REL: Final = 'nofollow noopener'

# Open links in another tab: an external site must not replace the article being read.
_SET_TAG_ATTRIBUTE_VALUES: Final[dict[str, dict[str, str]]] = {'a': {'target': '_blank'}}

_CLEANER: Final = nh3.Cleaner(
    tags=_ALLOWED_TAGS,
    attributes=_ALLOWED_ATTRIBUTES,
    url_schemes=_ALLOWED_URL_SCHEMES,
    link_rel=_LINK_REL,
    set_tag_attribute_values=_SET_TAG_ATTRIBUTE_VALUES,
    strip_comments=True,
)

# Tokens with no body structure, whose content is literal text. Never descending into these while walking tokens is itself the mechanism that keeps notation inside a code block from being picked up.
_BLOCK_CODE_TYPES: Final = frozenset({'fence', 'code_block'})
_HTML_TYPES: Final = frozenset({'html_block', 'html_inline'})


@dataclass(frozen=True)
class ExtractedImage:
    """One image found in a body.

    raw is the notation the image was written as. For a raw HTML <img> it holds the source string itself. For Markdown ![alt](url) it holds a canonical form rebuilt from what the parser resolved, because tokens keep no source offsets and the text as written cannot be recovered.
    """

    url: str
    raw: str


def render_markdown(body_md: str) -> str:
    """Convert Markdown into HTML that can be dropped straight into the article page.

    The return value is always sanitized. HTML that did not pass through this function must never be put into body_html.
    """
    return _CLEANER.clean(_MD.render(body_md))


def extract_image_urls(body_md: str) -> list[str]:
    """Return the URLs of the images the body references, in order of appearance, with duplicates removed.

    Picks up both Markdown ![alt](url) and raw HTML <img src="url">. Anything written inside a code block or inline code is not picked up.
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
    """Rewrite the image URLs in a body.

    Only URLs present in mapping are rewritten; the ones that are not (those that failed to fetch) stay as they were. The contents of code blocks and inline code are never rewritten: a URL written inside code to explain a procedure is article content, not a reference.
    """
    if not body_md or not mapping:
        return body_md
    # Match longest URL first. When one URL is a prefix of another (differing by a query string, say), matching the shorter one first leaves a snippet of the original URL trailing the replacement.
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
            # Entered a protected span: copy it through without touching a single character.
            pieces.append(body_md[index:skip_to])
            index = skip_to
            continue
        for key in keys:
            if not body_md.startswith(key, index):
                continue
            end = index + len(key)
            if _crosses_protected(protected, starts, index, end):
                # The match crosses into a protected span; rewriting it would cut into the code's content.
                continue
            pieces.append(mapping[key])
            index = end
            break
        else:
            pieces.append(body_md[index])
            index += 1
    return ''.join(pieces)


# Image extraction


class _ImgSrcCollector(HTMLParser):
    """Collect the src of every <img> in a raw HTML snippet, in written order.

    Raw HTML is not scanned with a regex. Quoted or unquoted attributes, case, newlines inside a tag, several src-looking strings: a regex gets one of those variations wrong sooner or later.
    """

    def __init__(self) -> None:
        # Resolve character references, so a URL containing &amp; is not fetched with the literal &amp; still in it.
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
        """Take everything collected so far and empty the internal buffer."""
        found = self.found
        self.found = []
        return found


def _extract_images(body_md: str) -> list[ExtractedImage]:
    """Return the images the body references, in order of appearance, duplicates included."""
    images: list[ExtractedImage] = []
    collector = _ImgSrcCollector()
    _collect_images(_MD.parse(body_md), collector, images)
    # If an unclosed tag is left over in the last token, close() flushes it out.
    collector.close()
    images.extend(collector.take())
    return images


def _collect_images(tokens: Sequence[Token], collector: _ImgSrcCollector, images: list[ExtractedImage]) -> None:
    """Walk the tokens in written order and collect the images.

    Code tokens (fence / code_block / code_inline) have no children, so the walk never reaches inside them. Not picking up notation written inside them is a plain consequence of that structure, not something filtered out afterwards.
    """
    for token in tokens:
        if token.type == 'image':
            src = token.attrGet('src')
            if src:
                images.append(ExtractedImage(url=str(src), raw=_markdown_image_raw(token)))
            # An image can be nested inside the alt text.
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
    """Rebuild the ![alt](url) notation from an image token."""
    src = token.attrGet('src') or ''
    title = token.attrGet('title')
    if title:
        return f'![{token.content}]({src} "{title}")'
    return f'![{token.content}]({src})'


# Ranges that rewriting must not touch


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Return the character ranges that must not be rewritten, overlaps merged, in ascending order.

    The ranges are code blocks (fence / code_block) and inline code. Block-code tokens carry a line range, so they resolve exactly. Inline code carries none, so each code span the parser found is located by searching for its notation within that line range, in order.
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
    """Locate each code span within the inline token's line range, in order, and collect it."""
    cursor = region[0]
    for child in inline.children or []:
        if child.type != 'code_inline':
            continue
        span = _find_code_span(text, cursor, region[1], child)
        if span is None:
            # The notation could not be located, e.g. a code span whose content contains a newline: code spans collapse newlines into spaces, so the token content no longer matches the source text. Protecting this whole line range does less harm than rewriting the wrong text: one rewrite fails to take effect, and the code stays intact.
            spans.append(region)
            return
        spans.append(span)
        cursor = span[1]


def _find_code_span(text: str, start: int, limit: int, token: Token) -> tuple[int, int] | None:
    """Return the character range of one code span, or None if it cannot be found.

    token.markup is the run of backticks and token.content is the content. CommonMark strips one space from each side only when the content has exactly one space on both sides, which is why there are two candidates to try.
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
    """Character position of the start of each line; offsets[i] is the start of line i."""
    offsets = [0]
    position = text.find('\n')
    while position != -1:
        offsets.append(position + 1)
        position = text.find('\n', position + 1)
    return offsets


def _line_span(token_map: Sequence[int], offsets: Sequence[int], text_length: int) -> tuple[int, int]:
    """Convert a token's line range [start_line, end_line) into a character range; the end line is not included."""
    start_line, end_line = token_map[0], token_map[1]
    start = offsets[start_line] if start_line < len(offsets) else text_length
    end = offsets[end_line] if end_line < len(offsets) else text_length
    return (min(start, text_length), min(end, text_length))


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort the spans ascending and collapse overlapping ones into one.

    Normalized here because the later lookups binary-search on the assumption that spans do not overlap.
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
    """Return the end of the protected span containing index, or None if there is none."""
    position = bisect.bisect_right(starts, index) - 1
    if position < 0:
        return None
    start, end = spans[position]
    return end if start <= index < end else None


def _crosses_protected(spans: Sequence[tuple[int, int]], starts: Sequence[int], start: int, end: int) -> bool:
    """Whether [start, end) crosses into a protected span that begins after start."""
    position = bisect.bisect_right(starts, start)
    return position < len(spans) and spans[position][0] < end
