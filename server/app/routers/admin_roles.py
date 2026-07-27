"""The guild's role listing (the admin screen's options).

The admin screen deals with settings that point at roles (admin.role_ids and the breaking-news roles) and with publishers' affiliation roles. Entering those as raw Discord snowflakes would have an admin turn on Developer Mode in Discord, right-click each role and paste the ID across by hand. **There is no reason to do that to admin.role_ids, the field where a paste error locks you out.** The bot already syncs role names, colours and ordering into discord_roles; only a route returning them was missing.

**An API key does not pass this route (deps.ensure_admin rejects it).** A key may borrow only the power its scope names, and no scope names "read roles". Both settings:admin and taxonomy:admin need the role listing, but tying it to either one would let that key read past its own remit. The Web UI is what needs this listing, and the Web UI always runs on a session, so this suffices. Add a scope naming it once a machine client comes to need the role listing.

The taxonomy admin API (admin_taxonomy) checks that role_id exists, so a client driving roles through a key still gets an answer of the form "that role has not been imported" without holding this listing.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends

from common.db import Database

from ..deps import get_db, require_admin
from ..principals import ADMINISTRATOR_BIT
from ..schemas import AdminRoleOut

# Imposed on the router rather than declared per route: forgetting the check when adding a route cannot create a route that passes straight through.
router = APIRouter(tags=['admin:roles'], dependencies=[Depends(require_admin())])

# Ordered to match Discord's own display (position descending). Admins choose while comparing this screen against Discord's role settings, so a different order leaves them hunting for the same name by eye.
# permissions is resolved to whether it is Administrator before returning; the bits themselves never reach the response.
_ROLES_SQL: Final = """
    SELECT id, name, color, position, (permissions & %s) <> 0 AS is_administrator
      FROM discord_roles
     ORDER BY position DESC, id
"""


@router.get('/api/admin/roles')
async def list_roles(db: Annotated[Database, Depends(get_db)]) -> list[AdminRoleOut]:
    """The target guild's role listing. Used for the fields of settings that point at roles and for the publisher affiliation-role options.

    The source of truth is Discord; this table is a snapshot the bot mirrors as it receives role create / update / delete events. A freshly created role can stay absent for a very short while, so the UI must keep direct ID entry as well.
    """
    rows = await db.fetch_all(_ROLES_SQL, ADMINISTRATOR_BIT)
    return [AdminRoleOut.model_validate(row) for row in rows]
