"""Rate limit headers on the article writes an API key makes.

Every response to POST /api/articles and PATCH /api/articles/{id} made with an API key reports where that key stands against its hourly limit, so an automated client can slow down before it is refused, not only learn of the limit from a 429. The figures are the same count that enforces the limit (articles.rate_limit_status), taken after the request finished, so a successful save is already counted in them.

Two header sets carry the same figures: the widely used X-RateLimit-Limit / X-RateLimit-Remaining / X-RateLimit-Reset, and RateLimit-Policy / RateLimit from the IETF httpapi working group draft. Every time value is in seconds from now, like Retry-After.

A route opts in with the track_rate_limit dependency; the middleware adds the headers to whatever response the route produced, error responses included. A request made through a session is not limited and gets no headers.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import Depends, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .articles import RATE_LIMIT_WINDOW_SECONDS, RateLimitStatus, rate_limit_status
from .deps import get_principal
from .principals import Principal

# Key in the request state holding the API key whose limit the response reports.
_STATE_KEY: Final = 'instant_news_rate_limited_token'

# The policy name in the RateLimit fields. There is one policy: article creates and updates per key per hour.
POLICY_NAME: Final = 'articles-write'

# The headers as declared in the OpenAPI document of the routes that carry them.
RATE_LIMIT_HEADERS_DOC: Final[dict[str, Any]] = {
    'X-RateLimit-Limit': {'description': 'API キーの 1 時間あたりの作成・更新の上限。API キー経由の要求にだけ付く。', 'schema': {'type': 'integer'}},
    'X-RateLimit-Remaining': {'description': 'この応答の時点で残っている回数。', 'schema': {'type': 'integer'}},
    'X-RateLimit-Reset': {'description': '窓内で最も古い保存が窓を抜け、1 回分が空くまでの秒数。使用が 0 なら 0。', 'schema': {'type': 'integer'}},
    'RateLimit-Policy': {'description': f'IETF 草案の形式の方針。"{POLICY_NAME}";q=<上限>;w={RATE_LIMIT_WINDOW_SECONDS}', 'schema': {'type': 'string'}},
    'RateLimit': {'description': f'IETF 草案の形式の残量。"{POLICY_NAME}";r=<残り>;t=<秒数>', 'schema': {'type': 'string'}},
}


async def track_rate_limit(request: Request, principal: Principal = Depends(get_principal)) -> None:
    """Mark the response of a rate-limited write to carry the key's standing. Nothing is marked for a session."""
    if principal.token_id is not None:
        setattr(request.state, _STATE_KEY, principal.token_id)


def rate_limit_headers(status: RateLimitStatus) -> dict[str, str]:
    return {
        'X-RateLimit-Limit': str(status.limit),
        'X-RateLimit-Remaining': str(status.remaining),
        'X-RateLimit-Reset': str(status.reset_seconds),
        'RateLimit-Policy': f'"{POLICY_NAME}";q={status.limit};w={RATE_LIMIT_WINDOW_SECONDS}',
        'RateLimit': f'"{POLICY_NAME}";r={status.remaining};t={status.reset_seconds}',
    }


class RateLimitHeadersMiddleware:
    """Add the headers when the response starts, once the route has finished and marked the request.

    It sits outside the exception handlers, so an error response from a marked route carries the headers too. An unhandled exception (a 500) is answered further out and carries none.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message['type'] == 'http.response.start':
                token_id = scope.get('state', {}).get(_STATE_KEY)
                if token_id is not None:
                    state = scope['app'].state
                    settings = await state.settings.snapshot()
                    status = await rate_limit_status(state.db, settings, token_id)
                    MutableHeaders(scope=message).update(rate_limit_headers(status))
            await send(message)

        await self.app(scope, receive, send_with_headers)
