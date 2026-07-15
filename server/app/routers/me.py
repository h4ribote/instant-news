"""自分自身に関する経路。利用者の情報・API キー・タグ購読。

いずれも「自分の」資源だけを扱う。対象は必ず要求の主体(principal.user_id)から決め、要求の本文やパスから受け取らない。他人の ID を指定する余地が無ければ、権限の検査を書き忘れることもない。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status

from common.db import Database
from common.settings import RuntimeSettings
from common.snowflake import to_snowflake

from ..auth import create_api_token, ensure_can_issue_scopes, list_api_tokens, revoke_api_token
from ..deps import get_db, get_principal, get_settings
from ..errors import Forbidden, NotFound
from ..principals import Principal, is_admin
from ..schemas import MeOut, TagOut, TokenCreate, TokenCreated, TokenOut

router = APIRouter(tags=['me'])

_TAG_COLUMNS = 't.id, t.slug, t.name, t.description, t.icon_url, t.notify_channel_id'


def _parse_id(value: str) -> int:
    """パスの ID を 64bit の整数へ変換する。解釈できない値は 404。

    形式の誤りを 422 として区別しない。存在しない ID を指定したのと結果は変わらず、区別して返すと「その形式なら在りうる」という情報だけを与えることになる。
    """
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound() from exc


def _reject_token_principal(principal: Principal) -> None:
    """API キーによるキーの発行・失効を拒む。

    キーがキーを発行できると、漏れたキーが自分の複製をいくらでも作れる。そうなると失効(revoked_at)が対処にならず、1 本消している間に別の 1 本が増える。キーの管理は必ず人間が Web UI か CLI から行う。
    """
    if principal.via_token:
        raise Forbidden('API キーの発行・失効は API キー経由では行えません。Web UI からログインするか CLI を使ってください。')


@router.get('/api/me', response_model=MeOut)
async def read_me(
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
) -> MeOut:
    """自分の情報・保有ロール・所属報道社・管理者かどうか・購読タグ。

    guild を離れた利用者でも読める。is_guild_member を偽として返すことが答えであり、ここを 403 にすると、なぜ何も書けないのかを画面から知る手段が無くなる。
    """
    user = await db.fetch_one(
        'SELECT id, discord_user_id, username, global_name, avatar_hash, is_guild_member, is_guild_owner FROM users WHERE id = %s',
        principal.user_id,
    )
    if user is None:
        # 認証が通ったのに行が無いのは、判定の最中に利用者が消えた場合に限る。
        raise NotFound('利用者が見つかりません。')
    roles = await db.fetch_all(
        'SELECT r.id, r.name, r.color, r.position FROM discord_roles r '
        'JOIN user_roles ur ON ur.role_id = r.id WHERE ur.user_id = %s ORDER BY r.position DESC, r.id',
        principal.user_id,
    )
    # 所属報道社は保有ロールから引く。publishers.role_id が NULL の報道社(解散済み)は、この結合では出てこない。
    publishers = await db.fetch_all(
        'SELECT p.id, p.role_id, p.slug, p.name, p.description, p.logo_url, p.stance FROM publishers p '
        'JOIN user_roles ur ON ur.role_id = p.role_id WHERE ur.user_id = %s ORDER BY p.name, p.id',
        principal.user_id,
    )
    subscriptions = await db.fetch_all(
        f'SELECT {_TAG_COLUMNS} FROM tags t '
        'JOIN tag_subscriptions s ON s.tag_id = t.id WHERE s.user_id = %s ORDER BY t.name, t.id',
        principal.user_id,
    )
    return MeOut.model_validate(
        {
            'user': user,
            'roles': roles,
            'publishers': publishers,
            # 権限ビットは返さず、解決済みの答えだけを渡す。画面が自前で管理者かどうかを組み立てると、判定がサーバーと画面の 2 箇所に分かれて必ずずれる。画面が管理者向けの導線やスコープの選択肢を出し分けるのはこの値だけを見て行う(出し分けは利便性であって権限の境界ではなく、境界は各経路の資格の検査が担う)。
            'is_admin': await is_admin(db, settings, principal),
            'subscriptions': subscriptions,
        }
    )


@router.get('/api/me/tokens', response_model=list[TokenOut])
async def read_tokens(
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> list[TokenOut]:
    """自分の API キーの一覧。生値は DB に無いため返せない(返せる設計であってはならない)。"""
    rows = await list_api_tokens(db, principal.user_id)
    return [TokenOut.model_validate(row) for row in rows]


@router.post('/api/me/tokens', response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def issue_token(
    payload: TokenCreate,
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
) -> TokenCreated:
    """API キーを発行する。生値はこの応答でだけ返り、以後は二度と取り出せない。

    発行の資格は「今の」設定とロールから判定する。ここで通ったことは、そのキーが将来も通ることを意味しない(キーの権限は要求のたびに判定し直される)。
    """
    _reject_token_principal(principal)
    await ensure_can_issue_scopes(db, settings, principal, payload.scopes)
    raw_token, row = await create_api_token(
        db,
        user_id=principal.user_id,
        name=payload.name,
        scopes=payload.scopes,
        expires_days=payload.expires_days,
    )
    created: dict[str, Any] = {**row, 'token': raw_token}
    return TokenCreated.model_validate(created)


@router.delete('/api/me/tokens/{token_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_token(
    token_id: str,
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> Response:
    """自分の API キーを失効させる。行は消さず revoked_at を入れる(いつ何を止めたのかを残すため)。"""
    _reject_token_principal(principal)
    if not await revoke_api_token(db, token_id=_parse_id(token_id), user_id=principal.user_id):
        # 他人のキーも「見つからない」として返す。存在するが自分のものではない、という区別は、他人のキーの ID を探る手掛かりになる。
        raise NotFound('API キーが見つかりません。')
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _load_tag(db: Database, tag_slug: str) -> dict[str, Any]:
    row = await db.fetch_one(
        'SELECT id, slug, name, description, icon_url, notify_channel_id FROM tags WHERE slug = %s',
        tag_slug,
    )
    if row is None:
        raise NotFound('タグが見つかりません。')
    return row


@router.get('/api/me/subscriptions', response_model=list[TagOut])
async def read_subscriptions(
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> list[TagOut]:
    """購読しているタグの一覧。"""
    rows = await db.fetch_all(
        f'SELECT {_TAG_COLUMNS} FROM tags t '
        'JOIN tag_subscriptions s ON s.tag_id = t.id WHERE s.user_id = %s ORDER BY t.name, t.id',
        principal.user_id,
    )
    return [TagOut.model_validate(row) for row in rows]


@router.put('/api/me/subscriptions/{tag_slug}', response_model=TagOut)
async def subscribe_tag(
    tag_slug: str,
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> TagOut:
    """タグを購読する。既に購読していても成功として扱う(PUT は同じ要求を何度出しても同じ状態になる)。"""
    tag = await _load_tag(db, tag_slug)
    await db.execute('INSERT IGNORE INTO tag_subscriptions (user_id, tag_id) VALUES (%s, %s)', principal.user_id, tag['id'])
    return TagOut.model_validate(tag)


@router.delete('/api/me/subscriptions/{tag_slug}', status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_tag(
    tag_slug: str,
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> Response:
    """タグの購読をやめる。購読していなくても成功として扱う(結果として望む状態は同じであるため)。"""
    tag = await _load_tag(db, tag_slug)
    await db.execute('DELETE FROM tag_subscriptions WHERE user_id = %s AND tag_id = %s', principal.user_id, tag['id'])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
