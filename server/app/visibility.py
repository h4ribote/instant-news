"""記事と画像の閲覧範囲の判定。

閲覧の可否は 2 つの軸の積で決まる。status(公開の段階)と visibility(閲覧範囲)であり、両方を満たさなければ見せない。status だけを見ると下書きが公開範囲の記事として漏れ、visibility だけを見ると公開前の原稿が読まれる。

判定はこのモジュールへ集約する。記事の詳細・一覧・全文検索・画像の配信・og メタの注入はいずれも同じ問いに答える必要があり、経路ごとに書くと必ずどれかが緩む。緩んだ 1 箇所が全体の閲覧制限を無効にする。

閲覧できない対象には 403 ではなく 404 を返すこと(errors.NotFound)。403 は「そこに何かがある」ことを教えてしまい、存在自体が秘密である限定公開の記事では、それだけで漏洩になる。この規則は呼び出し側の責務であり、このモジュールは真偽だけを返す。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from common.db import Database
from common.settings import RuntimeSettings

from .principals import Principal, is_admin

# この段階の記事だけが、著者本人と管理者以外の利用者に見える。draft / in_review / scheduled はまだ公に出していない原稿であり、archived は取り下げたものである。
PUBLISHED: Final = 'published'


async def can_view_article(db: Database, principal: Principal | None, article: Mapping[str, Any], *, settings: RuntimeSettings) -> bool:
    """principal が article を閲覧できるかを返す。

    article は articles の行であり、id / author_id / status / visibility を含んでいる必要がある。一覧のように行を既に引いてある場面で再取得しないよう、行そのものを受け取る。

    settings は管理者の判定に要る。管理者資格の 3 つ目の条件(settings の admin.role_ids に含まれるロールの保有)は設定を見ないと判定できず、省略可能にすると、渡し忘れた呼び出しだけが委譲された管理者を管理者と見なさなくなる。渡すのは要求ごとに settings 表から解決したものであること(管理ロールの変更が閲覧範囲へ即座に効く必要がある)。
    """
    author_id = article.get('author_id')
    if principal is not None and author_id is not None and int(author_id) == principal.user_id:
        # 自分が書いたものは、公開の段階にも閲覧範囲にも関わらず読める。
        return True
    if await _is_administrator(db, settings, principal):
        # 管理者は公開前の原稿を含めて読める。編集の責任を負う立場が自分の記事しか見られないと、公開前の確認が成立しない。
        return True
    if article.get('status') != PUBLISHED:
        return False
    visibility = article.get('visibility')
    if visibility == 'public':
        return True
    # public 以外は、対象 guild に現在参加しているログイン済みの利用者だけが候補になる。過去に参加していた者のセッションが残っていても、is_guild_member が偽になった時点で読めなくなる。
    if principal is None or not principal.is_guild_member:
        return False
    if visibility == 'members':
        return True
    if visibility == 'restricted':
        return await _has_visibility_role(db, principal, int(article['id']))
    # 未知の閲覧範囲は拒否する。列挙が増えたときに、判定を書き足すまでの間だけ見えなくなるのは正しい side であり、その逆(既定で見せる)は漏洩になる。
    return False


def visibility_sql_filter(principal: Principal | None) -> tuple[str, list[Any]]:
    """一覧・検索のクエリの WHERE へ差し込む条件の断片と、その引数を返す。

    返す断片は括弧で閉じてあるため、呼び出し側は AND で素直に連結できる。**条件は articles という名前で記事の表を参照する。**呼び出し側のクエリは articles に別名を付けてはならない(付けると、この断片が指す articles.* が解決できない列になり、クエリが実行時に落ちる)。

    この関数が答えるのは visibility の軸だけである。status の絞り込み(既定では published のみ)と、管理者による公開前の記事の閲覧は呼び出し側が足す。管理者の判定はロールの権限ビットを DB から引く必要があり、引数を取らない同期の関数では答えられない。

    著者本人の記事を含めるのは、can_view_article と一覧を一致させるためである。これが無いと、自分が書いた限定公開の記事が詳細では読めるのに一覧に出ない。
    """
    if principal is None:
        return ("(articles.visibility = 'public')", [])
    # clauses と params は同じ順序で積む。プレースホルダの並びと引数の並びがずれると、別のロールで判定が通ってしまう。
    clauses: list[str] = ["articles.visibility = 'public'", 'articles.author_id = %s']
    params: list[Any] = [principal.user_id]
    if principal.is_guild_member:
        clauses.append("articles.visibility = 'members'")
        if principal.role_ids:
            placeholders = _placeholders(len(principal.role_ids))
            clauses.append(
                f"(articles.visibility = 'restricted' AND EXISTS ("
                f'SELECT 1 FROM article_visibility_roles avr WHERE avr.article_id = articles.id AND avr.role_id IN ({placeholders})))'
            )
            params.extend(_ordered_role_ids(principal))
    return '(' + ' OR '.join(clauses) + ')', params


async def can_view_media(db: Database, principal: Principal | None, media_id: int, is_public: bool, *, settings: RuntimeSettings) -> bool:
    """principal が media_id の画像を受け取れるかを返す。

    公開の画像(カバー・広告・ロゴ・アイコン)は認証なしで配信する。そうでない画像は、それを参照している記事のうち 1 つでも principal が閲覧できるなら配信する。同じバイト列が複数の記事から参照されうるため、1 つでも読める記事があれば、その利用者はもうその画像を見られる立場にある。

    参照の検査を省いて「ログイン済みなら配信」にしてはならない。閲覧制限のある記事の図版が、その記事を読めない全メンバーへ配られる。
    """
    if is_public:
        return True
    # 参照元の記事を引く。article_media は本文の保存時に積まれた事実であり、本文を読み直して推測しない。
    articles = await db.fetch_all(
        'SELECT a.id, a.author_id, a.status, a.visibility'
        ' FROM article_media am JOIN articles a ON a.id = am.article_id'
        ' WHERE am.media_id = %s',
        media_id,
    )
    for article in articles:
        if await can_view_article(db, principal, article, settings=settings):
            return True
    return False


def _placeholders(count: int) -> str:
    """IN 句へ差し込む、個数分のプレースホルダを組み立てる。

    値そのものは決して SQL へ埋め込まない。組み立てるのは %s の並びだけであり、値は引数として渡す。ロール ID は int に正規化済みだが、ここで連結する習慣を作ると、いずれ文字列を受ける場所で同じ書き方をしてしまう。
    """
    return ', '.join(['%s'] * count)


def _ordered_role_ids(principal: Principal) -> list[int]:
    """principal の保有ロールを安定した順序で返す。

    frozenset の反復順は実行ごとに変わりうる。順序を固定しておくと、同じ主体の同じ問い合わせが常に同じ SQL と同じ引数になり、ログの突き合わせとプリペアドステートメントの再利用が効く。
    """
    return sorted(principal.role_ids)


async def _is_administrator(db: Database, settings: RuntimeSettings, principal: Principal | None) -> bool:
    """principal が管理者かを返す。未認証(None)なら偽。

    判定そのものは principals.is_admin に委ねる。ここに独自の実装を置くと、管理者の定義が 2 箇所に分かれ、必ずどちらかが取り残される。実際、設定(admin.role_ids)による委譲を見ない実装をここに持っていた間は、委譲された管理者が記事を編集できるのに閲覧すると 404 になっていた。「拒否する側へ倒れるから安全」は、その委譲を中心の機能として謳う以上、正しさの説明にならない。設定の出どころが settings 表へ移ってもこの一本化は保つ(判定を 2 箇所に分ければ、同じズレが同じ形で再発する)。
    """
    if principal is None:
        return False
    return await is_admin(db, settings, principal)


async def _has_visibility_role(db: Database, principal: Principal, article_id: int) -> bool:
    """principal が、その記事の閲覧を許可されたロールを 1 つ以上持つかを返す。"""
    if not principal.role_ids:
        return False
    role_ids = _ordered_role_ids(principal)
    found = await db.fetch_val(
        f'SELECT 1 FROM article_visibility_roles WHERE article_id = %s AND role_id IN ({_placeholders(len(role_ids))}) LIMIT 1',
        article_id,
        *role_ids,
    )
    return found is not None
