"""Routes for the caller's own data: user info, API keys, tag subscriptions.

Every route here touches only your own resources. The target is always taken from the requesting principal (principal.user_id), never from the request body or path. With no way to name someone else's ID, there is no permission check left to forget.
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
    """Convert a path ID to a 64-bit integer. An unparseable value is a 404.

    A malformed ID is deliberately not distinguished as 422: the outcome is the same as naming an ID that does not exist, and separating the two would only tell the caller that IDs of that shape could exist.
    """
    try:
        return to_snowflake(value)
    except ValueError as exc:
        raise NotFound() from exc


def _reject_token_principal(principal: Principal) -> None:
    """Refuse issuing or revoking a key when the caller is itself an API key.

    If a key could issue keys, a leaked key could mint unlimited copies of itself. Revocation (revoked_at) would then stop being a remedy: another key appears while you revoke one. Key management is always done by a human, from the Web UI or the CLI.
    """
    if principal.via_token:
        raise Forbidden('API キーの発行・失効は API キー経由では行えません。Web UI からログインするか CLI を使ってください。')


@router.get('/api/me', response_model=MeOut)
async def read_me(
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
) -> MeOut:
    """Your own info, the roles you hold, your affiliated publishers, whether you are an admin, and your subscribed tags.

    Readable even by a user who has left the guild: returning is_guild_member false is the answer. A 403 here would leave the UI with no way to tell why nothing can be written.
    """
    user = await db.fetch_one(
        'SELECT id, discord_user_id, username, global_name, avatar_hash, is_guild_member, is_guild_owner FROM users WHERE id = %s',
        principal.user_id,
    )
    if user is None:
        # Authentication passed yet the row is gone: only possible if the user was deleted mid-check.
        raise NotFound('利用者が見つかりません。')
    roles = await db.fetch_all(
        'SELECT r.id, r.name, r.color, r.position FROM discord_roles r '
        'JOIN user_roles ur ON ur.role_id = r.id WHERE ur.user_id = %s ORDER BY r.position DESC, r.id',
        principal.user_id,
    )
    # Affiliated publishers are looked up from the roles held. A publisher whose publishers.role_id is NULL (dissolved) never comes out of this join.
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
            # No permission bits go out, only the resolved answer: if the UI assembled admin status itself the decision would live in two places, server and UI, and would inevitably drift. The UI looks at this value alone to decide whether to show admin entry points and scope options (showing or hiding them is convenience, not the permission boundary; the boundary is each route's eligibility check).
            'is_admin': await is_admin(db, settings, principal),
            'subscriptions': subscriptions,
        }
    )


@router.get('/api/me/tokens', response_model=list[TokenOut])
async def read_tokens(
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> list[TokenOut]:
    """Listing of your own API keys. The raw value is not in the DB so it cannot be returned, and no design may ever make it returnable."""
    rows = await list_api_tokens(db, principal.user_id)
    return [TokenOut.model_validate(row) for row in rows]


@router.post('/api/me/tokens', response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
async def issue_token(
    payload: TokenCreate,
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
    settings: RuntimeSettings = Depends(get_settings),
) -> TokenCreated:
    """Issue an API key. The raw value is returned in this response only and can never be retrieved again.

    Eligibility to issue is decided from the settings and roles as they are now. Passing here does not mean the key will pass later: a key's permissions are decided afresh on every request.
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
    """Revoke your own API key. The row is kept and revoked_at is set, so the record of what was stopped and when survives."""
    _reject_token_principal(principal)
    if not await revoke_api_token(db, token_id=_parse_id(token_id), user_id=principal.user_id):
        # Someone else's key is reported as not found too: an "exists but is not yours" distinction would be a way to probe for other users' key IDs.
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
    """Listing of the tags you subscribe to."""
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
    """Subscribe to a tag. Already being subscribed still counts as success: repeating the same PUT lands on the same state however many times it is sent."""
    tag = await _load_tag(db, tag_slug)
    await db.execute('INSERT IGNORE INTO tag_subscriptions (user_id, tag_id) VALUES (%s, %s)', principal.user_id, tag['id'])
    return TagOut.model_validate(tag)


@router.delete('/api/me/subscriptions/{tag_slug}', status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_tag(
    tag_slug: str,
    principal: Principal = Depends(get_principal),
    db: Database = Depends(get_db),
) -> Response:
    """Unsubscribe from a tag. Not being subscribed still counts as success, since the desired end state is the same."""
    tag = await _load_tag(db, tag_slug)
    await db.execute('DELETE FROM tag_subscriptions WHERE user_id = %s AND tag_id = %s', principal.user_id, tag['id'])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
