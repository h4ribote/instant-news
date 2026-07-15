"""アプリケーションの例外と、HTTP 応答への変換。

例外 → HTTP の対応はこのモジュールへ集約し、main.py が register_exception_handlers(app) で登録する。サービス層は HTTP を知らずに AppError の派生を送出し、境界でだけ HTTP へ写す。こうしておくと、同じサービス層を CLI やバックグラウンドのループからも呼べる。

応答の本文はどの経路でも同じ形にする。{"error": {"code": ..., "message": ..., "details": ...}} の 1 つだけを返す。経路によって形が変わると、クライアントは複数の解釈を持たねばならず、必ずどちらかを取りこぼす。
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
    """意図して送出する例外の基底。status_code と code がそのまま HTTP 応答へ写る。"""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = 'internal_error'
    default_message: ClassVar[str] = 'サーバー内部でエラーが発生しました。'

    def __init__(self, message: str | None = None, *, details: Mapping[str, Any] | None = None) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] | None = dict(details) if details is not None else None
        super().__init__(self.message)

    def headers(self) -> dict[str, str]:
        """応答へ添えるヘッダ。既定では無い。"""
        return {}

    def to_body(self) -> dict[str, Any]:
        return _error_body(self.code, self.message, self.details)


class BadRequest(AppError):
    """要求の内容が不正。"""

    status_code = 400
    code = 'bad_request'
    default_message = '要求の内容が不正です。'


class ArticleError(BadRequest):
    """記事の内容が業務上の規則を満たしていない(開示の欠落・不正な指定など)。"""

    code = 'article_error'
    default_message = '記事の内容が不正です。'


class MediaError(BadRequest):
    """画像を受け付けられない(復号できない・大きすぎる・形式が不正など)。"""

    code = 'media_error'
    default_message = '画像を処理できませんでした。'


class Unauthorized(AppError):
    """認証されていない。誰であるかが分からない状態。"""

    status_code = 401
    code = 'unauthorized'
    default_message = '認証が必要です。'

    def headers(self) -> dict[str, str]:
        return {'WWW-Authenticate': 'Bearer'}


class Forbidden(AppError):
    """認証はされているが、その操作を行う資格が無い。"""

    status_code = 403
    code = 'forbidden'
    default_message = 'この操作を行う権限がありません。'


class ArticleForbidden(Forbidden):
    """記事に対する操作の資格が無い(速報の権限が無い、名乗れない報道社を指定した、など)。"""

    code = 'article_forbidden'
    default_message = 'この記事に対する操作を行う権限がありません。'


class NotFound(AppError):
    """対象が存在しない。

    閲覧の資格が無い対象にも 403 ではなくこれを返す。403 は「そこに何かがある」ことを教えてしまい、存在自体が秘密である限定公開の記事では、それだけで漏洩になる。
    """

    status_code = 404
    code = 'not_found'
    default_message = '見つかりません。'


class Conflict(AppError):
    """既存の状態と衝突する(slug の重複など)。"""

    status_code = 409
    code = 'conflict'
    default_message = '既存の状態と衝突しています。'


class QuotaExceeded(AppError):
    """利用量の上限を超えた(1 ユーザーあたりの画像の合計バイト数など)。"""

    status_code = 413
    code = 'quota_exceeded'
    default_message = '利用量の上限を超えています。'


class UnsupportedMediaType(AppError):
    """受け付けられない形式。"""

    status_code = 415
    code = 'unsupported_media_type'
    default_message = 'この形式は受け付けられません。'


class RateLimited(AppError):
    """単位時間あたりの上限を超えた。retry_after を与えると Retry-After ヘッダで再試行までの秒数を伝える。"""

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


# HTTPException(FastAPI や Starlette が直接送出するもの)を AppError と同じ本文へ揃えるための対応表。
_HTTP_STATUS_CODES: Final[dict[int, str]] = {
    400: 'bad_request',
    401: 'unauthorized',
    403: 'forbidden',
    404: 'not_found',
    405: 'method_not_allowed',
    409: 'conflict',
    413: 'payload_too_large',
    415: 'unsupported_media_type',
    422: 'validation_error',
    429: 'rate_limited',
    500: 'internal_error',
    503: 'service_unavailable',
}

_INTERNAL_ERROR_MESSAGE: Final = 'サーバー内部でエラーが発生しました。'

# HTTPException が呼び出し元の選んだ文言を持たない場合に使う既定の文言。
_HTTP_STATUS_MESSAGES: Final[dict[int, str]] = {
    400: '要求の内容が不正です。',
    401: '認証が必要です。',
    403: 'この操作を行う権限がありません。',
    404: '見つかりません。',
    405: 'この URL では使えないメソッドです。',
    409: '既存の状態と衝突しています。',
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
    """その状態コードの標準の理由句(404 なら 'Not Found')。未知のコードなら None。"""
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return None


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """FastAPI や Starlette が直接送出する HTTPException(経路が無い・メソッドが違う等)も AppError と同じ本文へ揃える。"""
    code = _HTTP_STATUS_CODES.get(exc.status_code, 'http_error')
    details: dict[str, Any] | None = None
    # detail を省いて送出された HTTPException には、Starlette が標準の理由句(英語)を入れる。これは呼び出し元が選んだ文言ではないため、自前の文言へ差し替える(そうしないと、経路が無いときだけ応答が英語になる)。
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    if detail is not None and detail == _standard_phrase(exc.status_code):
        detail = None
    if detail is not None:
        message = detail
    else:
        message = _HTTP_STATUS_MESSAGES.get(exc.status_code, _standard_phrase(exc.status_code) or _INTERNAL_ERROR_MESSAGE)
        # detail に構造化された値を入れて送出された場合も落とさずに渡す。
        details = {'detail': exc.detail} if exc.detail and not isinstance(exc.detail, str) else None
    if exc.status_code >= 500:
        logger.error('%s %s -> %d %s', request.method, request.url.path, exc.status_code, code)
    return JSONResponse(status_code=exc.status_code, content=_error_body(code, message, details), headers=getattr(exc, 'headers', None))


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """要求の検証に失敗したときの応答。どの項目が不正なのかは details へ入れる。

    pydantic の errors() は例外オブジェクトなど JSON にできない値を ctx に含めることがあるため、そのまま載せない。
    """
    logger.info('%s %s -> 422 validation_error', request.method, request.url.path)
    return JSONResponse(status_code=422, content=_error_body('validation_error', '要求の形式が不正です。', {'errors': jsonable_encoder(exc.errors())}))


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """想定外の例外。内容はログにだけ残し、応答には出さない(例外の文言にはテーブル名・SQL・ファイルパスなどが混ざる)。"""
    logger.exception('%s %s -> 500 未処理の例外', request.method, request.url.path)
    return JSONResponse(status_code=500, content=_error_body('internal_error', _INTERNAL_ERROR_MESSAGE, None))


def register_exception_handlers(app: FastAPI) -> None:
    """例外ハンドラを FastAPI アプリへ登録する。

    Starlette は例外の型の MRO を辿ってハンドラを探すため、AppError を 1 つ登録すれば全ての派生が拾われる。
    """
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
