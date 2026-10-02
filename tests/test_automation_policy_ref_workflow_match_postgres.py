"""A workflow version's `policy_ref` must name a policy of the same workflow.

`automation_policies.workflow_id` binds a policy to one workflow family.
Before this fix,
`POST /automations/workflows` only checked that the caller could read the
policy named by `policy_ref`, and dispatch resolved authority from the active
version's `policy_ref` with no family match either. A member could attach a
policy someone else created for workflow X (say `bounded_recurring`, no
per-run approval) to their own workflow Y and run Y under X's authority --
the confused deputy `API-SCHEMAS.md` rules out.

Now a mismatched `policy_ref` is refused at draft time (`422
POLICY_WORKFLOW_MISMATCH`) and at publish time (the same code, for rows
written before the fix), and every server-side policy lookup -- enqueue's
rate limit, the dispatch gate, the compensation re-check, simulate -- treats
a policy of another workflow as no policy at all, so a pre-fix active
version fails closed (`needs_review`, `no_policy`).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from automation_scope_support import (
    FakeAdapter,
    World,
    action_step,
    client_for,
    create_policy,
    headers,
    make_world,
    publish,
    registry_of,
    run_once,
    step_rows,
)
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_WORKFLOWS = "/api/v1/automations/workflows"


@pytest.fixture
def world() -> Iterator[World]:
    yield from make_world()


def _graph() -> dict[str, Any]:
    return {"steps": [action_step("s1", "fake.mismatch")]}


def _draft(world: World, workflow_id: str, policy_ref: UUID | None) -> UUID:
    """Writes a draft directly, bypassing the endpoint's check -- the shape
    of a row written before the fix."""
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            world.workspace_id,
            world.user_id,
            workflow_id=workflow_id,
            graph=_graph(),
            trigger_refs=[],
            policy_ref=policy_ref,
        )
    return draft.id


def _foreign_policy(world: World) -> UUID:
    """A `bounded_recurring` policy of workflow X, which needs no per-run
    approval."""
    _, policy = publish(world, _graph())
    return policy.id


def _version_rows(world: World, workflow_id: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, version, status, policy_ref FROM workflow_versions "
                    "WHERE workspace_id = :ws AND workflow_id = :wf ORDER BY version"
                ),
                {"ws": world.workspace_id, "wf": workflow_id},
            ).mappings()
        ]


def _post_draft(world: World, workflow_id: str, policy_ref: UUID) -> Any:
    with client_for(world) as client:
        return client.post(
            _WORKFLOWS,
            headers=headers(world, str(uuid4())),
            json={
                "workflow_id": workflow_id,
                "graph": _graph(),
                "trigger_refs": [],
                "policy_ref": str(policy_ref),
            },
        )


def _assert_mismatch(response: Any) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "POLICY_WORKFLOW_MISMATCH"


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


def test_draft_refuses_a_policy_of_another_workflow(world: World) -> None:
    foreign = _foreign_policy(world)
    workflow_id = f"test.mismatch.{uuid4().hex}"
    _draft(world, workflow_id, None)

    response = _post_draft(world, workflow_id, foreign)

    _assert_mismatch(response)
    assert [row["version"] for row in _version_rows(world, workflow_id)] == [1]


def test_draft_refuses_a_foreign_policy_for_a_brand_new_family(world: World) -> None:
    """A new family has no policy of its own yet, so any `policy_ref` names
    another workflow's policy."""
    foreign = _foreign_policy(world)
    workflow_id = f"test.mismatch.{uuid4().hex}"

    response = _post_draft(world, workflow_id, foreign)

    _assert_mismatch(response)
    assert _version_rows(world, workflow_id) == []


def test_draft_accepts_the_familys_own_policy(world: World) -> None:
    workflow_id = f"test.match.{uuid4().hex}"
    _draft(world, workflow_id, None)
    own = create_policy(
        world, workflow_id, action_types=["fake.external"], data_classes=["internal"]
    )

    response = _post_draft(world, workflow_id, own.id)

    assert response.status_code == 201, response.text
    assert response.json()["policy_ref"] == str(own.id)


# ---------------------------------------------------------------------------
# Publish (pre-fix drafts)
# ---------------------------------------------------------------------------


def test_publish_refuses_a_pre_fix_draft_naming_another_workflows_policy(
    world: World,
) -> None:
    foreign = _foreign_policy(world)
    workflow_id = f"test.mismatch.{uuid4().hex}"
    draft_id = _draft(world, workflow_id, foreign)

    with client_for(world) as client:
        response = client.post(
            f"{_WORKFLOWS}/{draft_id}/publish", headers=headers(world, str(uuid4()))
        )

    _assert_mismatch(response)
    assert [row["status"] for row in _version_rows(world, workflow_id)] == ["draft"]


# ---------------------------------------------------------------------------
# Enqueue and dispatch (pre-fix active versions)
# ---------------------------------------------------------------------------


def _activate_pre_fix(world: World, workflow_id: str, policy_ref: UUID) -> UUID:
    """A pre-fix active version: publish refuses it now, so flip the status
    directly (`status` is not one of the immutable columns)."""
    version_id = _draft(world, workflow_id, policy_ref)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE workflow_versions SET status = 'active' WHERE id = :id"),
            {"id": version_id},
        )
    return version_id


def test_dispatch_fails_closed_under_another_workflows_policy(world: World) -> None:
    foreign = _foreign_policy(world)
    workflow_id = f"test.mismatch.{uuid4().hex}"
    _activate_pre_fix(world, workflow_id, foreign)
    adapter = FakeAdapter("fake.mismatch")

    run = run_once(world, workflow_id, registry_of(adapter))

    assert run.status == "needs_review"
    assert adapter.execute_calls == 0
    assert step_rows(world, run.id) == []


def test_enqueue_ignores_the_rate_limit_of_another_workflows_policy(world: World) -> None:
    """Enqueue resolves the policy through the same workflow match: a
    foreign policy's ceiling is not this workflow's, and the run it lets
    through is blocked at dispatch above."""
    foreign = _foreign_policy(world)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE automation_policies SET rate_limit = CAST(:rate AS jsonb) WHERE id = :id"),
            {"id": foreign, "rate": '{"runs_per_workflow_per_hour": 0}'},
        )
    workflow_id = f"test.mismatch.{uuid4().hex}"
    _activate_pre_fix(world, workflow_id, foreign)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, world.workspace_id, world.user_id, workflow_id=workflow_id
        )

    assert isinstance(queued, automation_worker.WorkflowRun), queued


def test_simulate_reports_no_policy_for_another_workflows_policy(world: World) -> None:
    foreign = _foreign_policy(world)
    workflow_id = f"test.mismatch.{uuid4().hex}"
    version_id = _activate_pre_fix(world, workflow_id, foreign)

    with client_for(world) as client:
        response = client.post(
            f"{_WORKFLOWS}/{version_id}/simulate", headers=headers(world, str(uuid4()))
        )

    assert response.status_code == 200, response.text
    [step] = response.json()["steps"]
    assert step["dispatch_gate"] == "policy_blocked"
    assert step["policy_block_reason"] == "no_policy"
