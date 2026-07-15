"""プラットフォームの設定の管理用 API(settings:admin)。

**settings:admin は他のどのスコープよりも強い。**管理ロール(admin.role_ids)を書き換えられる、すなわち管理者の資格そのものを配れるためである。ads:admin や taxonomy:admin が広告・分類という限られた対象を動かすのに対し、このスコープは誰が管理者であるかを決められる。これを与えたキーは実質的に管理者権限の委譲であり、対象を限って渡すことができない。管理者が発行できるスコープを 1 つに束ねてはならないという原則が、ここでは特に重い。

**秘密の設定(bot のトークン・OAuth のシークレット)の生値はこの API から返さない。**設定済みかどうかだけを返す(SettingsStore.describe が担う)。API から返した時点で、画面を開ける者すべてに bot のトークンが渡る。設定を読める資格を与えることと、その秘密そのものを持ち出す力を与えることは別である。

更新は部分更新であり、省略したキーは変更しない。これは秘密の設定を「変更しない」と表現する唯一の手段でもある(画面は秘密の生値を読めないため、全件を送る方式では秘密が必ず壊れる)。

設定を誤って画面から締め出された場合(管理ロールの設定ミス等)、この API では復旧できない。要求のたびに管理者資格を再検査するため、資格を失った本人はこの経路にも入れなくなる。復旧の手段は CLI(python -m server.cli set-setting)だけである。
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends

from common.settings import SettingsStore

from ..deps import get_settings_store, require_scope
from ..errors import BadRequest
from ..principals import Principal
from ..schemas import SettingOut, SettingsOut, SettingsUpdate, SettingsUpdateOut

# 依存性を 1 つの物として持つ。ルーター全体へ課したうえで、主体そのものを要る経路が同じ物を参照することで、FastAPI の依存性キャッシュが 1 要求につき 1 回だけ評価する。
_settings_admin: Final = require_scope('settings:admin')

# 経路ごとの宣言ではなくルーターへ課す。新しい経路を足したときに検査を書き忘れても、素通りする経路が生まれない。
router = APIRouter(tags=['admin:settings'], dependencies=[Depends(_settings_admin)])


async def _describe(store: SettingsStore) -> list[SettingOut]:
    """定義と現在値の一覧。秘密の設定の value を null に伏せるのは describe の側である。"""
    return [SettingOut.model_validate(entry) for entry in await store.describe()]


@router.get('/api/admin/settings')
async def list_settings(store: Annotated[SettingsStore, Depends(get_settings_store)]) -> SettingsOut:
    """設定の定義と現在値を一覧する。画面はこの応答から入力欄を生成する。

    秘密の設定は value が null で返り、設定済みかどうかは is_set が示す。変更できない設定は editable が偽で返り、その理由は description にある。
    """
    return SettingsOut(settings=await _describe(store))


@router.patch('/api/admin/settings')
async def update_settings(
    payload: SettingsUpdate,
    principal: Annotated[Principal, Depends(_settings_admin)],
    store: Annotated[SettingsStore, Depends(get_settings_store)],
) -> SettingsUpdateOut:
    """設定を部分更新する。updates に載せたキーだけを変更し、省略したキーは現在の値のまま残す。

    updated_by にはセッション経由・API キー経由のどちらでも principal.user_id を入れる。キーは所有者の権限を機械が借りたものであり、キー自身が設定を変更したのではない。発行者を記録することで、責任の所在は必ず人間に残る。

    未知のキー・型の不一致・値域の逸脱・変更できない設定(discord.guild_id)への変更要求は、いずれも set_many が ValueError で拒む。**これを握り潰して黙って無視してはならない。**送った側は成功したと解釈し、変更したはずの値が効いていない状態だけが残る。理由を含む文言をそのまま 400 で返す。

    検証は set_many が全件まとめて先に行うため、1 つでも不正なら何も書かれない。一部だけが適用された中途半端な設定は生まれない。
    """
    try:
        updated = await store.set_many(payload.updates, updated_by=principal.user_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return SettingsUpdateOut(updated_keys=updated, settings=await _describe(store))
