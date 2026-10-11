"""Application exceptions and their translation into HTTP responses.

The exception -> HTTP mapping is concentrated here and registered by main.py through register_exception_handlers(app). The service layer knows nothing of HTTP: it raises AppError subclasses, and only this boundary maps them onto HTTP. That keeps the same service layer callable from the CLI and from background loops.

The response body has the same shape on every path: exactly one {"error": {"code": ..., "message": ..., "details": ...}}. If the shape varied by path, clients would have to hold two interpretations and would inevitably miss one of them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, ClassVar, Final

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base for deliberately raised exceptions. status_code and code map straight onto the HTTP response."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = 'internal_error'
    default_message: ClassVar[str] = 'サーバー内部でエラーが発生しました。'

    def __init__(self, message: str | None = None, *, details: Mapping[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] | None = dict(details) if details is not None else None
        super().__init__(self.message)

    def headers(self) -> dict[str, str]:
        """Headers to attach to the response. None by default."""
        return {}

    def to_body(self) -> dict[str, Any]:
        return _error_body(self.code, self.message, self.details)


class MovedPermanently(AppError):
    """The target lives at another URL now (an article whose slug changed).

    Cache-Control: no-cache makes caches revalidate before reusing the redirect. An article may take back a slug it gave up, and a 301 cached without revalidation would then bounce between the two slugs.
    """

    status_code = 301
    code = 'moved_permanently'
    default_message = 'この URL は移動しました。'

    def __init__(self, location: str, message: str | None = None) -> None:
        super().__init__(message, details={'location': location})
        self.location = location

    def headers(self) -> dict[str, str]:
        return {'Location': self.location, 'Cache-Control': 'no-cache'}


class BadRequest(AppError):
    """The request's content is invalid."""

    status_code = 400
    code = 'bad_request'
    default_message = '要求の内容が不正です。'


class ArticleError(BadRequest):
    """The article's content does not satisfy a business rule (a missing disclosure, an invalid selection, and so on)."""

    code = 'article_error'
    default_message = '記事の内容が不正です。'


class MediaError(BadRequest):
    """The image cannot be accepted (undecodable, too large, malformed, and so on)."""

    code = 'media_error'
    default_message = '画像を処理できませんでした。'


class Unauthorized(AppError):
    """Not authenticated: who the caller is remains unknown."""

    status_code = 401
    code = 'unauthorized'
    default_message = '認証が必要です。'

    def headers(self) -> dict[str, str]:
        return {'WWW-Authenticate': 'Bearer'}


class Forbidden(AppError):
    """Authenticated, but not eligible to perform the operation."""

    status_code = 403
    code = 'forbidden'
    default_message = 'この操作を行う権限がありません。'


class ArticleForbidden(Forbidden):
    """Not eligible for the operation on this article (no permission to flag breaking news, named a publisher the principal may not publish under, and so on)."""

    code = 'article_forbidden'
    default_message = 'この記事に対する操作を行う権限がありません。'


class NotFound(AppError):
    """The target does not exist.

    Returned instead of 403 for targets the caller is not eligible to view as well. A 403 reveals that something is there, and for a limited-visibility article whose very existence is secret, that alone is a leak.
    """

    status_code = 404
    code = 'not_found'
    default_message = '見つかりません。'


class Conflict(AppError):
    """Conflicts with existing state (a duplicate slug, for instance)."""

    status_code = 409
    code = 'conflict'
    default_message = '既存の状態と衝突しています。'


class PreconditionFailed(AppError):
    """The If-Match precondition does not hold: the target changed after the caller last read it."""

    status_code = 412
    code = 'precondition_failed'
    default_message = '記事は最後に読み込んだ後で更新されています。最新の内容を読み込み直してください。'


class QuotaExceeded(AppError):
    """A usage quota was exceeded (the total image bytes per user, for instance)."""

    status_code = 413
    code = 'quota_exceeded'
    default_message = '利用量の上限を超えています。'


class UnsupportedMediaType(AppError):
    """An unacceptable format."""

    status_code = 415
    code = 'unsupported_media_type'
    default_message = 'この形式は受け付けられません。'


class RateLimited(AppError):
    """A rate limit was exceeded. Passing retry_after reports the seconds until a retry in a Retry-After header."""

    status_code = 429
    code = 'rate_limited'
    default_message = '要求が多すぎます。時間をおいて再試行してください。'

    def __init__(self, message: str | None = None, *, retry_after: int | None = None, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after

    def headers(self) -> dict[str, str]:
        if self.retry_after is None:
            return {}
        return {'Retry-After': str(self.retry_after)}


# Maps HTTPException (the ones FastAPI and Starlette raise themselves) onto the same body as AppError.
_HTTP_STATUS_CODES: Final[dict[int, str]] = {
    400: 'bad_request',
    401: 'unauthorized',
    403: 'forbidden',
    404: 'not_found',
    405: 'method_not_allowed',
    409: 'conflict',
    412: 'precondition_failed',
    413: 'payload_too_large',
    415: 'unsupported_media_type',
    422: 'validation_error',
    429: 'rate_limited',
    500: 'internal_error',
    503: 'service_unavailable',
}

_INTERNAL_ERROR_MESSAGE: Final = 'サーバー内部でエラーが発生しました。'

# Default message used when an HTTPException carries no message chosen by its caller.
_HTTP_STATUS_MESSAGES: Final[dict[int, str]] = {
    400: '要求の内容が不正です。',
    401: '認証が必要です。',
    403: 'この操作を行う権限がありません。',
    404: '見つかりません。',
    405: 'この URL では使えないメソッドです。',
    409: '既存の状態と衝突しています。',
    412: '前提条件を満たしていません。',
    413: '要求が大きすぎます。',
    415: 'この形式は受け付けられません。',
    422: '要求の形式が不正です。',
    429: '要求が多すぎます。時間をおいて再試行してください。',
    503: '現在この要求を処理できません。',
}


def _error_body(code: str, message: str, details: Mapping[str, Any] | None) -> dict[str, Any]:
    error: dict[str, Any] = {'code': code, 'message': message}
    if details:
        error['details'] = jsonable_encoder(details)
    return {'error': error}


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.exception('%s %s -> %d', request.method, request.url.path, exc.status_code, exc_info=exc)
    else:
        logger.info('%s %s -> %d %s', request.method, request.url.path, exc.status_code, exc.code)
    headers = exc.headers()
    return JSONResponse(status_code=exc.status_code, content=exc.to_body(), headers=headers or None)


def _standard_phrase(status_code: int) -> str | None:
    """The standard reason phrase for a status code ('Not Found' for 404). None for an unknown code."""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return None


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Give the HTTPExceptions FastAPI and Starlette raise themselves (no such route, wrong method, and the like) the same body as AppError."""
    code = _HTTP_STATUS_CODES.get(exc.status_code, 'http_error')
    details: dict[str, Any] | None = None
    # Starlette fills in the standard English reason phrase when an HTTPException is raised without a detail. That is not a message the caller chose, so substitute our own; otherwise the response would turn English only when no route matches.
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    if detail is not None and detail == _standard_phrase(exc.status_code):
        detail = None
    if detail is not None:
        message = detail
    else:
        message = _HTTP_STATUS_MESSAGES.get(exc.status_code, _standard_phrase(exc.status_code) or _INTERNAL_ERROR_MESSAGE)
        # A structured detail is passed through rather than dropped.
        details = {'detail': exc.detail} if exc.detail and not isinstance(exc.detail, str) else None
    if exc.status_code >= 500:
        logger.error('%s %s -> %d %s', request.method, request.url.path, exc.status_code, code)
    return JSONResponse(status_code=exc.status_code, content=_error_body(code, message, details), headers=getattr(exc, 'headers', None))


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Response for a failed request validation. Which fields are invalid goes into details.

    pydantic's errors() can put values that JSON cannot represent, such as exception objects, into ctx, so it is never embedded raw.
    """
    logger.info('%s %s -> 422 validation_error', request.method, request.url.path)
    return JSONResponse(status_code=422, content=_error_body('validation_error', '要求の形式が不正です。', {'errors': jsonable_encoder(exc.errors())}))


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """An unexpected exception. The details go to the log only and never into the response, because exception text mixes in table names, SQL and file paths."""
    logger.exception('%s %s -> 500 未処理の例外', request.method, request.url.path)
    return JSONResponse(status_code=500, content=_error_body('internal_error', _INTERNAL_ERROR_MESSAGE, None))


def register_exception_handlers(app: FastAPI) -> None:
    """Register the exception handlers on the FastAPI app.

    Starlette walks the exception type's MRO to find a handler, so registering AppError alone picks up every subclass.
    """
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
