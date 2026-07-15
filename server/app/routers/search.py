"""記事の全文検索 API。

索引は articles の FULLTEXT (title, body_md) で、日本語を語の切れ目で分かち書きできないため ngram パーサを使う。検索語は自然言語モードで解釈する。ブールモード(+ - * " などが演算子として効く)にすると、記事の題名に含まれるだけの記号が検索式として解釈され、利用者の意図しない結果を返す。

検索は閲覧範囲の絞り込みを必ず通す。全文検索は「その語を含む記事が存在するか」を利用者に教える機能であり、絞り込みを外すと、読めない記事の題名や本文の断片が索引を通して漏れる。件数がゼロか否かだけでも、限定公開の記事の存在は伝わってしまう。
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

# 検索語の照合式。WHERE と ORDER BY の双方に同じ式を書く。MySQL は同一の MATCH 式を 1 度しか評価しないため二重の走査にはならず、こう書くことで並び順が適合度であることが明示される。
_MATCH: Final = 'MATCH(articles.title, articles.body_md) AGAINST (%s IN NATURAL LANGUAGE MODE)'


@router.get('/api/search', response_model=Page[ArticleListItem])
async def search_articles(
    db: Annotated[Database, Depends(get_db)],
    principal: Annotated[Principal | None, Depends(get_principal_optional)],
    q: Annotated[str, Query(max_length=200)] = '',
    page: Annotated[int, Query(ge=1)] = 1,
) -> Page[ArticleListItem]:
    """記事を全文検索し、適合度の高い順に返す。同じ適合度なら新しい順とする。

    検索語が空(または空白のみ)なら、索引を引かずに空の頁を返す。空の語で MATCH を評価しても結果は空であり、検索欄を開いただけの要求で全文索引を叩く理由が無い。
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
