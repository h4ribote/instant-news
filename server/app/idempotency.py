"""Idempotency-Key for the POSTs that create a resource (an article, an ad).

A client that lost the response to a network fault resends with the same Idempotency-Key and gets the resource its first request created, instead of a second one. The record is written in the same transaction as the resource, so a key is on record exactly when its resource exists: a request that failed leaves nothing behind and may simply be retried. The key is claimed first in that transaction, before the resource is inserted, so a concurrent resend blocks on the record's unique index until the first request commits, then finds the record and replays it, and never reaches the resource's own unique constraints (an article's generated slug) while the first is still open.

A key is scoped to the user and the operation, and expires after IDEMPOTENCY_TTL_HOURS. Reusing a key with a different body is refused rather than replayed, because the first result would answer a request that was never made.

This module knows nothing about HTTP; the routers turn a replay into a response.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from aiomysql import Connection, IntegrityError
from pydantic import BaseModel

from common.db import Database

from .errors import BadRequest, IdempotencyKeyReused

IDEMPOTENCY_TTL_HOURS: Final = 24

# Visible ASCII only, as the header carries an opaque token (typically a UUID); the column holds up to 255 characters.
_KEY_PATTERN: Final = re.compile(r'[\x21-\x7e]{1,255}')

# MySQL's ER_DUP_ENTRY.
_DUPLICATE_ENTRY: Final = 1062


@dataclass(frozen=True)
class IdempotencyRequest:
    endpoint: str
    key: str
    request_hash: str


@dataclass(frozen=True)
class IdempotentResult:
    """What the first request recorded: the resource it created, and the response parts the resource alone cannot rebuild."""

    resource_id: int
    replay: dict[str, Any] | None


class IdempotencyRace(Exception):
    """Another request under the same key committed first. Raised inside the creating transaction, so the caller's insert rolls back; the caller then replays the winner."""


def idempotency_request(endpoint: str, key: str | None, payload: BaseModel) -> IdempotencyRequest | None:
    """Read the header into a request, or None when the client sent none.

    The body is fingerprinted after validation and normalization, so a resend that differs only in key order or whitespace is the same request.
    """
    if key is None:
        return None
    if not _KEY_PATTERN.fullmatch(key):
        raise BadRequest('Idempotency-Key は 1 - 255 文字の表示可能な ASCII で指定してください。')
    body = json.dumps(payload.model_dump(mode='json', exclude_unset=True), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return IdempotencyRequest(endpoint=endpoint, key=key, request_hash=hashlib.sha256(body.encode()).hexdigest())


async def find_replay(db: Database, user_id: int, request: IdempotencyRequest) -> IdempotentResult | None:
    """Return the first request's result for this key, or None when the key is new or has expired.

    A record under the same key with a different body raises IdempotencyKeyReused. An expired record is deleted here so the key can be recorded afresh.
    """
    row = await db.fetch_one(
        'SELECT id, request_hash, resource_id, replay, '
        f'created_at >= NOW() - INTERVAL {IDEMPOTENCY_TTL_HOURS} HOUR AS live '
        'FROM idempotency_keys WHERE user_id = %s AND endpoint = %s AND idempotency_key = %s',
        user_id,
        request.endpoint,
        request.key,
    )
    if row is None:
        return None
    if not row['live']:
        await db.execute('DELETE FROM idempotency_keys WHERE id = %s', row['id'])
        return None
    if row['request_hash'] != request.request_hash:
        raise IdempotencyKeyReused()
    replay = json.loads(row['replay']) if row['replay'] is not None else None
    return IdempotentResult(resource_id=int(row['resource_id']), replay=replay)


async def claim(db: Database, conn: Connection, user_id: int, request: IdempotencyRequest) -> int:
    """Claim the key at the start of the transaction that creates the resource, and return the record's ID for complete().

    Raises IdempotencyRace when another request under the key committed first. The record holds resource_id 0 until complete() fills it in; both happen in one transaction, so no other request ever reads the 0.
    """
    try:
        return await db.execute_insert(
            'INSERT INTO idempotency_keys (user_id, endpoint, idempotency_key, request_hash, resource_id) VALUES (%s, %s, %s, %s, 0)',
            user_id,
            request.endpoint,
            request.key,
            request.request_hash,
            conn=conn,
        )
    except IntegrityError as exc:
        if exc.args and exc.args[0] == _DUPLICATE_ENTRY:
            raise IdempotencyRace() from exc
        raise


async def complete(db: Database, conn: Connection, record_id: int, resource_id: int, replay: dict[str, Any] | None = None) -> None:
    """Store the created resource on the claimed record, in the same transaction as claim()."""
    await db.execute(
        'UPDATE idempotency_keys SET resource_id = %s, replay = %s WHERE id = %s',
        resource_id,
        json.dumps(replay, ensure_ascii=False) if replay is not None else None,
        record_id,
        conn=conn,
    )


async def purge_expired(db: Database, *, limit: int) -> int:
    """Delete up to limit expired records and return how many went. A lookup already ignores them; this only keeps the table from growing."""
    return await db.execute(
        f'DELETE FROM idempotency_keys WHERE created_at < NOW() - INTERVAL {IDEMPOTENCY_TTL_HOURS} HOUR ORDER BY created_at LIMIT %s',
        limit,
    )
