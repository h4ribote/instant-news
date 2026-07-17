"""Principals (session / API key) and the permission check.

Sessions and API keys are two authentication paths folded into one type, Principal. Layers above need only ask "who is this and what do they hold", never "how did they authenticate".

**An API key's permissions must not be frozen at issue time.** A key is a machine borrowing its owner's permissions; the key itself holds none. Permissions are decided on every request from the current snapshot of users / user_roles. Freezing them would leave a demoted admin's key holding admin permissions, and removing the role — the most natural remedy — would stop working. This design is what makes role sync (performed by the bot) the effective means of revoking permissions.

Raw values of secrets (session tokens, API keys) are never stored. The DB holds only the SHA-256; lookup hashes the presented raw value by the same procedure and compares. A leaked DB cannot be turned back into a session or a key to impersonate anyone.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from aiomysql import Connection

from common.config import Config
from common.db import Database
from common.settings import RuntimeSettings

# Administrator in Discord's permission bitfield. A holder of a role with this bit set can perform every operation in that guild.
ADMINISTRATOR_BIT: Final = 1 << 3

# The source of truth for this value set is api_token_scopes.scope in migrations/0001_initial.sql.
Scope = Literal['articles:write', 'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin']

# Scopes that require admin eligibility. Carrying one on a key is not enough: the owner is checked for admin on every request.
# settings:admin can rewrite admin.role_ids, i.e. hand out admin eligibility itself, so it is stronger than any other scope. It still needs no special case, because membership in this set is precisely the mechanism that asks "is the owner an admin at this very moment" on every request. Once the owner loses admin, their settings:admin key can no longer even re-grant its owner an admin role.
ADMIN_SCOPES: Final[frozenset[str]] = frozenset({'articles:write:any', 'ads:admin', 'taxonomy:admin', 'settings:admin'})

# Scope implications: the dict key is the requested scope, the value is the set of scopes that can satisfy that request.
# articles:write:any is "create/update an article naming any author and publisher", which subsumes create/update with the author pinned to the owner (articles:write). Without the implication, a key granted only articles:write:any could not write a single article — the exact opposite of what the scope is defined to mean.
# Even when a request is allowed via an implication, the admin-eligibility check for the scope that allowed it must still run (deps.ensure_scope does this). articles:write:any is an admin scope; using it for an articles:write-equivalent operation does not excuse that check.
SCOPE_IMPLICATIONS: Final[dict[str, frozenset[str]]] = {
    'articles:write': frozenset({'articles:write', 'articles:write:any'}),
}


def granting_scopes(scope: str) -> frozenset[str]:
    """Return the set of scopes that can satisfy this request; a scope with no implication returns only itself."""
    return SCOPE_IMPLICATIONS.get(scope, frozenset({scope}))


def hash_secret(raw: str) -> str:
    """Hash a session token / API key raw value into the SHA-256 (64 hex digits) used for storage and lookup.

    Unlike passwords, these are values we generate ourselves with ample randomness, so there is no room for brute force and no stretching is needed. sessions.token_hash and api_tokens.token_hash are CHAR(64) to match the digit count of this output.
    """
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def secret_equal(left: str, right: str) -> bool:
    """Compare secret strings in constant time.

    A plain == stops at the position of the first mismatch, so the time the comparison takes reveals how many leading characters matched. hmac.compare_digest does not accept non-ASCII str, so encode to UTF-8 bytes before passing them in.
    """
    return hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


@dataclass(frozen=True)
class Principal:
    """The principal making the request.

    role_ids is the snapshot of *now*, re-read on every request. token_scopes is meaningful only for API-key requests and expresses the restriction placed on that key. A scope is not a permission in itself: it is a ceiling on how much of the owner's permissions the key may borrow (once the owner is no longer an admin, even a key carrying ads:admin will not pass an admin operation).
    """

    user_id: int
    discord_user_id: int
    is_guild_member: bool
    is_guild_owner: bool
    role_ids: frozenset[int]
    # api_tokens.id for an API-key request, None for a session request. Recorded on article revisions and image ingests as "which key actually posted this".
    token_id: int | None
    token_scopes: frozenset[str]

    @property
    def via_token(self) -> bool:
        """Whether this request came via an API key."""
        return self.token_id is not None


async def load_principal_for_user(
    db: Database,
    user_id: int,
    *,
    token_id: int | None = None,
    token_scopes: Iterable[str] = (),
    conn: Connection | None = None,
) -> Principal | None:
    """Build a principal from users.id; None if there is no row.

    Both the session path and the API-key path converge here once their authentication lookup is done. This is the only place that reads the inputs to the permission check (guild membership, owner, held roles), so those inputs can never vary by path.
    """
    row = await db.fetch_one(
        'SELECT id, discord_user_id, is_guild_member, is_guild_owner FROM users WHERE id = %s',
        user_id,
        conn=conn,
    )
    if row is None:
        return None
    role_rows = await db.fetch_all('SELECT role_id FROM user_roles WHERE user_id = %s', user_id, conn=conn)
    return Principal(
        user_id=int(row['id']),
        discord_user_id=int(row['discord_user_id']),
        is_guild_member=bool(row['is_guild_member']),
        is_guild_owner=bool(row['is_guild_owner']),
        role_ids=frozenset(int(role_row['role_id']) for role_row in role_rows),
        token_id=token_id,
        token_scopes=frozenset(token_scopes),
    )


async def load_principal_from_session(db: Database, config: Config, token_hash: str) -> Principal | None:
    """Resolve a principal from a session token's SHA-256; None if invalid.

    config is taken only for symmetry with the API-key path (the session lookup itself does not depend on config).
    """
    row = await db.fetch_one(
        'SELECT token_hash, user_id FROM sessions WHERE token_hash = %s AND expires_at > NOW()',
        token_hash,
    )
    # The lookup goes through the indexed token_hash, but the match is decided here: the DB collation (utf8mb4_0900_ai_ci) is case-insensitive, so SQL's = alone is never the grounds for a match.
    if row is None or not secret_equal(str(row['token_hash']), token_hash):
        return None
    principal = await load_principal_for_user(db, int(row['user_id']))
    if principal is None:
        return None
    await db.execute('UPDATE sessions SET last_seen_at = NOW() WHERE token_hash = %s', token_hash)
    return principal


async def load_principal_from_token(db: Database, config: Config, raw_token: str) -> Principal | None:
    """Resolve a principal from an API key's raw value; None if invalid.

    A key with revoked_at set, or past expires_at, is invalid. On top of that, **if the owner is not a member of the target guild, authentication does not succeed even though the lookup matched**. A key is a machine borrowing its owner's permissions, and once the owner has left the guild there is nobody left to borrow from. Rather than stripping permissions down to an "authenticated but powerless" principal, the credential itself is rejected.
    """
    token_hash = hash_secret(raw_token)
    row = await db.fetch_one(
        'SELECT id, token_hash, user_id FROM api_tokens '
        'WHERE token_hash = %s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW())',
        token_hash,
    )
    if row is None or not secret_equal(str(row['token_hash']), token_hash):
        return None
    token_id = int(row['id'])
    # Recorded before the eligibility check. last_used_at is the fact that this key was used, which is a different thing from whether the request was allowed (a key that no longer passes yet keeps being hammered is exactly the situation worth noticing).
    await db.execute('UPDATE api_tokens SET last_used_at = NOW() WHERE id = %s', token_id)
    scope_rows = await db.fetch_all('SELECT scope FROM api_token_scopes WHERE token_id = %s', token_id)
    principal = await load_principal_for_user(
        db,
        int(row['user_id']),
        token_id=token_id,
        token_scopes=(str(scope_row['scope']) for scope_row in scope_rows),
    )
    if principal is None or not principal.is_guild_member:
        return None
    return principal


async def is_admin(db: Database, settings: RuntimeSettings, principal: Principal) -> bool:
    """Decide whether this principal meets admin eligibility at the time of the request.

    Any one of these makes an admin:
      (a) The guild owner (users.is_guild_owner). The owner holds full authority over the guild regardless of roles, so looking at roles alone always misses them.
      (b) One of the held roles carries Discord's Administrator permission (1 << 3 in discord_roles.permissions).
      (c) One of the held roles is listed in the settings' admin.role_ids. Administrator permits every operation in the guild and is extremely strong; this is the escape hatch that avoids handing it out to someone you only want to run this platform.

    **Do not re-read settings inside this function.** The caller passes settings resolved from the settings table per request (in server, a dependency does this). Passing settings read at startup would mean that anyone who was an admin by that role alone stays an admin until restart, even after the admin role is removed in the UI. The role side of the inputs (principal.role_ids) is a current snapshot re-read on every request, and a skewed state where only the settings side is frozen must never be created.
    """
    if principal.is_guild_owner:
        return True
    if not principal.role_ids:
        return False
    if not principal.role_ids.isdisjoint(settings.admin.role_ids):
        return True
    role_ids = tuple(principal.role_ids)
    # The IN clause is built with one %s per element. Only the placeholders are interpolated; the values always go through as arguments.
    placeholders = ', '.join(['%s'] * len(role_ids))
    found = await db.fetch_val(
        f'SELECT 1 FROM discord_roles WHERE id IN ({placeholders}) AND (permissions & %s) <> 0 LIMIT 1',
        *role_ids,
        ADMINISTRATOR_BIT,
    )
    return found is not None


def has_scope(principal: Principal, scope: str) -> bool:
    """Whether the operation this scope names falls within the range granted to the principal.

    A scope restricts a key, not a person. A session principal (via_token false) is acting through the Web UI with their own permissions and has no notion of a key's range, so this always returns True. **That does not mean "may do anything".** Whether an admin operation is allowed is handled separately by is_admin's eligibility check, which this function does not stand in for.

    The check is not an exact match: it goes through implications (SCOPE_IMPLICATIONS). A higher scope subsuming a lower operation is the definition itself, and making every call site spell that relation out means the one site that forgets silently rejects a key issued exactly to spec. Returning True still does not mean the admin-eligibility check has been done; deps.ensure_scope decides what to check when a request was allowed via an implication.
    """
    if not principal.via_token:
        return True
    return not principal.token_scopes.isdisjoint(granting_scopes(scope))
