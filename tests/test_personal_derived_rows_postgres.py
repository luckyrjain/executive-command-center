"""Personal-derived rows (Spec A S1.3/S1.4, deep review F2 / plan task FX3).

A task/commitment/risk created by confirming an `email_action_detected`
recommendation copies email-derived content (title/summary); with
`ECC_PERSONAL_DATA_ISOLATION` on it is written private to the
recommendation's owner (plan note N23). It is not in
`PERSONAL_ROW_PREDICATES`, so before this fix a workspace owner/admin who
cannot read it could still:

- P3: self-grant it (`POST /sharing/grants` auto-widens `private` ->
  `shared_explicitly`, and the grantee reads the email-derived title);
- P4: transfer it to themselves (`POST /ownership/transfers`);
- and it blocked the mailbox owner's removal (`owned_resource_summary`),
  pushing admins towards exactly that transfer.

With the flag on these rows (`connector_security.PERSONAL_DERIVED_
PREDICATES`: the executed `create` target of an email recommendation) are
share-refused on every path -- grant, grant preview, ownership transfer,
delegation create -- for EVERY caller, the row's owner included (same as
S1.3's personal data set), with the refusal audit + metric; and they never
block removal (retained private, DS2). Flag off: exactly today.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import GmailSyncWorld, build_gmail_sync_world, csrf_headers
from sqlalchemy import text

from ecc import observability
from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_mutations import (
    create_recommendation,
    synthetic_request,
)
from ecc.platform import authz, connector_security
from ecc.platform.connector_security import (
    EMAIL_RECOMMENDATION_TYPE,
    PERSONAL_DERIVED_PREDICATES,
    PERSONAL_ROW_PREDICATES,
    is_personal_resource,
    personal_sql_params,
)

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
_FLAG_ON = {_FLAG: "true"}
_EVENT_TYPE = "personal_data.share_refused"
_EXTRA_CLEANUP_TABLES = (
    "attention_feedback",
    "ownership_transfers",
    "member_notifications",
    "resource_grants",
    "recommendation_feedback",
)
_TABLE_BY_TARGET = {"task": "tasks", "commitment": "commitments", "risk": "risks"}
_TARGETS = tuple(_TABLE_BY_TARGET)
_FIELDS_BY_TARGET: dict[str, dict[str, Any]] = {
    "task": {"title": "Reply to the confidential request"},
    "commitment": {"summary": "Send the confidential figures", "direction": "made_by_me"},
    "risk": {"description": "Confidential deal may slip", "probability": 3, "impact": 4},
}


# --- worlds -------------------------------------------------------------------


@pytest.fixture(scope="module")
def world() -> Iterator[GmailSyncWorld]:
    """One real-synced world (A owner + mailbox, B member + mailbox, and a
    `bystander` workspace owner who never syncs Gmail) for the refusal
    tests: each test derives its own fresh rows."""
    with build_gmail_sync_world(
        env=_FLAG_ON, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES, bystander=True
    ) as built:
        yield built


@pytest.fixture
def removal_world() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env=_FLAG_ON, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES, bystander=True
    ) as built:
        yield built


@pytest.fixture
def set_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    def _set(enabled: bool) -> None:
        monkeypatch.setenv(_FLAG, "true" if enabled else "false")
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


# --- helpers ------------------------------------------------------------------


def _bystander(world: GmailSyncWorld) -> UUID:
    assert world.bystander_user_id is not None
    return world.bystander_user_id


def _client(world: GmailSyncWorld, user_id: UUID) -> tuple[TestClient, str]:
    return world.harness.client_for(world.workspace_id, user_id)


def _account_id(user_id: UUID) -> UUID:
    with engine.connect() as connection:
        return UUID(
            str(
                connection.execute(
                    text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
                ).scalar_one()
            )
        )


def _rec_version(recommendation_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT version FROM recommendations WHERE id = :id"),
                {"id": recommendation_id},
            ).scalar_one()
        )


def _publish_and_confirm(world: GmailSyncWorld, user_id: UUID, rec_id: UUID) -> dict[str, Any]:
    client, token = _client(world, user_id)
    published = client.post(
        f"/api/v1/recommendations/{rec_id}/publish",
        json={"expected_version": _rec_version(rec_id)},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert published.status_code == 200, published.text
    confirmed = client.post(
        f"/api/v1/recommendations/{rec_id}/confirm",
        json={"expected_version": _rec_version(rec_id), "target_expected_version": None},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert confirmed.status_code == 200, confirmed.text
    result: dict[str, Any] = confirmed.json()["execution_result"]
    return result


def _email_recommendation(
    world: GmailSyncWorld, owner: UUID, target_type: str, *, visibility: str = "private"
) -> UUID:
    """An `email_action_detected` `create` recommendation of `owner`'s, as
    the Gmail detect-action hook writes one (the public create route
    refuses the type)."""
    with SessionFactory() as session:
        created = create_recommendation(
            session,
            AuthContext(workspace_id=world.workspace_id, user_id=owner, timezone="UTC"),
            RecommendationCreate(
                recommendation_type=EMAIL_RECOMMENDATION_TYPE,
                target_type=target_type,
                target_id=None,
                proposed_action={"operation": "create", "value": None},
                proposed_fields=_FIELDS_BY_TARGET[target_type],
                rationale="Detected action item.",
                confidence=0.8,
                evidence_ids=[],
                source="ai",
            ),
            synthetic_request(uuid4(), uuid4()),
            f"fx3-test:{uuid4()}",
            visibility=visibility,
        )
    return created.id


def _derived_row(world: GmailSyncWorld, owner: UUID, target_type: str) -> UUID:
    """Confirm a fresh email recommendation of `owner`'s; return the id of
    the task/commitment/risk it created."""
    rec_id = _email_recommendation(world, owner, target_type)
    result = _publish_and_confirm(world, owner, rec_id)
    assert result["target_type"] == target_type
    assert result["operation"] == "create"
    return UUID(result["target_id"])


def _manual_row(world: GmailSyncWorld, owner: UUID, target_type: str) -> UUID:
    """The same kind of row created directly (not email-derived)."""
    client, token = _client(world, owner)
    response = client.post(
        f"/api/v1/{_TABLE_BY_TARGET[target_type]}",
        json=_FIELDS_BY_TARGET[target_type],
        headers=csrf_headers(token, str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _row(table: str, row_id: UUID) -> tuple[UUID, str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT owner_id, visibility, version FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        ).one()
    return row[0], row[1], int(row[2])


def _grant_count(resource_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM resource_grants WHERE resource_id = :id"),
                {"id": resource_id},
            ).scalar_one()
        )


def _transfer_count(resource_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM ownership_transfers WHERE resource_id = :id"),
                {"id": resource_id},
            ).scalar_one()
        )


def _delegation_count(workspace_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM delegations WHERE workspace_id = :ws"),
                {"ws": workspace_id},
            ).scalar_one()
        )


def _refusal_audits(workspace_id: UUID, resource_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT aggregate_type, authorization_result, failure_code, metadata "
                    "FROM audit_events WHERE workspace_id = :ws AND event_type = :event_type "
                    "AND aggregate_id = :id"
                ),
                {"ws": workspace_id, "event_type": _EVENT_TYPE, "id": resource_id},
            )
        )


def _counter(resource_type: str, path: str) -> float:
    return observability.personal_data_share_refused_total._values.get((resource_type, path), 0.0)


def _call(
    world: GmailSyncWorld,
    path: str,
    caller: UUID,
    *,
    resource_type: str,
    resource_id: UUID,
    target_account_id: UUID,
    as_evidence_of: UUID | None = None,
) -> Any:
    client, token = _client(world, caller)
    body: dict[str, Any] = {
        "resource_type": resource_type,
        "resource_id": str(resource_id),
    }
    if path == "grant":
        return client.post(
            "/api/v1/sharing/grants",
            json={**body, "grantee_account_id": str(target_account_id), "actions": ["read"]},
            headers=csrf_headers(token),
        )
    if path == "grant_preview":
        return client.post(
            "/api/v1/sharing/grants/preview",
            json={**body, "grantee_account_id": str(target_account_id), "actions": ["read"]},
            headers=csrf_headers(token),
        )
    if path == "transfer":
        return client.post(
            "/api/v1/ownership/transfers",
            json={**body, "to_account_id": str(target_account_id)},
            headers=csrf_headers(token),
        )
    assert path == "delegation_create"
    if as_evidence_of is None:
        obligation_type, obligation_id = resource_type, resource_id
        evidence: list[dict[str, str]] = []
    else:
        obligation_type, obligation_id = "tasks", as_evidence_of
        evidence = [body]
    return client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(target_account_id),
            "obligation_type": obligation_type,
            "obligation_resource_id": str(obligation_id),
            "expected_outcome": "Handle it",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": evidence,
        },
        headers=csrf_headers(token, str(uuid4())),
    )


def _assert_refused(
    world: GmailSyncWorld,
    response: Any,
    *,
    path: str,
    resource_type: str,
    resource_id: UUID,
    counter_before: float,
    audits_before: int,
) -> None:
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RESOURCE_TYPE_NOT_GRANTABLE"
    audits = _refusal_audits(world.workspace_id, resource_id)
    assert len(audits) == audits_before + 1
    assert all(a.aggregate_type == resource_type for a in audits)
    assert all(a.authorization_result == "denied" for a in audits)
    assert _counter(resource_type, path) == counter_before + 1


# --- the predicate itself -------------------------------------------------------


def test_derived_predicates_are_separate_from_the_personal_data_set() -> None:
    """Kept out of `PERSONAL_ROW_PREDICATES`: the T15 backfill and the T18
    audit iterate that map with per-table owner rules (and the backfill
    refuses to run when the map names a table outside its `TABLES`)."""
    assert set(PERSONAL_DERIVED_PREDICATES) == {
        "tasks",
        "commitments",
        "risks",
        "recommendation_feedback",
        "attention_feedback",
    }
    assert not set(PERSONAL_DERIVED_PREDICATES) & set(PERSONAL_ROW_PREDICATES)
    # The removal non-blocking set is exactly the union.
    assert dict(authz._PERSONAL_ROWS_NOT_BLOCKING_REMOVAL) == {
        **PERSONAL_ROW_PREDICATES,
        **PERSONAL_DERIVED_PREDICATES,
    }
    for table in _TABLE_BY_TARGET.values():
        fragment = PERSONAL_DERIVED_PREDICATES[table]
        # Literal type (not a bind param) so the partial index
        # `ix_recommendations_email_derived_target` also serves generic
        # (prepared) plans.
        assert f"'{EMAIL_RECOMMENDATION_TYPE}'" in fragment
        assert f"{table}.id::text" in fragment
        assert f"{table}.workspace_id" in fragment


def test_is_personal_resource_matches_only_email_create_targets(world: GmailSyncWorld) -> None:
    a = world.a.user_id
    derived = {t: _derived_row(world, a, t) for t in _TARGETS}
    manual = {t: _manual_row(world, a, t) for t in _TARGETS}
    with SessionFactory() as session:
        for target_type, table in _TABLE_BY_TARGET.items():
            assert is_personal_resource(session, table, derived[target_type]) is True
            assert is_personal_resource(session, table, manual[target_type]) is False
            assert is_personal_resource(session, table, uuid4()) is False
        # A derived task id under another table is not that table's row.
        assert is_personal_resource(session, "commitments", derived["task"]) is False
        # `personal_derived_row_scope` still only follows the personal data
        # set: a derived row is not itself a source of derived rows.
        assert (
            connector_security.personal_derived_row_scope(session, "tasks", derived["task"]) is None
        )


def test_non_create_email_recommendation_does_not_mark_its_target(
    world: GmailSyncWorld,
) -> None:
    """An email recommendation that changes an existing row (set_status)
    records that row as `execution_result.target_id` too; only `create`
    targets are derived rows."""
    a = world.a.user_id
    task_id = _manual_row(world, a, "task")
    with SessionFactory() as session:
        created = create_recommendation(
            session,
            AuthContext(workspace_id=world.workspace_id, user_id=a, timezone="UTC"),
            RecommendationCreate(
                recommendation_type=EMAIL_RECOMMENDATION_TYPE,
                target_type="task",
                target_id=task_id,
                proposed_action={"operation": "set_status", "value": "in_progress"},
                expected_version=1,
                rationale="Detected status change.",
                confidence=0.8,
                source="ai",
            ),
            synthetic_request(uuid4(), uuid4()),
            f"fx3-test:{uuid4()}",
            visibility="private",
        )
    client, token = _client(world, a)
    assert (
        client.post(
            f"/api/v1/recommendations/{created.id}/publish",
            json={"expected_version": _rec_version(created.id)},
            headers=csrf_headers(token, str(uuid4())),
        ).status_code
        == 200
    )
    confirmed = client.post(
        f"/api/v1/recommendations/{created.id}/confirm",
        json={"expected_version": _rec_version(created.id), "target_expected_version": 1},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["execution_result"]["target_id"] == str(task_id)
    with SessionFactory() as session:
        assert is_personal_resource(session, "tasks", task_id) is False


# --- P3 / P4: bystander owner/admin -------------------------------------------


@pytest.mark.parametrize("target_type", _TARGETS)
@pytest.mark.parametrize("path", ["grant", "grant_preview", "transfer"])
def test_bystander_owner_cannot_grant_preview_or_transfer_derived_row(
    world: GmailSyncWorld, target_type: str, path: str
) -> None:
    """P3 (self-grant) / P4 (transfer to self) and the preview, for each
    derived type: 400 + refusal audit + metric; the row, its grants and
    its transfers are unchanged, and the bystander still cannot read it."""
    a, bystander = world.a.user_id, _bystander(world)
    table = _TABLE_BY_TARGET[target_type]
    row_id = _derived_row(world, a, target_type)
    before = _row(table, row_id)
    assert before[:2] == (a, "private")
    counter_before = _counter(table, path)

    response = _call(
        world,
        path,
        bystander,
        resource_type=table,
        resource_id=row_id,
        target_account_id=_account_id(bystander),
    )

    _assert_refused(
        world,
        response,
        path=path,
        resource_type=table,
        resource_id=row_id,
        counter_before=counter_before,
        audits_before=0,
    )
    assert _row(table, row_id) == before
    assert _grant_count(row_id) == 0
    assert _transfer_count(row_id) == 0
    client, _token = _client(world, bystander)
    assert client.get(f"/api/v1/{table}/{row_id}").status_code == 404


def test_bystander_grant_to_another_member_refused(world: GmailSyncWorld) -> None:
    """The write-path probe's variant: admin grants B (not themselves)."""
    a, bystander = world.a.user_id, _bystander(world)
    row_id = _derived_row(world, a, "task")
    counter_before = _counter("tasks", "grant")
    response = _call(
        world,
        "grant",
        bystander,
        resource_type="tasks",
        resource_id=row_id,
        target_account_id=world.b.account_id,
    )
    _assert_refused(
        world,
        response,
        path="grant",
        resource_type="tasks",
        resource_id=row_id,
        counter_before=counter_before,
        audits_before=0,
    )
    client, _token = _client(world, world.b.user_id)
    assert client.get(f"/api/v1/tasks/{row_id}").status_code == 404
    assert _row("tasks", row_id)[:2] == (a, "private")


@pytest.mark.parametrize("target_type", _TARGETS)
def test_bystander_delegation_create_on_derived_row(
    world: GmailSyncWorld, target_type: str
) -> None:
    """Without read access the bystander gets the existing 404; with read
    access from before the flag (an old grant), delegation create is
    refused (400 + audit + metric) and nothing is written."""
    a, bystander = world.a.user_id, _bystander(world)
    table = _TABLE_BY_TARGET[target_type]
    row_id = _derived_row(world, a, target_type)
    delegations_before = _delegation_count(world.workspace_id)

    hidden = _call(
        world,
        "delegation_create",
        bystander,
        resource_type=table,
        resource_id=row_id,
        target_account_id=world.b.account_id,
    )
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["error"]["code"] == "OBLIGATION_NOT_FOUND"

    # A read+write grant written while the flag was off.
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {table} SET visibility = 'shared_explicitly' WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        )
        connection.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "VALUES (:id, :ws, :grantee, :rt, :rid, ARRAY['read','write'], :by, now())"
            ),
            {
                "id": uuid4(),
                "ws": world.workspace_id,
                "grantee": _account_id(bystander),
                "rt": table,
                "rid": row_id,
                "by": a,
            },
        )
    before = _row(table, row_id)
    counter_before = _counter(table, "delegation_create")

    response = _call(
        world,
        "delegation_create",
        bystander,
        resource_type=table,
        resource_id=row_id,
        target_account_id=world.b.account_id,
    )

    _assert_refused(
        world,
        response,
        path="delegation_create",
        resource_type=table,
        resource_id=row_id,
        counter_before=counter_before,
        audits_before=0,
    )
    assert _row(table, row_id) == before
    assert _grant_count(row_id) == 1
    assert _delegation_count(world.workspace_id) == delegations_before


# --- the row's own owner (decision: refused too, as S1.3) -------------------------


@pytest.mark.parametrize("target_type", _TARGETS)
@pytest.mark.parametrize("path", ["grant", "grant_preview", "transfer", "delegation_create"])
def test_owner_cannot_share_own_derived_row(
    world: GmailSyncWorld, target_type: str, path: str
) -> None:
    a = world.a.user_id
    table = _TABLE_BY_TARGET[target_type]
    row_id = _derived_row(world, a, target_type)
    before = _row(table, row_id)
    counter_before = _counter(table, path)
    delegations_before = _delegation_count(world.workspace_id)

    response = _call(
        world,
        path,
        a,
        resource_type=table,
        resource_id=row_id,
        target_account_id=world.b.account_id,
    )

    _assert_refused(
        world,
        response,
        path=path,
        resource_type=table,
        resource_id=row_id,
        counter_before=counter_before,
        audits_before=0,
    )
    assert _row(table, row_id) == before
    assert _grant_count(row_id) == 0
    assert _transfer_count(row_id) == 0
    assert _delegation_count(world.workspace_id) == delegations_before


def test_owner_cannot_cite_derived_row_as_delegation_evidence(world: GmailSyncWorld) -> None:
    a = world.a.user_id
    derived = _derived_row(world, a, "risk")
    obligation = _manual_row(world, a, "task")
    delegations_before = _delegation_count(world.workspace_id)
    counter_before = _counter("risks", "delegation_create")
    response = _call(
        world,
        "delegation_create",
        a,
        resource_type="risks",
        resource_id=derived,
        target_account_id=world.b.account_id,
        as_evidence_of=obligation,
    )
    _assert_refused(
        world,
        response,
        path="delegation_create",
        resource_type="risks",
        resource_id=derived,
        counter_before=counter_before,
        audits_before=0,
    )
    assert _delegation_count(world.workspace_id) == delegations_before


@pytest.mark.parametrize("target_type", _TARGETS)
def test_non_derived_rows_still_shareable_and_transferable(
    world: GmailSyncWorld, target_type: str
) -> None:
    """Manually created rows keep today's behaviour with the flag on."""
    a, bystander = world.a.user_id, _bystander(world)
    table = _TABLE_BY_TARGET[target_type]
    shared = _manual_row(world, a, target_type)
    granted = _call(
        world,
        "grant",
        bystander,
        resource_type=table,
        resource_id=shared,
        target_account_id=world.b.account_id,
    )
    # A workspace-visible row needs `narrow_visibility` (409) -- not refused.
    assert granted.status_code == 409, granted.text
    preview = _call(
        world,
        "grant_preview",
        a,
        resource_type=table,
        resource_id=shared,
        target_account_id=world.b.account_id,
    )
    assert preview.status_code == 200, preview.text
    moved = _manual_row(world, a, target_type)
    transferred = _call(
        world,
        "transfer",
        bystander,
        resource_type=table,
        resource_id=moved,
        target_account_id=_account_id(bystander),
    )
    assert transferred.status_code == 201, transferred.text
    assert _refusal_audits(world.workspace_id, shared) == []
    assert _refusal_audits(world.workspace_id, moved) == []


# --- flag off: today --------------------------------------------------------------


def test_flag_off_derived_rows_grantable_and_transferable_as_today(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    """The probes' pre-fix behaviour, with the flag off: the bystander's
    self-grant auto-widens `private` -> `shared_explicitly` (201), and a
    transfer to self succeeds (201); no refusal audit."""
    a, bystander = world.a.user_id, _bystander(world)
    granted_id = _derived_row(world, a, "task")
    moved_id = _derived_row(world, a, "commitment")
    bystander_account = _account_id(bystander)

    set_isolation(False)
    granted = _call(
        world,
        "grant",
        bystander,
        resource_type="tasks",
        resource_id=granted_id,
        target_account_id=bystander_account,
    )
    transferred = _call(
        world,
        "transfer",
        bystander,
        resource_type="commitments",
        resource_id=moved_id,
        target_account_id=bystander_account,
    )

    assert granted.status_code == 201, granted.text
    assert _row("tasks", granted_id)[:2] == (a, "shared_explicitly")
    assert transferred.status_code == 201, transferred.text
    assert _row("commitments", moved_id)[0] == bystander
    assert _refusal_audits(world.workspace_id, granted_id) == []
    assert _refusal_audits(world.workspace_id, moved_id) == []


def test_flag_off_derived_rows_block_removal(
    removal_world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    b = removal_world.b.user_id
    with SessionFactory() as session:
        before = authz.owned_resource_summary(
            session, workspace_id=removal_world.workspace_id, users_id=b, exclude_personal_data=True
        )
    derived = {t: _derived_row(removal_world, b, t) for t in _TARGETS}
    assert all(_row(_TABLE_BY_TARGET[t], i)[:2] == (b, "private") for t, i in derived.items())
    set_isolation(False)
    with SessionFactory() as session:
        summary = authz.owned_resource_summary(
            session, workspace_id=removal_world.workspace_id, users_id=b
        )
    assert {"tasks", "commitments", "risks"} <= {r["resource_type"] for r in summary}
    assert not {"tasks", "commitments", "risks"} & {r["resource_type"] for r in before}


# --- removal (S1.4 / DS2) -------------------------------------------------------------


def test_removal_not_blocked_by_derived_rows_which_stay_private(
    removal_world: GmailSyncWorld,
) -> None:
    """B confirms email recommendations (task, commitment, risk) and is
    removed: the derived rows do not block removal and are retained,
    private and B's, afterwards (nobody else can read them)."""
    world = removal_world
    a, b = world.a.user_id, world.b.user_id
    derived = {t: _derived_row(world, b, t) for t in _TARGETS}
    # Feedback B gives on B's own email attention item (owned by B).
    attention_feedback = _attention_feedback(world, b, world.b.attention_item_ids[0])
    # A manual row B owns still blocks, as before.
    manual = _manual_row(world, b, "task")
    client, token = _client(world, a)

    blocked = client.delete(
        f"/api/v1/identity/workspaces/{world.workspace_id}/members/{b}",
        headers=csrf_headers(token),
    )
    assert blocked.status_code == 409, blocked.text
    owned = blocked.json()["error"]["details"]["owned_resources"]
    assert owned == [{"resource_type": "tasks", "count": 1}]

    moved = _call(
        world,
        "transfer",
        a,
        resource_type="tasks",
        resource_id=manual,
        target_account_id=world.a.account_id,
    )
    assert moved.status_code == 201, moved.text
    removed = client.delete(
        f"/api/v1/identity/workspaces/{world.workspace_id}/members/{b}",
        headers=csrf_headers(token),
    )
    assert removed.status_code == 200, removed.text
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT owner_id FROM attention_feedback WHERE id = :id"),
                {"id": attention_feedback},
            ).scalar_one()
            == b
        )

    for target_type, row_id in derived.items():
        table = _TABLE_BY_TARGET[target_type]
        assert _row(table, row_id)[:2] == (b, "private")
        assert _grant_count(row_id) == 0
        for viewer in (a, _bystander(world)):
            viewer_client, _token = _client(world, viewer)
            assert viewer_client.get(f"/api/v1/{table}/{row_id}").status_code == 404
        # ... and still cannot be transferred away by an owner afterwards.
        refused = _call(
            world,
            "transfer",
            _bystander(world),
            resource_type=table,
            resource_id=row_id,
            target_account_id=_account_id(_bystander(world)),
        )
        assert refused.status_code == 400, refused.text


# --- performance ----------------------------------------------------------------------

_INDEX = "ix_recommendations_email_derived_target"

# Per derived table: the INSERT of `owner`-owned private rows with ids from
# `:ids` (the columns every CHECK / NOT NULL needs, nothing else).
_SEED_ROWS = {
    "tasks": (
        "INSERT INTO tasks (id, workspace_id, owner_id, title, status, manual_priority, "
        "pinned, source_type, created_by, updated_by, created_at, updated_at, version, "
        "visibility) "
        "SELECT t, :ws, :a, 'perf', 'captured', 'medium', false, 'local', :a, :a, "
        "now(), now(), 1, 'private' FROM unnest(CAST(:ids AS uuid[])) AS t"
    ),
    "commitments": (
        "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
        "created_by, updated_by, created_at, updated_at, visibility) "
        "SELECT t, :ws, :a, 'perf', 'made_by_me', :a, :a, now(), now(), 'private' "
        "FROM unnest(CAST(:ids AS uuid[])) AS t"
    ),
    "risks": (
        "INSERT INTO risks (id, workspace_id, owner_id, description, probability, impact, "
        "created_by, updated_by, created_at, updated_at, visibility) "
        "SELECT t, :ws, :a, 'perf', 3, 3, :a, :a, now(), now(), 'private' "
        "FROM unnest(CAST(:ids AS uuid[])) AS t"
    ),
}


def _seed_derived_rows(
    connection: Any, world: GmailSyncWorld, *, row_count: int, rec_count: int
) -> dict[str, list[UUID]]:
    """Inside the caller's (rolled-back) transaction: for each of tasks,
    commitments and risks, `row_count` private rows owned by A, the first
    half of them the executed `create` target of one of `rec_count` email
    recommendations (per table) in A's workspace.

    Runs no ANALYZE itself: the plan-shape test (anti-join) holds on the
    statistics the database already has, as removal does in production.
    Rows written in a rolled-back transaction are never seen by
    autoanalyze, so a test that needs statistics for the seed ANALYZEs
    explicitly, inside `_restores_table_statistics()`."""
    ws, a = world.workspace_id, world.a.user_id
    connection.execute(text("SET LOCAL statement_timeout = '60s'"))
    ids: dict[str, list[UUID]] = {}
    for target_type, table in _TABLE_BY_TARGET.items():
        row_ids = [uuid4() for _ in range(row_count)]
        ids[table] = row_ids
        connection.execute(text(_SEED_ROWS[table]), {"ws": ws, "a": a, "ids": row_ids})
        connection.execute(
            text(
                "INSERT INTO recommendations (id, workspace_id, recommendation_type, "
                "target_type, proposed_action, rationale, confidence, status, source, "
                "execution_result, created_by, updated_by, created_at, updated_at, version, "
                "owner_id, visibility) "
                "SELECT gen_random_uuid(), :ws, :type, :target_column, "
                '\'{"operation": "create", "value": null}\'::jsonb, \'perf\', 0.5, '
                "'executed', 'ai', jsonb_build_object('operation', 'create', "
                "'target_type', CAST(:target_type AS text), 'target_id', "
                "COALESCE(t.id::text, gen_random_uuid()::text), 'resulting_version', 1), "
                ":a, :a, now(), now(), 1, :a, 'private' "
                "FROM generate_series(1, :rec_count) AS g "
                "LEFT JOIN unnest(CAST(:ids AS uuid[])) WITH ORDINALITY AS t(id, ord) "
                "ON t.ord = g"
            ),
            {
                "ws": ws,
                "a": a,
                "type": EMAIL_RECOMMENDATION_TYPE,
                "target_type": target_type,
                "target_column": target_type,
                "ids": row_ids[: row_count // 2],
                "rec_count": rec_count,
            },
        )
    connection.execute(text("SET LOCAL statement_timeout = '5s'"))
    connection.execute(text("SET LOCAL jit = off"))
    return ids


@contextmanager
def _restores_table_statistics() -> Iterator[None]:
    """For a test that ANALYZEs inside a transaction it rolls back:
    pg_statistic rolls back, but `pg_class.reltuples`/`relpages` are updated
    in place and persist, so afterwards the tables would look as large as
    the (rolled-back) seed to every later test on this database.
    Re-analyzing the now seed-free tables restores them."""
    try:
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            for table in ("recommendations", *_TABLE_BY_TARGET.values()):
                connection.execute(text(f"ANALYZE {table}"))  # noqa: S608 -- fixed names


def _plan(connection: Any, sql: str, params: dict[str, Any]) -> str:
    return "\n".join(row[0] for row in connection.execute(text(f"EXPLAIN {sql}"), params))


@pytest.mark.parametrize("table", ["tasks", "commitments", "risks"])
def test_removal_count_is_an_anti_join_never_a_per_row_subplan(
    world: GmailSyncWorld, table: str
) -> None:
    """Lens A-1 regression: the removal count must plan the derived
    exclusion as one anti-join (index-probed or hashed once), never as a
    correlated SubPlan re-run for every owned row."""
    sql = authz._owned_count_sql(table, exclude_personal_data=True)
    assert f"AND NOT {PERSONAL_DERIVED_PREDICATES[table]}" in sql
    assert "COALESCE" not in sql
    params = {
        "workspace_id": world.workspace_id,
        "users_id": world.a.user_id,
        **personal_sql_params(),
    }
    with engine.connect() as connection, connection.begin() as transaction:
        _seed_derived_rows(connection, world, row_count=500, rec_count=1_000)
        plan = _plan(connection, sql, params)
        transaction.rollback()
    assert "Anti Join" in plan, plan
    assert "SubPlan" not in plan, plan


def _normalized(expression: str) -> str:
    """A catalog-deparsed expression in the fragments' spelling: no `::text`
    casts, no parentheses around a bare column or around the whole
    expression."""
    expression = re.sub(r"\((\w+)\)", r"\1", expression.replace("::text", "").strip())
    while expression.startswith("(") and _closing_paren(expression) == len(expression) - 1:
        expression = expression[1:-1].strip()
    return expression


def _top_level_conjuncts(expression: str) -> list[str]:
    """Split on ` AND ` outside parentheses."""
    parts: list[str] = []
    depth, start = 0, 0
    for position, char in enumerate(expression):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and expression.startswith(" AND ", position):
            parts.append(expression[start:position])
            start = position + len(" AND ")
    tail = expression[start:]
    return [*parts, tail] if tail.strip() else parts


def _closing_paren(expression: str) -> int:
    """Index of the parenthesis closing the one at position 0."""
    depth = 0
    for position, char in enumerate(expression):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0:
            return position
    return -1


def _derived_index_shape() -> tuple[list[str], list[str]]:
    """`ix_recommendations_email_derived_target` from the catalog: its key
    expressions in order, and its partial predicate's conjuncts, both
    normalized."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT i.indexrelid, i.indnkeyatts, pg_get_expr(i.indpred, i.indrelid) "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name"
            ),
            {"name": _INDEX},
        ).one()
        keys = [
            _normalized(
                connection.execute(
                    text("SELECT pg_get_indexdef(:oid, :position, true)"),
                    {"oid": row[0], "position": position},
                ).scalar_one()
            )
            for position in range(1, row[1] + 1)
        ]
    predicate = _normalized(row[2] or "")
    conjuncts = [_normalized(part) for part in _top_level_conjuncts(predicate)]
    return keys, conjuncts


def _index_serves_probe_problems(
    keys: list[str], conjuncts: list[str], table: str, fragment: str
) -> list[str]:
    """Why the index could NOT serve `fragment` (a `PERSONAL_DERIVED_
    PREDICATES` entry) as one keyed lookup per outer row; empty when it can.

    - The index's keys must be exactly workspace_id, operation, target_type,
      target_id, in that order (equality on all four is one index search).
    - Each key must be an equality in the fragment, and workspace_id /
      target_id must be correlated to the outer `<table>` row -- that is
      what makes the lookup a per-row probe.
    - Every conjunct of the index's partial predicate must be a conjunct of
      the fragment (so the planner can prove the index applies), and the
      fragment must be a pure AND (an OR would break that implication)."""
    problems: list[str] = []
    expected_keys = [
        "workspace_id",
        "execution_result ->> 'operation'",
        "execution_result ->> 'target_type'",
        "execution_result ->> 'target_id'",
    ]
    if keys != expected_keys:
        problems.append(f"index keys {keys} != {expected_keys}")
    required = {
        "workspace_id": f"derived_rec.workspace_id = {table}.workspace_id",
        "execution_result ->> 'operation'": "derived_rec.execution_result ->> 'operation' = '",
        "execution_result ->> 'target_type'": (
            "derived_rec.execution_result ->> 'target_type' = '"
        ),
        "execution_result ->> 'target_id'": (
            f"(derived_rec.execution_result ->> 'target_id') = {table}.id::text"
        ),
    }
    for key in expected_keys:
        if required[key] not in fragment:
            problems.append(f"fragment does not constrain index key {key!r}: {required[key]!r}")
    if not conjuncts:
        problems.append("index is not partial")
    for conjunct in conjuncts:
        if f"derived_rec.{conjunct}" not in fragment:
            problems.append(f"fragment does not imply index predicate conjunct {conjunct!r}")
    if " OR " in fragment.upper():
        problems.append("fragment contains OR")
    return problems


def test_derived_target_index_can_serve_every_share_probe() -> None:
    """Deterministic (catalog, not planner): migration 0083's index has
    exactly the keys and partial predicate that make each derived table's
    `PERSONAL_DERIVED_PREDICATES` fragment a single keyed lookup per outer
    row (workspace_id + operation + target_type + target_id). Which plan the
    planner picks depends on statistics, so no EXPLAIN is asserted here; the
    anti-join shape is checked by `test_removal_count_is_an_anti_join_never_
    a_per_row_subplan`, and production-scale plans/timings are in the FX3
    review reports. Mutations of the index or of the fragments are checked
    below, so this cannot pass vacuously."""
    keys, conjuncts = _derived_index_shape()
    for table in _TABLE_BY_TARGET.values():
        fragment = PERSONAL_DERIVED_PREDICATES[table]
        assert _index_serves_probe_problems(keys, conjuncts, table, fragment) == [], table

    fragment = PERSONAL_DERIVED_PREDICATES["tasks"]
    mutated_indexes = {
        "no workspace_id": ([k for k in keys if k != "workspace_id"], conjuncts),
        "no target_id": ([k for k in keys if "target_id" not in k], conjuncts),
        "not partial": (keys, []),
    }
    for label, (mutated_keys, mutated_conjuncts) in mutated_indexes.items():
        assert _index_serves_probe_problems(mutated_keys, mutated_conjuncts, "tasks", fragment), (
            label
        )
    mutated_fragments = {
        "no workspace_id": fragment.replace(
            "derived_rec.workspace_id = tasks.workspace_id AND ", ""
        ),
        "target_id not correlated": fragment.replace("= tasks.id::text", "= :row_id"),
        "no type": fragment.replace(
            "derived_rec.recommendation_type = 'email_action_detected' AND ", ""
        ),
    }
    for label, mutated in mutated_fragments.items():
        assert mutated != fragment, label
        assert _index_serves_probe_problems(keys, conjuncts, "tasks", mutated), label


def test_removal_count_and_share_probe_timing_at_moderate_size(world: GmailSyncWorld) -> None:
    """Sanity bound at a moderate size (per table: 5k owned rows, 10k
    executed email recommendations), under the removal transaction's
    settings (5 s statement timeout, JIT off). Measured separately at
    200k/2M recommendations with 5k-50k tasks (FX3 rounds 1-2 reports)."""
    params = {
        "workspace_id": world.workspace_id,
        "users_id": world.a.user_id,
        **personal_sql_params(),
    }
    with _restores_table_statistics():
        _timed_counts_and_probes(world, params)


def _timed_counts_and_probes(world: GmailSyncWorld, params: dict[str, Any]) -> None:
    with engine.connect() as connection, connection.begin() as transaction:
        baseline = {
            table: connection.execute(
                text(authz._owned_count_sql(table, exclude_personal_data=True)), params
            ).scalar_one()
            for table in _TABLE_BY_TARGET.values()
        }
        ids = _seed_derived_rows(connection, world, row_count=5_000, rec_count=10_000)
        # Needed here, unlike the plan tests above: this test measures the
        # plan for tables that just grew by ~45k rows inside one
        # uncommitted transaction, which autoanalyze can never see. In
        # production the same growth is committed and autoanalyze keeps the
        # statistics current (the migration's own ANALYZE covers the start).
        # Without it the planner still believes the seeded tables are tiny.
        connection.execute(text("SET LOCAL statement_timeout = '60s'"))
        connection.execute(text("ANALYZE recommendations"))
        for table in ids:
            connection.execute(text(f"ANALYZE {table}"))
        connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        for table, row_ids in ids.items():
            remaining, elapsed, probe_elapsed = _time_count_and_probes(
                connection, table, row_ids, params
            )
            assert remaining == baseline[table] + 2_500
            if elapsed >= _COUNT_BUDGET_SECONDS or probe_elapsed >= _PROBE_BUDGET_SECONDS:
                # Wall-clock bounds trip under heavy machine load (50 probes
                # took 3.3 s at load ~150 vs ~0.1-0.4 s normally), so one
                # re-measurement on the same rows is allowed, as for the
                # ranking budget in `test_risks_attention_postgres.py`. A
                # real plan regression is slow on both passes.
                print(f"{table}: over budget, measuring once more")
                _, elapsed, probe_elapsed = _time_count_and_probes(
                    connection, table, row_ids, params
                )
            assert elapsed < _COUNT_BUDGET_SECONDS, (table, elapsed)
            assert probe_elapsed < _PROBE_BUDGET_SECONDS, (table, probe_elapsed)
        transaction.rollback()


_COUNT_BUDGET_SECONDS = 2.0
_PROBE_BUDGET_SECONDS = 1.0


def _time_count_and_probes(
    connection: Any, table: str, row_ids: list[UUID], params: dict[str, Any]
) -> tuple[int, float, float]:
    """One timed removal count plus 50 timed share probes on `table`; the
    probes' answers are asserted on every pass."""
    started = time.perf_counter()
    remaining = connection.execute(
        text(authz._owned_count_sql(table, exclude_personal_data=True)), params
    ).scalar_one()
    elapsed = time.perf_counter() - started
    started = time.perf_counter()
    for index, row_id in enumerate(row_ids[:50]):
        assert connection.execute(
            text(connector_security._SHARE_REFUSED_STATEMENTS[table]),
            {"id": row_id, **personal_sql_params()},
        ).scalar_one() is (index < 2_500)
    probe_elapsed = time.perf_counter() - started
    print(
        f"{table}: removal count {elapsed * 1000:.1f} ms; "
        f"50 share probes {probe_elapsed * 1000:.1f} ms"
    )
    return remaining, elapsed, probe_elapsed


def _feedback_ids(recommendation_id: UUID) -> list[UUID]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text("SELECT id FROM recommendation_feedback WHERE recommendation_id = :id"),
                {"id": recommendation_id},
            )
        ]


def test_feedback_on_email_recommendation_is_share_refused(world: GmailSyncWorld) -> None:
    """Confirming writes an `accept` feedback row owned by the confirmer:
    on an email recommendation it is derived (refused, non-blocking); on
    any other recommendation it is unchanged."""
    a, bystander = world.a.user_id, _bystander(world)
    email_rec = _email_recommendation(world, a, "task")
    _publish_and_confirm(world, a, email_rec)
    (email_feedback,) = _feedback_ids(email_rec)

    client, token = _client(world, a)
    other = client.post(
        "/api/v1/recommendations",
        headers=csrf_headers(token, str(uuid4())),
        json={
            "recommendation_type": "task_detected",
            "target_type": "task",
            "target_id": None,
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": _FIELDS_BY_TARGET["task"],
            "rationale": "Detected action item.",
            "confidence": 0.8,
            "evidence_ids": [],
            "source": "ai",
        },
    )
    assert other.status_code == 201, other.text
    other_rec = UUID(other.json()["id"])
    _publish_and_confirm(world, a, other_rec)
    (other_feedback,) = _feedback_ids(other_rec)

    with SessionFactory() as session:
        assert is_personal_resource(session, "recommendation_feedback", email_feedback) is True
        assert is_personal_resource(session, "recommendation_feedback", other_feedback) is False

    counter_before = _counter("recommendation_feedback", "transfer")
    refused = _call(
        world,
        "transfer",
        bystander,
        resource_type="recommendation_feedback",
        resource_id=email_feedback,
        target_account_id=_account_id(bystander),
    )
    _assert_refused(
        world,
        refused,
        path="transfer",
        resource_type="recommendation_feedback",
        resource_id=email_feedback,
        counter_before=counter_before,
        audits_before=0,
    )
    moved = _call(
        world,
        "transfer",
        bystander,
        resource_type="recommendation_feedback",
        resource_id=other_feedback,
        target_account_id=_account_id(bystander),
    )
    assert moved.status_code == 201, moved.text


def _attention_feedback(world: GmailSyncWorld, actor: UUID, item_id: UUID) -> UUID:
    client, token = _client(world, actor)
    response = client.post(
        f"/api/v1/attention/{item_id}/feedback",
        json={"label": "useful"},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def test_feedback_on_email_attention_item_is_share_refused(world: GmailSyncWorld) -> None:
    """Lens B-4: feedback on an email attention item (written `workspace`,
    owned by the actor) is derived: share-refused (audit + metric), row
    unchanged. Feedback on any other attention target is not."""
    a, bystander = world.a.user_id, _bystander(world)
    feedback_id = _attention_feedback(world, a, world.a.attention_item_ids[0])
    other_id = uuid4()
    with engine.begin() as connection:
        # Same shape, but its target is not an email attention item.
        connection.execute(
            text(
                "INSERT INTO attention_feedback (id, workspace_id, target_type, target_id, "
                "label, actor_id, policy_version, created_at, owner_id, visibility) "
                "VALUES (:id, :ws, 'attention_item', :target, 'useful', :a, 1, now(), :a, "
                "'workspace')"
            ),
            {"id": other_id, "ws": world.workspace_id, "target": uuid4(), "a": a},
        )
    with SessionFactory() as session:
        assert is_personal_resource(session, "attention_feedback", feedback_id) is True
        assert is_personal_resource(session, "attention_feedback", other_id) is False
        # The email item id itself under `attention_feedback` is not a row.
        assert (
            is_personal_resource(session, "attention_feedback", world.a.attention_item_ids[0])
            is False
        )

    for path in ("grant", "transfer"):
        counter_before = _counter("attention_feedback", path)
        audits_before = len(_refusal_audits(world.workspace_id, feedback_id))
        refused = _call(
            world,
            path,
            bystander,
            resource_type="attention_feedback",
            resource_id=feedback_id,
            target_account_id=_account_id(bystander),
        )
        _assert_refused(
            world,
            refused,
            path=path,
            resource_type="attention_feedback",
            resource_id=feedback_id,
            counter_before=counter_before,
            audits_before=audits_before,
        )
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT owner_id, visibility FROM attention_feedback WHERE id = :id"),
            {"id": feedback_id},
        ).one()
    assert tuple(row) == (a, "workspace")
    assert _grant_count(feedback_id) == 0
    moved = _call(
        world,
        "transfer",
        bystander,
        resource_type="attention_feedback",
        resource_id=other_id,
        target_account_id=_account_id(bystander),
    )
    assert moved.status_code == 201, moved.text
