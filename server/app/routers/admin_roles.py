"""guild のロールの一覧(管理画面の選択肢)。

管理画面は、ロールを指す設定(admin.role_ids・速報の各ロール)と報道社の所属ロールを扱う。それらを Discord のスノーフレークの直接入力にすると、管理者は Discord で開発者モードを有効にし、ロールを右クリックして ID を写して貼ることになる。**貼り間違いがそのまま自分の締め出しになる欄(admin.role_ids)まで同じ扱いにする理由は無い。**ロールの名前・色・並び順は bot が discord_roles へ同期しており、それを返す経路が無かっただけである。

**この経路は API キーでは通らない(deps.ensure_admin が拒む)。**キーに貸し出せるのはスコープが名指しした力だけであり、「ロールを読む」ことを名指しするスコープは無い。settings:admin と taxonomy:admin の双方がロールの一覧を要するからといって、どちらか一方のスコープに紐づけると、そのキーが自分の担当を越えたものを読めるようになる。この一覧を必要とするのは Web UI であり、Web UI は常にセッションで動くため、これで足りる。機械のクライアントがロールの一覧を要するようになった時点で、それを名指すスコープを設けること。

分類の管理 API(admin_taxonomy)が role_id の存在を検査してくれるため、キーからロールを扱うクライアントは、この一覧を持たなくても「そのロールは取り込まれていない」という形の答えを得られる。
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends

from common.db import Database

from ..deps import get_db, require_admin
from ..principals import ADMINISTRATOR_BIT
from ..schemas import AdminRoleOut

# 経路ごとの宣言ではなくルーターへ課す。新しい経路を足したときに検査を書き忘れても、素通りする経路が生まれない。
router = APIRouter(tags=['admin:roles'], dependencies=[Depends(require_admin())])

# 並びは Discord の表示と揃える(position の降順)。管理者はこの画面と Discord のロール設定を見比べながら選ぶため、並びが違うと同じ名前を目で探し直すことになる。
# permissions は Administrator かどうかへ解決してから返し、ビットそのものは応答に載せない。
_ROLES_SQL: Final = """
    SELECT id, name, color, position, (permissions & %s) <> 0 AS is_administrator
      FROM discord_roles
     ORDER BY position DESC, id
"""


@router.get('/api/admin/roles')
async def list_roles(db: Annotated[Database, Depends(get_db)]) -> list[AdminRoleOut]:
    """対象 guild のロールの一覧。ロールを指す設定の欄と、報道社の所属ロールの選択肢に使う。

    真実のソースは Discord であり、この表は bot がロールの作成・更新・削除のイベントを受けて写したスナップショットである。作ったばかりのロールがごく短い間まだ現れないことがあるため、画面は ID の直接入力も残すこと。
    """
    rows = await db.fetch_all(_ROLES_SQL, ADMINISTRATOR_BIT)
    return [AdminRoleOut.model_validate(row) for row in rows]
