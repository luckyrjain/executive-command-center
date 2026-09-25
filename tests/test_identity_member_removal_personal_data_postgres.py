"""Security Remediation Spec A S1.4 (T12): member removal is safe for
personal (Gmail-derived) data, behind `ECC_PERSONAL_DATA_ISOLATION`.

Built on the real-sync fixture (`tests/gmail_sync_fixtures.py`): member A
(`owner`, the workspace's original user) and member B (`member`) each have
a really-synced Gmail mailbox. After the sync B owns exactly B's personal
rows (gmail `connector_accounts`, its `sync_runs`/`sync_cursors`,
`email_thread` attention items, `email_action_detected` recommendations,
`email.detect_action` `ai_runs`) plus two Gmail-only person nodes (B's own
address and B's exclusive correspondent; the shared correspondent's node is
A's, who synced first). Setup here adds what the world lacks: more owners
(the re-own target rule) and mixed-source person nodes (a non-`gmail_sync`
evidence row next to the Gmail one).

Mixed-source nodes: the spec (S1.4(a,c)) excludes and re-owns only
*Gmail-only* person nodes; a mixed-source node is ordinary shared knowledge
the member owns, so it keeps blocking removal (409 `OWNED_RESOURCES_BLOCK_
REMOVAL`, nothing changed) until transferred, and removal never re-owns it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from gmail_sync_fixtures import (
    FakeGoogle,
    FakeMailbox,
    GmailSyncWorld,
    build_gmail_sync_world,
    cleanup_workspace,
    csrf_headers,
)
from identity_fixtures import create_identity
from sqlalchemy import text

import ecc.domains.identity.membership_removal as membership_removal_module
import ecc.domains.personal.gmail_revocation as gmail_revocation_module
from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.connectors import ConnectorAccountContext

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}
_EXTRA_CLEANUP_TABLES = ("ownership_transfers",)


# --- world ---------------------------------------------------------------------


@dataclass
class RemovalWorld:
    """The real-sync world plus identities this module adds to it (their
    `accounts` rows are deleted after the world's own cleanup)."""

    w: GmailSyncWorld
    extra_account_ids: list[UUID] = field(default_factory=list)

    @property
    def ws(self) -> UUID:
        return self.w.workspace_id

    @property
    def fake_google(self) -> FakeGoogle:
        return self.w.fake_google

    def add_member(
        self, *, role: str, joined_at: datetime, status: str = "active", email: str | None = None
    ) -> UUID:
        user_id = uuid4()
        with engine.begin() as connection:
            self.extra_account_ids.append(
                create_identity(
                    connection,
                    workspace_id=self.ws,
                    user_id=user_id,
                    email=email or f"t12-{user_id}@example.test",
                    now=joined_at,
                    role=role,
                )
            )
            if status != "active":
                connection.execute(
                    text(
                        "UPDATE workspace_memberships SET status = :status, removed_at = :at "
                        "WHERE workspace_id = :ws AND users_id = :uid"
                    ),
                    {"status": status, "at": joined_at, "ws": self.ws, "uid": user_id},
                )
        return user_id

    def remove(self, *, actor: UUID, target: UUID) -> httpx.Response:
        client, token = self.w.harness.client_for(self.ws, actor)
        return client.delete(
            f"/api/v1/identity/workspaces/{self.ws}/members/{target}",
            headers=csrf_headers(token),
        )

    def b_gmail_only_nodes(self) -> list[UUID]:
        return sorted(
            node_id
            for email, node_id in self.w.b.person_node_ids.items()
            if email != self.w.shared_correspondent_email
        )


@contextmanager
def _removal_world(env: dict[str, str] | None) -> Iterator[RemovalWorld]:
    world: RemovalWorld | None = None
    try:
        with build_gmail_sync_world(env=env) as built:
            world = RemovalWorld(w=built)
            try:
                yield world
            finally:
                # Rows this module writes outside the fixture's own cleanup
                # set (plan note N7), deleted before that cleanup runs.
                with engine.begin() as connection:
                    for table in _EXTRA_CLEANUP_TABLES:
                        connection.execute(
                            text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                            {"ws": built.workspace_id},
                        )
    finally:
        if world is not None and world.extra_account_ids:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM accounts WHERE id = ANY(:ids)"),
                    {"ids": world.extra_account_ids},
                )


@pytest.fixture
def world_on() -> Iterator[RemovalWorld]:
    with _removal_world(_FLAG_ON) as world:
        yield world


@pytest.fixture
def world_off() -> Iterator[RemovalWorld]:
    with _removal_world(None) as world:
        yield world


# --- small readers ---------------------------------------------------------------


def _blocking_nodes(count: int) -> list[dict[str, object]]:
    """`owned_resources` when `count` person nodes the member still owns
    block removal: with the flag on, each node's Gmail-derived
    `entity_aliases` row is owned by the mailbox owner too (T14b, plan note
    N11) and blocks alongside its node."""
    return [
        {"resource_type": "entity_aliases", "count": count},
        {"resource_type": "pkos_nodes", "count": count},
    ]


def _scalar(sql: str, **params: Any) -> Any:
    with engine.begin() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _connector(connector_id: UUID) -> Any:
    with engine.begin() as connection:
        return connection.execute(
            text(
                "SELECT status, disconnected_at, updated_by, version, owner_id "
                "FROM connector_accounts WHERE id = :id"
            ),
            {"id": connector_id},
        ).one()


def _membership_status(ws: UUID, users_id: UUID) -> str:
    return str(
        _scalar(
            "SELECT status FROM workspace_memberships WHERE workspace_id = :ws AND users_id = :u",
            ws=ws,
            u=users_id,
        )
    )


def _owners(table: str, ids: Sequence[UUID]) -> dict[UUID, UUID]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT id, owner_id FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
            {"ids": list(ids)},
        ).all()
    return {row[0]: row[1] for row in rows}


def _node_versions(ids: Sequence[UUID]) -> dict[UUID, tuple[UUID, int]]:
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT id, owner_id, version FROM pkos_nodes WHERE id = ANY(:ids)"),
            {"ids": list(ids)},
        ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def _audits(ws: UUID, event_type: str) -> list[Any]:
    with engine.begin() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, aggregate_version, actor_id, metadata "
                    "FROM audit_events WHERE workspace_id = :ws AND event_type = :et "
                    "ORDER BY aggregate_id"
                ),
                {"ws": ws, "et": event_type},
            ).all()
        )


def _outbox_payloads(ws: UUID, event_type: str) -> list[dict[str, Any]]:
    with engine.begin() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT payload FROM event_outbox WHERE workspace_id = :ws AND event_type = :et"
                ),
                {"ws": ws, "et": f"{event_type}.v1"},
            )
        ]


def _add_manual_evidence(ws: UUID, node_id: UUID) -> UUID:
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :ws, :node_id, 'manual', "
                "'t12-manual-ref', :sha256, :now)"
            ),
            {
                "id": evidence_id,
                "ws": ws,
                "node_id": node_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "now": datetime.now(UTC),
            },
        )
    return evidence_id


def _revoke_count(result: str) -> float:
    return observability.connector_revoke_total._values.get(("gmail", "removal", result), 0.0)


def _personal_row_owners(world: RemovalWorld) -> dict[str, dict[UUID, UUID]]:
    b = world.w.b
    return {
        "sync_runs": _owners("sync_runs", b.sync_run_ids),
        "sync_cursors": _owners("sync_cursors", b.sync_cursor_ids),
        "attention_items": _owners("attention_items", b.attention_item_ids),
        "recommendations": _owners("recommendations", b.recommendation_ids),
        "ai_runs": _owners("ai_runs", b.ai_run_ids),
        "email_threads": _owners("email_threads", b.email_thread_ids),
        "email_messages": _owners("email_messages", list(b.email_message_ids.values())),
        "pkos_evidence": _owners("pkos_evidence", b.evidence_ids),
    }


# --- flag off ------------------------------------------------------------------------


def test_flag_off_personal_rows_still_block_removal(world_off: RemovalWorld) -> None:
    world = world_off
    a, b = world.w.a, world.w.b

    response = world.remove(actor=a.user_id, target=b.user_id)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert {r["resource_type"] for r in error["details"]["owned_resources"]} == {
        "ai_runs",
        "attention_items",
        "connector_accounts",
        "pkos_nodes",
        "recommendations",
        "sync_cursors",
        "sync_runs",
    }
    assert _connector(b.connector_account_id).status == "active"
    assert _membership_status(world.ws, b.user_id) == "active"
    assert world.fake_google.revoked_tokens == []


# --- flag on: removable ------------------------------------------------------------


def test_member_with_only_disconnected_gmail_and_personal_rows_is_removable(
    world_on: RemovalWorld,
) -> None:
    world = world_on
    a, b = world.w.a, world.w.b
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET status = 'disconnected', disconnected_at = :now "
                "WHERE id = :id"
            ),
            {"now": datetime.now(UTC), "id": b.connector_account_id},
        )
    before_connector = _connector(b.connector_account_id)
    before_rows = _personal_row_owners(world)

    response = world.remove(actor=a.user_id, target=b.user_id)

    assert response.status_code == 200, response.text
    assert _membership_status(world.ws, b.user_id) == "removed"
    # Already disconnected: not touched again, nothing revoked.
    assert _connector(b.connector_account_id) == before_connector
    assert _audits(world.ws, "connector_account.disabled") == []
    assert world.fake_google.revoked_tokens == []
    # Personal rows retained exactly as they were (DS2: no purge).
    assert _personal_row_owners(world) == before_rows


class _CommitObservingAdapter:
    """Wraps the fixture's fake-transport `GmailAdapter`; records, at the
    moment each revoke happens, what a *separate* connection sees -- i.e.
    only committed state."""

    def __init__(self, inner: Any, workspace_id: UUID, removed_users_id: UUID) -> None:
        self._inner = inner
        self._ws = workspace_id
        self._removed = removed_users_id
        self.observed: list[tuple[UUID, str, str]] = []

    def disconnect(self, account: ConnectorAccountContext) -> None:
        connector_status = str(
            _scalar(
                "SELECT status FROM connector_accounts WHERE id = :id",
                id=account.connector_account_id,
            )
        )
        membership = _membership_status(self._ws, self._removed)
        self.observed.append((account.connector_account_id, connector_status, membership))
        self._inner.disconnect(account)


def test_active_gmail_disconnected_audited_and_revoked_after_commit(
    world_on: RemovalWorld,
) -> None:
    world = world_on
    a, b = world.w.a, world.w.b
    before = _connector(b.connector_account_id)
    before_rows = _personal_row_owners(world)
    a_before = _connector(a.connector_account_id)
    ok_before = _revoke_count("ok")

    with pytest.MonkeyPatch.context() as mp:
        spy = _CommitObservingAdapter(gmail_revocation_module._adapter, world.ws, b.user_id)
        mp.setattr(gmail_revocation_module, "_adapter", spy)
        response = world.remove(actor=a.user_id, target=b.user_id)

    assert response.status_code == 200, response.text
    after = _connector(b.connector_account_id)
    assert after.status == "disconnected"
    assert after.disconnected_at is not None
    assert after.updated_by == a.user_id
    assert after.version == before.version + 1
    assert after.owner_id == b.user_id

    audits = _audits(world.ws, "connector_account.disabled")
    assert [(r.aggregate_type, r.aggregate_id, r.aggregate_version) for r in audits] == [
        ("connector_account", b.connector_account_id, after.version)
    ]
    assert audits[0].actor_id == a.user_id
    assert audits[0].metadata == {"reason": "member_removed"}
    payloads = _outbox_payloads(world.ws, "connector_account.disabled")
    assert [p.get("reason") for p in payloads] == ["member_removed"]

    # Revoked B's grant only, and only once the removal was committed.
    assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("b")]
    assert spy.observed == [(b.connector_account_id, "disconnected", "removed")]
    assert _revoke_count("ok") == ok_before + 1

    # The remover's own Gmail is untouched.
    assert _connector(a.connector_account_id) == a_before
    # Every other personal row retained unchanged; consent left granted.
    assert _personal_row_owners(world) == before_rows
    assert (
        _scalar(
            "SELECT count(*) FROM domain_consents WHERE workspace_id = :ws AND owner_id = :u "
            "AND domain_key = 'email' AND revoked_at IS NULL",
            ws=world.ws,
            u=b.user_id,
        )
        == 1
    )


@contextmanager
def _live_row_in_other_workspace(google_email: str) -> Iterator[None]:
    """Another workspace holding a live gmail row for the same Google
    account (the grant-wide revoke hazard `revoke_is_safe` guards)."""
    ws, user_id, now = uuid4(), uuid4(), datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'T12 other workspace', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        account_id = create_identity(
            connection,
            workspace_id=ws,
            user_id=user_id,
            email=f"t12-other-{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :ws, 'gmail', :ext, 'T12 other', ARRAY[]::text[], :cred,
                    'active', 1, :u, :u, :now, :now, :u, 'workspace'
                )
                """
            ),
            {"id": uuid4(), "ws": ws, "ext": google_email, "cred": b"x", "u": user_id, "now": now},
        )
    try:
        yield
    finally:
        cleanup_workspace(ws, [account_id])


@pytest.mark.parametrize(("scope", "revoked"), [("global", False), ("none", True)])
def test_revoke_follows_revoke_safety_when_another_workspace_uses_the_account(
    world_on: RemovalWorld, scope: str, revoked: bool
) -> None:
    world = world_on
    a, b = world.w.a, world.w.b
    skipped_before = _revoke_count("skipped_unsafe")

    with _live_row_in_other_workspace(b.google_email), pytest.MonkeyPatch.context() as mp:
        mp.setenv("ECC_GMAIL_REVOKE_SCOPE", scope)
        get_settings.cache_clear()
        try:
            response = world.remove(actor=a.user_id, target=b.user_id)
        finally:
            mp.undo()
            get_settings.cache_clear()

    assert response.status_code == 200, response.text
    assert _connector(b.connector_account_id).status == "disconnected"
    if revoked:
        assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("b")]
        assert _revoke_count("skipped_unsafe") == skipped_before
    else:
        assert world.fake_google.revoked_tokens == []
        assert _revoke_count("skipped_unsafe") == skipped_before + 1


# --- flag on: Gmail-only person nodes ------------------------------------------------


@pytest.mark.parametrize("actor", ["self", "later_owner"])
def test_gmail_only_nodes_reowned_to_earliest_other_active_owner(
    world_on: RemovalWorld, actor: str
) -> None:
    """B is promoted to owner and backdated to be the earliest-joined owner.
    Candidates: D (owner, earlier, but removed), E (admin, earlier), B
    (the removed member), A (owner), C (owner, joined later). The target is
    A in both the self-removal and the C-removes-B case -- never the
    removed member, never the actor as such."""
    world = world_on
    a, b = world.w.a, world.w.b
    base = _scalar(
        "SELECT created_at FROM workspace_memberships WHERE workspace_id = :ws AND users_id = :u",
        ws=world.ws,
        u=a.user_id,
    )
    world.add_member(role="owner", joined_at=base - timedelta(hours=3), status="removed")
    world.add_member(role="admin", joined_at=base - timedelta(hours=2))
    c = world.add_member(role="owner", joined_at=base + timedelta(hours=1))
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET role = 'owner', created_at = :at "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"at": base - timedelta(hours=1), "ws": world.ws, "u": b.user_id},
        )
    nodes = world.b_gmail_only_nodes()
    assert len(nodes) == 2
    before = _node_versions(nodes)
    assert {owner for owner, _ in before.values()} == {b.user_id}
    shared_before = _node_versions([world.w.shared_person_node_id])
    actor_id = b.user_id if actor == "self" else c

    response = world.remove(actor=actor_id, target=b.user_id)

    assert response.status_code == 200, response.text
    after = _node_versions(nodes)
    assert after == {n: (a.user_id, before[n][1] + 1) for n in nodes}
    audits = _audits(world.ws, "pkos_node.ownership_reassigned")
    assert [(r.aggregate_type, r.aggregate_id, r.aggregate_version) for r in audits] == [
        ("pkos_node", n, after[n][1]) for n in nodes
    ]
    assert {r.actor_id for r in audits} == {actor_id}
    assert all(r.metadata == {"reason": "member_removed"} for r in audits)
    payloads = _outbox_payloads(world.ws, "pkos_node.ownership_reassigned")
    assert sorted((p["aggregate_id"], p["reason"], p["to_owner_id"]) for p in payloads) == sorted(
        (str(n), "member_removed", str(a.user_id)) for n in nodes
    )
    # A's own (shared-correspondent) node is not touched.
    assert _node_versions([world.w.shared_person_node_id]) == shared_before


def test_mixed_source_node_blocks_removal_until_transferred(world_on: RemovalWorld) -> None:
    world = world_on
    a, b = world.w.a, world.w.b
    mixed, gmail_only = world.b_gmail_only_nodes()
    _add_manual_evidence(world.ws, mixed)
    # A mixed-source node owned by someone else stays untouched throughout.
    _add_manual_evidence(world.ws, world.w.shared_person_node_id)
    shared_before = _node_versions([world.w.shared_person_node_id])
    nodes_before = _node_versions([mixed, gmail_only])

    blocked = world.remove(actor=a.user_id, target=b.user_id)

    assert blocked.status_code == 409, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert error["details"]["owned_resources"] == _blocking_nodes(1)
    # Refused atomically: nothing disconnected, re-owned or revoked.
    assert _connector(b.connector_account_id).status == "active"
    assert _membership_status(world.ws, b.user_id) == "active"
    assert _node_versions([mixed, gmail_only]) == nodes_before
    assert world.fake_google.revoked_tokens == []

    client, token = world.w.harness.client_for(world.ws, a.user_id)
    transfer = client.post(
        "/api/v1/ownership/transfers",
        headers=csrf_headers(token),
        json={
            "resource_type": "pkos_nodes",
            "resource_id": str(mixed),
            "to_account_id": str(a.account_id),
        },
    )
    assert transfer.status_code == 201, transfer.text
    mixed_after_transfer = _node_versions([mixed])

    removed = world.remove(actor=a.user_id, target=b.user_id)

    assert removed.status_code == 200, removed.text
    assert _node_versions([mixed]) == mixed_after_transfer
    assert _node_versions([gmail_only])[gmail_only][0] == a.user_id
    assert [r.aggregate_id for r in _audits(world.ws, "pkos_node.ownership_reassigned")] == [
        gmail_only
    ]
    assert _node_versions([world.w.shared_person_node_id]) == shared_before


def test_gmail_only_nodes_keep_blocking_when_no_other_active_owner(
    world_on: RemovalWorld,
) -> None:
    """Anomalous membership data (no active owner at all; the API itself
    never allows removing/demoting the last owner): there is nobody to
    re-own Gmail-only nodes to, so they block exactly as before."""
    world = world_on
    a, b = world.w.a, world.w.b
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET role = 'admin' "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"ws": world.ws, "u": a.user_id},
        )

    response = world.remove(actor=a.user_id, target=b.user_id)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert error["details"]["owned_resources"] == _blocking_nodes(2)
    assert _connector(b.connector_account_id).status == "active"
    assert _membership_status(world.ws, b.user_id) == "active"
    assert world.fake_google.revoked_tokens == []


def test_node_turning_gmail_only_after_reown_selection_still_blocks(
    world_on: RemovalWorld,
) -> None:
    """Regression (review A1): a node that is mixed-source when removal
    selects Gmail-only nodes to re-own, but whose non-Gmail evidence is
    deleted by a concurrent commit right after, is neither re-owned nor
    silently excluded: the owned check (run after the re-own, with
    `pkos_nodes` never excluded) sees it and the whole removal rolls
    back."""
    world = world_on
    a, b = world.w.a, world.w.b
    racing, gmail_only = world.b_gmail_only_nodes()
    manual_evidence = _add_manual_evidence(world.ws, racing)
    nodes_before = _node_versions([racing, gmail_only])
    original = membership_removal_module._gmail_only_person_nodes

    def select_then_concurrent_commit(session: Any, **kwargs: Any) -> list[UUID]:
        selected: list[UUID] = original(session, **kwargs)
        with engine.begin() as other:  # a separate, concurrently committing txn
            other.execute(text("DELETE FROM pkos_evidence WHERE id = :id"), {"id": manual_evidence})
        return selected

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            membership_removal_module, "_gmail_only_person_nodes", select_then_concurrent_commit
        )
        response = world.remove(actor=a.user_id, target=b.user_id)

    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert error["details"]["owned_resources"] == _blocking_nodes(1)
    # Everything done before the check (disconnect, re-own, audits) rolled back.
    assert _node_versions([racing, gmail_only]) == nodes_before
    assert _connector(b.connector_account_id).status == "active"
    assert _membership_status(world.ws, b.user_id) == "active"
    assert _audits(world.ws, "connector_account.disabled") == []
    assert _audits(world.ws, "pkos_node.ownership_reassigned") == []
    assert world.fake_google.revoked_tokens == []


def test_admin_removes_member_and_admin_gmail_is_untouched(world_on: RemovalWorld) -> None:
    world = world_on
    b = world.w.b
    base = _scalar(
        "SELECT created_at FROM workspace_memberships WHERE workspace_id = :ws AND users_id = :u",
        ws=world.ws,
        u=world.w.a.user_id,
    )
    # The Gmail OAuth start requires the caller's own account email to be
    # allowlisted, so the admin's account email is its Google address.
    admin_email = f"t12-admin-gmail-{uuid4().hex[:8]}@example.test"
    admin = world.add_member(role="admin", joined_at=base + timedelta(hours=1), email=admin_email)
    admin_connector = world.w.harness.connect_and_sync(
        workspace_id=world.ws,
        user_id=admin,
        key="admin",
        mailbox=FakeMailbox(google_email=admin_email, messages=()),
    )
    admin_before = _connector(admin_connector)
    assert admin_before.status == "active"

    response = world.remove(actor=admin, target=b.user_id)

    assert response.status_code == 200, response.text
    assert _connector(admin_connector) == admin_before
    assert _connector(b.connector_account_id).status == "disconnected"
    assert _connector(b.connector_account_id).updated_by == admin
    assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("b")]
    # Re-owned to the earliest other active owner (A), not the admin actor.
    assert {owner for owner, _ in _node_versions(world.b_gmail_only_nodes()).values()} == {
        world.w.a.user_id
    }


def test_concurrent_non_gmail_evidence_insert_during_reown_blocks_removal(
    world_on: RemovalWorld,
) -> None:
    """Regression (review F1): a second connection inserts non-Gmail
    evidence for one of B's Gmail-only nodes and holds it uncommitted (its FK
    check holds FOR KEY SHARE on the node). Removal's `SELECT ... FOR UPDATE`
    of Gmail-only nodes waits on it; the insert then commits. The node now
    has non-Gmail evidence, so the re-own (which re-checks the predicate in a
    fresh statement) must skip it, and the owned check must block: 409, the
    node still owned by B, no reassignment audit, everything rolled back."""
    world = world_on
    a, b = world.w.a, world.w.b
    racing, other = world.b_gmail_only_nodes()
    nodes_before = _node_versions([racing, other])
    result: dict[str, httpx.Response] = {}
    inserter = engine.connect()
    try:
        evidence_id = uuid4()
        inserter.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :ws, :node_id, 'manual', "
                "'t12-race-ref', :sha256, :now)"
            ),
            {
                "id": evidence_id,
                "ws": world.ws,
                "node_id": racing,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "now": datetime.now(UTC),
            },
        )  # uncommitted: holds FOR KEY SHARE on `racing`
        inserter_pid = inserter.execute(text("SELECT pg_backend_pid()")).scalar_one()

        def remove() -> None:
            result["response"] = world.remove(actor=a.user_id, target=b.user_id)

        worker = threading.Thread(target=remove)
        worker.start()
        waiting = False
        deadline = time.monotonic() + 10
        with engine.connect() as probe:
            while time.monotonic() < deadline and not waiting and worker.is_alive():
                waiting = bool(
                    probe.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                            "AND :blocker = ANY(pg_blocking_pids(pid)))"
                        ),
                        {"blocker": inserter_pid},
                    ).scalar_one()
                )
                probe.rollback()
                if not waiting:
                    time.sleep(0.02)
        assert waiting, "removal never waited on the uncommitted evidence insert"
        inserter.commit()
        worker.join(timeout=15)
        assert not worker.is_alive()
    finally:
        inserter.rollback()
        inserter.close()

    response = result["response"]
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert error["details"]["owned_resources"] == _blocking_nodes(1)
    assert _node_versions([racing, other]) == nodes_before
    assert _audits(world.ws, "pkos_node.ownership_reassigned") == []
    assert _audits(world.ws, "connector_account.disabled") == []
    assert _connector(b.connector_account_id).status == "active"
    assert _membership_status(world.ws, b.user_id) == "active"
    assert world.fake_google.revoked_tokens == []


def test_undecryptable_credential_still_disconnects_and_is_reported(
    world_on: RemovalWorld, caplog: pytest.LogCaptureFixture
) -> None:
    """Review F2: a credential that cannot be decrypted never blocks the
    disconnect, but the unrevokable grant is logged (exception class only)
    and counted as a revoke `error` -- after commit, never on a 409."""
    world = world_on
    a, b = world.w.a, world.w.b
    garbage = b"t12-not-a-fernet-token"
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE connector_accounts SET encrypted_credentials = :c WHERE id = :id"),
            {"c": garbage, "id": b.connector_account_id},
        )
    errors_before = _revoke_count("error")

    with caplog.at_level(logging.WARNING, logger="ecc.domains.identity.membership_removal"):
        response = world.remove(actor=a.user_id, target=b.user_id)

    assert response.status_code == 200, response.text
    assert _connector(b.connector_account_id).status == "disconnected"
    assert world.fake_google.revoked_tokens == []
    assert _revoke_count("error") == errors_before + 1
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("removal_revoke_credential_unavailable")
    ]
    assert len(messages) == 1
    assert "error_class=" in messages[0]
    assert garbage.decode() not in messages[0]
    assert b.google_email not in messages[0]
