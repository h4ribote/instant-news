"""Login and logout routes for Discord OAuth2.

Both are entered as top-level browser navigations, so they answer with a redirect rather than JSON. API keys (Authorization: Bearer) are not handled here: a key presents an already-issued credential, so it has no place in the authorization flow.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse

from ..auth import (
    OAUTH_CALLBACK_PATH,
    OAUTH_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    begin_login,
    clear_session_cookie,
    clear_state_cookie,
    complete_login,
    destroy_session,
    set_session_cookie,
    set_state_cookie,
)
from ..deps import get_config, get_db, get_settings
from ..errors import BadRequest
from ..principals import hash_secret

logger = logging.getLogger(__name__)

router = APIRouter(tags=['auth'])


# Both entry points answer with a 302 redirect, declared as such so the OpenAPI document does not describe a JSON body that never comes.
_REDIRECT: Final[dict[int | str, dict[str, Any]]] = {302: {'description': '転送する。', 'headers': {'Location': {'description': '転送先。', 'schema': {'type': 'string'}}}}}


@router.get('/auth/login', status_code=status.HTTP_302_FOUND, response_class=RedirectResponse, responses=_REDIRECT)
async def login(request: Request, redirect_to: str | None = None) -> RedirectResponse:
    """Send the user to Discord's consent screen.

    redirect_to is where the user lands after login, and is restricted to a path inside this site (sanitize_redirect_to does the checking). It is stashed in oauth_states rather than handed back to the browser, so the requested destination cannot be rewritten over the URL round trip.

    Of the authorization URL's ingredients, client_id is read from the settings table on every request, while the redirect_uri is derived from base_url in config.yaml (auth.oauth_redirect_uri). config is needed for that derivation and to decide whether the state cookie gets Secure.
    """
    db = await get_db(request)
    config = await get_config(request)
    settings = await get_settings(request)
    authorize_url, state = await begin_login(db, config, settings, redirect_to)
    # Pin the status to 302: FastAPI's default of 307 preserves the method, which would open a path that sends something other than GET to the consent screen.
    response = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, config, state)
    return response


@router.get(OAUTH_CALLBACK_PATH, status_code=status.HTTP_302_FOUND, response_class=RedirectResponse, responses=_REDIRECT)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """Take the authorization result, issue a session, and send the user on to the post-login destination.

    code and state are deliberately not declared as required parameters. When the user declines, or the flow fails, Discord comes back carrying only error and no code, so requiring them would surface the perfectly normal "the user declined" path as a format error (422).
    """
    db = await get_db(request)
    config = await get_config(request)
    if error:
        # A user declining authorization also lands here. Report it in our own words instead of showing Discord's verbatim.
        logger.info('Discord の認可が完了しませんでした: error=%s description=%s', error, error_description)
        raise BadRequest('Discord の認可が完了しませんでした。もう一度ログインしてください。')
    if not code or not state:
        raise BadRequest('認可の応答に code と state が含まれていません。もう一度ログインしてください。')
    # Settings need not be read until the authorization has actually succeeded. They are read on every request, so a client_secret replaced from the UI is used for the exchange from the next login onward.
    raw_session, redirect_to = await complete_login(
        db,
        config,
        await get_settings(request),
        code=code,
        state=state,
        # Checks that this is the browser that began the authorization. The state in the DB alone cannot close the path where an attacker starts an authorization and makes the victim walk its callback (logging the victim in under the attacker's account).
        state_cookie=request.cookies.get(OAUTH_STATE_COOKIE_NAME),
    )
    response = RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, config, raw_session)
    # Don't leave a state behind once it has served its purpose. The next login issues a new one.
    clear_state_cookie(response, config)
    return response


@router.post('/auth/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    """Destroy the session and drop the cookie.

    Requires no authentication. Logout should hold even when called with an already-invalid cookie; "you have to be logged in to log out" is nothing but a dead end for the user. The DB row is deleted, so even if the cookie survives for some reason, that session never passes again. A POST from another site carries no cookie (SameSite=Lax), so nobody can be walked into an unintended logout either.
    """
    db = await get_db(request)
    config = await get_config(request)
    raw_session = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_session:
        await destroy_session(db, hash_secret(raw_session))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, config)
    return response
