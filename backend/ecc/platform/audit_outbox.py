"""Shared `audit_events` + `event_outbox` write -- extracted from ~30
near-identical per-domain helpers (commonly `_write_side_effects`,
`_write_audit_and_outbox`, or a split `_write_audit`/`_write_outbox`
pair), one written per domain module rather than shared (Loop 2
architecture review's "audit_events/event_outbox write-helper
duplication" finding).

**Not a pure mechanical dedup -- three real behavioral patterns exist,
and this helper's parameters cover all of them:**

- The majority ("thin") shape writes a fixed 14-column `audit_events`
  row (no `before`/`after`/`idempotency_key_hash` content) and an
  8-column `event_outbox` row (no `causation_id`). This is every
  parameter's default.
- A "rich" trio (`communication/commitments.py`, `knowledge/notes.py`,
  `planning/tasks.py`) additionally records a real `idempotency_key_
  hash` and `before`/`after` state diffs -- pass those explicitly to
  populate them; they stay `NULL` otherwise, identical to every thin
  caller's existing SQL, which never referenced those (nullable)
  columns at all.
- `governance/recommendation_events.py`/`platform/dashboard_briefs.py`
  additionally set `event_outbox.causation_id` -- pass `causation_id`
  explicitly; it stays `NULL` (the schema's own default for an
  unrelated top-level event) otherwise.
- `automation/kill_switches.py`/`automation/local_adapters.py` write
  `source='automation'` instead of the `'user'` default -- pass
  `source` explicitly.
- `attention/meeting_prep.py`/`attention/planning.py` conditionally
  skip the `event_outbox` write entirely for an audit-only sub-action
  -- pass `emit_outbox=False`.
- `communication/commitments.py`'s `create_commitment` writes an
  `event_outbox.event_type` that is not just `f"{event_type}.v1"` --
  a commitment auto-detected from evidence records `audit_events.
  event_type='commitment.created'` but `event_outbox.event_type=
  'commitment.detected.v1'`, a genuinely different name, not a
  version suffix on the same one -- pass `outbox_event_type` explicitly
  for that one case; every other caller leaves it unset and gets the
  usual `f"{event_type}.v1"` derivation.

**Not covered, deliberately:** `ecc/audit.py`'s rejected-mutation
middleware write is a genuinely different shape (its own `failure_
code`/`authorization_result='rejected'` semantics, no `event_outbox`
write at all, and `except Exception: ... ; raise` intentionally
*swallowed* rather than re-raised) -- left as its own independent
implementation, not folded in here.

`owner_id`/`visibility` are deliberately never included in this
helper's `audit_events` INSERT column list, matching the majority
convention -- migration `0063_phase8_authz_visibility.py`'s own
`BEFORE INSERT` trigger populates `owner_id` from `actor_id` (and
`visibility` defaults to `'workspace'`) whether or not the column is
explicitly listed, so the few callers that used to pass `:actor_id,
'workspace'` explicitly (`engineering/connector_accounts.py`) get the
byte-identical stored result by omission.

`request_id`/`correlation_id` are derived from `request.state` here --
the near-identical private `_request_ids(request) -> tuple[UUID,
UUID]` every one of these ~30 files also defined had no other caller
in any of them, so it is retired along with the write helper it fed.

**Non-HTTP call sites** (`ai_runtime/runtime.py`'s background run
persistence, `automation/local_adapters.py`'s in-process action
adapters, `automation/kill_switches.py`'s primitive-argument
endpoints) have no `fastapi.Request` in scope at all -- each already
minted fresh `uuid4()` request/correlation ids directly rather than
deriving them from a request. Pass `request=None` explicitly for
these; `request_id`/`correlation_id` then default to a fresh `uuid4()`
each, matching that existing behavior exactly. Do not construct a
fake/empty-scope `Request` object just to trigger `_request_ids`'s
`AttributeError` fallback path -- that depends on an internal
implementation detail of a private helper, not a real contract.
"""

from __future__ import annotations

from datetime import datetime
from json import dumps
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.observability import record_audit_outbox_failure

_UNSET: Any = object()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def write_audit_and_outbox(
    session: Session,
    auth: AuthContext,
    request: Request | tuple[UUID, UUID] | None,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    changed_fields: list[str],
    payload: dict[str, Any],
    now: datetime,
    domain: str,
    metadata: dict[str, Any] | None = None,
    source: str = "user",
    authorization_result: str = "allowed",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    idempotency_key_hash: str | None = None,
    failure_code: str | None = None,
    causation_id: UUID | None = None,
    emit_outbox: bool = True,
    actor_id: UUID | None = _UNSET,
    outbox_event_type: str | None = None,
) -> None:
    """`event_type` is the base (unversioned) event name, e.g.
    `"calendar_event.updated"` -- `event_outbox.event_type` is stored as
    `f"{event_type}.v1"` by default, matching every existing caller.
    `domain` is passed to `record_audit_outbox_failure(domain)` on a
    write failure, matching each caller's own existing metric label.

    `outbox_event_type` overrides the stored `event_outbox.event_type`
    verbatim (no `.v1` appended) for the rare case where the outbox
    event is genuinely a different name from the audit event, not just
    a versioned variant of it -- see this module's own docstring.

    `request=None` for a non-HTTP call site with no `Request` in scope
    -- `request_id`/`correlation_id` are then a fresh `uuid4()` each,
    same as `_request_ids` already falls back to for a malformed
    `Request`. See this module's own docstring.

    `request` also accepts a pre-derived `(request_id, correlation_id)`
    tuple directly, for the "rich" trio's shared `insert_*`/`lifecycle_*`
    functions (`communication/commitments.py`'s `insert_commitment`/
    `lifecycle_write`, `planning/tasks.py`'s `insert_task`/
    `lifecycle_task_write`/`set_task_status_write`) -- each is itself a
    cross-file-shared function called by `governance/recommendation_
    targets.py`'s `execute_target` (which derives the tuple from its own
    real `Request` before calling in, precisely so these functions don't
    need a `Request` object threaded through their own public signature)
    -- and, for every one of them except `set_task_status_write` (which
    `execute_target` is its only caller), also by its own module's HTTP
    endpoint, which has a real `Request` and could derive the tuple
    itself.
    Passing the tuple straight through preserves that existing contract
    instead of re-deriving a second, potentially different, request_id/
    correlation_id pair from scratch.

    `actor_id` defaults to `auth.user_id` -- pass it explicitly only
    for the rare case where the acting identity for this specific
    event differs from `auth`'s own (e.g. `identity/invitations.py`'s
    `reject_invitation_endpoint`, whose caller has no `users` row in
    the *target* workspace at all, so `audit_events.actor_id` must be
    `NULL`, not `auth.user_id`). Pass `actor_id=None` explicitly for
    that `NULL` case -- it is distinguished from "not passed" via an
    internal sentinel, so `actor_id=None` reliably means `NULL`, not
    "use the default."
    """
    if isinstance(request, tuple):
        request_id, correlation_id = request
    elif request is not None:
        request_id, correlation_id = _request_ids(request)
    else:
        request_id, correlation_id = uuid4(), uuid4()
    resolved_actor_id = auth.user_id if actor_id is _UNSET else actor_id
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    idempotency_key_hash, before, after, changed_fields,
                    authorization_result, source, failure_code, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    :idempotency_key_hash, CAST(:before AS jsonb), CAST(:after AS jsonb),
                    :changed_fields, :authorization_result, :source, :failure_code,
                    CAST(:metadata AS jsonb), :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": aggregate_version,
                "actor_id": resolved_actor_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "idempotency_key_hash": idempotency_key_hash,
                "before": dumps(before) if before is not None else None,
                "after": dumps(after) if after is not None else None,
                "changed_fields": changed_fields,
                "authorization_result": authorization_result,
                "source": source,
                "failure_code": failure_code,
                "metadata": dumps(metadata or {}),
                "occurred_at": now,
            },
        )
        if emit_outbox:
            session.execute(
                text(
                    """
                    INSERT INTO event_outbox (
                        event_id, workspace_id, event_type, event_version,
                        correlation_id, causation_id, payload, occurred_at, attempt_count
                    ) VALUES (
                        :event_id, :workspace_id, :event_type, 1,
                        :correlation_id, :causation_id, CAST(:payload AS jsonb),
                        :occurred_at, 0
                    )
                    """
                ),
                {
                    "event_id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "event_type": outbox_event_type or f"{event_type}.v1",
                    "correlation_id": correlation_id,
                    "causation_id": causation_id,
                    "payload": dumps(payload),
                    "occurred_at": now,
                },
            )
    except SQLAlchemyError:
        record_audit_outbox_failure(domain)
        raise
