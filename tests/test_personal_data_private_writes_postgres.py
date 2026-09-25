"""Private write sites for Gmail-derived content (Security Remediation Spec
A S1.8(a) content part, T14b), behind `ECC_PERSONAL_DATA_ISOLATION`.

Flag on, at write time: `email_action_detected` recommendations, `email.*`
`ai_runs` (and their `ai_run_steps`), and `gmail_sync` `pkos_evidence` are
written `private` and owned by the mailbox owner; the Gmail sync's person
`entity_aliases` are owned by the mailbox owner but stay `workspace`
knowledge (DS3 (a')). Existing authz then answers every other member --
even a workspace `owner` -- exactly as for a row that does not exist.

Plan note N11: with the flag on the sync no longer leaves `entity_aliases`/
`ai_run_steps`/`pkos_evidence` owned by the workspace's original user (who
then could never be removed), and the mailbox owner stays removable (their
Gmail-derived aliases follow their re-owned person nodes; the steps of an
email run are personal rows that do not block removal).

Flag off: exactly the previous values. Every Gmail-derived row comes from
the real sync (`gmail_sync_fixtures`); the worlds here add a `bystander`
owner who joined first and never connects Gmail, so "the workspace's
original user" is someone other than the two mailbox owners.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import (
    GmailSyncWorld,
    MemberGmailArtifacts,
    build_gmail_sync_world,
    csrf_headers,
)
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime import runtime as runtime_module

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}
_FLAG_OFF = {"ECC_PERSONAL_DATA_ISOLATION": "false"}
# Written by these tests outside the fixture's own cleanup set (children
# first): removal/transfer bookkeeping and grants.
_EXTRA_CLEANUP_TABLES = ("member_notifications", "resource_grants", "ownership_transfers")


# --- worlds ---------------------------------------------------------------------


@pytest.fixture
def world_on() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env=_FLAG_ON, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES, bystander=True
    ) as world:
        yield world


@pytest.fixture
def world_off() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env=_FLAG_OFF, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES, bystander=True
    ) as world:
        yield world


# --- helpers ----------------------------------------------------------------------


def _bystander(world: GmailSyncWorld) -> UUID:
    assert world.bystander_user_id is not None
    return world.bystander_user_id


def _client(world: GmailSyncWorld, user_id: UUID) -> tuple[TestClient, str]:
    return world.harness.client_for(world.workspace_id, user_id)


def _rows(table: str, ids: Sequence[UUID]) -> dict[UUID, tuple[UUID, str]]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT id, owner_id, visibility FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
            {"ids": list(ids)},
        ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def _step_ids(run_ids: Sequence[UUID]) -> tuple[UUID, ...]:
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT id FROM ai_run_steps WHERE run_id = ANY(:ids) ORDER BY id"),
            {"ids": list(run_ids)},
        ).all()
    return tuple(row[0] for row in rows)


def _content_ids(member: MemberGmailArtifacts) -> dict[str, tuple[UUID, ...]]:
    return {
        "recommendations": member.recommendation_ids,
        "ai_runs": member.ai_run_ids,
        "ai_run_steps": _step_ids(member.ai_run_ids),
        "pkos_evidence": member.evidence_ids,
    }


def _created_aliases(world: GmailSyncWorld, key: str) -> list[UUID]:
    """Aliases the member's own sync created: every participant alias of
    their mailbox except the shared correspondent's (A synced it first)."""
    member = world.members[key]
    return [
        alias_id
        for email, alias_id in member.entity_alias_ids.items()
        if key == "a" or email != world.shared_correspondent_email
    ]


def _owned_count(ws: UUID, table: str, users_id: UUID) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    f"SELECT count(*) FROM {table} "  # noqa: S608
                    "WHERE workspace_id = :ws AND owner_id = :u"
                ),
                {"ws": ws, "u": users_id},
            ).scalar_one()
        )


def _remove(world: GmailSyncWorld, *, actor: UUID, target: UUID) -> Any:
    client, token = _client(world, actor)
    return client.delete(
        f"/api/v1/identity/workspaces/{world.workspace_id}/members/{target}",
        headers=csrf_headers(token),
    )


def _audit_ids(ws: UUID, event_type: str) -> list[UUID]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT aggregate_id FROM audit_events "
                "WHERE workspace_id = :ws AND event_type = :et ORDER BY aggregate_id"
            ),
            {"ws": ws, "et": event_type},
        ).all()
    return [row[0] for row in rows]


def _rec_version(recommendation_id: UUID) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text("SELECT version FROM recommendations WHERE id = :id"),
                {"id": recommendation_id},
            ).scalar_one()
        )


# --- write sites (flag on) ----------------------------------------------------------


def test_flag_on_sync_writes_content_private_to_mailbox_owner(world_on: GmailSyncWorld) -> None:
    for key in ("a", "b"):
        member = world_on.members[key]
        ids = _content_ids(member)
        for table, row_ids in ids.items():
            assert row_ids, f"{key}: the real sync wrote no {table}"
            assert set(_rows(table, row_ids).values()) == {(member.user_id, "private")}, table


def test_flag_on_aliases_owned_by_mailbox_owner_and_stay_workspace(
    world_on: GmailSyncWorld,
) -> None:
    for key in ("a", "b"):
        member = world_on.members[key]
        aliases = _created_aliases(world_on, key)
        assert aliases
        assert set(_rows("entity_aliases", aliases).values()) == {(member.user_id, "workspace")}
    # Person nodes are unchanged workspace knowledge (DS3 (a')).
    nodes = list(world_on.b.person_node_ids.values())
    assert {vis for _owner, vis in _rows("pkos_nodes", nodes).values()} == {"workspace"}


def test_flag_on_original_user_owns_nothing_the_sync_wrote(world_on: GmailSyncWorld) -> None:
    bystander = _bystander(world_on)
    for table in ("entity_aliases", "ai_run_steps", "pkos_evidence", "ai_runs", "recommendations"):
        assert _owned_count(world_on.workspace_id, table, bystander) == 0, table


def test_flag_on_other_members_cannot_list_read_or_act(world_on: GmailSyncWorld) -> None:
    """B (a `member`) and the bystander (a workspace `owner`) get exactly the
    responses existing authz gives for a nonexistent row; A (the mailbox
    owner) keeps full access."""
    a = world_on.a
    rec_id = a.recommendation_ids[0]
    run_id = a.ai_run_ids[0]
    step_id = _step_ids([run_id])[0]
    evidence_id = a.detection_evidence_ids[0]
    alias_id = _created_aliases(world_on, "a")[0]

    for viewer in (world_on.b.user_id, _bystander(world_on)):
        client, token = _client(world_on, viewer)

        listed = client.get(
            "/api/v1/recommendations", params={"recommendation_type": "email_action_detected"}
        )
        assert listed.status_code == 200, listed.text
        assert not {UUID(item["id"]) for item in listed.json()["items"]} & set(a.recommendation_ids)
        detail = client.get(f"/api/v1/recommendations/{rec_id}")
        assert detail.status_code == 404
        assert detail.json()["error"]["code"] == "RECOMMENDATION_NOT_FOUND"
        for action in ("publish", "confirm", "reject"):
            body = {"expected_version": _rec_version(rec_id)}
            acted = client.post(
                f"/api/v1/recommendations/{rec_id}/{action}",
                json=body,
                headers=csrf_headers(token, str(uuid4())),
            )
            assert acted.status_code == 404, (action, acted.text)
            assert acted.json()["error"]["code"] == "RECOMMENDATION_NOT_FOUND"

        run = client.get(f"/api/v1/ai/runs/{run_id}")
        assert run.status_code == 404
        assert run.json()["error"]["code"] == "AI_RUN_NOT_FOUND"
        cancel = client.post(f"/api/v1/ai/runs/{run_id}/cancel", headers=csrf_headers(token))
        assert cancel.status_code == 404
        assert cancel.json()["error"]["code"] == "AI_RUN_NOT_FOUND"

        evidence = client.get("/api/v1/evidence", params={"id": [str(evidence_id)]})
        assert evidence.status_code == 200
        assert evidence.json()["items"] == [
            {
                "id": str(evidence_id),
                "status": "missing",
                "source_type": None,
                "label": None,
                "captured_at": None,
            }
        ]
        deleted = client.post(
            f"/api/v1/evidence/{evidence_id}/delete",
            json={"reason": "not mine"},
            headers=csrf_headers(token, str(uuid4())),
        )
        assert deleted.status_code == 404
        assert deleted.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"

        for resource_type, resource_id in (
            ("recommendations", rec_id),
            ("ai_runs", run_id),
            ("ai_run_steps", step_id),
            ("pkos_evidence", evidence_id),
        ):
            permissions = client.get(f"/api/v1/sharing/resources/{resource_type}/{resource_id}")
            assert permissions.status_code == 404, resource_type
            assert permissions.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
        # The alias stays workspace knowledge: visible to every member.
        alias = client.get(f"/api/v1/sharing/resources/entity_aliases/{alias_id}")
        assert alias.status_code == 200, alias.text
        assert alias.json()["visibility"] == "workspace"

    # A, the mailbox owner, keeps full access.
    client, token = _client(world_on, a.user_id)
    listed = client.get(
        "/api/v1/recommendations", params={"recommendation_type": "email_action_detected"}
    )
    assert set(a.recommendation_ids) <= {UUID(item["id"]) for item in listed.json()["items"]}
    assert client.get(f"/api/v1/recommendations/{rec_id}").status_code == 200
    assert client.get(f"/api/v1/ai/runs/{run_id}").status_code == 200
    for resource_type, resource_id in (("ai_run_steps", step_id), ("pkos_evidence", evidence_id)):
        permissions = client.get(f"/api/v1/sharing/resources/{resource_type}/{resource_id}")
        assert permissions.status_code == 200, resource_type
        assert permissions.json()["visibility"] == "private"
    evidence = client.get("/api/v1/evidence", params={"id": [str(evidence_id)]})
    assert evidence.json()["items"][0]["status"] == "available"
    for action in ("publish", "reject"):
        acted = client.post(
            f"/api/v1/recommendations/{rec_id}/{action}",
            json={"expected_version": _rec_version(rec_id)},
            headers=csrf_headers(token, str(uuid4())),
        )
        assert acted.status_code == 200, (action, acted.text)
    deleted = client.post(
        f"/api/v1/evidence/{evidence_id}/delete",
        json={"reason": "mine"},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert deleted.status_code == 200, deleted.text


def test_flag_on_email_run_steps_cannot_be_shared(world_on: GmailSyncWorld) -> None:
    """`ai_run_steps` of an email run joined the personal data set (N11):
    even the owner cannot grant them (the existing share refusal)."""
    a = world_on.a
    client, token = _client(world_on, a.user_id)
    response = client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "ai_run_steps",
            "resource_id": str(_step_ids(a.ai_run_ids)[0]),
            "grantee_account_id": str(world_on.b.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=csrf_headers(token),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RESOURCE_TYPE_NOT_GRANTABLE"


def test_flag_on_non_email_rows_unchanged(world_on: GmailSyncWorld) -> None:
    a = world_on.a
    client, token = _client(world_on, a.user_id)
    created = client.post(
        "/api/v1/recommendations",
        headers=csrf_headers(token, str(uuid4())),
        json={
            "recommendation_type": "task_detected",
            "target_type": "task",
            "target_id": None,
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": {"title": "Follow up with vendor"},
            "rationale": "Detected action item.",
            "confidence": 0.8,
            "evidence_ids": [],
            "source": "ai",
        },
    )
    assert created.status_code == 201, created.text
    rec_id = UUID(created.json()["id"])
    assert _rows("recommendations", [rec_id])[rec_id] == (a.user_id, "workspace")
    assert (
        _client(world_on, world_on.b.user_id)[0]
        .get(f"/api/v1/recommendations/{rec_id}")
        .status_code
        == 200
    )

    # A non-email run through the one persistence path every run takes.
    run_id = uuid4()
    now = datetime.now(UTC)
    with SessionFactory() as session:
        runtime_module._persist_terminal(
            session,
            AuthContext(workspace_id=world_on.workspace_id, user_id=a.user_id, timezone="UTC"),
            run_id=run_id,
            task_type="attention.explain_item",
            data_class="sensitive",
            status="failed",
            error_code=None,
            started_at=now,
            policy_version=None,
            model_id=None,
            provider=None,
            prompt_id=None,
            prompt_version=None,
            evidence=[],
            output=None,
            prompt_tokens=None,
            output_tokens=None,
            attempts=1,
            steps=[{"sequence": 1, "kind": "tool_call", "status": "failed", "trace": {}}],
            input_ref={},
        )
    assert _rows("ai_runs", [run_id])[run_id] == (a.user_id, "workspace")
    steps = _step_ids([run_id])
    # Steps of a non-email run: the workspace's original user, as before.
    assert set(_rows("ai_run_steps", steps).values()) == {(_bystander(world_on), "workspace")}


# --- flag off: today's values ------------------------------------------------------


def test_flag_off_writes_todays_values(world_off: GmailSyncWorld) -> None:
    bystander = _bystander(world_off)
    for key in ("a", "b"):
        member = world_off.members[key]
        ids = _content_ids(member)
        assert set(_rows("recommendations", ids["recommendations"]).values()) == {
            (member.user_id, "workspace")
        }
        assert set(_rows("ai_runs", ids["ai_runs"]).values()) == {(member.user_id, "workspace")}
        # Trigger/SQL default: the workspace's original user.
        for table in ("ai_run_steps", "pkos_evidence"):
            assert set(_rows(table, ids[table]).values()) == {(bystander, "workspace")}, table
        assert set(_rows("entity_aliases", _created_aliases(world_off, key)).values()) == {
            (bystander, "workspace")
        }
    rec_id = world_off.a.recommendation_ids[0]
    assert (
        _client(world_off, world_off.b.user_id)[0]
        .get(f"/api/v1/recommendations/{rec_id}")
        .status_code
        == 200
    )


def test_flag_off_original_user_still_blocked_by_sync_rows(world_off: GmailSyncWorld) -> None:
    """Today's behavior (N11), unchanged with the flag off."""
    response = _remove(world_off, actor=world_off.a.user_id, target=_bystander(world_off))
    assert response.status_code == 409, response.text
    owned = {r["resource_type"] for r in response.json()["error"]["details"]["owned_resources"]}
    assert {"entity_aliases", "ai_run_steps", "pkos_evidence"} <= owned


# --- removal (flag on) ----------------------------------------------------------------


def test_flag_on_original_user_removable_after_others_synced(world_on: GmailSyncWorld) -> None:
    """N11 regression: the workspace's earliest user never connected Gmail
    and owns nothing the members' syncs wrote, so removal succeeds."""
    response = _remove(world_on, actor=world_on.a.user_id, target=_bystander(world_on))
    assert response.status_code == 200, response.text


def test_flag_on_mailbox_owner_removable(world_on: GmailSyncWorld) -> None:
    a = world_on.a
    bystander = _bystander(world_on)
    a_aliases = _created_aliases(world_on, "a")
    a_content = _content_ids(a)

    response = _remove(world_on, actor=bystander, target=a.user_id)

    assert response.status_code == 200, response.text
    # Gmail-only person nodes and their aliases re-owned together to the
    # earliest other active owner; one audit row per alias.
    assert {
        owner for owner, _ in _rows("pkos_nodes", list(a.person_node_ids.values())).values()
    } == {bystander}
    assert set(_rows("entity_aliases", a_aliases).values()) == {(bystander, "workspace")}
    assert _audit_ids(world_on.workspace_id, "entity_alias.ownership_reassigned") == sorted(
        a_aliases
    )
    # Personal content (incl. email run steps) is retained, not transferred.
    for table, row_ids in a_content.items():
        assert set(_rows(table, row_ids).values()) == {(a.user_id, "private")}, table


def test_flag_on_alias_follows_node_transferred_before_removal(world_on: GmailSyncWorld) -> None:
    """A Gmail-derived alias whose node the member already transferred away
    follows that node's owner at removal instead of blocking it."""
    a, b = world_on.a, world_on.b
    b_only = [email for email in b.person_node_ids if email != world_on.shared_correspondent_email]
    email = b_only[0]
    node_id = b.person_node_ids[email]
    # Give the node non-Gmail evidence so removal would not re-own it itself.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
                "sha256, captured_at, evidence_state, owner_id) VALUES (:id, :ws, :node, "
                "'manual_note', :ref, :sha, now(), 'available', :owner)"
            ),
            {
                "id": uuid4(),
                "ws": world_on.workspace_id,
                "node": node_id,
                "ref": f"manual:{node_id}",
                "sha": uuid4().hex,
                "owner": b.user_id,
            },
        )
    client, token = _client(world_on, a.user_id)
    transfer = client.post(
        "/api/v1/ownership/transfers",
        headers=csrf_headers(token),
        json={
            "resource_type": "pkos_nodes",
            "resource_id": str(node_id),
            "to_account_id": str(a.account_id),
        },
    )
    assert transfer.status_code == 201, transfer.text
    alias_id = b.entity_alias_ids[email]
    assert _rows("entity_aliases", [alias_id])[alias_id] == (b.user_id, "workspace")

    # The manual evidence row is B's; transfer it too so only the alias is left.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE pkos_evidence SET owner_id = :a WHERE node_id = :node "
                "AND source_type = 'manual_note'"
            ),
            {"a": a.user_id, "node": node_id},
        )
    response = _remove(world_on, actor=a.user_id, target=b.user_id)

    assert response.status_code == 200, response.text
    assert _rows("entity_aliases", [alias_id])[alias_id] == (a.user_id, "workspace")
    assert alias_id in _audit_ids(world_on.workspace_id, "entity_alias.ownership_reassigned")


def _add_manual_evidence(world: GmailSyncWorld, node_id: UUID, owner_id: UUID) -> UUID:
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
                "sha256, captured_at, evidence_state, owner_id) VALUES (:id, :ws, :node, "
                "'manual_note', :ref, :sha, now(), 'available', :owner)"
            ),
            {
                "id": evidence_id,
                "ws": world.workspace_id,
                "node": node_id,
                "ref": f"manual:{evidence_id}",
                "sha": uuid4().hex,
                "owner": owner_id,
            },
        )
    return evidence_id


def _add_alias(world: GmailSyncWorld, *, node_id: UUID, source_id: UUID, owner_id: UUID) -> UUID:
    alias_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO entity_aliases (id, workspace_id, entity_id, alias_type, "
                "normalized_value, source_id, confidence, created_at, owner_id) VALUES "
                "(:id, :ws, :node, 'name', :value, :source, 1.00, now(), :owner)"
            ),
            {
                "id": alias_id,
                "ws": world.workspace_id,
                "node": node_id,
                "value": f"manual-{alias_id}",
                "source": source_id,
                "owner": owner_id,
            },
        )
    return alias_id


def _alias_state(alias_id: UUID) -> tuple[UUID, int]:
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT owner_id, version FROM entity_aliases WHERE id = :id"), {"id": alias_id}
        ).one()
    return row[0], row[1]


def test_flag_on_non_gmail_alias_on_transferred_node_still_blocks(
    world_on: GmailSyncWorld,
) -> None:
    """Only Gmail-derived aliases (source = `gmail_sync` evidence) follow
    their node: a member-owned alias sourced from other evidence keeps
    blocking even once its node belongs to someone else."""
    a, b = world_on.a, world_on.b
    email = next(e for e in b.person_node_ids if e != world_on.shared_correspondent_email)
    node_id = b.person_node_ids[email]
    manual_evidence = _add_manual_evidence(world_on, node_id, a.user_id)
    manual_alias = _add_alias(
        world_on, node_id=node_id, source_id=manual_evidence, owner_id=b.user_id
    )
    client, token = _client(world_on, a.user_id)
    transfer = client.post(
        "/api/v1/ownership/transfers",
        headers=csrf_headers(token),
        json={
            "resource_type": "pkos_nodes",
            "resource_id": str(node_id),
            "to_account_id": str(a.account_id),
        },
    )
    assert transfer.status_code == 201, transfer.text
    before = _alias_state(manual_alias)

    response = _remove(world_on, actor=a.user_id, target=b.user_id)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert error["details"]["owned_resources"] == [{"resource_type": "entity_aliases", "count": 1}]
    assert _alias_state(manual_alias) == before
    assert _audit_ids(world_on.workspace_id, "entity_alias.ownership_reassigned") == []


def test_flag_on_alias_on_node_member_still_owns_is_not_reowned(
    world_on: GmailSyncWorld,
) -> None:
    """A Gmail-derived alias whose node the member still owns (a
    mixed-source node, which removal never re-owns) is not touched by the
    alias re-own: the owned check -- which runs after it in the same
    transaction -- still counts it."""
    a, b = world_on.a, world_on.b
    email = next(e for e in b.person_node_ids if e != world_on.shared_correspondent_email)
    node_id = b.person_node_ids[email]
    _add_manual_evidence(world_on, node_id, b.user_id)
    alias_id = b.entity_alias_ids[email]
    before = _alias_state(alias_id)

    response = _remove(world_on, actor=a.user_id, target=b.user_id)

    assert response.status_code == 409, response.text
    owned = response.json()["error"]["details"]["owned_resources"]
    # The node, its Gmail alias and the manual evidence row (owned by B,
    # not personal) all still count as B's.
    assert {"resource_type": "entity_aliases", "count": 1} in owned
    assert {"resource_type": "pkos_nodes", "count": 1} in owned
    assert _alias_state(alias_id) == before == (b.user_id, before[1])
