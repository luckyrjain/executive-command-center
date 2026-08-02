"""Phase 5 Automation Task 1: workflow schema (design doc Decision 2).

Covers, per the task's own scope ("workflow schema, policy model and
triggers -- the data layer only"):

1. The database-level immutability trigger (`trg_workflow_versions_
   immutability`, migration `0038_phase5_workflow_schema.py`) -- exercised
   with **direct SQL** `UPDATE` statements, bypassing `workflows.py`
   entirely, mirroring `test_ai_runtime_versioning_postgres.py`'s own
   structure for the identical Phase 4 property.
2. The partial unique index enforcing exactly one `active` version per
   `(workspace_id, workflow_id)`.
3. `workflows.py`'s hashing/graph-validation/CRUD/activation functions.
4. `GET|POST /api/v1/automations/workflows`, `GET .../{id}`,
   `POST .../{id}/publish|disable`.
5. Cross-workspace isolation.

Also covers the publish-time half of the compensation approval-gate-bypass
fix (`workflows.high_impact_compensation_action_refs` /
`WorkflowVersionHighImpactCompensationAdapter`): a `step_type='compensation'`
step whose own `action_ref` names an adapter declaring a non-empty
`high_impact_categories` is rejected at publish with
`422 COMPENSATION_ACTION_REF_HIGH_IMPACT`. Those tests live here, alongside
the other graph-validation and publish-endpoint coverage, rather than in
`test_automation_compensation_postgres.py` -- they are publish-path
properties and never dispatch a run. The dispatch-time half of that same fix
(`worker._compensation_policy_usable`) is covered in that other module.

Plus two gaps closed by the test-coverage audit, both about `POST
/workflows/{id}/disable` -- the route that had only same-workspace 409
coverage while its sibling `/publish` had both:

6. **Cross-workspace disable** is a 404, so workspace B cannot retire
   workspace A's active version (a denial of service against a running
   automation, strictly more consequential than the cross-workspace publish
   already covered).
7. **`Idempotency-Key` replay on disable, and `IDEMPOTENCY_CONFLICT` when one
   key is reused across `/publish` and `/disable`** -- this module's own
   hand-copied `_request_hash`/`_load_cached` pair had a replay test for
   `POST /workflows` only and no conflict test of any kind.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import registry as production_adapter_registry
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _valid_graph() -> dict[str, Any]:
    return {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "local.create_note",
                "input_mapping": {"title": "$trigger.title"},
                "on_success": "s2",
                "on_failure": "failed",
            },
            {
                "step_id": "s2",
                "step_type": "approval_gate",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            },
        ]
    }


# ---------------------------------------------------------------------------
# Shared workspace/user/session fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Workflow Test', 'Asia/Kolkata', :now)"
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
            "triggers",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
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


def _insert_workflow_family(workspace_id: UUID, user_id: UUID, workflow_id: str) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
                "created_at, updated_at) VALUES (:id, :workspace_id, :workflow_id, :created_by, "
                ":now, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "workflow_id": workflow_id,
                "created_by": user_id,
                "now": now,
            },
        )


def _insert_version_row(
    workspace_id: UUID,
    user_id: UUID,
    workflow_id: str,
    *,
    version: int,
    status: str,
    graph: dict[str, Any] | None = None,
) -> UUID:
    graph = graph if graph is not None else _valid_graph()
    row_id = uuid4()
    now = datetime.now(UTC)
    definition_hash = automation_workflows.compute_definition_hash(
        graph=graph, trigger_refs=[], policy_ref=None
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workflow_versions (
                    id, workspace_id, workflow_id, version, graph, trigger_refs, policy_ref,
                    definition_hash, status, created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :workflow_id, :version, CAST(:graph AS jsonb),
                    '[]'::jsonb, NULL, :definition_hash, :status, :created_by, :created_by,
                    :now, :now
                )
                """
            ),
            {
                "id": row_id,
                "workspace_id": workspace_id,
                "workflow_id": workflow_id,
                "version": version,
                "graph": dumps(graph),
                "definition_hash": definition_hash,
                "status": status,
                "created_by": user_id,
                "now": now,
            },
        )
    return row_id


# ---------------------------------------------------------------------------
# 1. Immutability trigger -- direct SQL only.
# ---------------------------------------------------------------------------


def test_draft_row_graph_is_freely_editable(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.draft-editable.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="draft")

    new_graph = {
        "steps": [{"step_id": "only", "step_type": "condition", "on_success": "succeeded"}]
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_versions SET graph = CAST(:graph AS jsonb) "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id"
            ),
            {"graph": dumps(new_graph), "workspace_id": workspace_id, "workflow_id": workflow_id},
        )
        row = (
            connection.execute(
                text(
                    "SELECT graph FROM workflow_versions WHERE workspace_id = :workspace_id "
                    "AND workflow_id = :workflow_id"
                ),
                {"workspace_id": workspace_id, "workflow_id": workflow_id},
            )
            .mappings()
            .one()
        )
    assert row["graph"]["steps"][0]["step_id"] == "only"


def test_active_row_graph_update_rejected_by_trigger(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.active-immutable.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="active")

    with pytest.raises(ProgrammingError) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workflow_versions SET graph = '{\"steps\":[]}'::jsonb "
                    "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id"
                ),
                {"workspace_id": workspace_id, "workflow_id": workflow_id},
            )
    assert "immutable" in str(excinfo.value).lower()


@pytest.mark.parametrize(
    "column,sql_value",
    [
        ("trigger_refs", "'[\"manual\"]'::jsonb"),
        ("policy_ref", "gen_random_uuid()"),
        ("definition_hash", "'" + "0" * 64 + "'"),
    ],
)
def test_retired_row_every_hashed_column_rejected(
    workflow_test_context: tuple[TestClient, UUID, UUID, str], column: str, sql_value: str
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.retired-immutable.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="retired")

    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text(  # noqa: S608
                    f"UPDATE workflow_versions SET {column} = {sql_value} "
                    "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id"
                ),
                {"workspace_id": workspace_id, "workflow_id": workflow_id},
            )


def test_status_and_updated_at_remain_editable_post_activation(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.status-editable.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="active")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_versions SET status = 'retired', updated_at = now() "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        )
        status_value = connection.execute(
            text(
                "SELECT status FROM workflow_versions WHERE workspace_id = :workspace_id "
                "AND workflow_id = :workflow_id"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        ).scalar_one()
    assert status_value == "retired"


def test_partial_unique_index_rejects_second_active_row(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.dup-active.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="active")

    with pytest.raises(IntegrityError):
        _insert_version_row(workspace_id, user_id, workflow_id, version=2, status="active")


# ---------------------------------------------------------------------------
# 3. workflows.py functions.
# ---------------------------------------------------------------------------


def test_compute_definition_hash_is_deterministic_and_sensitive_to_every_input() -> None:
    graph = _valid_graph()
    h1 = automation_workflows.compute_definition_hash(
        graph=graph, trigger_refs=["manual"], policy_ref=None
    )
    h2 = automation_workflows.compute_definition_hash(
        graph=graph, trigger_refs=["manual"], policy_ref=None
    )
    assert h1 == h2
    assert len(h1) == 64

    other_graph = automation_workflows.compute_definition_hash(
        graph={"steps": []}, trigger_refs=["manual"], policy_ref=None
    )
    assert other_graph != h1

    other_triggers = automation_workflows.compute_definition_hash(
        graph=graph, trigger_refs=["event"], policy_ref=None
    )
    assert other_triggers != h1

    ref = uuid4()
    other_policy = automation_workflows.compute_definition_hash(
        graph=graph, trigger_refs=["manual"], policy_ref=ref
    )
    assert other_policy != h1


def test_validate_graph_shape_accepts_a_well_formed_graph() -> None:
    assert automation_workflows.validate_graph_shape(_valid_graph()) == []


def test_validate_graph_shape_rejects_duplicate_step_ids() -> None:
    graph = {
        "steps": [
            {"step_id": "s1", "step_type": "condition", "on_success": "succeeded"},
            {"step_id": "s1", "step_type": "condition", "on_success": "succeeded"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("duplicate" in v for v in violations)


def test_validate_graph_shape_rejects_unknown_edge_target() -> None:
    graph = {"steps": [{"step_id": "s1", "step_type": "condition", "on_success": "nonexistent"}]}
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("unknown step" in v for v in violations)


def test_validate_graph_shape_rejects_backward_reference() -> None:
    graph = {
        "steps": [
            {"step_id": "s1", "step_type": "condition", "on_success": "s2"},
            {"step_id": "s2", "step_type": "condition", "on_success": "s1"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("not strictly later" in v for v in violations)


def test_validate_graph_shape_requires_action_ref_for_action_steps() -> None:
    graph = {"steps": [{"step_id": "s1", "step_type": "action", "on_success": "succeeded"}]}
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("action_ref required" in v for v in violations)


# ---------------------------------------------------------------------------
# Task 6: compensate_ref structural validation (Decision 9).
# ---------------------------------------------------------------------------


def _graph_with_compensation() -> dict[str, Any]:
    """A well-formed graph: `s1` (action) declares `compensate_ref="c1"`;
    `c1` (compensation) is never a target of any `on_success`/`on_failure`
    edge, only referenced via `compensate_ref`.
    """
    return {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "compensate_ref": "c1",
                "on_success": "s2",
                "on_failure": "failed",
            },
            {
                "step_id": "s2",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "on_success": "succeeded",
                "on_failure": "failed",
            },
            {
                "step_id": "c1",
                "step_type": "compensation",
                "action_ref": "fake.external_action",
            },
        ]
    }


def test_validate_graph_shape_accepts_well_formed_compensate_ref() -> None:
    assert automation_workflows.validate_graph_shape(_graph_with_compensation()) == []


def test_validate_graph_shape_rejects_compensate_ref_on_non_action_step() -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "condition",
                "compensate_ref": "c1",
                "on_success": "c1",
            },
            {"step_id": "c1", "step_type": "compensation", "action_ref": "fake.external_action"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("compensate_ref is only valid on step_type='action'" in v for v in violations)


def test_validate_graph_shape_rejects_compensate_ref_to_unknown_step() -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "compensate_ref": "nonexistent",
                "on_success": "succeeded",
            }
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("compensate_ref references unknown step" in v for v in violations)


def test_validate_graph_shape_rejects_compensate_ref_to_non_compensation_step() -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "compensate_ref": "s2",
                "on_success": "s2",
            },
            {
                "step_id": "s2",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "on_success": "succeeded",
            },
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("must name a step_type='compensation' step" in v for v in violations)


def test_validate_graph_shape_rejects_compensation_step_as_on_success_target() -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "on_success": "c1",
            },
            {"step_id": "c1", "step_type": "compensation", "action_ref": "fake.external_action"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("never as a target of on_success/on_failure" in v for v in violations)


def test_validate_graph_shape_rejects_compensation_step_as_on_failure_target() -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "on_success": "succeeded",
                "on_failure": "c1",
            },
            {"step_id": "c1", "step_type": "compensation", "action_ref": "fake.external_action"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("never as a target of on_success/on_failure" in v for v in violations)


def test_validate_graph_shape_rejects_nested_compensate_ref() -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "compensate_ref": "c1",
                "on_success": "succeeded",
            },
            {
                "step_id": "c1",
                "step_type": "compensation",
                "action_ref": "fake.external_action",
                "compensate_ref": "c2",
            },
            {"step_id": "c2", "step_type": "compensation", "action_ref": "fake.external_action"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any("must not itself declare a compensate_ref" in v for v in violations)


def test_validate_graph_shape_rejects_two_action_steps_sharing_one_compensate_ref() -> None:
    """Found during Task 6's own review: `worker._dispatch_compensation_step`
    idempotency-gates on `workflow_run_steps`' `(run_id, step_index)` key --
    the compensation step's own graph position -- so if two action steps
    both named the same `compensate_ref`, the second step's dispatch would
    silently find the first's row already `succeeded` and return early,
    never calling `compensate()` or writing a `compensation_steps` ledger
    row for the second original step. Rejected here at graph-validation
    time instead of loosening that dispatch idempotency key.
    """
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "compensate_ref": "c1",
                "on_success": "s2",
            },
            {
                "step_id": "s2",
                "step_type": "action",
                "action_ref": "fake.external_action",
                "compensate_ref": "c1",
                "on_success": "succeeded",
            },
            {"step_id": "c1", "step_type": "compensation", "action_ref": "fake.external_action"},
        ]
    }
    violations = automation_workflows.validate_graph_shape(graph)
    assert any(
        "already claimed by step 's1'" in v and "compensate_ref 'c1'" in v for v in violations
    )


# ---------------------------------------------------------------------------
# Compensation steps may never name a high-impact adapter (publish-time,
# registry-dependent -- `workflows.high_impact_compensation_action_refs`).
#
# The bug this closes: `worker._dispatch_compensation_step` is the only code
# path that dispatches a `step_type='compensation'` step, and it deliberately
# never runs `worker._evaluate_dispatch_gate`, so it never creates the
# `approval_requests` row that `APPROVAL-POLICY.md` requires for *any*
# adapter declaring a non-empty `high_impact_categories` ("always requires
# per-run approval regardless of policy mode"). Nothing previously stopped a
# compensation step's own `action_ref` from naming such an adapter, so a
# low-impact action step with a `compensate_ref` pointing at one would run
# that adapter's real `execute()`, unapproved, the moment any later step in
# the run failed. Rejecting the graph at publish time makes that unreachable
# by construction, for every adapter that will ever be registered.
#
# These use the shared *production* registry (`adapters.registry`) rather
# than a private fake: the point is the real declared categories of the real
# adapters (`local.send_test_notification` -> {'person-directed'},
# `fake.external_action` -> {'public'}, `local.create_note` -> frozenset()),
# which is exactly what `publish_workflow_endpoint` checks against.
# ---------------------------------------------------------------------------


def _graph_with_high_impact_compensation() -> dict[str, Any]:
    """`s1` is a low-impact action (`local.create_note`, no declared
    categories) whose `compensate_ref` points at `c1`, a compensation step
    naming `local.send_test_notification` -- an adapter declaring
    `high_impact_categories={'person-directed'}`. Structurally valid by
    every `validate_graph_shape` rule; unpublishable only because of what
    `c1`'s `action_ref` resolves to.
    """
    return {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "local.create_note",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
                "compensate_ref": "c1",
            },
            {
                "step_id": "c1",
                "step_type": "compensation",
                "action_ref": "local.send_test_notification",
                "input_mapping": {},
            },
        ]
    }


def test_high_impact_compensation_graph_is_shape_valid_so_only_the_new_check_rejects_it() -> None:
    """Establishes that the pure, registry-free shape validation genuinely
    does *not* catch this -- i.e. the new publish-time check is the only
    thing standing between this graph and an unapproved high-impact
    execution, which is precisely why it has to exist.
    """
    assert automation_workflows.validate_graph_shape(_graph_with_high_impact_compensation()) == []


def test_high_impact_compensation_action_refs_flags_the_compensation_step() -> None:
    violations = automation_workflows.high_impact_compensation_action_refs(
        _graph_with_high_impact_compensation(), production_adapter_registry
    )
    assert len(violations) == 1
    assert "step 'c1'" in violations[0]
    assert "local.send_test_notification" in violations[0]
    assert "person-directed" in violations[0]


def test_high_impact_compensation_action_refs_ignores_high_impact_action_steps() -> None:
    """Only `step_type='compensation'` steps are constrained. An ordinary
    action step naming a high-impact adapter is entirely legal -- it goes
    through `worker._evaluate_dispatch_gate`, which creates the per-run
    approval request. Narrowing this check to compensation steps is the
    whole point; widening it would break every legitimate high-impact
    workflow.
    """
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "local.send_test_notification",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
                "compensate_ref": "c1",
            },
            {
                "step_id": "c1",
                "step_type": "compensation",
                "action_ref": "local.create_note",
                "input_mapping": {},
            },
        ]
    }
    assert (
        automation_workflows.high_impact_compensation_action_refs(
            graph, production_adapter_registry
        )
        == []
    )


def test_high_impact_compensation_action_refs_leaves_unresolvable_refs_to_the_other_check() -> None:
    """An `action_ref` that resolves to nothing is `unregistered_action_
    refs`' concern (its own distinct 422 code), not this one's -- so this
    function must stay silent about it, or the 422 a caller sees would
    depend on check ordering rather than on what is actually wrong.
    """
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "local.create_note",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
                "compensate_ref": "c1",
            },
            {
                "step_id": "c1",
                "step_type": "compensation",
                "action_ref": "test.definitely-not-a-real-adapter",
                "input_mapping": {},
            },
        ]
    }
    assert (
        automation_workflows.high_impact_compensation_action_refs(
            graph, production_adapter_registry
        )
        == []
    )
    assert automation_workflows.unregistered_action_refs(graph, production_adapter_registry) != []


def test_activate_workflow_version_rejects_high_impact_compensation_adapter(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.high-impact-comp.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    draft_id = _insert_version_row(
        workspace_id,
        user_id,
        workflow_id,
        version=1,
        status="draft",
        graph=_graph_with_high_impact_compensation(),
    )

    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.activate_workflow_version(
                session,
                workspace_id,
                draft_id,
                adapter_registry=production_adapter_registry,
            )
        assert isinstance(result, automation_workflows.WorkflowVersionHighImpactCompensationAdapter)
        assert result.workflow_id == workflow_id
        assert any("local.send_test_notification" in v for v in result.violations)

    # The version genuinely never activated.
    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM workflow_versions WHERE id = :id"), {"id": draft_id}
        ).scalar_one()
    assert status == "draft"


def test_publish_high_impact_compensation_step_is_rejected_422(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The end-to-end guarantee: the real HTTP publish path (which always
    passes the shared production registry) refuses to activate a graph whose
    compensation step names a high-impact adapter.
    """
    client, _workspace_id, _user_id, token = workflow_test_context
    created = client.post(
        "/api/v1/automations/workflows",
        json={
            "workflow_id": f"test.publish-high-impact-comp.{uuid4().hex}",
            "graph": _graph_with_high_impact_compensation(),
            "trigger_refs": [],
        },
        headers=_headers(token, key="high-impact-comp-create"),
    ).json()
    assert "id" in created, created

    publish = client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="high-impact-comp-publish"),
    )
    assert publish.status_code == 422, publish.text
    error = publish.json()["error"]
    assert error["code"] == "COMPENSATION_ACTION_REF_HIGH_IMPACT"
    # A readable message, never a title-cased code and never raw JSON.
    assert "per-run approval" in error["message"]
    violations = error["details"]["violations"]
    assert any("local.send_test_notification" in v for v in violations)

    # Still a draft -- nothing was activated.
    assert client.get(f"/api/v1/automations/workflows/{created['id']}").json()["status"] == "draft"


def test_create_workflow_draft_creates_family_and_first_version(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.create-draft.{uuid4().hex}"

    with SessionFactory() as session:
        with session.begin():
            created = automation_workflows.create_workflow_draft(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                graph=_valid_graph(),
                trigger_refs=["manual"],
                policy_ref=None,
            )
        assert created.version == 1
        assert created.status == "draft"
        assert created.workflow_id == workflow_id


def test_create_workflow_draft_second_call_increments_version(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.increment.{uuid4().hex}"

    with SessionFactory() as session:
        with session.begin():
            automation_workflows.create_workflow_draft(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                graph=_valid_graph(),
                trigger_refs=[],
                policy_ref=None,
            )
    with SessionFactory() as session:
        with session.begin():
            second = automation_workflows.create_workflow_draft(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                graph=_valid_graph(),
                trigger_refs=[],
                policy_ref=None,
            )
        assert second.version == 2


def test_activate_workflow_version_retires_outgoing_and_activates_incoming(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.activate.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    v1_id = _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="active")
    v2_id = _insert_version_row(workspace_id, user_id, workflow_id, version=2, status="draft")

    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.activate_workflow_version(session, workspace_id, v2_id)
        assert isinstance(result, automation_workflows.WorkflowVersion)
        assert result.status == "active"

    with engine.connect() as connection:
        v1_status = connection.execute(
            text("SELECT status FROM workflow_versions WHERE id = :id"), {"id": v1_id}
        ).scalar_one()
        v2_status = connection.execute(
            text("SELECT status FROM workflow_versions WHERE id = :id"), {"id": v2_id}
        ).scalar_one()
    assert v1_status == "retired"
    assert v2_status == "active"


def test_activate_workflow_version_reactivating_already_active_is_a_noop(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.reactivate-noop.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    v1_id = _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="active")

    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.activate_workflow_version(session, workspace_id, v1_id)
        assert isinstance(result, automation_workflows.WorkflowVersion)
        assert result.status == "active"


def test_activate_workflow_version_retired_target_returns_not_draft(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.retired-target.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    v1_id = _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="retired")

    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.activate_workflow_version(session, workspace_id, v1_id)
        assert isinstance(result, automation_workflows.WorkflowVersionNotDraft)


def test_activate_workflow_version_unknown_id_returns_not_found(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, _user_id, _token = workflow_test_context
    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.activate_workflow_version(session, workspace_id, uuid4())
        assert isinstance(result, automation_workflows.WorkflowVersionNotFound)


def test_disable_workflow_version_retires_active(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.disable.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    v1_id = _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="active")

    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.disable_workflow_version(session, workspace_id, v1_id)
        assert isinstance(result, automation_workflows.WorkflowVersion)
        assert result.status == "retired"


def test_disable_workflow_version_non_active_returns_not_active(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = workflow_test_context
    workflow_id = f"test.disable-not-active.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    v1_id = _insert_version_row(workspace_id, user_id, workflow_id, version=1, status="draft")

    with SessionFactory() as session:
        with session.begin():
            result = automation_workflows.disable_workflow_version(session, workspace_id, v1_id)
        assert isinstance(result, automation_workflows.WorkflowVersionNotActive)


# ---------------------------------------------------------------------------
# 4. HTTP endpoints.
# ---------------------------------------------------------------------------


def test_create_workflow_via_endpoint(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-create.{uuid4().hex}"

    response = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": ["manual"]},
        headers=_headers(token, key="create-1"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workflow_id"] == workflow_id
    assert body["version"] == 1
    assert body["status"] == "draft"
    assert len(body["definition_hash"]) == 64


def test_create_workflow_second_call_appends_version_two(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-append.{uuid4().hex}"
    payload = {"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []}

    first = client.post(
        "/api/v1/automations/workflows", json=payload, headers=_headers(token, key="append-1")
    )
    second = client.post(
        "/api/v1/automations/workflows", json=payload, headers=_headers(token, key="append-2")
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["version"] == 1
    assert second.json()["version"] == 2


def test_create_workflow_malformed_graph_is_schema_invalid(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    graph = {
        "steps": [
            {"step_id": "s1", "step_type": "condition", "on_success": "s1"},
        ]
    }
    response = client.post(
        "/api/v1/automations/workflows",
        json={
            "workflow_id": f"test.http-invalid.{uuid4().hex}",
            "graph": graph,
            "trigger_refs": [],
        },
        headers=_headers(token, key="invalid-1"),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SCHEMA_INVALID"


def test_get_workflow_via_endpoint(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-get.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="get-create"),
    ).json()

    response = client.get(f"/api/v1/automations/workflows/{created['id']}")
    assert response.status_code == 200
    assert response.json()["workflow_id"] == workflow_id


def test_get_unknown_workflow_is_404(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = workflow_test_context
    response = client.get(f"/api/v1/automations/workflows/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_list_workflows_via_endpoint(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-list.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="list-create"),
    )
    response = client.get("/api/v1/automations/workflows")
    assert response.status_code == 200
    summaries = {w["workflow_id"]: w for w in response.json()["workflows"]}
    assert workflow_id in summaries
    assert summaries[workflow_id]["latest_version"] == 1
    assert summaries[workflow_id]["active_version"] is None
    # Task 7: the list summary must carry the latest version's own row id
    # (never a bare version number) so a caller can navigate straight into
    # GET /workflows/{version_id}, which is addressed by that UUID, not by
    # (workflow_id, version) -- a real gap found and closed while building
    # the frontend workflow list against this endpoint.
    assert summaries[workflow_id]["latest_version_id"] == created.json()["id"]
    assert summaries[workflow_id]["active_version_id"] is None

    publish = client.post(
        f"/api/v1/automations/workflows/{created.json()['id']}/publish",
        headers=_headers(token, key="list-publish"),
    )
    assert publish.status_code == 200
    republished = client.get("/api/v1/automations/workflows")
    republished_by_id = {w["workflow_id"]: w for w in republished.json()["workflows"]}
    assert republished_by_id[workflow_id]["active_version_id"] == created.json()["id"]


def test_publish_workflow_via_endpoint(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-publish.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="publish-create"),
    ).json()

    response = client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="publish-1"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"


def test_publish_unknown_workflow_is_404(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    response = client.post(
        f"/api/v1/automations/workflows/{uuid4()}/publish",
        headers=_headers(token, key="publish-404"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_publish_retired_version_is_conflict(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = workflow_test_context
    workflow_id = f"test.http-publish-retired.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    retired_id = _insert_version_row(
        workspace_id, user_id, workflow_id, version=1, status="retired"
    )

    response = client.post(
        f"/api/v1/automations/workflows/{retired_id}/publish",
        headers=_headers(token, key="publish-retired"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKFLOW_VERSION_NOT_DRAFT"


def test_disable_active_workflow_via_endpoint(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-disable.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="disable-create"),
    ).json()
    client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="disable-publish"),
    )

    response = client.post(
        f"/api/v1/automations/workflows/{created['id']}/disable",
        headers=_headers(token, key="disable-1"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "retired"


def test_disable_draft_workflow_is_workflow_not_active(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-disable-draft.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="disable-draft-create"),
    ).json()

    response = client.post(
        f"/api/v1/automations/workflows/{created['id']}/disable",
        headers=_headers(token, key="disable-draft-1"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_ACTIVE"


def test_create_workflow_requires_csrf(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = workflow_test_context
    response = client.post(
        "/api/v1/automations/workflows",
        json={
            "workflow_id": f"test.no-csrf.{uuid4().hex}",
            "graph": _valid_graph(),
            "trigger_refs": [],
        },
        headers={"Idempotency-Key": "no-csrf"},
    )
    assert response.status_code == 403


def test_create_workflow_requires_authentication(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    client.cookies.clear()
    response = client.post(
        "/api/v1/automations/workflows",
        json={
            "workflow_id": f"test.no-auth.{uuid4().hex}",
            "graph": _valid_graph(),
            "trigger_refs": [],
        },
        headers=_headers(token, key="no-auth"),
    )
    assert response.status_code == 401


def test_create_workflow_idempotency_replay_returns_identical_response(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-idempotent.{uuid4().hex}"
    payload = {"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []}
    headers = _headers(token, key="replay-key")

    first = client.post("/api/v1/automations/workflows", json=payload, headers=headers)
    second = client.post("/api/v1/automations/workflows", json=payload, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body


def test_workflow_is_hidden_across_workspaces(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.cross-workspace.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="cross-create"),
    ).json()

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=other_workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
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
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        get_response = other_client.get(f"/api/v1/automations/workflows/{created['id']}")
        assert get_response.status_code == 404
        assert get_response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"

        list_response = other_client.get("/api/v1/automations/workflows")
        assert list_response.status_code == 200
        assert all(w["workflow_id"] != workflow_id for w in list_response.json()["workflows"])

        publish_response = other_client.post(
            f"/api/v1/automations/workflows/{created['id']}/publish",
            headers=_headers(other_token, key="cross-publish"),
        )
        assert publish_response.status_code == 404
    finally:
        other_client.close()
        _cleanup_workspace(other_workspace_id)


def test_disable_is_rejected_across_workspaces(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The cross-workspace half of `POST /workflows/{id}/disable`, which had
    only same-workspace coverage (`test_disable_draft_workflow_is_workflow_
    not_active`'s 409) before this test.

    `test_workflow_is_hidden_across_workspaces` above already proves this
    property for `/publish`; leaving `/disable` uncovered meant the two
    sibling routes -- written together, sharing `_load_cached`/`_write_side_
    effects`/`session.begin()` scaffolding, and differing only in which
    domain function they call -- had asymmetric protection. A regression that
    dropped `auth.workspace_id` from `disable_workflow_version`'s own
    `WHERE` clause (or reordered its not-found check after the mutation)
    would be caught for publish and pass silently for disable.

    Disable is the more consequential of the pair to leave open. Publishing
    someone else's draft activates a version they wrote and would have
    activated anyway; disabling their *active* version is an unauthenticated
    denial of service against a running automation -- every subsequent
    `enqueue_run` for that workflow fails `WORKFLOW_NOT_ACTIVE`, with the
    only trace being an `audit_events` row attributed to a user in a
    different workspace.

    404, not 403: `WORKFLOW_NOT_FOUND` is the same response an id that never
    existed gets, so the route never confirms to workspace B that this
    version id is real -- the identical shape `/publish`'s own isolation
    assertion pins.
    """
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.cross-workspace-disable.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="cross-disable-create"),
    ).json()
    published = client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="cross-disable-publish"),
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "active"

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Disable Isolation Peer', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=other_workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
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
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        disable_response = other_client.post(
            f"/api/v1/automations/workflows/{created['id']}/disable",
            headers=_headers(other_token, key="cross-disable-1"),
        )
        assert disable_response.status_code == 404
        assert disable_response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"
    finally:
        other_client.close()
        _cleanup_workspace(other_workspace_id)

    # The version really is still active in its own workspace -- the refused
    # call had no partial effect, and the owning workspace can still disable
    # it itself (so the 404 above was about authorization, not about the row
    # having become undisableable).
    with SessionFactory() as session:
        still_active = automation_workflows.get_workflow_version_by_id(
            session, _workspace_id, UUID(created["id"])
        )
    assert still_active is not None
    assert still_active.status == "active"

    own_disable = client.post(
        f"/api/v1/automations/workflows/{created['id']}/disable",
        headers=_headers(token, key="cross-disable-own"),
    )
    assert own_disable.status_code == 200, own_disable.text
    assert own_disable.json()["status"] == "retired"


# ---------------------------------------------------------------------------
# Idempotency-Key handling on POST /workflows/{id}/disable.
#
# Added by the test-coverage audit: this module had a replay test for
# `POST /workflows` only (`test_create_workflow_idempotency_replay_returns_
# identical_response`) and no conflict test of any kind. Disable is the
# mutating route worth covering next -- creating a draft twice is harmless,
# while disable retires the version every `enqueue_run` resolves against.
# ---------------------------------------------------------------------------


def test_disable_workflow_idempotency_key_replays_cached_response(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A retried disable under the same key replays the first 200 rather than
    surfacing the `409 WORKFLOW_NOT_ACTIVE` that a genuine second disable
    attempt correctly produces (`test_disable_draft_workflow_is_workflow_not_
    active` covers that other case).

    Same reasoning as the policy-revoke replay test: disable is a
    stop-the-automation action, so the retry-after-a-dropped-response path is
    exactly the one an operator exercises under pressure, and a 409 there is
    indistinguishable from "someone else disabled this" -- ambiguity about
    whose intent took effect, at the worst possible moment.

    The `retired` status and `version` are compared field-by-field rather
    than just checking two 200s, because the replay must return the *same*
    row's state: `_load_cached` deserializing into `WorkflowVersionResponse`
    is a hand-copied helper in this module, and a mismatch between what was
    cached and what is replayed is the failure mode a status-code-only
    assertion misses entirely.
    """
    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-disable-idempotent.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="disable-idempotent-create"),
    ).json()
    client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="disable-idempotent-publish"),
    )

    headers = _headers(token, key="off-retry-key")
    first = client.post(f"/api/v1/automations/workflows/{created['id']}/disable", headers=headers)
    second = client.post(f"/api/v1/automations/workflows/{created['id']}/disable", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body
    assert first_body["status"] == "retired"


def test_publish_then_disable_under_one_key_is_idempotency_conflict(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Both `/publish` and `/disable` take an empty body, so the *action*
    string (`f"publish:{version_id}"` vs `f"disable:{version_id}"`) is the
    only thing separating their request hashes. Reusing one key across the
    two must 409 `IDEMPOTENCY_CONFLICT`.

    The regression this guards is the mirror image of the policy-revoke
    conflict test's: if the action prefix were ever dropped from
    `_request_hash`'s material -- an easy "these two endpoints hash the same
    empty body, why compute it twice" edit -- then a disable issued under a
    key already used to publish the same version would return the cached
    *publish* response. The caller would be told the version is `active`
    while the disable it asked for never happened, which is the strictly
    worse direction of the two: an operator believes an automation was left
    running when they actually failed to stop it, or vice versa, with no
    error anywhere to prompt a re-check.

    The persisted state is asserted, not just the 409, so this test fails on a
    silently-applied second write as well as on a silent replay.
    """
    from ecc.observability import render_metrics

    client, _workspace_id, _user_id, token = workflow_test_context
    workflow_id = f"test.http-publish-disable-key.{uuid4().hex}"
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": _valid_graph(), "trigger_refs": []},
        headers=_headers(token, key="publish-disable-create"),
    ).json()

    headers = _headers(token, key="publish-disable-shared-key")
    published = client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish", headers=headers
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "active"

    conflicting = client.post(
        f"/api/v1/automations/workflows/{created['id']}/disable", headers=headers
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert 'ecc_idempotency_conflicts_total{domain="automation_workflows"}' in render_metrics()

    # Neither a replay nor a write: the version is still active.
    with SessionFactory() as session:
        persisted = automation_workflows.get_workflow_version_by_id(
            session, _workspace_id, UUID(created["id"])
        )
    assert persisted is not None
    assert persisted.status == "active"


def test_create_workflow_rejects_nonexistent_policy_ref(
    workflow_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = workflow_test_context
    response = client.post(
        "/api/v1/automations/workflows",
        json={
            "workflow_id": f"test.bad-policy-ref.{uuid4().hex}",
            "graph": _valid_graph(),
            "trigger_refs": [],
            "policy_ref": str(uuid4()),
        },
        headers=_headers(token, key="bad-policy-ref"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "POLICY_NOT_FOUND"
