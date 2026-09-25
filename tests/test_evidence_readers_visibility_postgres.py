"""Evidence readers honor visibility (Security Remediation Spec A S1.14, T21),
behind `ECC_PERSONAL_DATA_ISOLATION`.

With the flag on, Gmail `pkos_evidence` rows are written `private` to the
mailbox owner (T14b). The readers that select evidence without going through
the evidence endpoints must then leave out evidence the caller cannot read:

- `knowledge.get_entity` tool, meeting prep (`evidence_gaps`), retrieval
  (`evidence_state`, and the shared retrieval-document body built from
  claims): a visibility predicate;
- claims, relationships, risk reviews, commitments: citing an evidence id the
  caller cannot read is rejected exactly like an unknown id.

Member A (owner) and member B (member) each real-sync a mailbox
(`gmail_sync_fixtures`); both mailboxes share one correspondent, whose single
person node (owned by A, workspace-visible) carries A's resolution/detection
evidence and B's detection evidence. A `workspace`-visible, non-Gmail
evidence row on that node stays visible to both. Flag off: exactly today.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from gmail_sync_fixtures import GmailSyncWorld, build_gmail_sync_world
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.tools import ToolResult
from ecc.domains.knowledge import embeddings
from ecc.domains.knowledge.tools import get_entity_tool

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}
_FLAG_OFF = {"ECC_PERSONAL_DATA_ISOLATION": "false"}
# Rows these tests write outside the fixture's own cleanup set, children
# first (plan note N7).
_EXTRA_CLEANUP_TABLES = (
    "meeting_packs",
    "meeting_participants",
    "meetings",
    "risk_reviews",
    "timeline_entries",
    "embedding_projections",
    "retrieval_documents",
    "knowledge_claims",
    "pkos_edges",
)


@pytest.fixture
def world_on() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(env=_FLAG_ON, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES) as world:
        yield world


@pytest.fixture
def world_off() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(env=_FLAG_OFF, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES) as world:
        yield world


# --- helpers ----------------------------------------------------------------------


def _node_evidence(world: GmailSyncWorld, key: str) -> set[UUID]:
    """Member `key`'s Gmail evidence on the shared correspondent's node."""
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT id FROM pkos_evidence WHERE node_id = :node_id AND id = ANY(:ids)"),
            {"node_id": world.shared_person_node_id, "ids": list(world.members[key].evidence_ids)},
        ).all()
    ids = {row[0] for row in rows}
    assert ids, f"member {key} has no Gmail evidence on the shared node"
    return ids


def _insert_workspace_evidence(world: GmailSyncWorld, *, state: str = "available") -> UUID:
    """A non-Gmail, `workspace`-visible evidence row on the shared node,
    owned by A -- every member may read it with the flag on or off."""
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256,
                    captured_at, evidence_state, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :node_id, 'test', :source_ref, :sha256,
                    :captured_at, :state, :owner_id, 'workspace'
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": world.workspace_id,
                "node_id": world.shared_person_node_id,
                "source_ref": f"test:{evidence_id}",
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": datetime.now(UTC) - timedelta(days=1),
                "state": state,
                "owner_id": world.a.user_id,
            },
        )
    return evidence_id


def _set_evidence_state(ids: set[UUID], state: str, *, captured_at: datetime | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE pkos_evidence SET evidence_state = :state, "
                "captured_at = COALESCE(CAST(:captured_at AS timestamptz), captured_at) "
                "WHERE id = ANY(:ids)"
            ),
            {"state": state, "ids": list(ids), "captured_at": captured_at},
        )


def _auth(world: GmailSyncWorld, key: str) -> AuthContext:
    return AuthContext(
        workspace_id=world.workspace_id, user_id=world.members[key].user_id, timezone="UTC"
    )


def _tool_evidence(world: GmailSyncWorld, key: str) -> set[UUID]:
    with SessionFactory() as session:
        result = get_entity_tool(session, _auth(world, key), world.shared_person_node_id)
        session.rollback()
    assert isinstance(result, ToolResult), result
    return {UUID(item["id"]) for item in result.output["evidence"]}


def _post(world: GmailSyncWorld, key: str, path: str, body: dict[str, Any]) -> Any:
    return world.client(key).post(
        path, headers=world.headers(key, idempotency_key=str(uuid4())), json=body
    )


def _create_meeting(world: GmailSyncWorld, key: str) -> UUID:
    """A meeting owned by member `key` with the shared correspondent as its
    only participant (added through the production endpoint)."""
    meeting_id = uuid4()
    user_id = world.members[key].user_id
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    id, workspace_id, title, standalone_starts_at, standalone_ends_at,
                    standalone_timezone, status, agenda, created_by, updated_by,
                    created_at, updated_at, version, owner_id
                ) VALUES (
                    :id, :workspace_id, 'Contract sync', :starts_at, :ends_at, 'UTC',
                    'planned', 'Review the contract', :user_id, :user_id, :now, :now, 1, :user_id
                )
                """
            ),
            {
                "id": meeting_id,
                "workspace_id": world.workspace_id,
                "starts_at": now + timedelta(days=1),
                "ends_at": now + timedelta(days=1, hours=1),
                "user_id": user_id,
                "now": now,
            },
        )
    added = _post(
        world,
        key,
        f"/api/v1/meetings/{meeting_id}/participants",
        {"entity_id": str(world.shared_person_node_id)},
    )
    assert added.status_code == 201, added.text
    return meeting_id


def _prep_gap_ids(world: GmailSyncWorld, key: str) -> set[UUID]:
    meeting_id = _create_meeting(world, key)
    response = world.client(key).post(
        f"/api/v1/meetings/{meeting_id}/prep",
        headers=world.headers(key, idempotency_key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return {UUID(gap["id"]) for gap in response.json()["evidence_gaps"]}


def _node_name(node_id: UUID) -> str:
    with engine.begin() as connection:
        return str(
            connection.execute(
                text("SELECT canonical_name FROM pkos_nodes WHERE id = :id"), {"id": node_id}
            ).scalar_one()
        )


class _ConstantEmbeddingProvider:
    """Every text embeds to the same unit vector: any document with an
    embedding is a perfect semantic match for any query, so a query that
    matches nothing lexically reaches documents only through hybrid
    retrieval's semantic branch."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (embeddings.EMBEDDING_DIMENSIONS - 1) for _ in texts]


@pytest.fixture
def constant_embeddings() -> Iterator[None]:
    embeddings.set_provider_for_testing(_ConstantEmbeddingProvider())
    try:
        yield
    finally:
        embeddings.set_provider_for_testing(None)


def _retrieve_shared_node(
    world: GmailSyncWorld, key: str, *, semantic_only: bool = False, node_id: UUID | None = None
) -> dict[str, Any]:
    node_id = node_id or world.shared_person_node_id
    params: dict[str, Any] = {"q": _node_name(node_id), "limit": 100}
    if semantic_only:
        params = {"q": "zzqx unmatched wording", "limit": 100, "mode": "hybrid"}
    response = world.client(key).get("/api/v1/knowledge/retrieve", params=params)
    assert response.status_code == 200, response.text
    if semantic_only:
        assert response.json()["mode"] == "hybrid", response.json()
    items = [item for item in response.json()["items"] if item["entity_id"] == str(node_id)]
    assert len(items) == 1, response.json()
    return dict(items[0])


def _other_node(world: GmailSyncWorld, key: str) -> UUID:
    """Member `key`'s own-sender person node (not the shared one)."""
    member = world.members[key]
    return next(
        node_id
        for email, node_id in member.person_node_ids.items()
        if node_id != world.shared_person_node_id and email.startswith("only-")
    )


def _cite_claim(world: GmailSyncWorld, key: str, evidence_id: UUID) -> Any:
    return _post(
        world,
        key,
        f"/api/v1/knowledge/entities/{world.shared_person_node_id}/claims",
        {"predicate": "role", "value": {"title": "counsel"}, "source_id": str(evidence_id)},
    )


def _cite_relationship(world: GmailSyncWorld, key: str, evidence_id: UUID) -> Any:
    return _post(
        world,
        key,
        f"/api/v1/knowledge/entities/{world.shared_person_node_id}/relationships",
        {
            "relationship_type": "RELATES_TO",
            "to_entity_id": str(_other_node(world, key)),
            "evidence_id": str(evidence_id),
        },
    )


def _cite_risk_review(world: GmailSyncWorld, key: str, evidence_id: UUID) -> Any:
    risk = _post(
        world, key, "/api/v1/risks", {"description": "Contract risk", "probability": 3, "impact": 3}
    )
    assert risk.status_code == 201, risk.text
    return _post(
        world,
        key,
        f"/api/v1/risks/{risk.json()['id']}/review",
        {
            "expected_version": risk.json()["version"],
            "outcome": "no_change",
            "evidence_refs": [str(evidence_id)],
        },
    )


def _cite_commitment(world: GmailSyncWorld, key: str, evidence_id: UUID) -> Any:
    return _post(
        world,
        key,
        "/api/v1/commitments",
        {
            "summary": "Send the contract",
            "direction": "made_by_me",
            "evidence_id": str(evidence_id),
        },
    )


_CITERS = {
    "claims": (_cite_claim, 404, "EVIDENCE_NOT_FOUND"),
    "relationships": (_cite_relationship, 404, "EVIDENCE_NOT_FOUND"),
    "risk_reviews": (_cite_risk_review, 422, "EVIDENCE_UNAVAILABLE"),
    "commitments": (_cite_commitment, 404, "EVIDENCE_NOT_FOUND"),
}


def _error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


# --- visibility predicate readers ---------------------------------------------------


def test_entity_tool_excludes_other_members_gmail_evidence(world_on: GmailSyncWorld) -> None:
    a_ids, b_ids = _node_evidence(world_on, "a"), _node_evidence(world_on, "b")
    shared = _insert_workspace_evidence(world_on)

    assert _tool_evidence(world_on, "b") == b_ids | {shared}
    assert _tool_evidence(world_on, "a") == a_ids | {shared}


def test_entity_tool_flag_off_returns_every_evidence_row(world_off: GmailSyncWorld) -> None:
    a_ids, b_ids = _node_evidence(world_off, "a"), _node_evidence(world_off, "b")
    shared = _insert_workspace_evidence(world_off)

    assert _tool_evidence(world_off, "b") == a_ids | b_ids | {shared}


def test_meeting_prep_excludes_other_members_gmail_evidence(world_on: GmailSyncWorld) -> None:
    # Only non-`available` evidence surfaces in a pack (`evidence_gaps`).
    a_ids, b_ids = _node_evidence(world_on, "a"), _node_evidence(world_on, "b")
    _set_evidence_state(a_ids | b_ids, "missing")
    shared = _insert_workspace_evidence(world_on, state="missing")

    assert _prep_gap_ids(world_on, "b") == b_ids | {shared}
    assert _prep_gap_ids(world_on, "a") == a_ids | {shared}


def test_meeting_prep_flag_off_lists_every_evidence_gap(world_off: GmailSyncWorld) -> None:
    a_ids, b_ids = _node_evidence(world_off, "a"), _node_evidence(world_off, "b")
    _set_evidence_state(a_ids | b_ids, "missing")
    shared = _insert_workspace_evidence(world_off, state="missing")

    assert _prep_gap_ids(world_off, "b") == a_ids | b_ids | {shared}


def test_retrieval_body_and_evidence_state_exclude_private_evidence(
    world_on: GmailSyncWorld, constant_embeddings: None
) -> None:
    a_ids = _node_evidence(world_on, "a")
    b_ids = _node_evidence(world_on, "b")
    shared = _insert_workspace_evidence(world_on)
    # A records a claim backed by their own private Gmail evidence, and one
    # backed by workspace evidence; both refresh the node's retrieval document.
    private_claim = _post(
        world_on,
        "a",
        f"/api/v1/knowledge/entities/{world_on.shared_person_node_id}/claims",
        {"predicate": "mailsecret", "value": {"v": "fromgmail"}, "source_id": str(min(a_ids))},
    )
    assert private_claim.status_code == 201, private_claim.text
    public_claim = _post(
        world_on,
        "a",
        f"/api/v1/knowledge/entities/{world_on.shared_person_node_id}/claims",
        {"predicate": "publicfact", "value": {"v": "known"}, "source_id": str(shared)},
    )
    assert public_claim.status_code == 201, public_claim.text

    # The shared body carries the workspace-backed claim only -- for every
    # caller, since one projection serves every member who can read the node.
    for key in ("a", "b"):
        snippet = _retrieve_shared_node(world_on, key)["snippet"]
        assert "publicfact" in snippet
        assert "mailsecret" not in snippet and "fromgmail" not in snippet

    # `evidence_state` is the caller's latest *readable* evidence: A's
    # (newest) evidence going missing is visible to A, not to B.
    _set_evidence_state(a_ids, "missing", captured_at=datetime.now(UTC) + timedelta(minutes=5))
    _set_evidence_state(b_ids, "available", captured_at=datetime.now(UTC))
    for semantic_only in (False, True):
        a_item = _retrieve_shared_node(world_on, "a", semantic_only=semantic_only)
        b_item = _retrieve_shared_node(world_on, "b", semantic_only=semantic_only)
        assert a_item["evidence_state"] == "missing"
        assert b_item["evidence_state"] == "available"
    # The semantic-only path really was taken (no lexical factor at all).
    assert b_item["matching_mode"] == "semantic"

    # A node whose only evidence is A's private Gmail evidence: B sees no
    # evidence state at all (lexical candidates and the hybrid merge's own
    # fallback lookup alike), A sees theirs.
    a_only_node = _other_node(world_on, "a")
    with engine.begin() as connection:
        a_only_evidence = connection.execute(
            text("SELECT id FROM pkos_evidence WHERE node_id = :node_id ORDER BY id LIMIT 1"),
            {"node_id": a_only_node},
        ).scalar_one()
    claim = _post(
        world_on,
        "a",
        f"/api/v1/knowledge/entities/{a_only_node}/claims",
        {"predicate": "role", "value": {"v": "sender"}, "source_id": str(a_only_evidence)},
    )
    assert claim.status_code == 201, claim.text
    for semantic_only in (False, True):
        b_item = _retrieve_shared_node(
            world_on, "b", semantic_only=semantic_only, node_id=a_only_node
        )
        a_item = _retrieve_shared_node(
            world_on, "a", semantic_only=semantic_only, node_id=a_only_node
        )
        assert b_item["evidence_state"] == "unknown"
        assert a_item["evidence_state"] == "available"


def test_retrieval_flag_off_is_unchanged(
    world_off: GmailSyncWorld, constant_embeddings: None
) -> None:
    a_ids = _node_evidence(world_off, "a")
    claim = _post(
        world_off,
        "a",
        f"/api/v1/knowledge/entities/{world_off.shared_person_node_id}/claims",
        {"predicate": "mailsecret", "value": {"v": "fromgmail"}, "source_id": str(min(a_ids))},
    )
    assert claim.status_code == 201, claim.text
    assert "mailsecret" in _retrieve_shared_node(world_off, "b")["snippet"]

    _set_evidence_state(a_ids, "missing", captured_at=datetime.now(UTC) + timedelta(minutes=5))
    for semantic_only in (False, True):
        item = _retrieve_shared_node(world_off, "b", semantic_only=semantic_only)
        assert item["evidence_state"] == "missing"


# --- cited evidence ids ---------------------------------------------------------------


@pytest.mark.parametrize("endpoint", sorted(_CITERS))
def test_citing_unreadable_evidence_is_rejected_as_not_found(
    world_on: GmailSyncWorld, endpoint: str
) -> None:
    cite, status_code, code = _CITERS[endpoint]
    a_evidence = min(_node_evidence(world_on, "a"))

    rejected = cite(world_on, "b", a_evidence)
    assert (rejected.status_code, _error_code(rejected)) == (status_code, code), rejected.text
    # Indistinguishable from an id that does not exist at all.
    unknown = cite(world_on, "b", uuid4())
    assert (unknown.status_code, _error_code(unknown)) == (status_code, code), unknown.text

    # The owner is unaffected, and workspace evidence stays citable by B.
    owner = cite(world_on, "a", a_evidence)
    assert owner.status_code == 201, owner.text
    shared = cite(world_on, "b", _insert_workspace_evidence(world_on))
    assert shared.status_code == 201, shared.text


@pytest.mark.parametrize("endpoint", sorted(_CITERS))
def test_citing_other_members_evidence_flag_off_is_unchanged(
    world_off: GmailSyncWorld, endpoint: str
) -> None:
    cite, _, _ = _CITERS[endpoint]
    accepted = cite(world_off, "b", min(_node_evidence(world_off, "a")))
    assert accepted.status_code == 201, accepted.text
