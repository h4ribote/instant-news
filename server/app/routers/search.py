"""Full-text search API for articles.

The index is the FULLTEXT (title, body_md) on articles, with the ngram parser because Japanese is not written with word breaks to tokenize on. Search terms are read in NATURAL LANGUAGE MODE. In BOOLEAN MODE + - * " and the like act as operators, so punctuation that merely happens to sit in an article's title would be parsed as a search expression and return results the user never intended.

Search always goes through the visibility filter. Full-text search tells the user whether an article containing a given word exists, so dropping the filter leaks the titles and body snippets of unreadable articles through the index. Even a bare zero/non-zero result count gives away that a limited-visibility article exists.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query

from common.db import Database

from ..deps import get_db, get_principal_optional
from ..principals import Principal
from ..schemas import ArticleListItem, Page
from .taxonomy import DEFAULT_PER_PAGE, fetch_article_page, published_and_visible

router = APIRouter(tags=['search'])

# The search-term match expression, written identically in WHERE and ORDER BY. MySQL evaluates one and the same MATCH expression only once, so this costs no second scan, and it makes explicit that the ordering is by relevance.
_MATCH: Final = 'MATCH(articles.title, articles.body_md) AGAINST (%s IN NATURAL LANGUAGE MODE)'


@router.get('/api/search', response_model=Page[ArticleListItem])
async def search_articles(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    q: Annotated[str, Query(max_length=200)] = '',
    page: Annotated[int, Query(ge=1)] = 1,
) -> Page[ArticleListItem]:
    """Full-text search articles, most relevant first, ties broken newest-first.

    An empty (or whitespace-only) term returns an empty page of results without touching the index: MATCH on an empty term matches nothing anyway, and merely opening the search field is no reason to hit the full-text index.
    """
    query = q.strip()
    if not query:
        return Page[ArticleListItem](items=[], page=page, per_page=DEFAULT_PER_PAGE, total=0, has_next=False)
    where, args = published_and_visible(principal)
    return await fetch_article_page(
        db,
        where=f'{_MATCH} AND {where}',
        where_args=[query, *args],
        order_by=f'{_MATCH} DESC, articles.published_at DESC, articles.id DESC',
        order_args=[query],
        page=page,
    )
