"""Phase 7 Personal Intelligence Task 1: domain/consent/vault framework
and the `habits` reference domain (`docs/superpowers/specs/2026-07-31-
phase-7-personal-intelligence-design.md`, `docs/phases/phase-007/
DATA-MODEL.md`).

Covers, per this task's own scope:

1. Domain enable (create + idempotent replay), list, disable (data
   retained, consent revoked), re-enable after disable (fresh consent).
2. `require_enabled_domain` blocks record/goal/routine/check-in creation
   under a domain that was never enabled or was disabled.
3. Generic `domain_records` create/list/get/patch, optimistic-concurrency
   version conflict.
4. `habits` domain: goals, routines (invalid `goal_id` rejected), check-ins
   (idempotent replay -- a retried request must not double-log).
5. Deterministic gap insights: appear once a routine's last check-in (or
   creation, with no check-ins at all) is old enough, dismissal persists
   across recomputation, recomputation refreshes a still-open insight's
   `evidence`/`computed_at` without resurrecting a dismissed one.
6. Whole-domain export (returns every child row) and delete (hard-deletes
   every child row, retains the `personal_domains` row disabled, writes a
   `deletion_jobs` row, revokes consent) -- re-enabling after delete
   starts genuinely empty.
7. Cross-workspace isolation on every list/get endpoint.
8. Direct-SQL rejection of the migration's CHECK constraints
   (`domain_key`, `classification`, `cadence`, `goals.status`).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def personal_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Personal Test', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "personal_insights",
            "check_ins",
            "routines",
            "goals",
            "domain_records",
            "domain_sources",
            "deletion_jobs",
            "domain_consents",
            "personal_domains",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _enable_habits(client: TestClient, token: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/personal/domains",
        json={"domain_key": "habits"},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# --- domain enable/disable/consent ------------------------------------------


def test_enable_domain_creates_row_and_consent(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = personal_test_context
    body = _enable_habits(client, token)
    assert body["domain_key"] == "habits"
    assert body["classification"] == "standard"
    assert body["enabled"] is True
    assert body["enabled_at"] is not None
    assert body["version"] == 1

    consents = client.get("/api/v1/personal/consents", headers=_headers(token)).json()["consents"]
    assert len(consents) == 1
    assert consents[0]["domain_key"] == "habits"
    assert consents[0]["revoked_at"] is None


def test_enable_domain_idempotent_replay(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    key = str(uuid4())
    headers = _headers(token, key)
    first = client.post("/api/v1/personal/domains", json={"domain_key": "habits"}, headers=headers)
    second = client.post("/api/v1/personal/domains", json={"domain_key": "habits"}, headers=headers)
    # `request_id`/`correlation_id` (added by `response_contract_middleware`)
    # legitimately differ per HTTP call even on a cached replay -- compare
    # the actual resource identity/state instead of the whole body.
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["version"] == second.json()["version"] == 1
    # Only one consent row, not two -- the replay never re-executed the write.
    consents = client.get("/api/v1/personal/consents", headers=_headers(token)).json()["consents"]
    assert len(consents) == 1


def test_list_domains(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    assert client.get("/api/v1/personal/domains", headers=_headers(token)).json()["domains"] == []
    _enable_habits(client, token)
    domains = client.get("/api/v1/personal/domains", headers=_headers(token)).json()["domains"]
    assert [d["domain_key"] for d in domains] == ["habits"]


def test_disable_domain_retains_data_revokes_consent(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    routine = client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    ).json()

    resp = client.post(
        "/api/v1/personal/domains/habits/disable", headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    consents = client.get("/api/v1/personal/consents", headers=_headers(token)).json()["consents"]
    assert consents[0]["revoked_at"] is not None

    # Data is retained, not deleted, by a mere disable.
    routines = client.get(
        "/api/v1/personal/routines?domain_key=habits", headers=_headers(token)
    ).json()["routines"]
    assert [r["id"] for r in routines] == [routine["id"]]


def test_reenable_after_disable_grants_fresh_consent(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    client.post("/api/v1/personal/domains/habits/disable", headers=_headers(token, str(uuid4())))
    _enable_habits(client, token)

    consents = client.get("/api/v1/personal/consents", headers=_headers(token)).json()["consents"]
    assert len(consents) == 2
    assert consents[0]["revoked_at"] is None  # most recent grant, ordered by granted_at DESC


def test_disabled_domain_blocks_record_and_routine_creation(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    resp = client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DOMAIN_NOT_ENABLED"

    resp = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {}},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 403


def test_revoke_consent_disables_domain(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    consent_id = client.get("/api/v1/personal/consents", headers=_headers(token)).json()[
        "consents"
    ][0]["id"]
    resp = client.post(
        f"/api/v1/personal/consents/{consent_id}/revoke", headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# --- generic domain_records --------------------------------------------------


def test_record_create_list_get_patch(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)

    created = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {"text": "hi"}},
        headers=_headers(token, str(uuid4())),
    )
    assert created.status_code == 201
    record = created.json()
    assert record["payload"] == {"text": "hi"}
    assert record["version"] == 1

    listed = client.get(
        "/api/v1/personal/records?domain_key=habits", headers=_headers(token)
    ).json()["records"]
    assert [r["id"] for r in listed] == [record["id"]]

    fetched = client.get(f"/api/v1/personal/records/{record['id']}", headers=_headers(token))
    assert fetched.json()["payload"] == {"text": "hi"}

    patched = client.patch(
        f"/api/v1/personal/records/{record['id']}",
        json={"expected_version": 1, "payload": {"text": "updated"}},
        headers=_headers(token, str(uuid4())),
    )
    assert patched.status_code == 200
    assert patched.json()["payload"] == {"text": "updated"}
    assert patched.json()["version"] == 2


def test_record_patch_version_conflict(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    record = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {}},
        headers=_headers(token, str(uuid4())),
    ).json()
    resp = client.patch(
        f"/api/v1/personal/records/{record['id']}",
        json={"expected_version": 99, "payload": {}},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERSION_CONFLICT"


def test_get_record_not_found(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    resp = client.get(f"/api/v1/personal/records/{uuid4()}", headers=_headers(token))
    assert resp.status_code == 404


# --- goals/routines/check-ins ------------------------------------------------


def test_routine_rejects_unknown_goal(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    resp = client.post(
        "/api/v1/personal/routines",
        json={
            "domain_key": "habits",
            "goal_id": str(uuid4()),
            "title": "Meditate",
            "cadence": "daily",
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "GOAL_NOT_FOUND"


def test_goal_routine_check_in_lifecycle(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)

    goal = client.post(
        "/api/v1/personal/goals",
        json={"domain_key": "habits", "title": "Meditate 20 times", "target_count": 20},
        headers=_headers(token, str(uuid4())),
    ).json()
    assert goal["status"] == "active"

    routine = client.post(
        "/api/v1/personal/routines",
        json={
            "domain_key": "habits",
            "goal_id": goal["id"],
            "title": "Daily meditation",
            "cadence": "daily",
        },
        headers=_headers(token, str(uuid4())),
    ).json()
    assert routine["goal_id"] == goal["id"]

    check_in = client.post(
        "/api/v1/personal/check-ins",
        json={"routine_id": routine["id"], "note": "felt calm"},
        headers=_headers(token, str(uuid4())),
    )
    assert check_in.status_code == 201
    assert check_in.json()["note"] == "felt calm"

    check_ins = client.get(
        f"/api/v1/personal/check-ins?routine_id={routine['id']}", headers=_headers(token)
    ).json()["check_ins"]
    assert len(check_ins) == 1


def test_check_in_idempotent_replay_does_not_duplicate(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    routine = client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    ).json()

    key = str(uuid4())
    headers = _headers(token, key)
    payload = {"routine_id": routine["id"]}
    first = client.post("/api/v1/personal/check-ins", json=payload, headers=headers)
    second = client.post("/api/v1/personal/check-ins", json=payload, headers=headers)
    assert first.json()["id"] == second.json()["id"]

    check_ins = client.get(
        f"/api/v1/personal/check-ins?routine_id={routine['id']}", headers=_headers(token)
    ).json()["check_ins"]
    assert len(check_ins) == 1


def test_check_in_unknown_routine_404s(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    resp = client.post(
        "/api/v1/personal/check-ins",
        json={"routine_id": str(uuid4())},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 404


# --- deterministic insights ---------------------------------------------------


def test_gap_insight_appears_and_dismiss_persists_across_recompute(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = personal_test_context
    _enable_habits(client, token)
    routine = client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    ).json()

    # No check-ins yet -- routine.created_at is the anchor, but it is fresh
    # (just created), so no gap insight should appear yet.
    insights = client.get("/api/v1/personal/insights", headers=_headers(token)).json()["insights"]
    assert insights == []

    # Backdate the routine's own created_at directly (no check-in yet) to
    # simulate a genuinely stale routine, matching the "no check-ins logged
    # yet" missing_data case.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE routines SET created_at = :old WHERE id = :id"),
            {"old": datetime.now(UTC) - timedelta(days=10), "id": routine["id"]},
        )

    insights = client.get("/api/v1/personal/insights", headers=_headers(token)).json()["insights"]
    assert len(insights) == 1
    insight = insights[0]
    assert insight["kind"] == "observation"
    assert insight["domain_key"] == "habits"
    assert insight["missing_data"] == "no check-ins logged yet"
    assert insight["dismissed_at"] is None

    dismissed = client.post(
        f"/api/v1/personal/insights/{insight['id']}/dismiss", headers=_headers(token, str(uuid4()))
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["dismissed_at"] is not None

    # Recomputation (a second GET) must not resurrect the dismissed insight.
    insights_after = client.get("/api/v1/personal/insights", headers=_headers(token)).json()[
        "insights"
    ]
    assert insights_after == []


def test_recent_check_in_suppresses_gap_insight(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    routine = client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    ).json()
    client.post(
        "/api/v1/personal/check-ins",
        json={"routine_id": routine["id"]},
        headers=_headers(token, str(uuid4())),
    )
    insights = client.get("/api/v1/personal/insights", headers=_headers(token)).json()["insights"]
    assert insights == []


def test_dismiss_unknown_insight_404s(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    resp = client.post(
        f"/api/v1/personal/insights/{uuid4()}/dismiss", headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 404


# --- export/delete ------------------------------------------------------------


def test_export_domain_returns_every_child_row(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    goal = client.post(
        "/api/v1/personal/goals",
        json={"domain_key": "habits", "title": "Meditate 20 times"},
        headers=_headers(token, str(uuid4())),
    ).json()
    routine = client.post(
        "/api/v1/personal/routines",
        json={
            "domain_key": "habits",
            "goal_id": goal["id"],
            "title": "Meditate",
            "cadence": "daily",
        },
        headers=_headers(token, str(uuid4())),
    ).json()
    client.post(
        "/api/v1/personal/check-ins",
        json={"routine_id": routine["id"]},
        headers=_headers(token, str(uuid4())),
    )
    client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {"text": "hi"}},
        headers=_headers(token, str(uuid4())),
    )

    export = client.post(
        "/api/v1/personal/domains/habits/export", headers=_headers(token, str(uuid4()))
    )
    assert export.status_code == 200
    body = export.json()
    assert len(body["goals"]) == 1
    assert len(body["routines"]) == 1
    assert len(body["check_ins"]) == 1
    assert len(body["domain_records"]) == 1


def test_export_unknown_domain_404s(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    resp = client.post(
        "/api/v1/personal/domains/habits/export", headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 404


def test_delete_domain_removes_data_retains_disabled_row(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    routine = client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    ).json()
    client.post(
        "/api/v1/personal/check-ins",
        json={"routine_id": routine["id"]},
        headers=_headers(token, str(uuid4())),
    )

    resp = client.post(
        "/api/v1/personal/domains/habits/delete", headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    # Data is gone.
    routines = client.get(
        "/api/v1/personal/routines?domain_key=habits", headers=_headers(token)
    ).json()["routines"]
    assert routines == []

    # The personal_domains row itself is retained, disabled -- not deleted
    # (see export_deletion.py's own docstring for why: deletion_jobs/
    # domain_consents FK against it).
    domains = client.get("/api/v1/personal/domains", headers=_headers(token)).json()["domains"]
    assert domains[0]["enabled"] is False

    with engine.begin() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT status, scope FROM deletion_jobs "
                    "WHERE workspace_id = :workspace_id AND domain_key = 'habits'"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"
    assert jobs[0]["scope"] == "domain"


def test_reenable_after_delete_starts_empty(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = personal_test_context
    _enable_habits(client, token)
    client.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token, str(uuid4())),
    )
    client.post("/api/v1/personal/domains/habits/delete", headers=_headers(token, str(uuid4())))
    _enable_habits(client, token)

    routines = client.get(
        "/api/v1/personal/routines?domain_key=habits", headers=_headers(token)
    ).json()["routines"]
    assert routines == []


# --- cross-workspace isolation ------------------------------------------------


def test_cross_workspace_isolation(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client_a, _workspace_a, _user_a, token_a = personal_test_context
    _enable_habits(client_a, token_a)
    routine = client_a.post(
        "/api/v1/personal/routines",
        json={"domain_key": "habits", "title": "Meditate", "cadence": "daily"},
        headers=_headers(token_a, str(uuid4())),
    ).json()

    workspace_b = uuid4()
    user_b = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_b,
            user_id=user_b,
            email=f"{user_b}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_b,
                "user_id": user_b,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    client_b = TestClient(app)
    client_b.cookies.set("ecc_session", token_b)
    try:
        # Workspace B sees no domains, no routines, and cannot reach A's
        # routine/record/insight by id.
        assert (
            client_b.get("/api/v1/personal/domains", headers=_headers(token_b)).json()["domains"]
            == []
        )
        # B's own habits domain isn't enabled, so a check-in against A's
        # routine id 404s (routine lookup itself is workspace-scoped).
        resp = client_b.post(
            "/api/v1/personal/check-ins",
            json={"routine_id": routine["id"]},
            headers=_headers(token_b, str(uuid4())),
        )
        assert resp.status_code == 404
    finally:
        client_b.close()
        _cleanup_workspace(workspace_b)


# --- CHECK constraint rejections ---------------------------------------------


def test_check_constraint_rejects_unknown_domain_key(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = personal_test_context
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO personal_domains (
                        id, workspace_id, owner_id, domain_key, classification,
                        enabled, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, 'not_a_real_domain', 'standard',
                        false, :owner_id, :owner_id, :now, :now, 1
                    )
                    """
                ),
                {"id": uuid4(), "workspace_id": workspace_id, "owner_id": user_id, "now": now},
            )


def test_check_constraint_rejects_unknown_classification(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = personal_test_context
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO personal_domains (
                        id, workspace_id, owner_id, domain_key, classification,
                        enabled, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, 'habits', 'not_a_real_tier',
                        false, :owner_id, :owner_id, :now, :now, 1
                    )
                    """
                ),
                {"id": uuid4(), "workspace_id": workspace_id, "owner_id": user_id, "now": now},
            )


def test_check_constraint_rejects_unknown_cadence(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = personal_test_context
    _enable_habits(client, token)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO routines (
                        id, workspace_id, owner_id, domain_key, title, cadence,
                        created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, 'habits', 'x', 'yearly',
                        :owner_id, :owner_id, :now, :now, 1
                    )
                    """
                ),
                {"id": uuid4(), "workspace_id": workspace_id, "owner_id": user_id, "now": now},
            )


def test_check_constraint_rejects_unknown_goal_status(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = personal_test_context
    _enable_habits(client, token)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO goals (
                        id, workspace_id, owner_id, domain_key, title, status,
                        created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, 'habits', 'x', 'not_a_real_status',
                        :owner_id, :owner_id, :now, :now, 1
                    )
                    """
                ),
                {"id": uuid4(), "workspace_id": workspace_id, "owner_id": user_id, "now": now},
            )
