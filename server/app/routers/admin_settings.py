"""Admin API for the platform settings (settings:admin).

**settings:admin is stronger than any other scope.** It can rewrite the admin roles (admin.role_ids), which is to say it can hand out admin eligibility itself. Where ads:admin and taxonomy:admin move a bounded set of things (ads, taxonomy), this scope decides who is an admin at all. A key holding it is effectively a delegation of admin permission, and there is no way to narrow what it is delegated over. The principle that the scopes an admin can issue are kept separate rather than bundled into one weighs especially heavily here.

**The raw value of a secret setting (the bot token, the OAuth client secret) is never returned from this API.** Only whether it is set, which SettingsStore.describe decides. Returning it once means handing the bot token to everyone who can open the UI. Granting the eligibility to read settings is not the same as granting the power to carry the secret itself away.

Updates are partial: an omitted key keeps its current value. That is also the only way to say "leave this one alone" about a secret setting, because the UI cannot read a secret's raw value, so a send-every-key scheme would destroy every secret.

If a settings change locks you out of the admin screen (a misconfigured admin role, say), this API cannot undo it. Admin eligibility is re-checked on every request, so the person who lost it cannot get in through this path either. The only way back is the CLI (python -m server.cli set-setting).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends

from common.settings import SettingsStore

from ..deps import get_settings_store, require_scope
from ..errors import BadRequest
from ..principals import Principal
from ..schemas import SettingOut, SettingsOut, SettingsUpdate, SettingsUpdateOut

# Held as a single object: imposed on the whole router, and referenced by the route that needs the principal itself, so FastAPI's dependency cache evaluates it only once per request.
_settings_admin: Final = require_scope('settings:admin')

# Imposed on the router rather than declared per route: forgetting the check when adding a route cannot create a route that passes straight through.
router = APIRouter(tags=['admin:settings'], dependencies=[Depends(_settings_admin)])


async def _describe(store: SettingsStore) -> list[SettingOut]:
    """Definitions and current values. Masking a secret setting's value to null is describe's job, not this one's."""
    return [SettingOut.model_validate(entry) for entry in await store.describe()]


@router.get('/api/admin/settings')
async def list_settings(store: Annotated[SettingsStore, Depends(get_settings_store)]) -> SettingsOut:
    """List the settings definitions and their current values. The UI builds its input fields from this response.

    A secret setting comes back with value null, and is_set says whether it is set. A setting that cannot be changed comes back with editable false, and description says why.
    """
    return SettingsOut(items=await _describe(store))


@router.patch('/api/admin/settings')
async def update_settings(
    payload: SettingsUpdate,
    principal: Annotated[Principal, Depends(_settings_admin)],
    store: Annotated[SettingsStore, Depends(get_settings_store)],
) -> SettingsUpdateOut:
    """Partially update the settings. Only the keys listed in updates change; an omitted key keeps its current value.

    updated_by gets principal.user_id on the session path and the API key path alike. A key is the owner's permission borrowed by a machine; the key itself did not change the setting. Recording the issuer keeps responsibility with a human.

    An unknown key, a type mismatch, a value outside the range, and a change to a setting that is not editable (discord.guild_id) are all rejected by set_many with ValueError. **Never swallow that and silently ignore it.** The caller reads the request as a success, and all that is left is a value they believe they changed that is not in effect. Return the message, reason and all, as a 400.

    set_many validates every entry up front, so a single bad key means nothing is written and no half-applied settings state can arise.
    """
    try:
        updated = await store.set_many(payload.updates, updated_by=principal.user_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return SettingsUpdateOut(updated_keys=updated, items=await _describe(store))
