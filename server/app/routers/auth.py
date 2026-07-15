"""Discord OAuth2 によるログインとログアウトの経路。

いずれもブラウザのトップレベルの遷移として使われるため、応答は JSON ではなくリダイレクトである。API キー(Authorization: Bearer)はここでは扱わない。キーは発行済みの資格情報を提示するものであり、認可フローを通る余地が無い。
"""

from __future__ import annotations

import logging

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


@router.get('/auth/login')
async def login(request: Request, redirect_to: str | None = None) -> RedirectResponse:
    """Discord の認可画面へ送る。

    redirect_to はログイン後の戻り先。このサイト内のパスに限る(検査は sanitize_redirect_to が行う)。値はブラウザへ返さず oauth_states に預けるため、戻り先の指定が URL の往復で書き換わることがない。

    認可 URL の材料のうち client_id は要求のたびに settings 表から読み、認可後に戻る URL は config.yaml の base_url から導出する(auth.oauth_redirect_uri)。config はその導出と、state の Cookie に Secure を付けるかどうかの判断に要る。
    """
    db = await get_db(request)
    config = await get_config(request)
    settings = await get_settings(request)
    authorize_url, state = await begin_login(db, config, settings, redirect_to)
    # 302 を明示する。FastAPI の既定である 307 はメソッドを保存するため、認可画面へ GET 以外で送ろうとする経路が生まれうる。
    response = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    set_state_cookie(response, config, state)
    return response


@router.get(OAUTH_CALLBACK_PATH)
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """認可の結果を受け取り、セッションを発行して戻り先へ送る。

    code / state を必須の引数として宣言しない。認可を断った場合や失敗した場合、Discord は code を付けずに error だけを載せて戻してくるため、必須にすると「利用者が断った」という正常な経路が形式の誤り(422)として現れてしまう。
    """
    db = await get_db(request)
    config = await get_config(request)
    if error:
        # 利用者が認可を断った場合もここへ来る。Discord の文言をそのまま見せず、こちらの言葉で伝える。
        logger.info('Discord の認可が完了しませんでした: error=%s description=%s', error, error_description)
        raise BadRequest('Discord の認可が完了しませんでした。もう一度ログインしてください。')
    if not code or not state:
        raise BadRequest('認可の応答に code と state が含まれていません。もう一度ログインしてください。')
    # 設定を読むのは認可が成立してからでよい。要求のたびに読むため、client_secret を画面から差し替えれば次のログインからその値で交換される。
    raw_session, redirect_to = await complete_login(
        db,
        config,
        await get_settings(request),
        code=code,
        state=state,
        # 認可を開始したブラウザであることの照合。DB の state だけでは、攻撃者が開始した認可の戻り先を被害者に踏ませる経路(攻撃者のアカウントで被害者をログインさせる)を塞げない。
        state_cookie=request.cookies.get(OAUTH_STATE_COOKIE_NAME),
    )
    response = RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, config, raw_session)
    # 役目を終えた state を残さない。次のログインでは新しい state を発行する。
    clear_state_cookie(response, config)
    return response


@router.post('/auth/logout', status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    """セッションを破棄し、Cookie を落とす。

    認証を要求しない。既に無効な Cookie で呼ばれてもログアウトは成立しているべきであり、「ログアウトするにはログインしていなければならない」という状態は利用者にとって手詰まりでしかない。DB の行を消すため、Cookie が何らかの理由で残っても、そのセッションは二度と通らない。他サイトからの POST で呼ばれても Cookie が載らない(SameSite=Lax)ため、意図しないログアウトを踏ませることもできない。
    """
    db = await get_db(request)
    config = await get_config(request)
    raw_session = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_session:
        await destroy_session(db, hash_secret(raw_session))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, config)
    return response
