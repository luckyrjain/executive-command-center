"""Cross-domain sweep of the lock-before-authorize fix (#314/#315/#316):
calendar events, tasks, commitments, risks, recommendations, notes, the
knowledge graph (entities, claims, evidence, relationships, resolution
candidates, merge/reverse/split) and automation (workflow publish/disable,
approvals, run cancel/pause/resume, policy revoke) all authorized the caller *before* taking
`SELECT ... FOR UPDATE` on the row the decision is about. An ownership
transfer (`authz_grants` transfer locks the row, rewrites `owner_id`, and
does not bump `version`) that committed while the request waited on that
row lock left the request writing to a row the caller could no longer see.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing.

Concurrency is real (request thread + separate connection); "is waiting" is
observed in `pg_stat_activity` (scoped to backends blocked by this test's
lock holder), not assumed from timing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.automation import workflows as automation_workflows
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

settings = get_settings()
Snapshot = dict[str, list[dict[str, Any]]]
_WAIT_SECONDS = 15

# Every table a seed below writes a row the racing caller must not see once
# it is transferred -- all flipped to `private` after seeding.
_PRIVATE_TABLES = (
    "tasks",
    "commitments",
    "risks",
    "notes",
    "calendar_events",
    "recommendations",
    "pkos_nodes",
    "pkos_evidence",
    "pkos_edges",
    "knowledge_claims",
    "entity_aliases",
    "resolution_candidates",
    "entity_operations",
    "workflow_definitions",
    "workflow_versions",
    "workflow_runs",
    "approval_requests",
    "automation_policies",
)


@dataclass
class RaceWorld:
    """One workspace: A (`owner`), B (`admin`, the racing caller and the
    rows' original owner) and C (`member`, the transfer target)."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str


def _session_row(
    connection: Connection, ws: UUID, user_id: UUID, token: str, now: datetime
) -> None:
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
        ),
        {
            "id": uuid4(),
            "workspace_id": ws,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "last_seen_at": now,
        },
    )


def _workspace_tables() -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema = 'public' AND c.column_name = 'workspace_id' "
                    "AND t.table_type = 'BASE TABLE' AND c.table_name <> 'workspaces'"
                )
            ).scalars()
        )


def _delete_workspace(ws: UUID) -> None:
    """Deletes every workspace-scoped row, retrying in passes so foreign
    keys between the many tables these seeds touch resolve without a
    hand-maintained order."""
    with engine.connect() as connection:
        account_ids = list(
            connection.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
            ).scalars()
        )
    pending = _workspace_tables()
    for _ in range(10):
        failed: list[str] = []
        with engine.begin() as connection:
            for table in pending:
                savepoint = connection.begin_nested()
                try:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                        {"ws": ws},
                    )
                    savepoint.commit()
                except Exception:  # noqa: BLE001 -- FK order, retried next pass
                    savepoint.rollback()
                    failed.append(table)
        if not failed:
            break
        pending = failed
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
        connection.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids})


@pytest.fixture
def race_world() -> Iterator[RaceWorld]:
    ws, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Lock Before Authorize Race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="admin")
        create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session_row(connection, ws, b, b_token, now)
    try:
        yield RaceWorld(ws=ws, a=a, b=b, c=c, b_token=b_token)
    finally:
        _delete_workspace(ws)


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


def _client(w: RaceWorld) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    return client


def _api(w: RaceWorld, path: str, body: dict[str, Any]) -> dict[str, Any]:
    client = _client(w)  # no `with`: skip lifespan startup work
    try:
        response = client.post(path, headers=_headers(w.b_token), json=body)
    finally:
        client.close()
    assert response.status_code in (200, 201), response.text
    return dict(response.json())


def _sql(sql: str, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Seeds: each inserts rows owned by B and returns the ids the request path
# needs. `id` is always the row the race locks and transfers.
# ---------------------------------------------------------------------------


def _task(w: RaceWorld) -> UUID:
    task_id = uuid4()
    _sql(
        "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
        "created_at, updated_at) VALUES (:id, :ws, :b, 'Race task', :b, :b, now(), now())",
        id=task_id,
        ws=w.ws,
        b=w.b,
    )
    return task_id


def _commitment(w: RaceWorld) -> UUID:
    commitment_id = uuid4()
    _sql(
        "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, status, "
        "created_by, updated_by, created_at, updated_at) "
        "VALUES (:id, :ws, :b, 'Race commitment', 'made_by_me', 'active', :b, :b, now(), now())",
        id=commitment_id,
        ws=w.ws,
        b=w.b,
    )
    return commitment_id


def _risk(w: RaceWorld) -> UUID:
    risk_id = uuid4()
    _sql(
        "INSERT INTO risks (id, workspace_id, description, probability, impact, owner_id, "
        "created_by, updated_by, created_at, updated_at) "
        "VALUES (:id, :ws, 'Race risk', 3, 3, :b, :b, :b, now(), now())",
        id=risk_id,
        ws=w.ws,
        b=w.b,
    )
    return risk_id


def _node(w: RaceWorld, name: str = "Race Entity") -> UUID:
    node_id = uuid4()
    _sql(
        "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
        "created_at, updated_at, owner_id) VALUES (:id, :ws, 'person', :name, now(), now(), :b)",
        id=node_id,
        ws=w.ws,
        name=name,
        b=w.b,
    )
    return node_id


def _evidence(w: RaceWorld, node_id: UUID) -> UUID:
    evidence_id = uuid4()
    _sql(
        "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
        "sha256, captured_at, owner_id) "
        "VALUES (:id, :ws, :node, 'seed_fixture', :ref, :digest, now(), :b)",
        id=evidence_id,
        ws=w.ws,
        node=node_id,
        ref=f"test://lock-race/{evidence_id}",
        digest=sha256(str(evidence_id).encode()).hexdigest(),
        b=w.b,
    )
    return evidence_id


def _candidate(w: RaceWorld, status: str = "open") -> dict[str, UUID]:
    left, right, candidate_id = _node(w, "Ada Lovelace"), _node(w, "Ada Lovelase"), uuid4()
    _sql(
        "INSERT INTO resolution_candidates (id, workspace_id, left_entity_id, right_entity_id, "
        "score, factors_json, resolver_version, status, created_at, owner_id) "
        "VALUES (:id, :ws, :left, :right, 0.9, '{}'::jsonb, 'race-v1', :status, now(), :b)",
        id=candidate_id,
        ws=w.ws,
        left=left,
        right=right,
        status=status,
        b=w.b,
    )
    return {"id": candidate_id, "left": left, "right": right}


def _merged(w: RaceWorld) -> dict[str, UUID]:
    """A real merge (through the API) the reverse/split cases act on."""
    pair = _candidate(w, status="confirmed")
    operation = _api(
        w,
        "/api/v1/knowledge/entities/merge",
        {
            "candidate_id": str(pair["id"]),
            "target_entity_id": str(pair["left"]),
            "expected_target_version": 1,
            "expected_source_version": 1,
            "reason": "confirmed duplicate",
        },
    )
    return {"operation": UUID(operation["id"]), "target": pair["left"], "source": pair["right"]}


def _recommendation(
    w: RaceWorld, *, status: str, target_type: str, target_id: UUID, action: dict[str, Any]
) -> UUID:
    recommendation_id = uuid4()
    _sql(
        "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
        "target_id, proposed_action, rationale, confidence, status, evidence_ids, source, "
        "created_by, updated_by, created_at, updated_at, version, expected_version, owner_id) "
        "VALUES (:id, :ws, 'task_priority', :target_type, :target_id, "
        "CAST(:action AS jsonb), 'Race rationale', 0.9, :status, ARRAY[]::uuid[], 'rule', "
        ":b, :b, now(), now(), 1, 1, :b)",
        id=recommendation_id,
        ws=w.ws,
        target_type=target_type,
        target_id=target_id,
        action=dumps(action),
        status=status,
        b=w.b,
    )
    return recommendation_id


def _workflow_version(w: RaceWorld, status: str, workflow_id: str | None = None) -> UUID:
    workflow_id = workflow_id or f"race.workflow.{uuid4().hex[:8]}"
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "condition",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    version_id = uuid4()
    _sql(
        "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
        "created_at, updated_at, owner_id) VALUES (:id, :ws, :wf, :b, now(), now(), :b)",
        id=uuid4(),
        ws=w.ws,
        wf=workflow_id,
        b=w.b,
    )
    _sql(
        "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
        "trigger_refs, policy_ref, definition_hash, status, created_by, updated_by, "
        "created_at, updated_at, owner_id) VALUES (:id, :ws, :wf, 1, CAST(:graph AS jsonb), "
        "'[]'::jsonb, NULL, :digest, :status, :b, :b, now(), now(), :b)",
        id=version_id,
        ws=w.ws,
        wf=workflow_id,
        graph=dumps(graph),
        digest=automation_workflows.compute_definition_hash(
            graph=graph, trigger_refs=[], policy_ref=None
        ),
        status=status,
        b=w.b,
    )
    return version_id


_DIGEST = sha256(b"lock-race-action").hexdigest()


def _approval(w: RaceWorld) -> UUID:
    run_id, approval_id = uuid4(), uuid4()
    _workflow_version(w, "active", "race.workflow")
    _sql(
        "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, status, "
        "queued_at, created_by, created_at, updated_at, owner_id) "
        "VALUES (:id, :ws, 'race.workflow', 1, 'waiting_approval', now(), :b, now(), now(), :b)",
        id=run_id,
        ws=w.ws,
        b=w.b,
    )
    _sql(
        "INSERT INTO approval_requests (id, workspace_id, run_id, step_index, action_digest, "
        "status, requested_at, expires_at, created_at, updated_at, owner_id) "
        "VALUES (:id, :ws, :run, 0, :digest, 'pending', now(), now() + interval '1 day', "
        "now(), now(), :b)",
        id=approval_id,
        ws=w.ws,
        run=run_id,
        digest=_DIGEST,
        b=w.b,
    )
    return approval_id


def _run(w: RaceWorld, status: str) -> UUID:
    run_id = uuid4()
    _workflow_version(w, "active", "race.workflow")
    _sql(
        "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, status, "
        "queued_at, created_by, created_at, updated_at, owner_id) "
        "VALUES (:id, :ws, 'race.workflow', 1, :status, now(), :b, now(), now(), :b)",
        id=run_id,
        ws=w.ws,
        status=status,
        b=w.b,
    )
    return run_id


def _policy(w: RaceWorld) -> UUID:
    policy_id = uuid4()
    _workflow_version(w, "active", "race.workflow")
    _sql(
        "INSERT INTO automation_policies (id, workspace_id, workflow_id, value_limit, "
        "count_limit, approval_mode, expires_at, created_by, updated_by, created_at, "
        "updated_at, owner_id) VALUES (:id, :ws, 'race.workflow', 0, 0, 'per_run', "
        "now() + interval '1 day', :b, :b, now(), now(), :b)",
        id=policy_id,
        ws=w.ws,
        b=w.b,
    )
    return policy_id


def _seed_claim(w: RaceWorld) -> dict[str, UUID]:
    node_id = _node(w)
    evidence_id = _evidence(w, node_id)
    claim_id = uuid4()
    _sql(
        "INSERT INTO knowledge_claims (id, workspace_id, subject_id, predicate, value_json, "
        "source_id, created_at, owner_id) "
        "VALUES (:id, :ws, :node, 'title', '{\"text\": \"old\"}'::jsonb, :ev, now(), :b)",
        id=claim_id,
        ws=w.ws,
        node=node_id,
        ev=evidence_id,
        b=w.b,
    )
    return {"id": node_id, "claim": claim_id, "source": evidence_id}


def _seed_edge(w: RaceWorld) -> dict[str, UUID]:
    left, right = _node(w, "Edge Left"), _node(w, "Edge Right")
    evidence_id = _evidence(w, left)
    edge_id = uuid4()
    _sql(
        "INSERT INTO pkos_edges (id, workspace_id, source_node_id, target_node_id, edge_type, "
        "evidence_id, owner_id) VALUES (:id, :ws, :l, :r, 'RELATES_TO', :ev, :b)",
        id=edge_id,
        ws=w.ws,
        l=left,
        r=right,
        ev=evidence_id,
        b=w.b,
    )
    return {"id": edge_id}


def _seed_merge(w: RaceWorld) -> dict[str, UUID]:
    pair = _candidate(w, status="confirmed")
    # The target entity is the row the race transfers.
    return {"id": pair["left"], "candidate": pair["id"], "source": pair["right"]}


def _seed_merged_by_operation(w: RaceWorld) -> dict[str, UUID]:
    merged = _merged(w)
    return {"id": merged["operation"]}


def _seed_merged_by_entity(w: RaceWorld) -> dict[str, UUID]:
    merged = _merged(w)
    return {"id": merged["source"], "operation": merged["operation"]}


def _seed_task_recommendation(w: RaceWorld, *, lock_target: bool) -> dict[str, UUID]:
    task_id = _task(w)
    recommendation_id = _recommendation(
        w,
        status="pending_confirmation",
        target_type="task",
        target_id=task_id,
        action={"operation": "set_status", "value": "in_progress"},
    )
    if lock_target:
        return {"id": task_id, "recommendation": recommendation_id}
    return {"id": recommendation_id}


def _seed_risk_recommendation(w: RaceWorld) -> dict[str, UUID]:
    risk_id = _risk(w)
    recommendation_id = _recommendation(
        w,
        status="pending_confirmation",
        target_type="risk",
        target_id=risk_id,
        action={"operation": "set_status", "value": "mitigating"},
    )
    return {"id": risk_id, "recommendation": recommendation_id}


def _seed_commitment_recommendation(w: RaceWorld) -> dict[str, UUID]:
    commitment_id = _commitment(w)
    recommendation_id = _recommendation(
        w,
        status="pending_confirmation",
        target_type="commitment",
        target_id=commitment_id,
        action={"operation": "set_status", "value": "fulfilled"},
    )
    return {"id": commitment_id, "recommendation": recommendation_id}


def _seed_proposed_recommendation(w: RaceWorld) -> dict[str, UUID]:
    return {
        "id": _recommendation(
            w,
            status="proposed",
            target_type="task",
            target_id=_task(w),
            action={"operation": "set_priority", "value": "critical"},
        )
    }


def _one(seed: Callable[[RaceWorld], UUID]) -> Callable[[RaceWorld], dict[str, UUID]]:
    return lambda w: {"id": seed(w)}


@dataclass(frozen=True)
class Case:
    table: str  # the table of the row `id` names -- locked and transferred
    seed: Callable[[RaceWorld], dict[str, UUID]]
    path: str  # formatted with the seed's ids
    body: dict[str, Any] | None
    not_found: str
    method: str = "POST"
    ok_status: int = 200  # uncontested success, asserted by the control


_V1: dict[str, Any] = {"expected_version": 1}
_CONFIRM: dict[str, Any] = {"expected_version": 1, "target_expected_version": 1}
_FUTURE = (datetime.now(UTC) + timedelta(days=3)).isoformat()


CASES: dict[str, Case] = {
    # calendar/events.py
    "calendar_patch": Case(
        "calendar_events",
        _one(
            lambda w: _calendar_event(w),
        ),
        "/api/v1/calendar/events/{id}",
        {"expected_version": 1, "title": "Race probe"},
        "CALENDAR_EVENT_NOT_FOUND",
        method="PATCH",
    ),
    "calendar_archive": Case(
        "calendar_events",
        _one(lambda w: _calendar_event(w)),
        "/api/v1/calendar/events/{id}/archive",
        _V1,
        "CALENDAR_EVENT_NOT_FOUND",
    ),
    # planning/tasks.py
    "task_patch": Case(
        "tasks",
        _one(_task),
        "/api/v1/tasks/{id}",
        {"expected_version": 1, "title": "Race probe"},
        "TASK_NOT_FOUND",
        method="PATCH",
    ),
    "task_complete": Case(
        "tasks", _one(_task), "/api/v1/tasks/{id}/complete", _V1, "TASK_NOT_FOUND"
    ),
    "task_set_status_via_recommendation": Case(
        "tasks",
        lambda w: _seed_task_recommendation(w, lock_target=True),
        "/api/v1/recommendations/{recommendation}/confirm",
        _CONFIRM,
        "TASK_NOT_FOUND",
    ),
    # communication/commitments.py
    "commitment_patch": Case(
        "commitments",
        _one(_commitment),
        "/api/v1/commitments/{id}",
        {"expected_version": 1, "summary": "Race probe"},
        "COMMITMENT_NOT_FOUND",
        method="PATCH",
    ),
    "commitment_fulfil": Case(
        "commitments",
        _one(_commitment),
        "/api/v1/commitments/{id}/fulfil",
        _V1,
        "COMMITMENT_NOT_FOUND",
    ),
    # governance/risk_mutations.py
    "risk_patch": Case(
        "risks",
        _one(_risk),
        "/api/v1/risks/{id}",
        {"expected_version": 1, "description": "Race probe"},
        "RISK_NOT_FOUND",
        method="PATCH",
    ),
    "risk_archive": Case("risks", _one(_risk), "/api/v1/risks/{id}/archive", _V1, "RISK_NOT_FOUND"),
    "risk_set_status_via_recommendation": Case(
        "risks",
        _seed_risk_recommendation,
        "/api/v1/recommendations/{recommendation}/confirm",
        _CONFIRM,
        "RISK_NOT_FOUND",
    ),
    "commitment_lifecycle_via_recommendation": Case(
        "commitments",
        _seed_commitment_recommendation,
        "/api/v1/recommendations/{recommendation}/confirm",
        _CONFIRM,
        "COMMITMENT_NOT_FOUND",
    ),
    # governance/recommendation_mutations.py
    "recommendation_publish": Case(
        "recommendations",
        _seed_proposed_recommendation,
        "/api/v1/recommendations/{id}/publish",
        _V1,
        "RECOMMENDATION_NOT_FOUND",
    ),
    "recommendation_confirm": Case(
        "recommendations",
        lambda w: _seed_task_recommendation(w, lock_target=False),
        "/api/v1/recommendations/{id}/confirm",
        _CONFIRM,
        "RECOMMENDATION_NOT_FOUND",
    ),
    # knowledge/notes.py
    "note_patch": Case(
        "notes",
        _one(lambda w: _note(w)),
        "/api/v1/notes/{id}",
        {"expected_version": 1, "body": "Race probe"},
        "NOTE_NOT_FOUND",
        method="PATCH",
    ),
    "note_archive": Case(
        "notes", _one(lambda w: _note(w)), "/api/v1/notes/{id}/archive", _V1, "NOTE_NOT_FOUND"
    ),
    # knowledge/entities_mutations.py
    "entity_patch": Case(
        "pkos_nodes",
        _one(_node),
        "/api/v1/knowledge/entities/{id}",
        {"expected_version": 1, "canonical_name": "Race probe"},
        "ENTITY_NOT_FOUND",
        method="PATCH",
    ),
    "entity_archive": Case(
        "pkos_nodes",
        _one(_node),
        "/api/v1/knowledge/entities/{id}/archive",
        _V1,
        "ENTITY_NOT_FOUND",
    ),
    # knowledge/claims.py -- authorized against, and so locked on, the entity
    "claim_supersede": Case(
        "pkos_nodes",
        _seed_claim,
        "/api/v1/knowledge/entities/{id}/claims/{claim}/supersede",
        None,  # built from the seed's evidence id, see _body
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    # knowledge/evidence.py
    "evidence_delete": Case(
        "pkos_evidence",
        _one(lambda w: _evidence(w, _node(w))),
        "/api/v1/evidence/{id}/delete",
        {"reason": "race probe"},
        "EVIDENCE_NOT_FOUND",
    ),
    # knowledge/relationships_mutations.py
    "relationship_invalidate": Case(
        "pkos_edges",
        _seed_edge,
        "/api/v1/knowledge/relationships/{id}/invalidate",
        {},
        "RELATIONSHIP_NOT_FOUND",
    ),
    # knowledge/resolution.py
    "candidate_confirm": Case(
        "resolution_candidates",
        _candidate,
        "/api/v1/knowledge/resolution/candidates/{id}/confirm",
        {"reason": "race probe"},
        "CANDIDATE_NOT_FOUND",
    ),
    "candidate_defer": Case(
        "resolution_candidates",
        _candidate,
        "/api/v1/knowledge/resolution/candidates/{id}/defer",
        {"deferred_until": _FUTURE},
        "CANDIDATE_NOT_FOUND",
    ),
    # knowledge/entity_operations.py
    "merge_target_entity": Case(
        "pkos_nodes",
        _seed_merge,
        "/api/v1/knowledge/entities/merge",
        None,  # see _body
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    "reverse_operation": Case(
        "entity_operations",
        _seed_merged_by_operation,
        "/api/v1/knowledge/entity-operations/{id}/reverse",
        {"reason": "race probe"},
        "OPERATION_NOT_FOUND",
        ok_status=201,
    ),
    "reverse_source_entity": Case(
        "pkos_nodes",
        _seed_merged_by_entity,
        "/api/v1/knowledge/entity-operations/{operation}/reverse",
        {"reason": "race probe"},
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    "split_operation": Case(
        "entity_operations",
        _seed_merged_by_operation,
        "/api/v1/knowledge/entity-operations/{id}/split",
        {"reason": "race probe"},
        "OPERATION_NOT_FOUND",
        ok_status=201,
    ),
    "split_source_entity": Case(
        "pkos_nodes",
        _seed_merged_by_entity,
        "/api/v1/knowledge/entity-operations/{operation}/split",
        {"reason": "race probe"},
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    # automation/workflows.py, approvals.py, runs.py, policy.py
    "workflow_publish": Case(
        "workflow_versions",
        _one(lambda w: _workflow_version(w, "draft")),
        "/api/v1/automations/workflows/{id}/publish",
        None,
        "WORKFLOW_NOT_FOUND",
    ),
    "workflow_disable": Case(
        "workflow_versions",
        _one(lambda w: _workflow_version(w, "active")),
        "/api/v1/automations/workflows/{id}/disable",
        None,
        "WORKFLOW_NOT_FOUND",
    ),
    "approval_approve": Case(
        "approval_requests",
        _one(_approval),
        "/api/v1/automations/approvals/{id}/approve",
        {"action_digest": _DIGEST},
        "APPROVAL_NOT_FOUND",
    ),
    "approval_reject": Case(
        "approval_requests",
        _one(_approval),
        "/api/v1/automations/approvals/{id}/reject",
        {},
        "APPROVAL_NOT_FOUND",
    ),
    "run_cancel": Case(
        "workflow_runs",
        _one(lambda w: _run(w, "queued")),
        "/api/v1/automations/runs/{id}/cancel",
        None,
        "RUN_NOT_FOUND",
    ),
    "run_pause": Case(
        "workflow_runs",
        _one(lambda w: _run(w, "running")),
        "/api/v1/automations/runs/{id}/pause",
        None,
        "RUN_NOT_FOUND",
    ),
    "run_resume": Case(
        "workflow_runs",
        _one(lambda w: _run(w, "paused")),
        "/api/v1/automations/runs/{id}/resume",
        None,
        "RUN_NOT_FOUND",
    ),
    "policy_revoke": Case(
        "automation_policies",
        _one(_policy),
        "/api/v1/automations/policies/{id}/revoke",
        None,
        "POLICY_NOT_FOUND",
    ),
}


def _calendar_event(w: RaceWorld) -> UUID:
    event_id = uuid4()
    _sql(
        "INSERT INTO calendar_events (id, workspace_id, title, starts_at, ends_at, timezone, "
        "created_by, updated_by, created_at, updated_at, owner_id) "
        "VALUES (:id, :ws, 'Race event', now() + interval '1 day', "
        "now() + interval '1 day 1 hour', 'UTC', :b, :b, now(), now(), :b)",
        id=event_id,
        ws=w.ws,
        b=w.b,
    )
    return event_id


def _note(w: RaceWorld) -> UUID:
    note_id = uuid4()
    _sql(
        "INSERT INTO notes (id, workspace_id, owner_id, body, created_by, updated_by, "
        "created_at, updated_at) VALUES (:id, :ws, :b, 'Race note', :b, :b, now(), now())",
        id=note_id,
        ws=w.ws,
        b=w.b,
    )
    return note_id


def _body(name: str, case: Case, ids: dict[str, UUID]) -> dict[str, Any] | None:
    if name == "claim_supersede":
        return {"predicate": "title", "value": {"text": "new"}, "source_id": str(ids["source"])}
    if name == "merge_target_entity":
        return {
            "candidate_id": str(ids["candidate"]),
            "target_entity_id": str(ids["id"]),
            "expected_target_version": 1,
            "expected_source_version": 1,
            "reason": "race probe",
        }
    return case.body


def _seed(w: RaceWorld, case: Case) -> dict[str, UUID]:
    ids = case.seed(w)
    with engine.begin() as connection:
        for table in _PRIVATE_TABLES:
            connection.execute(
                text(f"UPDATE {table} SET visibility = 'private' WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": w.ws},
            )
    return ids


def _lock_waiters(table: str, holder_pid: int) -> int:
    """Backends blocked on `holder_pid` (this test's lock holder, so an
    unrelated concurrent backend cannot satisfy the wait) in a locking read
    of `table`."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* :pattern"
                ),
                {"holder": holder_pid, "pattern": f"FROM {table}\\s.*FOR (NO KEY )?UPDATE"},
            ).scalar_one()
        )


def _wait_for_lock_waiter(table: str, holder_pid: int) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while _lock_waiters(table, holder_pid) < 1:
        if time.monotonic() > deadline:
            raise AssertionError(f"mutation never blocked on the {table} row lock")
        time.sleep(0.05)


def _row_snapshot(table: str, row_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(f"SELECT * FROM {table} WHERE id = :id"),  # noqa: S608 -- CASES literal
                {"id": row_id},
            )
            .mappings()
            .one()
        )


def _workspace_snapshot(ws: UUID, connection: Connection | None = None) -> Snapshot:
    """Every row of every workspace-scoped table except the per-request
    bookkeeping ones -- any write the request made shows up here."""
    if connection is None:
        with engine.connect() as own:
            return _workspace_snapshot(ws, own)
    snapshot: Snapshot = {}
    for table in sorted(set(_workspace_tables()) - {"idempotency_records", "sessions"}):
        rows = connection.execute(
            text(f"SELECT * FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
            {"ws": ws},
        ).mappings()
        snapshot[table] = sorted((dict(row) for row in rows), key=str)
    return snapshot


def _race(
    w: RaceWorld, name: str, case: Case, ids: dict[str, UUID], *, transfer: bool
) -> tuple[Any, Snapshot]:
    """Holds the row lock in a separate transaction (optionally transferring
    the row to C there), fires B's mutation, waits until it is blocked on
    that lock, then commits. Returns the response and the workspace
    snapshot taken right after the transfer committed."""
    client = _client(w)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.request(
                case.method,
                case.path.format(**ids),
                headers=_headers(w.b_token),
                json=_body(name, case, ids),
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    try:
        holder = engine.connect()
        holder_tx = holder.begin()
        thread = threading.Thread(target=fire)
        try:
            holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
            holder.execute(
                text(f"SELECT id FROM {case.table} WHERE id = :id FOR UPDATE"),  # noqa: S608
                {"id": ids["id"]},
            )
            if transfer:
                holder.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"id": ids["id"], "c": w.c},
                )
            # Snapshot through the holder (the transfer included) before the
            # request starts, so nothing it writes can be in it and the
            # snapshot does not eat into its statement timeout.
            before = _workspace_snapshot(w.ws, holder)
            thread.start()
            _wait_for_lock_waiter(case.table, holder_pid)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            if thread.ident is not None:  # a setup failure must not be masked
                thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "mutation request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], before
    finally:
        client.close()


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(
    race_world: RaceWorld, name: str
) -> None:
    w, case = race_world, CASES[name]
    ids = _seed(w, case)

    response, before = _race(w, name, case, ids, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert _row_snapshot(case.table, ids["id"])["owner_id"] == w.c
    after = _workspace_snapshot(w.ws)
    # A rejected mutation is itself audited (`*.mutation_rejected`); that
    # row records the refusal and is the one write allowed here.
    after["audit_events"] = [
        row for row in after["audit_events"] if row["authorization_result"] != "rejected"
    ]
    assert after == before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    race_world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds and
    writes -- the 404 above comes from the transfer, not from the wait."""
    w, case = race_world, CASES[name]
    ids = _seed(w, case)

    response, before = _race(w, name, case, ids, transfer=False)

    assert response.status_code == case.ok_status, response.text
    assert _workspace_snapshot(w.ws) != before
