"""Shared connector-ownership / personal-data-isolation primitives (Spec A
"Shared definitions": personal data set, revocation safety, refusal audit).

Call sites adopt each helper incrementally: conflict narrowing and the
guarded, safety-checked revoke sites (Gmail callback, engineering disable,
Gmail revocation cascade) are adopted; the personal-data share refusal
(S1.3, behind `ECC_PERSONAL_DATA_ISOLATION`) is used by grant create/preview,
ownership transfer and delegation create/accept; member removal --
`identity/membership_removal.py` and `authz.owned_resource_summary` --
uses the lock key, personal-data predicates and revoke-safety helpers;
the removal-race writers (S1.11) take the lock key's shared side and
re-check membership (`require_active_members_locked`); the Gmail
callback's identity-binding refusal (S1.1) and its adapter reject hook
(S1.10) use the refusal audit and revoke-safety helpers. Keeping the
definitions in one place is the point -- the personal-data predicates, the revoke-safety rule
and the membership-mutation lock key must not drift between call sites.

Logging discipline (Spec A T7): nothing here ever logs or labels an email
address, an external account id, a credential, or an exception *message*
(psycopg's ``DETAIL: Key (...)=(...)`` text carries row values). Only
exception class names, SQLSTATEs and constraint names are logged -- and
they go into the %-formatted log *message*, not ``extra=``:
``ecc.logging.JsonFormatter`` serializes a fixed field set, so ``extra``
keys never reach production logs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from psycopg import errors as pg_errors
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory
from ecc.observability import (
    RevokeResult,
    RevokeSite,
    record_audit_outbox_failure,
    record_connector_revoke,
    record_personal_data_share_refused,
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
# Public: reserved for the Gmail action-detection hook -- `POST /api/v1/
# recommendations` refuses it (plan note N19(2)).
EMAIL_RECOMMENDATION_TYPE: Final = "email_action_detected"
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
        # The steps of an email `ai_runs` row (plan note N11). Their
        # `trace` is redacted (tool name / outcome / schema-error path),
        # but a step only exists as part of a mailbox owner's email run
        # and is written with that run's owner/visibility, so it follows
        # the parent: private, never shareable, not blocking removal.
        # EXISTS keyed on the parent's primary key (one index probe per
        # step), not a correlated `run_id IN (SELECT ...)` re-scanning the
        # workspace's runs per step row -- `owned_resource_summary` counts
        # every step a member owns under the membership lock.
        "ai_run_steps": (
            "EXISTS (SELECT 1 FROM ai_runs r WHERE r.id = ai_run_steps.run_id "
            "AND r.workspace_id = ai_run_steps.workspace_id "
            "AND r.task_type = ANY(:task_types))"
        ),
        "pkos_evidence": "pkos_evidence.source_type = :evidence_source_type",
    }
)

# Rows DERIVED from the personal data set (deep review F2, plan task FX3):
# the task/commitment/risk a confirmed `email_action_detected`
# recommendation created (`recommendation_targets._execute_create`) copies
# email-derived content (title/summary) and, flag on, is written private to
# the recommendation's owner (plan note N23); plus the feedback rows on an
# email recommendation or an email attention item. Same boolean-fragment
# contract as `PERSONAL_ROW_PREDICATES` (unaliased `<table>` row, bound with
# `personal_sql_params()` -- these need none), and additionally every
# fragment is a single `EXISTS (...)`, never NULL: `authz.owned_resource_
# summary` uses them as a top-level `NOT <fragment>`, which the planner turns
# into one anti-join (hashed once, or index-probed via migration 0083's
# `(workspace_id, execution_result ->> 'operation', ->> 'target_type',
# ->> 'target_id')` index -- partial on `recommendation_type =
# 'email_action_detected' AND execution_result IS NOT NULL` -- the workspace
# taken from the outer `workspace_id = :workspace_id`) instead of a
# correlated SubPlan per row.
#
# Share-refused like the personal data set (`is_personal_resource`) and never
# blocking member removal (`authz._PERSONAL_ROWS_NOT_BLOCKING_REMOVAL`), but
# deliberately a separate map: the T15 backfill and the T18 audit iterate
# `PERSONAL_ROW_PREDICATES` with per-table owner rules, and derived rows need
# their own (the backfill's derived-row rule, plan task FX2, reads this map
# and `email_derived_sources_sql`; it refuses to run when either map names a
# table outside its `TABLES`). `personal_derived_row_scope` also stays on the
# personal data set only (a derived row is not a source of derived rows).
#
# Provenance is the executed recommendation itself: only
# `confirm_recommendation` writes `execution_result`, once, and an executed
# email recommendation is redacted in place -- never deleted -- by the
# Gmail revocation cascade, so no provenance column (and no backfill of
# one) is needed, and rows confirmed while the flag was off are recognised
# too. Those flag-off-era rows are still `workspace`-visible until the
# backfill's derived-row rule (FX2, plan note N40) makes them private: with
# the flag on they are share-refused and no longer block removal, so run
# the backfill right after enabling ECC_PERSONAL_DATA_ISOLATION (it requires
# the flag; rollout runbook). With the flag off (the default) nothing here
# changes behaviour. `operation = 'create'` matters: a
# non-create email recommendation records the pre-existing row it changed as
# `target_id`. The type is a literal, not `:recommendation_type`, so the
# partial index `ix_recommendations_email_derived_target` is provably usable
# by generic (prepared) plans as well.
#
# `_EMAIL_DERIVED_SOURCE` (the recommendation-side conditions) is shared
# with `email_derived_sources_sql` below, so the per-row fragment and the
# per-batch set query cannot drift.
_EMAIL_DERIVED_SOURCE = (
    "derived_rec.recommendation_type = 'email_action_detected' "
    "AND derived_rec.execution_result IS NOT NULL "
    "AND derived_rec.execution_result ->> 'operation' = 'create' "
    "AND derived_rec.execution_result ->> 'target_type' = '{target_type}'"
)
_EMAIL_DERIVED_TARGET_OF = (
    "EXISTS (SELECT 1 FROM recommendations derived_rec "
    "WHERE derived_rec.workspace_id = {table}.workspace_id "
    "AND (derived_rec.execution_result ->> 'target_id') = {table}.id::text "
    "AND " + _EMAIL_DERIVED_SOURCE + ")"
)
# Derived table -> the `execution_result ->> 'target_type'` naming its rows.
EMAIL_DERIVED_TARGET_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {"tasks": "task", "commitments": "commitment", "risks": "risk"}
)
PERSONAL_DERIVED_PREDICATES: Final[Mapping[str, str]] = MappingProxyType(
    {
        **{
            table: _EMAIL_DERIVED_TARGET_OF.format(table=table, target_type=target_type)
            for table, target_type in EMAIL_DERIVED_TARGET_TYPES.items()
        },
        # Accept/dismiss/defer feedback on an email recommendation, owned by
        # the member who gave it (`actor_id`, migration 0063). Confirming an
        # email recommendation writes one, so without this every member who
        # acted on their own email recommendations stayed removal-blocked
        # (and an admin would have to transfer the feedback to themselves).
        # Primary-key probe of the parent recommendation.
        "recommendation_feedback": (
            "EXISTS (SELECT 1 FROM recommendations derived_rec "
            "WHERE derived_rec.id = recommendation_feedback.recommendation_id "
            "AND derived_rec.workspace_id = recommendation_feedback.workspace_id "
            "AND derived_rec.recommendation_type = 'email_action_detected')"
        ),
        # Useful/not-useful feedback on an email attention item (written
        # `workspace`, owned by the actor: `attention.record_attention_
        # feedback`) -- same gap. Primary-key probe of the parent item.
        # Limitation: once the item is deleted -- by the Gmail revocation
        # cascade, or by the routine attention recompute
        # (`attention.py`'s regenerate deletes an `email_thread` item whose
        # thread is no longer eligible) -- its feedback is no longer
        # recognisable (no FK, no provenance): shareable and removal-
        # blocking again, i.e. exactly the pre-FX3 baseline, no worse.
        "attention_feedback": (
            "EXISTS (SELECT 1 FROM attention_items derived_item "
            "WHERE attention_feedback.target_type = 'attention_item' "
            "AND derived_item.id = attention_feedback.target_id "
            "AND derived_item.workspace_id = attention_feedback.workspace_id "
            "AND derived_item.entity_type = 'email_thread')"
        ),
    }
)


def email_derived_sources_sql(table: str) -> str:
    """The recommendations that make rows of `table` (a key of
    `EMAIL_DERIVED_TARGET_TYPES`) email-derived, as ONE uncorrelated set
    query instead of the per-row `PERSONAL_DERIVED_PREDICATES[table]`
    probe: `(recommendation_id, target_id)` for every recommendation
    satisfying that fragment's recommendation-side conditions whose
    `target_id` (text) is in `:target_ids`, in `:workspace_id`. Equality /
    `= ANY` on all four keys of migration 0083's partial index
    `ix_recommendations_email_derived_target` (the literal type proves it
    applies), so it is one index scan per call. Used by the T15 backfill to
    resolve a batch of derived rows' sources. Unknown `table` -> KeyError
    (never reaches SQL)."""
    target_type = EMAIL_DERIVED_TARGET_TYPES[table]
    return (
        "SELECT derived_rec.id AS recommendation_id, "
        "derived_rec.execution_result ->> 'target_id' AS target_id "
        "FROM recommendations derived_rec "
        "WHERE derived_rec.workspace_id = :workspace_id "
        "AND (derived_rec.execution_result ->> 'target_id') = ANY(:target_ids) "
        "AND " + _EMAIL_DERIVED_SOURCE.format(target_type=target_type)
    )


_PERSONAL_PREDICATES: Final[dict[str, str]] = {
    table: f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {table}.id = :id AND ({fragment}))"  # noqa: S608
    for table, fragment in PERSONAL_ROW_PREDICATES.items()
}

PERSONAL_RESOURCE_TYPES: Final[frozenset[str]] = frozenset(_PERSONAL_PREDICATES)

# What `is_personal_resource` (every S1.3 share path) checks: the personal
# data set plus the rows derived from it.
_SHARE_REFUSED_STATEMENTS: Final[dict[str, str]] = {
    **_PERSONAL_PREDICATES,
    **{
        table: f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {table}.id = :id AND ({fragment}))"  # noqa: S608
        for table, fragment in PERSONAL_DERIVED_PREDICATES.items()
    },
}

# `owner_id` of a personal-data row by id (no row -> not personal data).
_PERSONAL_OWNER_STATEMENTS: Final[dict[str, str]] = {
    table: f"SELECT {table}.owner_id FROM {table} WHERE {table}.id = :id AND ({fragment})"  # noqa: S608
    for table, fragment in PERSONAL_ROW_PREDICATES.items()
}

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
        "recommendation_type": EMAIL_RECOMMENDATION_TYPE,
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
    data set, or a row derived from it (`PERSONAL_DERIVED_PREDICATES`) --
    i.e. a row no S1.3 share path (grant, preview, transfer, delegation)
    may share, whoever asks. Any other resource_type -> False (no query).
    A nonexistent id -> False.
    """
    # `statement` is a code-defined `_SHARE_REFUSED_STATEMENTS` value,
    # never request text; `resource_type` only selects which one.
    statement = _SHARE_REFUSED_STATEMENTS.get(resource_type)
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


@dataclass(frozen=True)
class PersonalRowScope:
    """`owner_id`/`visibility` for a Gmail-derived row at write time (Spec A
    S1.8(a), `ECC_PERSONAL_DATA_ISOLATION`).

    Content rows are written `private` and owned by the mailbox owner;
    workspace knowledge (person nodes, their `entity_aliases`) stays
    `workspace` (DS3 (a')) but is owned by the mailbox owner, not by the
    default-owner trigger's "workspace's earliest user" (plan note N11).
    Flag off -> `(None, "workspace")`: the insert leaves `owner_id` to the
    table's default-owner trigger (fires on NULL) and writes the column's
    default visibility -- exactly the previous rows. `mailbox_owner_id`
    must always come from the connector account's own `owner_id` (or an
    `AuthContext` built from it), never from request input.
    """

    owner_id: UUID | None
    visibility: Literal["private", "workspace"]


def personal_content_scope(mailbox_owner_id: UUID) -> PersonalRowScope:
    """Owner/visibility for a Gmail-derived content row (evidence, email
    `ai_runs` and their steps, email recommendations)."""
    if personal_data_isolation_enabled():
        return PersonalRowScope(owner_id=mailbox_owner_id, visibility="private")
    return PersonalRowScope(owner_id=None, visibility="workspace")


def personal_derived_row_scope(
    session: Session, resource_type: str, resource_id: UUID
) -> PersonalRowScope | None:
    """Owner/visibility for a row *derived from* `(resource_type,
    resource_id)` -- e.g. the task/commitment/risk a confirmed
    `email_action_detected` recommendation creates (plan note N23), which
    copies email-derived content (title/summary) out of the source.

    Flag on and the source is in the personal data set -> the source row's
    own owner (the mailbox owner) + `private`. Otherwise None: the caller
    writes exactly what it wrote before this flag existed.
    """
    if not personal_data_isolation_enabled():
        return None
    statement = _PERSONAL_OWNER_STATEMENTS.get(resource_type)
    if statement is None:
        return None
    owner_id = session.execute(
        text(statement), {"id": resource_id, **personal_sql_params()}
    ).scalar_one_or_none()
    if owner_id is None:
        return None
    return PersonalRowScope(owner_id=owner_id, visibility="private")


def personal_knowledge_owner(mailbox_owner_id: UUID) -> UUID | None:
    """`owner_id` for Gmail-derived workspace knowledge (`entity_aliases`):
    the mailbox owner with the flag on, else None (trigger default)."""
    return mailbox_owner_id if personal_data_isolation_enabled() else None


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
# `RevokeSite` / `RevokeResult` live in `ecc.observability` (this module
# imports it; the reverse would be a cycle) and are re-exported here.

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
    """`ConnectorAdapter.disconnect`. Returning normally means the grant is
    gone or the provider has no revoke API (a documented no-op); raising
    means the provider grant may still be live (e.g. `GmailAdapter`'s
    `GmailRevokeFailed`). See `CONNECTOR-CONTRACT.md`."""

    def disconnect(self, account: C) -> None: ...


def revoke_guarded[C](
    adapter: _Disconnector[C], context: C, *, provider: str, site: RevokeSite
) -> bool:
    """Best-effort provider-side revoke that never raises. Logs only the
    exception class (never its message, the credential, or the account's
    email) and counts `ecc_connector_revoke_total{provider,site,result}`:
    `ok` when `disconnect` returns, `error` when it raises -- for Gmail
    that includes a Google refusal, a transport error and an unusable
    stored credential, so `error` means the grant may still be live at
    the provider. Returns True on success.
    """
    try:
        adapter.disconnect(context)
    except Exception as exc:
        _logger.warning(
            "connector_revoke_failed provider=%s site=%s error_class=%s",
            provider,
            site,
            type(exc).__name__,
        )
        record_connector_revoke(provider, site, "error")
        return False
    record_connector_revoke(provider, site, "ok")
    return True


def record_revoke_skipped_unsafe(*, provider: str, site: RevokeSite) -> None:
    """Count a revoke deliberately skipped because `revoke_is_safe` said no."""
    record_connector_revoke(provider, site, "skipped_unsafe")


def revoke_scope_for(provider: str) -> RevokeScope:
    """The revoke-safety scope that applies to `provider`.

    Personal providers (Gmail) use `ECC_GMAIL_REVOKE_SCOPE`: their OAuth
    revoke may end the whole Google grant, shared by every row for the
    same Google account. Every other registered provider's `disconnect()`
    is a documented no-op (PAT-based; `gitlab_adapter.py` / `github_
    adapter.py` / `jira_adapter.py` / `datadog_adapter.py`), so there is
    no grant to protect: `"none"` -- no safety query, and no misleading
    `skipped_unsafe` count for a revoke that would not have done anything.
    """
    if provider in PERSONAL_PROVIDERS:
        return get_settings().gmail_revoke_scope
    return "none"


def revoke_if_safe[C](
    adapter: _Disconnector[C],
    context: C,
    *,
    provider: str,
    external_account_id: str | None,
    token_kind: RevokeTokenKind,
    exclude_row_id: UUID | None,
    site: RevokeSite,
) -> RevokeResult:
    """`revoke_is_safe` then `revoke_guarded` -- the one entry point every
    provider revoke site uses. Never raises.

    Call only AFTER the caller's business transaction has committed or
    rolled back and its session released: the safety check runs on its
    own short-lived `SessionFactory()` session, closed before the
    (possibly slow, blocking) provider network call. Under scope `"none"`
    no session is opened at all.

    A failure of the safety check itself fails closed: the revoke is not
    attempted, the exception class is logged, and the attempt is counted
    as `result="error"`.
    """
    scope = revoke_scope_for(provider)
    if scope != "none":
        try:
            with SessionFactory() as check_session:
                safe = revoke_is_safe(
                    check_session,
                    provider=provider,
                    external_account_id=external_account_id,
                    token_kind=token_kind,
                    exclude_row_id=exclude_row_id,
                    scope=scope,
                )
        except Exception as exc:
            _logger.warning(
                "connector_revoke_safety_check_failed provider=%s site=%s error_class=%s",
                provider,
                site,
                type(exc).__name__,
            )
            record_connector_revoke(provider, site, "error")
            return "error"
        if not safe:
            record_revoke_skipped_unsafe(provider=provider, site=site)
            return "skipped_unsafe"
    return "ok" if revoke_guarded(adapter, context, provider=provider, site=site) else "error"


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

    Call only AFTER the caller's business transaction has ended (committed
    or rolled back) -- never while that transaction still holds row locks
    on `users` / `workspaces`: the audit INSERT here runs on a second
    pooled connection whose FK checks take `KEY SHARE` locks on those same
    rows, so calling it mid-transaction can self-deadlock the request
    (and holds two pool connections at once).
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
            "refusal_audit_write_failed event_type=%s error_class=%s",
            event_type,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Personal-data share refusal (S1.3)
# ---------------------------------------------------------------------------

ShareRefusalPath = Literal["grant", "grant_preview", "transfer", "delegation_create"]


def personal_data_isolation_enabled() -> bool:
    """`ECC_PERSONAL_DATA_ISOLATION` (default off). Off -> every S1.3 call
    site behaves exactly as before this flag existed."""
    return get_settings().personal_data_isolation


def refuse_personal_data_share(
    auth: AuthContext,
    request: Request | None,
    *,
    resource_type: str,
    resource_id: UUID | None,
    path: ShareRefusalPath,
) -> HTTPException:
    """Record a refused share of a personal-data row -- a `denied`
    `personal_data.share_refused` audit in its own transaction plus
    `ecc_personal_data_share_refused_total{resource_type,path}` -- and
    return the existing `400 RESOURCE_TYPE_NOT_GRANTABLE` for the caller to
    raise. Call only AFTER the business transaction has ended (rolled back),
    never while it still holds row locks.
    """
    write_refusal_audit(
        auth,
        request,
        event_type="personal_data.share_refused",
        aggregate_type=resource_type,
        aggregate_id=resource_id,
        reason="personal_data",
        provider_or_type=resource_type,
        payload_key="resource_type",
    )
    record_personal_data_share_refused(resource_type, path)
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="RESOURCE_TYPE_NOT_GRANTABLE"
    )


@dataclass
class ShareRefusalTarget:
    """The row a `personal_data_share_guard` block is about to check; the
    block sets `resource_id` before each `require_not_personal_data` call
    when it checks more than one row (delegation evidence)."""

    resource_id: UUID | None = None


@contextmanager
def personal_data_share_guard(
    auth: AuthContext,
    request: Request | None,
    *,
    path: ShareRefusalPath,
    resource_id: UUID | None = None,
) -> Iterator[ShareRefusalTarget]:
    """Wrap a business transaction: `with personal_data_share_guard(...),
    session.begin():`. A `PersonalDataNotGrantable` raised inside first
    unwinds `session.begin()` (rollback, row locks released), then is
    translated here into the refusal audit + metric + `400`. Every other
    exception passes through untouched.
    """
    target = ShareRefusalTarget(resource_id=resource_id)
    try:
        yield target
    except PersonalDataNotGrantable as exc:
        raise refuse_personal_data_share(
            auth,
            request,
            resource_type=exc.resource_type,
            resource_id=target.resource_id,
            path=path,
        ) from None


# ---------------------------------------------------------------------------
# Membership-mutation advisory lock key
# ---------------------------------------------------------------------------


# Spec A S1.11 (removal race, threat T8). Member removal and role change
# (`identity/membership_removal.py`) take this key EXCLUSIVELY; every write
# path that persists rows owned by a member takes it SHARED
# (`lock_membership_shared`) and then re-checks that member's active
# membership (`member_is_active`) in the same transaction. A removal that
# commits first is therefore seen by the re-check; one that starts later
# waits for the writer to commit, and then sees (and disconnects /
# re-owns / blocks on) whatever the writer wrote. Holders of the shared
# lock never make network calls while holding it, with one accepted,
# documented exception: sync phase 1 holds this workspace-wide shared lock
# across `ensure_fresh_credential` (an OAuth token-endpoint POST when the
# access token has expired, up to 10s). A concurrent removal / role change
# waits on it and can fail with a 500 once the wait exceeds
# `ecc.database.STATEMENT_TIMEOUT_MS` (5s; retryable), and other
# shared-lock writers queue behind that waiting exclusive request in the
# meantime. Moving the refresh out of the locked section is Spec B work.
#
# Lock ordering (normative; no path may take these in another order, or
# removal and a writer can deadlock):
#
#   1. membership advisory lock (this key; exclusive in removal/role
#      change, shared everywhere else)
#   2. idempotency advisory lock (`platform/idempotency.lock_idempotency`;
#      sync phase 1 only)
#   3. row locks (`SELECT ... FOR UPDATE`, UPDATE, unique-index inserts)
#
# Adopters, each taking the lock as the FIRST statement of its
# transaction: removal + role change (exclusive); Gmail OAuth callback
# write txn (`personal/gmail_oauth.py`); connector sync phases 1 and 3
# (`engineering/connector_accounts._run_connector_sync`); Gmail sync
# phase-2 writes (`personal/gmail_adapter._process_message`,
# `resolve_or_create_person`, `fetch_and_store_body`); action detection's
# evidence write (`personal/gmail_action_detection`) and, opt-in via
# `require_active_actor=True`, its `ai_runs`/`ai_run_steps` persist
# (`ai_runtime/runtime._persist_terminal`) and recommendation insert
# (`governance/recommendation_mutations.create_recommendation`, before its
# idempotency lock); ownership transfer (`platform/authz_grants`); and
# every authorized write transaction in the modules below, through
# `authz.lock_membership_for_write`, which can also re-check the caller's
# role under the lock (ADR-0014; adopted module by module, each set guarded
# by its `tests/test_*_membership_lock_coverage.py`):
#   - `attention/*`, including `meeting_prep`;
#   - `planning/tasks`, `communication/commitments`;
#   - `calendar/events`, `scheduling/meetings`;
#   - `automation/*` user endpoints (the worker, scheduler and adapter
#     sessions have no caller and are excluded) and
#     `ai_runtime/prompts.activate_policy`;
#   - `collaboration/delegations` and `platform/dashboard_briefs` (the
#     brief GET too, since it may generate the brief; the membership lock
#     is taken before the per-user brief lock);
#   - `engineering/connector_accounts` endpoints and
#     `engineering/decisions_incidents`. Connector create locks in both of
#     its write transactions, never across `adapter.authorize()`. Sync
#     keeps its own `require_active_members_locked` design above.
# Meeting-prep enrichment is the one inversion: its
# session-scoped `held_idempotency_lock` (a different connection) is taken
# before this lock, which it takes per write transaction so a removal never
# waits on a model call. A cycle through it needs a same-user, same-key
# request on the transaction-scoped idempotency lock plus a pending removal,
# and Postgres cannot detect it across the two connections; the main
# engine's statement timeout breaks it (a retryable 500, not a hang).
#
# FX5 (consent race): every Gmail write transaction above additionally
# re-checks the mailbox owner's `email` consent under row locks, as its
# first row locks (step 3): the owner's `email` `personal_domains` row, then
# the writer's own `gmail` `connector_accounts` row (all of the owner's rows
# by id only when the connector is unknown), both `FOR KEY SHARE`
# (`personal/gmail_shared.require_email_consent_locked`; inactive ->
# `EmailConsentInactiveError`). The revocation cascade
# (`personal/gmail_revocation`) and its callers lock the same rows `FOR
# UPDATE`, in the same order, before purging; member removal locks the
# connector rows `FOR UPDATE` after this membership lock.


def membership_mutation_lock_key(workspace_id: UUID) -> str:
    """The advisory-lock key string `identity/membership_removal.py` uses
    for its exclusive membership-mutation lock (both its role-change and
    removal endpoints call this helper). Callers taking the shared
    side (Gmail callback, sync phase 1) must use this helper so the key
    cannot drift from removal's.
    """
    return f"membership-mutation:{workspace_id}"


class MembershipInactiveError(Exception):
    """A write path re-checked, under the shared membership lock, that the
    member who would own the rows is no longer an active member of the
    workspace (removed mid-flight, Spec A S1.11). Carries no identifiers.
    """

    def __init__(self) -> None:
        super().__init__("MEMBERSHIP_INACTIVE")


class EmailConsentInactiveError(Exception):
    """A Gmail write path re-checked, under its lock on the owner's `gmail`
    `connector_accounts` rows (`gmail_shared.require_email_consent_locked`),
    that the mailbox owner's `email` consent is no longer active or the
    connector is `disconnected` (consent withdrawn mid-flight). Nothing was
    written in that transaction. Carries no identifiers.
    """

    code = "EMAIL_CONSENT_NOT_ACTIVE"

    def __init__(self) -> None:
        super().__init__(self.code)


def lock_membership_shared(session: Session, workspace_id: UUID) -> None:
    """Take the SHARED side of the membership-mutation advisory lock for
    the rest of `session`'s current transaction. Must be the first lock the
    transaction takes (see the lock-ordering note above)."""
    session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:lock_key, 0))"),
        {"lock_key": membership_mutation_lock_key(workspace_id)},
    )


def member_is_active(session: Session, *, workspace_id: UUID, users_id: UUID) -> bool:
    """`users.account_id` -> `workspace_memberships(workspace_id,
    account_id)` with `status = 'active'` (Spec A S1.11 / delta F7).
    Read-only; call after `lock_membership_shared` for a race-free answer.
    """
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM users u "
                "JOIN workspace_memberships wm "
                "ON wm.workspace_id = u.workspace_id AND wm.account_id = u.account_id "
                "WHERE u.id = :users_id AND u.workspace_id = :workspace_id "
                "AND wm.status = 'active')"
            ),
            {"users_id": users_id, "workspace_id": workspace_id},
        ).scalar_one()
    )


def require_active_members_locked(
    session: Session, *, workspace_id: UUID, users_ids: Iterable[UUID]
) -> None:
    """`lock_membership_shared` + `member_is_active` for each of
    `users_ids`; raises `MembershipInactiveError` if any is inactive. The
    caller's transaction keeps the shared lock until it ends, so no removal
    can commit between this check and the caller's writes."""
    lock_membership_shared(session, workspace_id)
    for users_id in dict.fromkeys(users_ids):
        if not member_is_active(session, workspace_id=workspace_id, users_id=users_id):
            raise MembershipInactiveError
