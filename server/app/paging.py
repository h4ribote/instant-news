"""Cursor pagination shared by every listing that grows with activity.

A cursor is opaque to clients: base64url of a small JSON object. It names the listing it was issued for (the listing's name and a hash of its filters), so a cursor handed to another listing or to other filters is refused rather than answered with rows that belong to neither.

Most listings page by keyset: the cursor carries the sort key of the row at the page boundary and a direction, and the next query continues strictly after (or before) that key. Rows inserted meanwhile therefore never shift a later page, which is what OFFSET paging gets wrong, and a deep page costs the same as the first. The sort keys must be non-null and end in a unique column, so every row has exactly one place in the order.

Search is the exception: relevance scores move for every document as the index grows, so no key stays put. Its cursor carries a position instead.

Cursor values reach SQL only as placeholders. A forged cursor can at most select a different window of the same listing, and the caller's visibility condition is part of every query regardless.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from fastapi import Query

from common.db import Database

from .errors import InvalidCursor

_CURSOR_VERSION: Final = 1
_AFTER: Final = 'a'
_BEFORE: Final = 'b'

KeyKind = Literal['int', 'datetime']


@dataclass(frozen=True)
class SortKey:
    """One column of a listing's order. expr is SQL written as a constant by the caller; value reads the same quantity from a fetched row."""

    expr: str
    value: Callable[[Mapping[str, Any]], Any]
    kind: KeyKind
    descending: bool = False


@dataclass(frozen=True)
class KeysetOrder:
    keys: tuple[SortKey, ...]

    def order_by(self, *, reverse: bool = False) -> str:
        parts = []
        for key in self.keys:
            descending = key.descending != reverse
            parts.append(f'{key.expr} {"DESC" if descending else "ASC"}')
        return ', '.join(parts)

    def beyond(self, values: Sequence[Any], *, before: bool) -> tuple[str, list[Any]]:
        """The condition selecting rows strictly after (or before) the boundary key in this order, and its arguments.

        It is the row-value comparison expanded per column, because the columns do not all run in the same direction: (k1 > v1) OR (k1 = v1 AND k2 > v2) OR ..., with > turned into < for a descending column and both flipped for before.
        """
        clauses: list[str] = []
        args: list[Any] = []
        for index, key in enumerate(self.keys):
            terms = [f'{prior.expr} = %s' for prior in self.keys[:index]]
            greater = key.descending == before
            terms.append(f'{key.expr} {">" if greater else "<"} %s')
            clauses.append('(' + ' AND '.join(terms) + ')')
            args.extend(values[: index + 1])
        return '(' + ' OR '.join(clauses) + ')', args

    def key_of(self, row: Mapping[str, Any]) -> list[Any]:
        return [key.value(row) for key in self.keys]


@dataclass(frozen=True)
class PagingParams:
    """The paging query parameters every listing takes."""

    per_page: int
    cursor: str | None
    include_total: bool


def paging_params(default: int, maximum: int) -> Callable[..., PagingParams]:
    """Build the dependency reading per_page / cursor / include_total, with this listing's default and maximum page size.

    The bounds sit in the parameter defaults rather than in Annotated: annotations are strings under postponed evaluation, and maximum, a closure variable, could not be resolved from one.
    """

    def dependency(
        per_page: int = Query(default, ge=1, le=maximum, description='1 ページの件数。'),
        cursor: str | None = Query(None, description='前の応答の next_cursor または prev_cursor。省略すると先頭のページ。'),
        include_total: bool = Query(False, description='一覧全体の件数(total)も数えるか。'),
    ) -> PagingParams:
        return PagingParams(per_page=per_page, cursor=cursor, include_total=include_total)

    return dependency


@dataclass(frozen=True)
class PageRequest:
    """What a listing request asks for. listing identifies the listing and its filters; a cursor is accepted only by the listing that issued it."""

    listing: str
    per_page: int
    cursor: str | None
    include_total: bool


@dataclass(frozen=True)
class PageRows:
    rows: list[dict[str, Any]]
    per_page: int
    next_cursor: str | None
    prev_cursor: str | None
    total: int | None


def page_request(name: str, filters: Mapping[str, Any], params: PagingParams) -> PageRequest:
    """Build the request, binding it to the listing name and the filters that shape its rows."""
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return PageRequest(listing=f'{name}:{digest}', per_page=params.per_page, cursor=params.cursor, include_total=params.include_total)


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _decode(request: PageRequest) -> dict[str, Any] | None:
    if request.cursor is None:
        return None
    try:
        padded = request.cursor + '=' * (-len(request.cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise InvalidCursor() from exc
    if not isinstance(payload, dict) or payload.get('v') != _CURSOR_VERSION or payload.get('l') != request.listing:
        raise InvalidCursor()
    return payload


def _encode_key(order: KeysetOrder, values: Sequence[Any]) -> list[Any]:
    return [value.isoformat() if key.kind == 'datetime' else int(value) for key, value in zip(order.keys, values, strict=True)]


def _decode_key(order: KeysetOrder, values: Any) -> list[Any]:
    if not isinstance(values, list) or len(values) != len(order.keys):
        raise InvalidCursor()
    decoded: list[Any] = []
    for key, value in zip(order.keys, values, strict=True):
        if key.kind == 'datetime':
            if not isinstance(value, str):
                raise InvalidCursor()
            try:
                decoded.append(datetime.fromisoformat(value))
            except ValueError as exc:
                raise InvalidCursor() from exc
        else:
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidCursor()
            decoded.append(value)
    return decoded


def _keyset_cursor(request: PageRequest, order: KeysetOrder, direction: str, row: Mapping[str, Any]) -> str:
    return _encode({'v': _CURSOR_VERSION, 'l': request.listing, 'd': direction, 'k': _encode_key(order, order.key_of(row))})


async def _total(db: Database, request: PageRequest, count_from: str, where: str, where_args: Sequence[Any]) -> int | None:
    if not request.include_total:
        return None
    return int(await db.fetch_val(f'SELECT COUNT(*) {count_from} WHERE {where}', *where_args) or 0)


async def fetch_keyset(
    db: Database,
    request: PageRequest,
    *,
    select: str,
    count_from: str,
    where: str,
    where_args: Sequence[Any],
    order: KeysetOrder,
) -> PageRows:
    """Fetch one page of select ... WHERE where in the given order.

    select is the SELECT list with its FROM and joins; count_from is the FROM and joins the total needs (only those the condition refers to). Both are constants of the caller. One row beyond the page is fetched to learn whether more follow; a page fetched backwards is read in reverse order and turned round.
    """
    payload = _decode(request)
    limit = request.per_page + 1
    if payload is None:
        rows = await db.fetch_all(f'{select} WHERE {where} ORDER BY {order.order_by()} LIMIT %s', *where_args, limit)
        more = len(rows) > request.per_page
        rows = rows[: request.per_page]
        next_cursor = _keyset_cursor(request, order, _AFTER, rows[-1]) if more else None
        prev_cursor = None
    else:
        direction = payload.get('d')
        if direction not in (_AFTER, _BEFORE):
            raise InvalidCursor()
        before = direction == _BEFORE
        condition, condition_args = order.beyond(_decode_key(order, payload.get('k')), before=before)
        rows = await db.fetch_all(
            f'{select} WHERE {where} AND {condition} ORDER BY {order.order_by(reverse=before)} LIMIT %s',
            *where_args,
            *condition_args,
            limit,
        )
        more = len(rows) > request.per_page
        rows = rows[: request.per_page]
        if before:
            rows.reverse()
            prev_cursor = _keyset_cursor(request, order, _BEFORE, rows[0]) if more else None
            next_cursor = _keyset_cursor(request, order, _AFTER, rows[-1]) if rows else None
        else:
            next_cursor = _keyset_cursor(request, order, _AFTER, rows[-1]) if more else None
            prev_cursor = _keyset_cursor(request, order, _BEFORE, rows[0]) if rows else None
    return PageRows(
        rows=rows,
        per_page=request.per_page,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        total=await _total(db, request, count_from, where, where_args),
    )


async def fetch_by_position(
    db: Database,
    request: PageRequest,
    *,
    select: str,
    count_from: str,
    where: str,
    where_args: Sequence[Any],
    order_by: str,
    order_args: Sequence[Any] = (),
) -> PageRows:
    """Fetch one page by position, for an order no key can follow (search relevance). The cursor carries the offset of the page it points at."""
    payload = _decode(request)
    offset = 0
    if payload is not None:
        offset = payload.get('o')
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidCursor()
    rows = await db.fetch_all(
        f'{select} WHERE {where} ORDER BY {order_by} LIMIT %s OFFSET %s',
        *where_args,
        *order_args,
        request.per_page + 1,
        offset,
    )
    more = len(rows) > request.per_page

    def cursor_at(position: int) -> str:
        return _encode({'v': _CURSOR_VERSION, 'l': request.listing, 'o': position})

    return PageRows(
        rows=rows[: request.per_page],
        per_page=request.per_page,
        next_cursor=cursor_at(offset + request.per_page) if more else None,
        prev_cursor=cursor_at(max(offset - request.per_page, 0)) if offset > 0 else None,
        total=await _total(db, request, count_from, where, where_args),
    )
