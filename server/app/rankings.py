"""報道社ランキングの集計。

反応の実体は Discord 側にあり、bot が article_discord_posts へ賛成(up_count)と反対(down_count)の数を、article_comments へコメントを写してくる。1 記事が複数チャンネルへ通知されうるため、記事としての賛否はその記事の全行の合計であり、報道社としての実績はさらにその報道社の全記事の合計になる。

点数は記事ごとに次の規則で求め、それを報道社ごとに合計する(賛成 up、反対 down、コメント数 c として)。

  ・2 × down ≥ up … その記事は叩かれている。点数は up + c とし、反対は加えない。
  ・2 × down < up  … 叩かれてはいない。点数は up + (down ÷ 2 の切り捨て) + c とし、反対も関心の証として半分だけ加える。

反対が点数を引くことはない。反対は「読まれなかった」ことではなく「読まれたうえで反対された」ことの印であり、賛成に対して十分小さい限りは注目の大きさとして数える。一方で反対が賛成に迫るほど付いた記事は、その注目を実績として数えない。読者へ見せる記事の点数(賛成 − 反対)とは別の量であり、こちらは順位を決めるためだけに使う。

判定は必ず記事ごとに行う。叩かれているかどうかは記事の性質であり、報道社の合計に対して判定すると、1 本の炎上がその報道社の他の記事の評価まで一斉に変えてしまう。したがって集計は 2 段になる(記事ごとに畳んでから、報道社ごとに畳む)。1 段の GROUP BY では、条件を記事ごとに当てる場所が無い。

集計対象を status='published' かつ visibility='public' に限るのは、閲覧制限のある記事を公開のランキングに混ぜないためである。ランキングは誰でも見られる単一の成果物であり、members 限定・ロール限定の記事を数に含めると、その記事を読めない者にも「そこに何かがある」ことと、その反応の大きさが漏れる。

記事広告(is_sponsored)は含める。開示されている以上、読者はそれが広告であることを踏まえたうえで反応しており、その反応は実際に起きた事実である。除外すると、開示した記事だけが実績を失うことになり、開示しない動機を作ってしまう。
"""

from __future__ import annotations

from typing import Any, Final

from common.db import Database

# 期間の既定値と、1 度に返す報道社の既定数。
DEFAULT_DAYS: Final = 7
DEFAULT_LIMIT: Final = 10

# 記事 1 本の点数。上の docstring の規則をそのまま書き下したものであり、記事ごとに畳んだ表(別名 a)に対して当てる。
# FLOOR で切り捨てるのは、反対 1 件を 0.5 点として持ち込まないため(点数は整数で扱う)。
ARTICLE_SCORE_SQL: Final = '''
    CASE WHEN 2 * a.down_votes >= a.up_votes
         THEN a.up_votes + a.comments
         ELSE a.up_votes + FLOOR(a.down_votes / 2) + a.comments
    END
'''


def article_totals_sql(where: str) -> str:
    '''記事ごとに賛否とコメント数を畳む問い合わせを組み立てる。外側は必ず別名 a で受ける(ARTICLE_SCORE_SQL がその名前を参照するため)。

    where には呼び出し側が対象の記事を絞る条件を渡す。プレースホルダを含んでよいが、値そのものを文字列で埋め込んではならない。

    通知先が 0 件の記事(カテゴリにもタグにも通知チャンネルが無い記事)は賛否を持ち得ないが、記事数には数える必要があるため LEFT JOIN で落とさない。
    コメント数を結合ではなく相関副問い合わせで求めるのは、通知の行と結合すると 1 記事が通知先の数だけ行に増え、賛否の合計がその数だけ水増しされるためである。
    '''
    return f'''
        SELECT articles.id AS article_id,
               articles.publisher_id AS publisher_id,
               COALESCE(SUM(adp.up_count), 0) AS up_votes,
               COALESCE(SUM(adp.down_count), 0) AS down_votes,
               (SELECT COUNT(*) FROM article_comments ac WHERE ac.article_id = articles.id) AS comments
          FROM articles
          LEFT JOIN article_discord_posts adp ON adp.article_id = articles.id
         WHERE {where}
         GROUP BY articles.id
    '''


# 報道社ごとに、記事ごとの点数を合計する。
_RANKING_SQL: Final = f'''
    SELECT
        p.id,
        p.role_id,
        p.slug,
        p.name,
        p.description,
        p.logo_url,
        p.stance,
        COUNT(*) AS article_count,
        COALESCE(SUM(a.up_votes), 0) AS up_count,
        COALESCE(SUM(a.down_votes), 0) AS down_count,
        COALESCE(SUM(a.comments), 0) AS comment_count,
        COALESCE(SUM({ARTICLE_SCORE_SQL}), 0) AS score
    FROM ({article_totals_sql("articles.status = 'published' AND articles.visibility = 'public' AND articles.published_at >= NOW() - INTERVAL %s DAY")}) a
    JOIN publishers p ON p.id = a.publisher_id
    GROUP BY p.id
    ORDER BY score DESC, article_count DESC, p.name ASC
    LIMIT %s
'''


async def publisher_ranking(db: Database, *, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """直近 days 日の報道社ランキングを score の降順で最大 limit 件返す。

    score の規則はモジュールの docstring に述べたとおりである。同点は記事数の多い順、さらに同じなら名前順で並べ、結果の順序が呼び出しごとに揺れないようにする。

    返す各行は {'publisher': {...}, 'article_count': int, 'up_count': int, 'down_count': int, 'comment_count': int, 'score': int} の形をとる。publisher には報道社の行をそのまま入れるため、そのまま PublisherOut として検証できる。

    days・limit は呼び出し側(ルータのクエリパラメータ)で範囲を検証する前提だが、0 以下の指定は結果が空になるか SQL が壊れるだけで意図が伝わらないため、ここで明確に弾く。
    """
    if days < 1:
        raise ValueError(f'集計期間の日数は 1 以上で指定してください: {days}')
    if limit < 1:
        raise ValueError(f'取得件数は 1 以上で指定してください: {limit}')
    rows = await db.fetch_all(_RANKING_SQL, days, limit)
    return [
        {
            'publisher': {
                'id': row['id'],
                'role_id': row['role_id'],
                'slug': row['slug'],
                'name': row['name'],
                'description': row['description'],
                'logo_url': row['logo_url'],
                'stance': row['stance'],
            },
            'article_count': int(row['article_count']),
            # SUM() は MySQL では DECIMAL を返すため、JSON へ出す前に int へ寄せる。
            'up_count': int(row['up_count']),
            'down_count': int(row['down_count']),
            'comment_count': int(row['comment_count']),
            'score': int(row['score']),
        }
        for row in rows
    ]
