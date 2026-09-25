"""`ecc.platform.connector_security` (Spec A "Shared definitions") against a
real Postgres database: the personal-data predicates, `revoke_is_safe`
under both scopes, `IntegrityError` classification, the out-of-transaction
refusal audit, `revoke_guarded`, the membership-mutation lock key (as member
removal really takes it), the new connector-security counters, and the
three new settings' defaults.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ecc import observability
from ecc.auth import AuthContext
from ecc.config import Settings, get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.router import TASK_REQUIREMENTS
from ecc.main import app
from ecc.platform import authz, connector_security
from ecc.platform.connector_security import (
    EMAIL_TASK_TYPES,
    PERSONAL_PROVIDERS,
    PERSONAL_RESOURCE_TYPES,
    PERSONAL_ROW_PREDICATES,
    PersonalDataNotGrantable,
    integrity_error_log_fields,
    is_personal_resource,
    is_unique_violation,
    membership_mutation_lock_key,
    record_revoke_skipped_unsafe,
    require_not_personal_data,
    revoke_guarded,
    revoke_is_safe,
    write_refusal_audit,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_GOOGLE_ACCOUNT = "connector-security-owner@example.test"
_UNIQUE_CONSTRAINT = "uq_connector_accounts_workspace_provider_external_id"
_TOKEN_KINDS = ("minted_unpersisted", "replaced", "duplicate", "disconnected_row")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_workspace(connection: Any, *, now: datetime) -> tuple[UUID, UUID]:
    workspace_id = uuid4()
    user_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO workspaces (id, name, timezone, created_at) "
            "VALUES (:id, 'Connector Security Test', 'UTC', :now)"
        ),
        {"id": workspace_id, "now": now},
    )
    create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"cs-{user_id}@example.test",
        now=now,
    )
    return workspace_id, user_id


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        account_ids = [
            row[0]
            for row in connection.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            )
        ]
        for table in (
            "audit_events",
            "event_outbox",
            "recommendations",
            "pkos_evidence",
            "pkos_nodes",
            "attention_items",
            "ai_runs",
            "sync_runs",
            "sync_cursors",
            "connector_accounts",
            "workspace_memberships",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": workspace_id},
            )
        connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        if account_ids:
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids}
            )


@pytest.fixture
def two_workspaces() -> Iterator[dict[str, Any]]:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        ws_a, user_a = _create_workspace(connection, now=now)
        ws_b, user_b = _create_workspace(connection, now=now)
    try:
        yield {"ws_a": ws_a, "user_a": user_a, "ws_b": ws_b, "user_b": user_b, "now": now}
    finally:
        _cleanup_workspace(ws_a)
        _cleanup_workspace(ws_b)


def _insert_connector(
    connection: Any,
    *,
    workspace_id: UUID,
    user_id: UUID,
    provider: str,
    external_account_id: str,
    status: str = "active",
    now: datetime,
    account_id: UUID | None = None,
) -> UUID:
    account_id = account_id or uuid4()
    connection.execute(
        text(
            """
            INSERT INTO connector_accounts (
                id, workspace_id, provider, external_account_id, display_name,
                granted_scopes, encrypted_credentials, status, version,
                created_by, updated_by, created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :provider, :external_account_id, 'Security test',
                ARRAY[]::text[], :credential, :status, 1,
                :user_id, :user_id, :now, :now, :user_id, 'workspace'
            )
            """
        ),
        {
            "id": account_id,
            "workspace_id": workspace_id,
            "provider": provider,
            "external_account_id": external_account_id,
            "credential": b"not-a-real-ciphertext",
            "status": status,
            "user_id": user_id,
            "now": now,
        },
    )
    return account_id


# ---------------------------------------------------------------------------
# revoke_is_safe
# ---------------------------------------------------------------------------


def _is_safe(
    *,
    scope: str,
    token_kind: str,
    external_account_id: str | None = _GOOGLE_ACCOUNT,
    exclude_row_id: UUID | None = None,
) -> bool:
    with SessionFactory() as session:
        return revoke_is_safe(
            session,
            provider="gmail",
            external_account_id=external_account_id,
            token_kind=token_kind,  # type: ignore[arg-type]
            exclude_row_id=exclude_row_id,
            scope=scope,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("token_kind", _TOKEN_KINDS)
def test_revoke_is_safe_scope_none_is_always_true(
    two_workspaces: dict[str, Any], token_kind: str
) -> None:
    ctx = two_workspaces
    with engine.begin() as connection:
        row_id = _insert_connector(
            connection,
            workspace_id=ctx["ws_b"],
            user_id=ctx["user_b"],
            provider="gmail",
            external_account_id=_GOOGLE_ACCOUNT,
            now=ctx["now"],
        )
    exclude = row_id if token_kind == "disconnected_row" else None
    assert _is_safe(scope="none", token_kind=token_kind, exclude_row_id=exclude) is True
    assert _is_safe(scope="none", token_kind=token_kind, external_account_id=None) is True


@pytest.mark.parametrize("token_kind", _TOKEN_KINDS)
def test_revoke_is_safe_global_no_rows_is_true(
    two_workspaces: dict[str, Any], token_kind: str
) -> None:
    assert (
        _is_safe(
            scope="global",
            token_kind=token_kind,
            external_account_id=f"nobody-{uuid4()}@example.test",
        )
        is True
    )


@pytest.mark.parametrize("token_kind", _TOKEN_KINDS)
def test_revoke_is_safe_global_unknown_external_account_is_unsafe(
    two_workspaces: dict[str, Any], token_kind: str
) -> None:
    assert _is_safe(scope="global", token_kind=token_kind, external_account_id=None) is False


@pytest.mark.parametrize("token_kind", _TOKEN_KINDS)
def test_revoke_is_safe_global_live_row_in_another_workspace_blocks(
    two_workspaces: dict[str, Any], token_kind: str
) -> None:
    ctx = two_workspaces
    with engine.begin() as connection:
        # The row the caller is acting on, in workspace A.
        own_row = _insert_connector(
            connection,
            workspace_id=ctx["ws_a"],
            user_id=ctx["user_a"],
            provider="gmail",
            external_account_id=_GOOGLE_ACCOUNT,
            status="disconnected",
            now=ctx["now"],
        )
        # Another member's live row for the same Google account, workspace B.
        _insert_connector(
            connection,
            workspace_id=ctx["ws_b"],
            user_id=ctx["user_b"],
            provider="gmail",
            external_account_id=_GOOGLE_ACCOUNT,
            now=ctx["now"],
        )
    exclude = own_row if token_kind == "disconnected_row" else None
    assert _is_safe(scope="global", token_kind=token_kind, exclude_row_id=exclude) is False
    # Scope none ignores it.
    assert _is_safe(scope="none", token_kind=token_kind, exclude_row_id=exclude) is True


@pytest.mark.parametrize("token_kind", _TOKEN_KINDS)
def test_revoke_is_safe_global_disconnected_rows_do_not_block(
    two_workspaces: dict[str, Any], token_kind: str
) -> None:
    ctx = two_workspaces
    with engine.begin() as connection:
        for ws, user in ((ctx["ws_a"], ctx["user_a"]), (ctx["ws_b"], ctx["user_b"])):
            _insert_connector(
                connection,
                workspace_id=ws,
                user_id=user,
                provider="gmail",
                external_account_id=_GOOGLE_ACCOUNT,
                status="disconnected",
                now=ctx["now"],
            )
    assert _is_safe(scope="global", token_kind=token_kind) is True


def test_revoke_is_safe_global_exclude_row_id_excludes_self(
    two_workspaces: dict[str, Any],
) -> None:
    ctx = two_workspaces
    with engine.begin() as connection:
        # Still live in the DB at check time (e.g. checked before the status
        # flip commits) -- exclusion must make it not count.
        row_id = _insert_connector(
            connection,
            workspace_id=ctx["ws_a"],
            user_id=ctx["user_a"],
            provider="gmail",
            external_account_id=_GOOGLE_ACCOUNT,
            now=ctx["now"],
        )
    assert _is_safe(scope="global", token_kind="disconnected_row", exclude_row_id=row_id) is True
    # Without exclusion (replaced/duplicate/minted), the same live row blocks.
    for kind in ("replaced", "duplicate", "minted_unpersisted"):
        assert _is_safe(scope="global", token_kind=kind) is False


def test_revoke_is_safe_global_other_provider_does_not_block(
    two_workspaces: dict[str, Any],
) -> None:
    ctx = two_workspaces
    with engine.begin() as connection:
        _insert_connector(
            connection,
            workspace_id=ctx["ws_b"],
            user_id=ctx["user_b"],
            provider="github",
            external_account_id=_GOOGLE_ACCOUNT,
            now=ctx["now"],
        )
    assert _is_safe(scope="global", token_kind="minted_unpersisted") is True


# ---------------------------------------------------------------------------
# is_personal_resource / require_not_personal_data
# ---------------------------------------------------------------------------


def _insert_sync_rows(
    connection: Any, *, workspace_id: UUID, account_id: UUID, now: datetime
) -> tuple[UUID, UUID]:
    run_id = uuid4()
    cursor_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
            "status, items_processed, started_at, created_at) VALUES "
            "(:id, :ws, :account_id, 'backfill', 'succeeded', 0, :now, :now)"
        ),
        {"id": run_id, "ws": workspace_id, "account_id": account_id, "now": now},
    )
    connection.execute(
        text(
            "INSERT INTO sync_cursors (id, workspace_id, connector_account_id, resource_type, "
            "cursor_value, updated_at) VALUES "
            "(:id, :ws, :account_id, 'message', NULL, :now)"
        ),
        {"id": cursor_id, "ws": workspace_id, "account_id": account_id, "now": now},
    )
    return run_id, cursor_id


def _insert_attention_item(
    connection: Any, *, workspace_id: UUID, user_id: UUID, entity_type: str, now: datetime
) -> UUID:
    item_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at,
                owner_id, visibility
            ) VALUES (
                :id, :ws, :entity_type, :entity_id, 1,
                80, 0.9, '{}'::jsonb, 'Test', :now, :expires_at,
                :user_id, 'workspace'
            )
            """
        ),
        {
            "id": item_id,
            "ws": workspace_id,
            "entity_type": entity_type,
            "entity_id": uuid4(),
            "now": now,
            "expires_at": now + timedelta(days=7),
            "user_id": user_id,
        },
    )
    return item_id


def _insert_recommendation(
    connection: Any, *, workspace_id: UUID, user_id: UUID, recommendation_type: str, now: datetime
) -> UUID:
    recommendation_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO recommendations (
                id, workspace_id, recommendation_type, target_type, target_id,
                proposed_action, rationale, confidence, status, evidence_ids,
                source, created_by, updated_by, created_at, updated_at, version
            ) VALUES (
                :id, :ws, :recommendation_type, 'task', NULL,
                '{}'::jsonb, 'Test rationale', 0.9, 'proposed', ARRAY[]::uuid[],
                'ai', :user_id, :user_id, :now, :now, 1
            )
            """
        ),
        {
            "id": recommendation_id,
            "ws": workspace_id,
            "recommendation_type": recommendation_type,
            "user_id": user_id,
            "now": now,
        },
    )
    return recommendation_id


def _insert_ai_run(
    connection: Any, *, workspace_id: UUID, user_id: UUID, task_type: str, now: datetime
) -> UUID:
    run_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO ai_runs (
                id, workspace_id, actor_id, task_type, data_class, status,
                input_ref, evidence, attempts, started_at, created_at, updated_at
            ) VALUES (
                :id, :ws, :user_id, :task_type, 'sensitive',
                'running', '{}'::jsonb, '[]'::jsonb, 0, :now, :now, :now
            )
            """
        ),
        {"id": run_id, "ws": workspace_id, "user_id": user_id, "task_type": task_type, "now": now},
    )
    return run_id


def _insert_evidence(
    connection: Any, *, workspace_id: UUID, user_id: UUID, source_type: str, now: datetime
) -> UUID:
    node_id = uuid4()
    connection.execute(
        text(
            """
            INSERT INTO pkos_nodes (
                id, workspace_id, node_type, canonical_name, attributes,
                status, confidence, version, created_at, updated_at,
                owner_id, visibility
            ) VALUES (
                :id, :ws, 'person', 'Test Person', '{}'::jsonb,
                'active', 1.00, 1, :now, :now, :user_id, 'workspace'
            )
            """
        ),
        {"id": node_id, "ws": workspace_id, "user_id": user_id, "now": now},
    )
    evidence_id = uuid4()
    source_ref = f"{source_type}:{evidence_id}"
    connection.execute(
        text(
            """
            INSERT INTO pkos_evidence (
                id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
            ) VALUES (:id, :ws, :node_id, :source_type, :source_ref, :sha256, :now)
            """
        ),
        {
            "id": evidence_id,
            "ws": workspace_id,
            "node_id": node_id,
            "source_type": source_type,
            "source_ref": source_ref,
            "sha256": sha256(source_ref.encode()).hexdigest(),
            "now": now,
        },
    )
    return evidence_id


@pytest.fixture
def personal_and_other_rows(two_workspaces: dict[str, Any]) -> dict[str, dict[str, UUID]]:
    ctx = two_workspaces
    ws, user, now = ctx["ws_a"], ctx["user_a"], ctx["now"]
    with engine.begin() as connection:
        gmail = _insert_connector(
            connection,
            workspace_id=ws,
            user_id=user,
            provider="gmail",
            external_account_id=_GOOGLE_ACCOUNT,
            now=now,
        )
        github = _insert_connector(
            connection,
            workspace_id=ws,
            user_id=user,
            provider="github",
            external_account_id=f"gh-{uuid4()}",
            now=now,
        )
        gmail_run, gmail_cursor = _insert_sync_rows(
            connection, workspace_id=ws, account_id=gmail, now=now
        )
        github_run, github_cursor = _insert_sync_rows(
            connection, workspace_id=ws, account_id=github, now=now
        )
        rows = {
            "personal": {
                "connector_accounts": gmail,
                "sync_runs": gmail_run,
                "sync_cursors": gmail_cursor,
                "attention_items": _insert_attention_item(
                    connection, workspace_id=ws, user_id=user, entity_type="email_thread", now=now
                ),
                "recommendations": _insert_recommendation(
                    connection,
                    workspace_id=ws,
                    user_id=user,
                    recommendation_type="email_action_detected",
                    now=now,
                ),
                "ai_runs": _insert_ai_run(
                    connection,
                    workspace_id=ws,
                    user_id=user,
                    task_type="email.detect_action",
                    now=now,
                ),
                "pkos_evidence": _insert_evidence(
                    connection, workspace_id=ws, user_id=user, source_type="gmail_sync", now=now
                ),
            },
            "other": {
                "connector_accounts": github,
                "sync_runs": github_run,
                "sync_cursors": github_cursor,
                "attention_items": _insert_attention_item(
                    connection, workspace_id=ws, user_id=user, entity_type="task", now=now
                ),
                "recommendations": _insert_recommendation(
                    connection,
                    workspace_id=ws,
                    user_id=user,
                    recommendation_type="task_suggestion",
                    now=now,
                ),
                "ai_runs": _insert_ai_run(
                    connection,
                    workspace_id=ws,
                    user_id=user,
                    task_type="attention.explain_item",
                    now=now,
                ),
                "pkos_evidence": _insert_evidence(
                    connection, workspace_id=ws, user_id=user, source_type="manual_note", now=now
                ),
            },
        }
    return rows


def test_personal_resource_types_cover_the_spec_set() -> None:
    assert PERSONAL_PROVIDERS == frozenset({"gmail"})
    assert PERSONAL_RESOURCE_TYPES == frozenset(
        {
            "connector_accounts",
            "sync_runs",
            "sync_cursors",
            "attention_items",
            "recommendations",
            "ai_runs",
            "pkos_evidence",
        }
    )


@pytest.mark.parametrize("resource_type", sorted(PERSONAL_RESOURCE_TYPES))
def test_is_personal_resource_per_type(
    personal_and_other_rows: dict[str, dict[str, UUID]], resource_type: str
) -> None:
    personal_id = personal_and_other_rows["personal"][resource_type]
    other_id = personal_and_other_rows["other"][resource_type]
    with SessionFactory() as session:
        assert is_personal_resource(session, resource_type, personal_id) is True
        assert is_personal_resource(session, resource_type, other_id) is False
        assert is_personal_resource(session, resource_type, uuid4()) is False
        with pytest.raises(PersonalDataNotGrantable) as excinfo:
            require_not_personal_data(session, resource_type, personal_id)
        assert excinfo.value.resource_type == resource_type
        assert str(personal_id) not in str(excinfo.value)
        require_not_personal_data(session, resource_type, other_id)


def test_removal_exclusions_are_exactly_the_personal_data_set() -> None:
    """`authz`'s member-removal exclusions are derived from the one
    personal-data definition (review B-NB-1): same tables, same SQL, and
    `pkos_nodes` is never excluded (Gmail-only nodes are re-owned first;
    any node still owned blocks)."""
    assert dict(authz._PERSONAL_ROWS_NOT_BLOCKING_REMOVAL) == dict(PERSONAL_ROW_PREDICATES)
    assert set(authz._PERSONAL_ROWS_NOT_BLOCKING_REMOVAL) == PERSONAL_RESOURCE_TYPES
    assert "pkos_nodes" not in authz._PERSONAL_ROWS_NOT_BLOCKING_REMOVAL


def test_is_personal_resource_other_resource_type_is_false(
    personal_and_other_rows: dict[str, dict[str, UUID]],
) -> None:
    gmail_id = personal_and_other_rows["personal"]["connector_accounts"]
    with SessionFactory() as session:
        # Same id under a resource type outside the personal set.
        assert is_personal_resource(session, "tasks", gmail_id) is False
        assert is_personal_resource(session, "not_a_table; DROP TABLE x", gmail_id) is False
        require_not_personal_data(session, "tasks", gmail_id)


def test_email_task_types_cover_every_registered_email_task_type() -> None:
    registered = {task_type for task_type in TASK_REQUIREMENTS if task_type.startswith("email.")}
    assert registered, "expected at least one registered email.* task type"
    assert registered <= EMAIL_TASK_TYPES
    assert all(task_type.startswith("email.") for task_type in EMAIL_TASK_TYPES)


# ---------------------------------------------------------------------------
# is_unique_violation / integrity_error_log_fields
# ---------------------------------------------------------------------------


def _capture_integrity_error(statement: str, params: dict[str, Any]) -> IntegrityError:
    with SessionFactory() as session:
        try:
            with session.begin():
                session.execute(text(statement), params)
        except IntegrityError as exc:
            return exc
    raise AssertionError("expected IntegrityError")


_INSERT_CONNECTOR_SQL = """
    INSERT INTO connector_accounts (
        id, workspace_id, provider, external_account_id, display_name,
        granted_scopes, encrypted_credentials, status, version,
        created_by, updated_by, created_at, updated_at, owner_id, visibility
    ) VALUES (
        :id, :ws, 'gmail', :external_account_id, 'Security test',
        ARRAY[]::text[], :credential, 'active', 1,
        :user_id, :user_id, :now, :now, :user_id, 'workspace'
    )
"""


def test_is_unique_violation_matches_only_the_named_constraint(
    two_workspaces: dict[str, Any],
) -> None:
    ctx = two_workspaces
    with engine.begin() as connection:
        existing = _insert_connector(
            connection,
            workspace_id=ctx["ws_a"],
            user_id=ctx["user_a"],
            provider="gmail",
            external_account_id=_GOOGLE_ACCOUNT,
            now=ctx["now"],
        )
    base = {
        "ws": ctx["ws_a"],
        "credential": b"x",
        "user_id": ctx["user_a"],
        "now": ctx["now"],
    }

    duplicate = _capture_integrity_error(
        _INSERT_CONNECTOR_SQL, {**base, "id": uuid4(), "external_account_id": _GOOGLE_ACCOUNT}
    )
    assert is_unique_violation(duplicate, _UNIQUE_CONSTRAINT) is True
    assert is_unique_violation(duplicate, "uq_some_other_constraint") is False
    assert integrity_error_log_fields(duplicate) == ("23505", _UNIQUE_CONSTRAINT)

    # A unique violation of a different constraint (primary key).
    pkey = _capture_integrity_error(
        _INSERT_CONNECTOR_SQL,
        {**base, "id": existing, "external_account_id": f"other-{uuid4()}@example.test"},
    )
    assert is_unique_violation(pkey, _UNIQUE_CONSTRAINT) is False
    sqlstate, constraint = integrity_error_log_fields(pkey)
    assert sqlstate == "23505"
    assert constraint is not None and constraint != _UNIQUE_CONSTRAINT

    # A foreign-key violation (sync run for a nonexistent connector).
    fk = _capture_integrity_error(
        "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
        "status, items_processed, started_at, created_at) VALUES "
        "(:id, :ws, :account_id, 'backfill', 'running', 0, :now, :now)",
        {"id": uuid4(), "ws": ctx["ws_a"], "account_id": uuid4(), "now": ctx["now"]},
    )
    assert is_unique_violation(fk, _UNIQUE_CONSTRAINT) is False
    fk_state, fk_constraint = integrity_error_log_fields(fk)
    assert fk_state == "23503"
    assert fk_constraint is not None
    assert is_unique_violation(fk, fk_constraint) is False


def test_is_unique_violation_without_driver_diag_is_false() -> None:
    exc = IntegrityError("INSERT ...", {}, Exception("no diag"))
    assert is_unique_violation(exc, _UNIQUE_CONSTRAINT) is False
    assert integrity_error_log_fields(exc) == (None, None)


# ---------------------------------------------------------------------------
# write_refusal_audit
# ---------------------------------------------------------------------------


def _refusal_rows(workspace_id: UUID, event_type: str) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, authorization_result, "
                    "failure_code, metadata, actor_id FROM audit_events "
                    "WHERE workspace_id = :ws AND event_type = :event_type"
                ),
                {"ws": workspace_id, "event_type": event_type},
            )
        )


def _outbox_payloads(workspace_id: UUID, event_type: str) -> list[Any]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT payload FROM event_outbox "
                    "WHERE workspace_id = :ws AND event_type = :event_type"
                ),
                {"ws": workspace_id, "event_type": f"{event_type}.v1"},
            )
        ]


def test_write_refusal_audit_persists_after_business_txn_rolls_back(
    two_workspaces: dict[str, Any],
) -> None:
    ctx = two_workspaces
    auth = AuthContext(workspace_id=ctx["ws_a"], user_id=ctx["user_a"], timezone="UTC")
    event_type = "connector_account.enrollment_refused"
    business_row = uuid4()

    with SessionFactory() as session:
        session.begin()
        session.execute(
            text(_INSERT_CONNECTOR_SQL),
            {
                "id": business_row,
                "ws": ctx["ws_a"],
                "external_account_id": _GOOGLE_ACCOUNT,
                "credential": b"x",
                "user_id": ctx["user_a"],
                "now": ctx["now"],
            },
        )
        write_refusal_audit(
            auth,
            None,
            event_type=event_type,
            aggregate_type="connector_account_enrollment",
            aggregate_id=None,
            reason="identity_mismatch",
            provider_or_type="gmail",
        )
        session.rollback()

    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM connector_accounts WHERE id = :id"),
                {"id": business_row},
            ).scalar_one()
            == 0
        )

    rows = _refusal_rows(ctx["ws_a"], event_type)
    assert len(rows) == 1
    aggregate_type, aggregate_id, authorization_result, failure_code, metadata, actor_id = rows[0]
    assert aggregate_type == "connector_account_enrollment"
    assert isinstance(aggregate_id, UUID)
    assert authorization_result == "denied"
    assert failure_code == "identity_mismatch"
    assert actor_id == ctx["user_a"]
    assert metadata == {"reason": "identity_mismatch", "provider": "gmail"}

    payloads = _outbox_payloads(ctx["ws_a"], event_type)
    assert payloads == [{"reason": "identity_mismatch", "provider": "gmail"}]
    serialized = json.dumps([metadata, *payloads])
    assert "@" not in serialized
    assert _GOOGLE_ACCOUNT not in serialized


def test_write_refusal_audit_resource_type_payload_and_given_aggregate(
    two_workspaces: dict[str, Any],
) -> None:
    ctx = two_workspaces
    auth = AuthContext(workspace_id=ctx["ws_a"], user_id=ctx["user_a"], timezone="UTC")
    event_type = "personal_data.share_refused"
    aggregate_id = uuid4()
    write_refusal_audit(
        auth,
        None,
        event_type=event_type,
        aggregate_type="connector_accounts",
        aggregate_id=aggregate_id,
        reason="personal_data",
        provider_or_type="connector_accounts",
        payload_key="resource_type",
    )
    rows = _refusal_rows(ctx["ws_a"], event_type)
    assert len(rows) == 1
    assert rows[0][1] == aggregate_id
    assert rows[0][4] == {"reason": "personal_data", "resource_type": "connector_accounts"}


def _audit_failures(domain: str) -> float:
    return observability.audit_outbox_failures_total._values.get((domain,), 0.0)


def test_write_refusal_audit_never_raises_and_counts_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Boom(RuntimeError):
        pass

    def _broken_factory() -> Any:
        raise _Boom("secret-detail owner@example.test")

    monkeypatch.setattr(connector_security, "SessionFactory", _broken_factory)
    before = _audit_failures("connector_security")
    auth = AuthContext(workspace_id=uuid4(), user_id=uuid4(), timezone="UTC")
    with caplog.at_level(logging.ERROR, logger="ecc.platform.connector_security"):
        write_refusal_audit(
            auth,
            None,
            event_type="connector_account.enrollment_refused",
            aggregate_type="connector_account_enrollment",
            aggregate_id=None,
            reason="identity_mismatch",
            provider_or_type="gmail",
        )
    assert _audit_failures("connector_security") == before + 1
    assert "secret-detail" not in caplog.text
    assert "@example.test" not in caplog.text
    assert any(getattr(r, "error_class", None) == "_Boom" for r in caplog.records)


def test_write_refusal_audit_db_failure_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unknown workspace/actor -> the INSERT itself fails (FK) inside
    # write_audit_and_outbox, which counts it; the wrapper must not double-count.
    before = _audit_failures("connector_security")
    auth = AuthContext(workspace_id=uuid4(), user_id=uuid4(), timezone="UTC")
    write_refusal_audit(
        auth,
        None,
        event_type="connector_account.enrollment_refused",
        aggregate_type="connector_account_enrollment",
        aggregate_id=None,
        reason="identity_mismatch",
        provider_or_type="gmail",
    )
    assert _audit_failures("connector_security") == before + 1


# ---------------------------------------------------------------------------
# revoke_guarded
# ---------------------------------------------------------------------------


class _Adapter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[object] = []

    def disconnect(self, account: object) -> None:
        self.calls.append(account)
        if self.error is not None:
            raise self.error


def _revokes(provider: str, site: str, result: str) -> float:
    return observability.connector_revoke_total._values.get((provider, site, result), 0.0)


def test_revoke_guarded_success_counts_ok() -> None:
    adapter = _Adapter()
    context = object()
    before = _revokes("gmail", "disable", "ok")
    assert revoke_guarded(adapter, context, provider="gmail", site="disable") is True
    assert adapter.calls == [context]
    assert _revokes("gmail", "disable", "ok") == before + 1


def test_revoke_guarded_swallows_exception_logs_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _RevokeFailed(RuntimeError):
        pass

    adapter = _Adapter(_RevokeFailed("token=ya29.secret owner@example.test"))
    before = _revokes("gmail", "removal", "error")
    with caplog.at_level(logging.WARNING, logger="ecc.platform.connector_security"):
        assert revoke_guarded(adapter, object(), provider="gmail", site="removal") is False
    assert _revokes("gmail", "removal", "error") == before + 1
    assert "ya29" not in caplog.text
    assert "@example.test" not in caplog.text
    record = next(r for r in caplog.records if r.getMessage() == "connector_revoke_failed")
    assert getattr(record, "error_class", None) == "_RevokeFailed"
    assert record.exc_info is None


def test_record_revoke_skipped_unsafe_counts() -> None:
    before = _revokes("gmail", "callback_failure", "skipped_unsafe")
    record_revoke_skipped_unsafe(provider="gmail", site="callback_failure")
    assert _revokes("gmail", "callback_failure", "skipped_unsafe") == before + 1


# ---------------------------------------------------------------------------
# membership_mutation_lock_key
# ---------------------------------------------------------------------------


def test_member_removal_waits_on_the_helper_lock_key() -> None:
    """Removal's own advisory lock is exactly `membership_mutation_lock_key`
    (the key the Gmail callback / sync take the shared side of): while
    another connection holds that key, `DELETE .../members/{id}` blocks on
    it, and proceeds once it is released."""
    now = datetime.now(UTC)
    with engine.begin() as connection:
        workspace_id, user_id = _create_workspace(connection, now=now)
        token = f"session-{uuid4()}"
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, expires_at, "
                "last_seen_at) VALUES (:id, :ws, :u, :hash, :expires, :now)"
            ),
            {
                "id": uuid4(),
                "ws": workspace_id,
                "u": user_id,
                "hash": sha256(token.encode()).hexdigest(),
                "expires": now + timedelta(hours=1),
                "now": now,
            },
        )
    key = membership_mutation_lock_key(workspace_id)
    csrf = hmac.new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    result: dict[str, Any] = {}
    holder = engine.connect()
    try:
        holder.execute(text("SELECT pg_advisory_lock(hashtextextended(:k, 0))"), {"k": key})
        holder.commit()
        with TestClient(app) as client:
            client.cookies.set("ecc_session", token)

            def remove() -> None:
                result["response"] = client.delete(
                    f"/api/v1/identity/workspaces/{workspace_id}/members/{uuid4()}",
                    headers={"X-CSRF-Token": csrf},
                )

            worker = threading.Thread(target=remove)
            worker.start()
            waiting = False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not waiting:
                waiting = bool(
                    holder.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                            "AND NOT granted AND ((classid::bigint << 32) | objid::bigint) "
                            "= hashtextextended(:k, 0))"
                        ),
                        {"k": key},
                    ).scalar_one()
                )
                holder.commit()
                if not waiting:
                    time.sleep(0.05)
            assert "response" not in result
            holder.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
            holder.commit()
            worker.join(timeout=10)
        assert waiting, "removal never waited on membership_mutation_lock_key's lock"
        assert result["response"].status_code == 404, result["response"].text
    finally:
        holder.execute(text("SELECT pg_advisory_unlock_all()"))
        holder.commit()
        holder.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE workspace_id = :ws"), {"ws": workspace_id}
            )
        _cleanup_workspace(workspace_id)


# ---------------------------------------------------------------------------
# Metrics exposition
# ---------------------------------------------------------------------------


def test_connector_security_counters_are_rendered() -> None:
    observability.record_connector_enrollment_refused("gmail", "identity_mismatch")
    observability.record_personal_data_share_refused("connector_accounts", "grant")
    observability.record_connector_revoke("gmail", "cascade", "ok")
    observability.record_connector_access_denied("gmail", "sync")
    observability.record_gmail_refresh_rejected("invalid_grant", "lt_1h")
    rendered = observability.render_metrics()
    for line in (
        "# TYPE ecc_connector_enrollment_refused_total counter",
        "# TYPE ecc_personal_data_share_refused_total counter",
        "# TYPE ecc_connector_revoke_total counter",
        "# TYPE ecc_connector_access_denied_total counter",
        "# TYPE ecc_gmail_refresh_rejected_total counter",
        'ecc_connector_enrollment_refused_total{provider="gmail",reason="identity_mismatch"}',
        'ecc_personal_data_share_refused_total{resource_type="connector_accounts",path="grant"}',
        'ecc_connector_revoke_total{provider="gmail",site="cascade",result="ok"}',
        'ecc_connector_access_denied_total{provider="gmail",route="sync"}',
        'ecc_gmail_refresh_rejected_total{error="invalid_grant",since_reconnect="lt_1h"}',
    ):
        assert line in rendered


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_connector_security_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ECC_GMAIL_REVOKE_SCOPE",
        "ECC_PERSONAL_DATA_ISOLATION",
        "ECC_GMAIL_REQUIRE_IDENTITY_MATCH",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    try:
        fresh = Settings(_env_file=None)  # type: ignore[call-arg]
        assert fresh.gmail_revoke_scope == "global"
        assert fresh.personal_data_isolation is False
        assert fresh.gmail_require_identity_match is False
    finally:
        get_settings.cache_clear()


def test_connector_security_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_REVOKE_SCOPE", "none")
    monkeypatch.setenv("ECC_PERSONAL_DATA_ISOLATION", "true")
    monkeypatch.setenv("ECC_GMAIL_REQUIRE_IDENTITY_MATCH", "true")
    get_settings.cache_clear()
    try:
        loaded = get_settings()
        assert loaded.gmail_revoke_scope == "none"
        assert loaded.personal_data_isolation is True
        assert loaded.gmail_require_identity_match is True
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_gmail_revoke_scope_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_REVOKE_SCOPE", "workspace")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]
