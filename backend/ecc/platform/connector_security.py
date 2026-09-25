"""Shared connector-ownership / personal-data-isolation primitives (Spec A
"Shared definitions": personal data set, revocation safety, refusal audit).

Tasks adopt each helper at its call sites (grant/transfer/delegation
refusal, Gmail callback identity binding, guarded revoke sites; member
removal -- `identity/membership_removal.py` and `authz.owned_resource_
summary` -- already uses the lock key, personal-data predicates and
revoke-safety helpers). Keeping the definitions in one
place is the point -- the personal-data predicates, the revoke-safety rule
and the membership-mutation lock key must not drift between call sites.

Logging discipline (Spec A T7): nothing here ever logs or labels an email
address, an external account id, a credential, or an exception *message*
(psycopg's ``DETAIL: Key (...)=(...)`` text carries row values). Only
exception class names, SQLSTATEs and constraint names are logged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import Request
from psycopg import errors as pg_errors
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.database import SessionFactory
from ecc.observability import (
    record_audit_outbox_failure,
    record_connector_revoke,
)
from ecc.platform.audit_outbox import write_audit_and_outbox

_logger = logging.getLogger("ecc.platform.connector_security")

# ---------------------------------------------------------------------------
# Personal data set
# ---------------------------------------------------------------------------

PERSONAL_PROVIDERS: Final[frozenset[str]] = frozenset({"gmail"})

# Every AI task type whose input/output is derived from a personal mailbox.
# Extend this whenever a new `email.*` task type is registered in
# `ecc.domains.ai_runtime.router.TASK_REQUIREMENTS` -- a test asserts every
# registered `email.*` task type is listed here.
EMAIL_TASK_TYPES: Final[frozenset[str]] = frozenset({"email.detect_action"})

# The recommendation / attention / evidence discriminators the Gmail sync and
# action-detection paths write (`gmail_action_detection.py`, `gmail_adapter.py`,
# `attention.py`); mirrored by `gmail_revocation.py`'s cascade predicates.
_EMAIL_ATTENTION_ENTITY_TYPE: Final = "email_thread"
_EMAIL_RECOMMENDATION_TYPE: Final = "email_action_detected"
_GMAIL_EVIDENCE_SOURCE_TYPE: Final = "gmail_sync"

# The single source of truth for the personal data set: resource_type ->
# boolean SQL fragment over an unaliased `<resource_type>` row, bound with
# `personal_sql_params()`. Consumed by `is_personal_resource` (EXISTS by id,
# below) and by `authz.owned_resource_summary` (rows that do not block
# member removal), so the two cannot drift. Every fragment is a
# code-defined literal (no interpolation of caller input); an unknown
# resource_type never reaches SQL.
_PERSONAL_CONNECTOR_OF = (
    "{table}.connector_account_id IN (SELECT ca.id FROM connector_accounts ca "
    "WHERE ca.workspace_id = {table}.workspace_id AND ca.provider = ANY(:providers))"
)
PERSONAL_ROW_PREDICATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "connector_accounts": "connector_accounts.provider = ANY(:providers)",
        "sync_runs": _PERSONAL_CONNECTOR_OF.format(table="sync_runs"),
        "sync_cursors": _PERSONAL_CONNECTOR_OF.format(table="sync_cursors"),
        "attention_items": "attention_items.entity_type = :attention_entity_type",
        "recommendations": "recommendations.recommendation_type = :recommendation_type",
        "ai_runs": "ai_runs.task_type = ANY(:task_types)",
        "pkos_evidence": "pkos_evidence.source_type = :evidence_source_type",
    }
)

_PERSONAL_PREDICATES: Final[dict[str, str]] = {
    table: f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {table}.id = :id AND ({fragment}))"  # noqa: S608
    for table, fragment in PERSONAL_ROW_PREDICATES.items()
}

PERSONAL_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(_PERSONAL_PREDICATES)

# A "Gmail-only person node" (Spec A S1.4(c)): a `pkos_nodes` row with
# `node_type='person'` that has at least one `gmail_sync` evidence row and
# no evidence of any other `source_type`. A boolean SQL fragment over an
# unaliased `pkos_nodes` row; bind it with `personal_sql_params()`. Shared
# by `membership_removal`, which re-owns such nodes (any node the member
# still owns afterwards blocks removal).
GMAIL_ONLY_PERSON_NODE_PREDICATE: Final = (
    "(pkos_nodes.node_type = :person_node_type "
    "AND EXISTS (SELECT 1 FROM pkos_evidence ev "
    "WHERE ev.workspace_id = pkos_nodes.workspace_id AND ev.node_id = pkos_nodes.id "
    "AND ev.source_type = :evidence_source_type) "
    "AND NOT EXISTS (SELECT 1 FROM pkos_evidence ev "
    "WHERE ev.workspace_id = pkos_nodes.workspace_id AND ev.node_id = pkos_nodes.id "
    "AND ev.source_type <> :evidence_source_type))"
)


def personal_sql_params() -> dict[str, object]:
    """Bind parameters for every personal-data SQL fragment/statement in
    this module (and `authz`'s removal exclusions built from them)."""
    return {
        "providers": sorted(PERSONAL_PROVIDERS),
        "task_types": sorted(EMAIL_TASK_TYPES),
        "attention_entity_type": _EMAIL_ATTENTION_ENTITY_TYPE,
        "recommendation_type": _EMAIL_RECOMMENDATION_TYPE,
        "evidence_source_type": _GMAIL_EVIDENCE_SOURCE_TYPE,
        "person_node_type": "person",
    }


class PersonalDataNotGrantable(Exception):
    """Raised by `require_not_personal_data` for a row in the personal data
    set. Callers map it to `400 RESOURCE_TYPE_NOT_GRANTABLE` (plus a
    `personal_data.share_refused` refusal audit and metric). Carries only
    the resource type -- never the row id or any row content.
    """

    def __init__(self, resource_type: str) -> None:
        super().__init__(resource_type)
        self.resource_type = resource_type


def is_personal_resource(session: Session, resource_type: str, resource_id: UUID) -> bool:
    """True iff `(resource_type, resource_id)` is a row in Spec A's personal
    data set. Any resource_type outside that set -> False (no query). A
    nonexistent id -> False.
    """
    statement = _PERSONAL_PREDICATES.get(resource_type)
    if statement is None:
        return False
    result = session.execute(
        text(statement), {"id": resource_id, **personal_sql_params()}
    ).scalar_one()
    return bool(result)


def require_not_personal_data(session: Session, resource_type: str, resource_id: UUID) -> None:
    """Raise `PersonalDataNotGrantable` when the row is personal data."""
    if is_personal_resource(session, resource_type, resource_id):
        raise PersonalDataNotGrantable(resource_type)


# ---------------------------------------------------------------------------
# IntegrityError classification (generalized from
# `attention/meeting_prep.py:_violated_constraint`)
# ---------------------------------------------------------------------------


def violated_constraint(exc: IntegrityError) -> str | None:
    """The constraint/index name a psycopg 3 `IntegrityError` violated
    (`exc.orig.diag.constraint_name`), or None when unavailable.
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    return name if isinstance(name, str) else None


def is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    """True only for a unique violation (SQLSTATE 23505) of exactly
    `constraint_name` -- an FK violation, a check violation, or a unique
    violation of a *different* constraint is False, so callers can
    re-raise everything they are not specifically defending against.
    """
    return (
        isinstance(getattr(exc, "orig", None), pg_errors.UniqueViolation)
        and violated_constraint(exc) == constraint_name
    )


def integrity_error_log_fields(exc: IntegrityError) -> tuple[str | None, str | None]:
    """`(sqlstate, constraint_name)` -- the only parts of an
    `IntegrityError` that are safe to log. Never log `str(exc)`: the
    driver message includes `DETAIL: Key (...)=(...)` / `Failing row
    contains (...)`, i.e. row values (emails, credential bytes).
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    return (sqlstate if isinstance(sqlstate, str) else None, violated_constraint(exc))


# ---------------------------------------------------------------------------
# Revocation safety
# ---------------------------------------------------------------------------

RevokeScope = Literal["none", "global"]
RevokeTokenKind = Literal["minted_unpersisted", "replaced", "duplicate", "disconnected_row"]
RevokeSite = Literal[
    "callback_failure",
    "callback_duplicate",
    "reconnect_replaced",
    "disable",
    "cascade",
    "removal",
    "adapter_callback",
    "remediation",
]
RevokeResult = Literal["ok", "error", "skipped_unsafe"]

_LIVE_ROW_EXISTS_SQL: Final = """
    SELECT EXISTS (
        SELECT 1 FROM connector_accounts
        WHERE provider = :provider
          AND external_account_id = :external_account_id
          AND status <> 'disconnected'
          AND (CAST(:exclude_row_id AS uuid) IS NULL OR id <> CAST(:exclude_row_id AS uuid))
    )
"""


def revoke_is_safe(
    session: Session,
    *,
    provider: str,
    external_account_id: str | None,
    token_kind: RevokeTokenKind,
    exclude_row_id: UUID | None,
    scope: RevokeScope,
) -> bool:
    """Whether revoking a provider grant cannot break another live
    connection (Spec A "Revocation safety").

    - `scope="none"` (provider tokens proven per-token): always True.
    - `scope="global"` (grant-wide until proven otherwise): False when
      `external_account_id` is unknown; otherwise True iff no
      non-`disconnected` `connector_accounts` row for the same
      `(provider, external_account_id)` exists in ANY workspace, other than
      `exclude_row_id`. This is a documented tenancy exception: a boolean
      existence check only, no row data is read or returned.

    `exclude_row_id` rule (the caller's responsibility; not second-guessed
    here): `disconnected_row` -> the id of the row being disconnected;
    `replaced` / `duplicate` / `minted_unpersisted` -> None. `token_kind`
    is accepted for call-site documentation and future per-kind policy.

    Check and revoke are not atomic; that window is accepted by the spec.
    """
    del token_kind  # documented above; the rule is uniform today
    if scope == "none":
        return True
    if external_account_id is None:
        return False
    live_row_exists = session.execute(
        text(_LIVE_ROW_EXISTS_SQL),
        {
            "provider": provider,
            "external_account_id": external_account_id,
            "exclude_row_id": exclude_row_id,
        },
    ).scalar_one()
    return not bool(live_row_exists)


class _Disconnector[C](Protocol):
    def disconnect(self, account: C) -> None: ...


def revoke_guarded[C](
    adapter: _Disconnector[C], context: C, *, provider: str, site: RevokeSite
) -> bool:
    """Best-effort provider-side revoke that never raises. Logs only the
    exception class (never its message, the credential, or the account's
    email) and counts `ecc_connector_revoke_total{provider,site,result}`.
    Returns True on success.
    """
    try:
        adapter.disconnect(context)
    except Exception as exc:
        _logger.warning(
            "connector_revoke_failed",
            extra={"provider": provider, "site": site, "error_class": type(exc).__name__},
        )
        record_connector_revoke(provider, site, "error")
        return False
    record_connector_revoke(provider, site, "ok")
    return True


def record_revoke_skipped_unsafe(*, provider: str, site: RevokeSite) -> None:
    """Count a revoke deliberately skipped because `revoke_is_safe` said no."""
    record_connector_revoke(provider, site, "skipped_unsafe")


# ---------------------------------------------------------------------------
# Refusal audit
# ---------------------------------------------------------------------------

RefusalPayloadKey = Literal["provider", "resource_type"]


def write_refusal_audit(
    auth: AuthContext,
    request: Request | None,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID | None,
    reason: str,
    provider_or_type: str,
    payload_key: RefusalPayloadKey = "provider",
) -> None:
    """Persist a `denied` audit + outbox row for a refused action in its
    OWN short transaction (`SessionFactory()`), so it survives the caller's
    business transaction rolling back. Payload is `{"reason": ...,
    <payload_key>: provider_or_type}` -- never emails or row data.
    `aggregate_id=None` (no row exists, e.g. a refused enrollment) -> a
    fresh `uuid4()`.

    Never raises: on failure it logs the exception class and counts
    `ecc_audit_outbox_failures_total{domain="connector_security"}`; the
    caller still returns its refusal.
    """
    domain = "connector_security"
    payload: dict[str, str] = {"reason": reason, payload_key: provider_or_type}
    written = False
    try:
        with SessionFactory() as session, session.begin():
            write_audit_and_outbox(
                session,
                auth,
                request,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id if aggregate_id is not None else uuid4(),
                aggregate_version=0,
                changed_fields=[],
                payload=payload,
                metadata=payload,
                now=datetime.now(UTC),
                domain=domain,
                authorization_result="denied",
                failure_code=reason,
            )
            written = True
    except Exception as exc:
        # write_audit_and_outbox already counted its own SQLAlchemyError;
        # count everything else (commit failure, non-DB error) here.
        if written or not isinstance(exc, SQLAlchemyError):
            record_audit_outbox_failure(domain)
        _logger.error(
            "refusal_audit_write_failed",
            extra={"event_type": event_type, "error_class": type(exc).__name__},
        )


# ---------------------------------------------------------------------------
# Membership-mutation advisory lock key
# ---------------------------------------------------------------------------


def membership_mutation_lock_key(workspace_id: UUID) -> str:
    """The advisory-lock key string `identity/membership_removal.py` uses
    for its exclusive membership-mutation lock (both its role-change and
    removal endpoints call this helper). Callers taking the shared
    side (Gmail callback, sync phase 1) must use this helper so the key
    cannot drift from removal's.
    """
    return f"membership-mutation:{workspace_id}"
