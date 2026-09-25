"""Personal-data isolation (Security Remediation Spec A), behind
`ECC_PERSONAL_DATA_ISOLATION`.

Sharing section (S1.3, T11): with the flag on, a row in the personal data
set (`connector_accounts` gmail, its `sync_runs`/`sync_cursors`,
`attention_items` email_thread, `recommendations` email_action_detected,
`ai_runs` email.detect_action, `pkos_evidence` gmail_sync) cannot be
granted, previewed for a grant, ownership-transferred, or named as
delegation evidence: each path answers the existing
`400 RESOURCE_TYPE_NOT_GRANTABLE`, writes one `denied`
`personal_data.share_refused` audit row (no emails) and increments
`ecc_personal_data_share_refused_total{resource_type,path}`. Delegation
*accept* keeps its skip-on-failed-check behavior: a personal evidence item
is simply not granted. Non-personal rows are unaffected; flag off is
exactly the previous behavior; a caller in another workspace still gets the
existing `404` with no personal-data signal.

Every personal row comes from the real Gmail sync (`gmail_sync_fixtures`).
Later tasks (T12, T14a) append their own sections to this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import (
    GmailSyncWorld,
    MemberGmailArtifacts,
    build_gmail_sync_world,
    cleanup_workspace,
    csrf_headers,
)
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc import observability
from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
_EVENT_TYPE = "personal_data.share_refused"
_NOT_GRANTABLE = "RESOURCE_TYPE_NOT_GRANTABLE"

# Tables these tests write that `gmail_sync_fixtures._CLEANUP_TABLES` does
# not cover (children first; `resource_grants.delegation_id` FKs to
# `delegations`, whose evidence/events cascade).
_EXTRA_CLEANUP_TABLES = (
    "member_notifications",
    "resource_grants",
    "delegations",
    "ownership_transfers",
    "incidents",
)

_PERSONAL_TYPES = (
    "connector_accounts",
    "sync_runs",
    "sync_cursors",
    "attention_items",
    "recommendations",
    "ai_runs",
    "pkos_evidence",
)
_PATHS = ("grant", "grant_preview", "transfer", "delegation_create")


def _personal_id(member: MemberGmailArtifacts, resource_type: str) -> UUID:
    ids: dict[str, UUID] = {
        "connector_accounts": member.connector_account_id,
        "sync_runs": member.sync_run_ids[0],
        "sync_cursors": member.sync_cursor_ids[0],
        "attention_items": member.attention_item_ids[0],
        "recommendations": member.recommendation_ids[0],
        "ai_runs": member.ai_run_ids[0],
        "pkos_evidence": member.resolution_evidence_ids[0],
    }
    return ids[resource_type]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def world() -> Iterator[GmailSyncWorld]:
    """One real-synced two-member world for the whole module (the sync is
    the expensive part). Refusal tests never mutate it; tests that do
    mutate (flag-off / non-personal success paths) each use their own
    rows."""
    with build_gmail_sync_world(extra_cleanup_tables=_EXTRA_CLEANUP_TABLES) as built:
        yield built


@pytest.fixture
def set_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    def _set(enabled: bool) -> None:
        monkeypatch.setenv(_FLAG, "true" if enabled else "false")
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def incident_id(world: GmailSyncWorld) -> UUID:
    return _create_incident(world, "a")


# --- helpers ------------------------------------------------------------------


def _create_incident(world: GmailSyncWorld, key: str) -> UUID:
    response = world.client(key).post(
        "/api/v1/engineering/incidents",
        json={"title": "Obligation", "severity": "high", "detected_at": _now_iso()},
        headers=world.headers(key, idempotency_key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _insert_github_connector(world: GmailSyncWorld) -> UUID:
    connector_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :ws, 'github', :external_account_id, 'Engineering',
                    ARRAY[]::text[], :credential, 'active', 1,
                    :user_id, :user_id, :now, :now, :user_id, 'workspace'
                )
                """
            ),
            {
                "id": connector_id,
                "ws": world.workspace_id,
                "external_account_id": f"gh-{connector_id}",
                "credential": b"x",
                "user_id": world.a.user_id,
                "now": now,
            },
        )
    return connector_id


def _grant(
    client: TestClient,
    headers: dict[str, str],
    *,
    resource_type: str,
    resource_id: UUID,
    grantee_account_id: UUID,
) -> Any:
    return client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "grantee_account_id": str(grantee_account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=headers,
    )


def _preview(
    client: TestClient,
    headers: dict[str, str],
    *,
    resource_type: str,
    resource_id: UUID,
    grantee_account_id: UUID,
) -> Any:
    return client.post(
        "/api/v1/sharing/grants/preview",
        json={
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "grantee_account_id": str(grantee_account_id),
            "actions": ["read"],
        },
        headers=headers,
    )


def _transfer(
    client: TestClient,
    headers: dict[str, str],
    *,
    resource_type: str,
    resource_id: UUID,
    to_account_id: UUID,
) -> Any:
    return client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": resource_type,
            "resource_id": str(resource_id),
            "to_account_id": str(to_account_id),
        },
        headers=headers,
    )


def _propose(
    client: TestClient,
    headers: dict[str, str],
    *,
    recipient_account_id: UUID,
    obligation_type: str,
    obligation_id: UUID,
    evidence: list[tuple[str, UUID]],
) -> Any:
    return client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(recipient_account_id),
            "obligation_type": obligation_type,
            "obligation_resource_id": str(obligation_id),
            "expected_outcome": "Handle it",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [
                {"resource_type": resource_type, "resource_id": str(resource_id)}
                for resource_type, resource_id in evidence
            ],
        },
        headers=headers,
    )


def _call_path(
    world: GmailSyncWorld,
    path: str,
    *,
    resource_type: str,
    resource_id: UUID,
    incident_id: UUID,
    caller: str = "a",
    target: str = "b",
) -> Any:
    """Drive one sharing path as member `caller`, naming member `target` as
    grantee / recipient / new owner."""
    client = world.client(caller)
    target_account = world.members[target].account_id
    if path == "grant":
        return _grant(
            client,
            world.headers(caller),
            resource_type=resource_type,
            resource_id=resource_id,
            grantee_account_id=target_account,
        )
    if path == "grant_preview":
        return _preview(
            client,
            world.headers(caller),
            resource_type=resource_type,
            resource_id=resource_id,
            grantee_account_id=target_account,
        )
    if path == "transfer":
        return _transfer(
            client,
            world.headers(caller),
            resource_type=resource_type,
            resource_id=resource_id,
            to_account_id=target_account,
        )
    assert path == "delegation_create"
    return _propose(
        client,
        world.headers(caller, idempotency_key=str(uuid4())),
        recipient_account_id=target_account,
        obligation_type="incidents",
        obligation_id=incident_id,
        evidence=[(resource_type, resource_id)],
    )


def _refusal_audits(workspace_id: UUID, resource_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT id, aggregate_type, authorization_result, failure_code, metadata "
                    "FROM audit_events WHERE workspace_id = :ws AND event_type = :event_type "
                    "AND aggregate_id = :id"
                ),
                {"ws": workspace_id, "event_type": _EVENT_TYPE, "id": resource_id},
            )
        )


def _all_refusal_audit_count(*workspace_ids: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE event_type = :event_type AND workspace_id = ANY(:ws)"
                ),
                {"event_type": _EVENT_TYPE, "ws": list(workspace_ids)},
            ).scalar_one()
        )


def _refusal_outbox_payloads(workspace_id: UUID) -> list[Any]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT payload FROM event_outbox "
                    "WHERE workspace_id = :ws AND event_type = :event_type"
                ),
                {"ws": workspace_id, "event_type": f"{_EVENT_TYPE}.v1"},
            )
        ]


def _counter(resource_type: str, path: str) -> float:
    return observability.personal_data_share_refused_total._values.get((resource_type, path), 0.0)


def _counter_total() -> float:
    return sum(observability.personal_data_share_refused_total._values.values())


def _scalar(sql: str, params: dict[str, Any]) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _grant_count(resource_id: UUID) -> int:
    return int(
        _scalar("SELECT count(*) FROM resource_grants WHERE resource_id = :id", {"id": resource_id})
    )


def _owner_and_visibility(resource_type: str, resource_id: UUID) -> tuple[UUID, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT owner_id, visibility FROM {resource_type} WHERE id = :id"),  # noqa: S608
            {"id": resource_id},
        ).one()
    return row[0], row[1]


def _delegation_count(workspace_id: UUID) -> int:
    return int(
        _scalar("SELECT count(*) FROM delegations WHERE workspace_id = :ws", {"ws": workspace_id})
    )


def _all_emails(world: GmailSyncWorld) -> set[str]:
    emails = {world.shared_correspondent_email}
    for mailbox in world.mailboxes.values():
        emails.add(mailbox.google_email)
        emails.update(mailbox.participant_emails)
    return emails


# --- S1.3: refusal matrix -----------------------------------------------------


def test_personal_rows_are_the_expected_personal_types(world: GmailSyncWorld) -> None:
    """Guards the matrix below: every id it uses really is a personal row
    of the named discriminator (so a 400 is the personal-data refusal, not
    an accident)."""
    a = world.a
    assert (
        _scalar(
            "SELECT provider FROM connector_accounts WHERE id = :id", {"id": a.connector_account_id}
        )
        == "gmail"
    )
    assert (
        _scalar(
            "SELECT entity_type FROM attention_items WHERE id = :id",
            {"id": a.attention_item_ids[0]},
        )
        == "email_thread"
    )
    assert (
        _scalar(
            "SELECT recommendation_type FROM recommendations WHERE id = :id",
            {"id": a.recommendation_ids[0]},
        )
        == "email_action_detected"
    )
    assert (
        _scalar("SELECT task_type FROM ai_runs WHERE id = :id", {"id": a.ai_run_ids[0]})
        == "email.detect_action"
    )
    assert (
        _scalar(
            "SELECT source_type FROM pkos_evidence WHERE id = :id",
            {"id": a.resolution_evidence_ids[0]},
        )
        == "gmail_sync"
    )


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("resource_type", _PERSONAL_TYPES)
def test_flag_on_refuses_sharing_personal_row(
    world: GmailSyncWorld,
    incident_id: UUID,
    set_isolation: Callable[[bool], None],
    resource_type: str,
    path: str,
) -> None:
    set_isolation(True)
    resource_id = _personal_id(world.a, resource_type)
    owner_before = _owner_and_visibility(resource_type, resource_id)
    grants_before = _grant_count(resource_id)
    delegations_before = _delegation_count(world.workspace_id)
    audits_before = len(_refusal_audits(world.workspace_id, resource_id))
    counter_before = _counter(resource_type, path)

    response = _call_path(
        world, path, resource_type=resource_type, resource_id=resource_id, incident_id=incident_id
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == _NOT_GRANTABLE
    assert _counter(resource_type, path) == counter_before + 1

    audits = _refusal_audits(world.workspace_id, resource_id)
    assert len(audits) == audits_before + 1
    _id, aggregate_type, authorization_result, failure_code, metadata = audits[-1]
    assert aggregate_type == resource_type
    assert authorization_result == "denied"
    assert failure_code == "personal_data"
    assert metadata == {"reason": "personal_data", "resource_type": resource_type}

    # The business transaction rolled back: nothing shared, moved, or proposed.
    assert _owner_and_visibility(resource_type, resource_id) == owner_before
    assert _grant_count(resource_id) == grants_before
    assert _delegation_count(world.workspace_id) == delegations_before


def test_refusal_audit_and_outbox_carry_no_emails(
    world: GmailSyncWorld, incident_id: UUID, set_isolation: Callable[[bool], None]
) -> None:
    set_isolation(True)
    response = _call_path(
        world,
        "grant",
        resource_type="connector_accounts",
        resource_id=world.a.connector_account_id,
        incident_id=incident_id,
    )
    assert response.status_code == 400, response.text

    audits = _refusal_audits(world.workspace_id, world.a.connector_account_id)
    payloads = _refusal_outbox_payloads(world.workspace_id)
    assert audits and payloads
    serialized = json.dumps([[str(v) for v in row] for row in audits] + payloads)
    assert "@" not in serialized
    for email in _all_emails(world):
        assert email not in serialized
    assert world.a.google_email not in response.text


def test_delegation_create_refuses_personal_obligation(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    """The obligation is always evidence too (accept grants it), so a
    personal obligation is refused like a personal evidence item."""
    set_isolation(True)
    before = _counter("connector_accounts", "delegation_create")
    response = _propose(
        world.client("a"),
        world.headers("a", idempotency_key=str(uuid4())),
        recipient_account_id=world.b.account_id,
        obligation_type="connector_accounts",
        obligation_id=world.a.connector_account_id,
        evidence=[],
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == _NOT_GRANTABLE
    assert _counter("connector_accounts", "delegation_create") == before + 1


def test_delegation_create_refused_when_any_evidence_item_is_personal(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    set_isolation(True)
    obligation = _create_incident(world, "a")
    other_incident = _create_incident(world, "a")
    delegations_before = _delegation_count(world.workspace_id)
    response = _propose(
        world.client("a"),
        world.headers("a", idempotency_key=str(uuid4())),
        recipient_account_id=world.b.account_id,
        obligation_type="incidents",
        obligation_id=obligation,
        evidence=[("incidents", other_incident), ("ai_runs", world.a.ai_run_ids[0])],
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == _NOT_GRANTABLE
    assert _delegation_count(world.workspace_id) == delegations_before
    # Audited against the personal item, not the obligation.
    assert _refusal_audits(world.workspace_id, world.a.ai_run_ids[0])
    assert not _refusal_audits(world.workspace_id, obligation)


# --- S1.3: delegation accept skips personal evidence --------------------------


def _delegation_grants(delegation_id: UUID) -> set[tuple[str, UUID]]:
    with engine.connect() as connection:
        return {
            (row[0], row[1])
            for row in connection.execute(
                text(
                    "SELECT resource_type, resource_id FROM resource_grants "
                    "WHERE delegation_id = :id AND revoked_at IS NULL"
                ),
                {"id": delegation_id},
            )
        }


def _propose_then_accept(
    world: GmailSyncWorld,
    set_isolation: Callable[[bool], None],
    *,
    personal: tuple[str, UUID],
    accept_with_flag: bool,
) -> tuple[UUID, UUID, UUID]:
    """A proposes (flag off, so the proposal is allowed) to B with one
    non-personal and one personal evidence item; B accepts with the flag
    set to `accept_with_flag`. Returns (delegation, obligation, evidence
    incident) ids."""
    set_isolation(False)
    obligation = _create_incident(world, "a")
    evidence_incident = _create_incident(world, "a")
    proposed = _propose(
        world.client("a"),
        world.headers("a", idempotency_key=str(uuid4())),
        recipient_account_id=world.b.account_id,
        obligation_type="incidents",
        obligation_id=obligation,
        evidence=[("incidents", evidence_incident), personal],
    )
    assert proposed.status_code == 201, proposed.text
    delegation_id = UUID(proposed.json()["id"])

    set_isolation(accept_with_flag)
    accepted = world.client("b").post(
        f"/api/v1/delegations/{delegation_id}/accept",
        headers=world.headers("b", idempotency_key=str(uuid4())),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    return delegation_id, obligation, evidence_incident


def test_flag_on_delegation_accept_skips_personal_evidence(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    # A's own row: the delegator must be able to read the evidence to
    # propose it (B's email attention items are not visible to A). Skipped
    # at accept, so the row is left untouched for the other tests.
    personal = ("attention_items", world.a.attention_item_ids[0])
    counter_before = _counter_total()
    audits_before = _all_refusal_audit_count(world.workspace_id)

    delegation_id, obligation, evidence_incident = _propose_then_accept(
        world, set_isolation, personal=personal, accept_with_flag=True
    )

    assert _delegation_grants(delegation_id) == {
        ("incidents", obligation),
        ("incidents", evidence_incident),
    }
    # Accept is not a refusing path: no 400, no refusal audit, no counter.
    assert _counter_total() == counter_before
    assert _all_refusal_audit_count(world.workspace_id) == audits_before


def test_flag_off_delegation_accept_grants_personal_evidence(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    personal = ("sync_cursors", world.b.sync_cursor_ids[0])
    delegation_id, obligation, evidence_incident = _propose_then_accept(
        world, set_isolation, personal=personal, accept_with_flag=False
    )
    assert _delegation_grants(delegation_id) == {
        ("incidents", obligation),
        ("incidents", evidence_incident),
        personal,
    }


# --- S1.3: non-personal rows unaffected --------------------------------------


def test_flag_on_non_personal_rows_still_shareable_and_transferable(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    set_isolation(True)
    counter_before = _counter_total()
    github_connector = _insert_github_connector(world)
    incident = _create_incident(world, "a")
    client, headers = world.client("a"), world.headers("a")

    preview = _preview(
        client,
        headers,
        resource_type="connector_accounts",
        resource_id=github_connector,
        grantee_account_id=world.b.account_id,
    )
    assert preview.status_code == 200, preview.text

    grant = _grant(
        client,
        headers,
        resource_type="incidents",
        resource_id=incident,
        grantee_account_id=world.b.account_id,
    )
    assert grant.status_code == 201, grant.text

    proposed = _propose(
        client,
        world.headers("a", idempotency_key=str(uuid4())),
        recipient_account_id=world.b.account_id,
        obligation_type="incidents",
        obligation_id=incident,
        evidence=[("connector_accounts", github_connector)],
    )
    assert proposed.status_code == 201, proposed.text

    transfer = _transfer(
        client,
        headers,
        resource_type="connector_accounts",
        resource_id=github_connector,
        to_account_id=world.b.account_id,
    )
    assert transfer.status_code == 201, transfer.text
    assert _owner_and_visibility("connector_accounts", github_connector)[0] == world.b.user_id

    assert _counter_total() == counter_before


# --- S1.3: flag off is today's behavior --------------------------------------


def test_flag_off_personal_rows_shareable_and_transferable_as_today(
    world: GmailSyncWorld, set_isolation: Callable[[bool], None]
) -> None:
    set_isolation(False)
    counter_before = _counter_total()
    audits_before = _all_refusal_audit_count(world.workspace_id)
    b = world.b
    client, headers = world.client("a"), world.headers("a")

    preview = _preview(
        client,
        headers,
        resource_type="sync_runs",
        resource_id=b.sync_run_ids[0],
        grantee_account_id=world.a.account_id,
    )
    assert preview.status_code == 200, preview.text

    grant = _grant(
        client,
        headers,
        resource_type="connector_accounts",
        resource_id=b.connector_account_id,
        grantee_account_id=world.a.account_id,
    )
    assert grant.status_code == 201, grant.text

    # Today an admin can move a member's Gmail-derived row to themselves --
    # the gap S1.3 closes when the flag is on.
    transfer = _transfer(
        client,
        headers,
        resource_type="ai_runs",
        resource_id=b.ai_run_ids[0],
        to_account_id=world.a.account_id,
    )
    assert transfer.status_code == 201, transfer.text
    assert _owner_and_visibility("ai_runs", b.ai_run_ids[0])[0] == world.a.user_id

    incident = _create_incident(world, "a")
    proposed = _propose(
        client,
        world.headers("a", idempotency_key=str(uuid4())),
        recipient_account_id=b.account_id,
        obligation_type="incidents",
        obligation_id=incident,
        evidence=[("recommendations", b.recommendation_ids[0])],
    )
    assert proposed.status_code == 201, proposed.text

    assert _counter_total() == counter_before
    assert _all_refusal_audit_count(world.workspace_id) == audits_before


# --- S1.3: cross-workspace ids stay a plain 404 -------------------------------


@pytest.fixture
def outsider(world: GmailSyncWorld) -> Iterator[tuple[TestClient, str, UUID, UUID]]:
    """An `owner` of a second, unrelated workspace (no Gmail), authenticated
    through the world's harness. Yields (client, session token, one of its
    own incidents, its workspace id)."""
    workspace_id, user_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Outsider', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        account_id = create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"outsider-{user_id}@example.test",
            now=now,
            role="owner",
        )
    try:
        client, token = world.harness.client_for(workspace_id, user_id)
        response = client.post(
            "/api/v1/engineering/incidents",
            json={"title": "Outsider", "severity": "high", "detected_at": _now_iso()},
            headers=csrf_headers(token, str(uuid4())),
        )
        assert response.status_code == 201, response.text
        yield client, token, UUID(response.json()["id"]), workspace_id
    finally:
        cleanup_workspace(workspace_id, [account_id], extra_cleanup_tables=_EXTRA_CLEANUP_TABLES)


@pytest.mark.parametrize("path", _PATHS)
def test_flag_on_cross_workspace_personal_id_is_plain_404(
    world: GmailSyncWorld,
    outsider: tuple[TestClient, str, UUID, UUID],
    set_isolation: Callable[[bool], None],
    path: str,
) -> None:
    """Another workspace's caller naming this workspace's Gmail row gets
    exactly the response a nonexistent id gets -- no 400, no refusal audit,
    no counter: the personal-data predicate never runs for a row outside
    the caller's workspace."""
    set_isolation(True)
    client, token, own_incident, outsider_workspace_id = outsider
    counter_before = _counter_total()
    audits_before = _all_refusal_audit_count(world.workspace_id, outsider_workspace_id)

    def call(resource_id: UUID) -> Any:
        if path == "grant":
            return _grant(
                client,
                csrf_headers(token),
                resource_type="connector_accounts",
                resource_id=resource_id,
                grantee_account_id=world.a.account_id,
            )
        if path == "grant_preview":
            return _preview(
                client,
                csrf_headers(token),
                resource_type="connector_accounts",
                resource_id=resource_id,
                grantee_account_id=world.a.account_id,
            )
        if path == "transfer":
            return _transfer(
                client,
                csrf_headers(token),
                resource_type="connector_accounts",
                resource_id=resource_id,
                to_account_id=world.a.account_id,
            )
        return _propose(
            client,
            csrf_headers(token, str(uuid4())),
            recipient_account_id=world.a.account_id,
            obligation_type="incidents",
            obligation_id=own_incident,
            evidence=[("connector_accounts", resource_id)],
        )

    foreign = call(world.a.connector_account_id)
    missing = call(uuid4())

    assert foreign.status_code == 404, foreign.text

    def shape(response: Any) -> tuple[int, str, str, Any]:
        error = response.json()["error"]
        return response.status_code, error["code"], error["message"], error["details"]

    assert shape(foreign) == shape(missing)
    assert _counter_total() == counter_before
    assert _all_refusal_audit_count(world.workspace_id, outsider_workspace_id) == audits_before
