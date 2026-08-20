"""Idempotency-key storage: advisory-lock acquisition plus the
`idempotency_records` cache read/write every mutating, `Idempotency-Key`-
bearing endpoint in this codebase needs. Promoted from `ecc.domains.personal.
domains` (the `request_hash`/`lock_idempotency`/`load_cached`/
`store_idempotency` quartet) and `ecc.domains.ai_runtime.runtime`
(`held_idempotency_lock`, and near-identical `_load_cached`/
`_store_idempotency`/`_request_hash` copies duplicated across `runtime.py`/
`evaluation.py`/`prompts.py` -- not byte-identical: `prompts.py`'s
`_store_idempotency` alone took a `response_status` parameter, the other two
hardcoded `200`) into one shared platform module -- an architecture-review
deepening found the same quartet hand-copied at least seven times across
`ai_runtime`, `personal` and `attention`.

Two lock functions, not one, because two genuinely different critical
sections exist. `lock_idempotency` is transaction-scoped
(`pg_advisory_xact_lock`), sufficient for a caller whose whole critical
section fits inside one `session.begin()` block. `held_idempotency_lock` is
session-scoped, held on its own dedicated connection independent of the
caller's session, for a critical section spanning multiple internal commits
(`ai_runtime.execute_run`, whose own `_persist_terminal` commits partway
through a run -- see that function's own docstring for the full reasoning).
Picking the lighter lock when it is safe, and the heavier one only when a
caller's own commits would otherwise release it early, is a caller-side
decision -- this module offers both rather than forcing one shape on every
caller.

`load_cached` is generic over `response_model`, not one copy per Pydantic
model: pass `response_model=SomeResponse` to get a validated
`SomeResponse | None` back (matches `ai_runtime`'s three former copies);
omit it to get the raw `dict[str, Any] | None` every `personal`-package call
site already expects (matches `personal/domains.py`'s former copies) -- both
read the same `idempotency_records` row, only the deserialize step differs,
so neither caller had to change its own calling convention to converge on
one read implementation. `store_idempotency` has no equivalent split: it
always takes an already-serialized `dict[str, Any]` (every caller, including
`ai_runtime`'s three, calls `response.model_dump(mode="json")` itself before
passing it in) -- the write side never needed generic dispatch, since a
`dict` is already the one shape `INSERT ... CAST(:response_body AS jsonb)`
wants.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Any, overload

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.database import lock_engine
from ecc.observability import record_idempotency_conflict


def request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock_key(auth: AuthContext, key: str) -> str:
    return f"{auth.workspace_id}:{auth.user_id}:{key}"


def lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": _lock_key(auth, key)},
    )


@contextmanager
def held_idempotency_lock(auth: AuthContext, key: str) -> Iterator[None]:
    """A session-scoped `pg_advisory_lock`, held on its own dedicated
    connection for this context manager's entire duration -- unlike
    `lock_idempotency`'s `pg_advisory_xact_lock`, this is not released early
    by an internal `session.commit()` partway through the caller's critical
    section (`ai_runtime.runtime._persist_terminal`'s docstring). Reach for
    this only when the critical section does not fit inside one
    `session.begin()` block.

    `ai_runtime.runtime.create_run` is the motivating caller: it wraps its
    whole body (cache check, existence check, `execute_run`, idempotency
    store) in this lock, not just the initial cache lookup, because that
    whole body -- not only the cache read -- is the critical section. Two
    concurrent requests carrying the same `Idempotency-Key` must not both
    reach `execute_run` and independently trigger a real model call;
    `execute_run` also cannot rely on the lighter `lock_idempotency` because
    it is not scoped to one transaction (`_persist_terminal` commits partway
    through a run, and `execute_run` is also called directly, outside any
    `session.begin()`, from `evaluation.py`'s per-example loop). Every other
    idempotency-key endpoint in this codebase, whose critical section does
    fit inside one `session.begin()` block, safely uses `lock_idempotency`
    instead.

    Uses `ecc.database.lock_engine` (`NullPool`, no `statement_timeout`), not
    the app's main `engine` -- this lock can be held for tens of seconds to
    minutes (a synchronous model call), and the main engine's shared,
    size-capped pool plus its 5-second `statement_timeout` connect-listener
    are both sized/tuned for ordinary short queries, not a lock-wait meant to
    block indefinitely until the first request releases it. See
    `lock_engine`'s own docstring in `database.py`.
    """
    lock_key = _lock_key(auth, key)
    with lock_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"), {"lock_key": lock_key}
        )
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )


@overload
def load_cached(
    session: Session, auth: AuthContext, key: str, req_hash: str, *, domain: str
) -> dict[str, Any] | None: ...
@overload
def load_cached[ResponseT: BaseModel](
    session: Session,
    auth: AuthContext,
    key: str,
    req_hash: str,
    *,
    domain: str,
    response_model: type[ResponseT],
) -> ResponseT | None: ...
def load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    req_hash: str,
    *,
    domain: str,
    response_model: type[BaseModel] | None = None,
) -> BaseModel | dict[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body FROM idempotency_records
                WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                  AND key = :key AND expires_at > :now
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": key,
                "now": datetime.now(UTC),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        record_idempotency_conflict(domain)
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    if response_model is not None:
        return response_model.model_validate(row["response_body"])
    result: dict[str, Any] = row["response_body"]
    return result


def store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    req_hash: str,
    response_body: dict[str, Any],
    now: datetime,
    response_status: int = 200,
    ttl: timedelta = timedelta(days=365),
) -> None:
    """`ttl` defaults to the 365-day retention most callers use. Two caller
    families deliberately pass a shorter `ttl=timedelta(hours=24)` instead.
    `platform/dashboard_briefs.py`'s `refresh_morning_brief` matches its own
    daily-cadence semantics (an `Idempotency-Key` for "refresh today's
    brief" has no reason to keep replaying a cached response a year later).
    `domains/collaboration/delegations.py`'s five call sites (create/accept/
    reject/revoke/complete) follow the same reasoning: each is a mutating
    delegation lifecycle action, not a long-lived record, so none of them
    need year-long replay protection either. Neither is an oversight to
    normalize away.
    """
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": req_hash,
            "response_status": response_status,
            "response_body": dumps(response_body),
            "created_at": now,
            "expires_at": now + ttl,
        },
    )
