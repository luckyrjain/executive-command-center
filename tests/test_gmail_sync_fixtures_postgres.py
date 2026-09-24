"""Self-test for `tests/gmail_sync_fixtures.py` (Security Remediation Spec
A, T13): the real-sync fixture produces every Gmail-derived artifact type,
through the production path, owned as production owns it today, for two
members of one workspace -- plus the shared-correspondent and colliding-
`external_message_id` shapes later tasks (T12, T14a/b, T15, T21) rely on.

Ownership expectations here are the flag-off baseline (see the fixture
module docstring): `pkos_evidence`/`entity_aliases` rows are owned by the
workspace's original user (member A) regardless of mailbox, because their
INSERTs leave `owner_id` to the `..._from_workspace_original_user` trigger.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from gmail_sync_fixtures import (  # noqa: F401 -- fixtures used by name
    _GMAIL_ADAPTER_MODULES,
    FakeGoogle,
    GmailSyncWorld,
    GmailSyncWorldFactory,
    gmail_sync_harness,
    gmail_sync_world,
    gmail_sync_world_factory,
)
from sqlalchemy import text

import ecc.domains.ai_runtime.runtime as runtime_module
import ecc.domains.engineering.connector_accounts as connector_accounts_module
from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _owners(table: str, ids: Sequence[UUID]) -> dict[UUID, UUID]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(f"SELECT id, owner_id FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
            {"ids": list(ids)},
        ).all()
    return {row[0]: row[1] for row in rows}


def _assert_owned_by(table: str, ids: Sequence[UUID], owner_id: UUID) -> None:
    assert ids, f"no {table} rows collected"
    owners = _owners(table, ids)
    assert set(owners) == set(ids), f"{table}: some collected ids missing from the table"
    assert set(owners.values()) == {owner_id}, f"{table}: unexpected owners {owners}"


def test_every_artifact_type_exists_with_expected_owner_per_member(
    gmail_sync_world: GmailSyncWorld,  # noqa: F811
) -> None:
    world = gmail_sync_world
    workspace_original_user = world.a.user_id

    for key, member in world.members.items():
        mailbox = world.mailboxes[key]
        inbound = len(mailbox.messages)

        # Connector account: the gmail row for this member's own mailbox.
        with engine.begin() as connection:
            connector = connection.execute(
                text(
                    "SELECT provider, external_account_id, owner_id, created_by, status "
                    "FROM connector_accounts WHERE id = :id"
                ),
                {"id": member.connector_account_id},
            ).one()
        assert connector.provider == "gmail"
        assert connector.external_account_id == member.google_email
        assert connector.owner_id == member.user_id
        assert connector.created_by == member.user_id
        assert connector.status == "active"

        _assert_owned_by("sync_runs", member.sync_run_ids, member.user_id)
        _assert_owned_by("sync_cursors", member.sync_cursor_ids, member.user_id)
        _assert_owned_by("email_threads", member.email_thread_ids, member.user_id)
        assert set(member.email_message_ids) == {m.external_message_id for m in mailbox.messages}
        _assert_owned_by("email_messages", list(member.email_message_ids.values()), member.user_id)

        # One attention item, recommendation and ai_run per inbound thread/message.
        assert len(member.attention_item_ids) == len(member.email_thread_ids) == inbound
        _assert_owned_by("attention_items", member.attention_item_ids, member.user_id)
        assert len(member.recommendation_ids) == inbound
        _assert_owned_by("recommendations", member.recommendation_ids, member.user_id)
        assert len(member.ai_run_ids) == inbound
        _assert_owned_by("ai_runs", member.ai_run_ids, member.user_id)

        # Participants: every sender plus the mailbox owner's own address.
        assert set(member.person_node_ids) == mailbox.participant_emails
        assert set(member.entity_alias_ids) == mailbox.participant_emails
        exclusive_nodes = [
            node_id
            for email, node_id in member.person_node_ids.items()
            if email != world.shared_correspondent_email
        ]
        _assert_owned_by("pkos_nodes", exclusive_nodes, member.user_id)
        # Flag-off baseline: trigger-assigned workspace original user.
        _assert_owned_by(
            "entity_aliases", list(member.entity_alias_ids.values()), workspace_original_user
        )
        assert len(member.detection_evidence_ids) == inbound
        assert member.resolution_evidence_ids
        _assert_owned_by("pkos_evidence", member.evidence_ids, workspace_original_user)

    # Detail checks on artifact kinds, workspace-wide.
    with engine.begin() as connection:
        nodes = (
            connection.execute(
                text("SELECT DISTINCT node_type FROM pkos_nodes WHERE id = ANY(:ids)"),
                {"ids": [n for m in world.members.values() for n in m.person_node_ids.values()]},
            )
            .scalars()
            .all()
        )
        evidence_types = (
            connection.execute(
                text("SELECT DISTINCT source_type FROM pkos_evidence WHERE id = ANY(:ids)"),
                {"ids": [e for m in world.members.values() for e in m.evidence_ids]},
            )
            .scalars()
            .all()
        )
        rec_types = connection.execute(
            text(
                "SELECT DISTINCT recommendation_type, status FROM recommendations "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": [r for m in world.members.values() for r in m.recommendation_ids]},
        ).all()
        run_types = connection.execute(
            text("SELECT DISTINCT task_type, status FROM ai_runs WHERE id = ANY(:ids)"),
            {"ids": [r for m in world.members.values() for r in m.ai_run_ids]},
        ).all()
        run_kinds = connection.execute(
            text("SELECT DISTINCT run_type, status FROM sync_runs WHERE id = ANY(:ids)"),
            {"ids": [r for m in world.members.values() for r in m.sync_run_ids]},
        ).all()
    assert nodes == ["person"]
    assert evidence_types == ["gmail_sync"]
    assert [tuple(r) for r in rec_types] == [("email_action_detected", "proposed")]
    assert [tuple(r) for r in run_types] == [("email.detect_action", "completed")]
    assert [tuple(r) for r in run_kinds] == [("backfill", "succeeded")]

    # No artifact id is attributed to both members (except the shared node).
    for attr in (
        "sync_run_ids",
        "sync_cursor_ids",
        "email_thread_ids",
        "attention_item_ids",
        "recommendation_ids",
        "ai_run_ids",
        "evidence_ids",
    ):
        assert not set(getattr(world.a, attr)) & set(getattr(world.b, attr)), attr


def test_shared_correspondent_maps_to_one_person_node(
    gmail_sync_world: GmailSyncWorld,  # noqa: F811
) -> None:
    world = gmail_sync_world
    shared = world.shared_correspondent_email
    assert world.a.person_node_ids[shared] == world.b.person_node_ids[shared]
    assert world.a.entity_alias_ids[shared] == world.b.entity_alias_ids[shared]
    assert world.shared_person_node_id == world.a.person_node_ids[shared]
    # Only one of each exists workspace-wide; owned by the first syncer (A).
    with engine.begin() as connection:
        alias_count = connection.execute(
            text(
                "SELECT count(*) FROM entity_aliases WHERE workspace_id = :workspace_id "
                "AND alias_type = 'email' AND normalized_value = :email"
            ),
            {"workspace_id": world.workspace_id, "email": shared},
        ).scalar_one()
    assert alias_count == 1
    assert _owners("pkos_nodes", [world.shared_person_node_id]) == {
        world.shared_person_node_id: world.a.user_id
    }
    # Distinct mailboxes, distinct Google accounts.
    assert world.a.google_email != world.b.google_email
    assert world.a.connector_account_id != world.b.connector_account_id


def test_colliding_external_message_id_exists_for_both_members(
    gmail_sync_world: GmailSyncWorld,  # noqa: F811
) -> None:
    world = gmail_sync_world
    colliding = world.colliding_external_message_id
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT id, owner_id FROM email_messages "
                "WHERE workspace_id = :workspace_id AND external_message_id = :ext"
            ),
            {"workspace_id": world.workspace_id, "ext": colliding},
        ).all()
        detection_evidence = (
            connection.execute(
                text(
                    "SELECT id FROM pkos_evidence WHERE workspace_id = :workspace_id "
                    "AND source_ref = :ref"
                ),
                {"workspace_id": world.workspace_id, "ref": f"gmail:detect_action:{colliding}"},
            )
            .scalars()
            .all()
        )
    assert {row.owner_id for row in rows} == {world.a.user_id, world.b.user_id}
    assert len(rows) == 2
    assert {row.id for row in rows} == {
        world.a.email_message_ids[colliding],
        world.b.email_message_ids[colliding],
    }
    # Each member's detection run registered its own evidence row citing
    # the same raw id -- the ambiguous-owner shape the backfill must skip.
    assert len(detection_evidence) == 2
    assert set(detection_evidence) <= set(world.a.detection_evidence_ids) | set(
        world.b.detection_evidence_ids
    )
    assert set(detection_evidence) & set(world.a.detection_evidence_ids)
    assert set(detection_evidence) & set(world.b.detection_evidence_ids)


def test_evidence_ids_by_source_ref_scopes_a_colliding_ref_to_each_member(
    gmail_sync_world: GmailSyncWorld,  # noqa: F811
) -> None:
    world = gmail_sync_world
    ref = f"gmail:detect_action:{world.colliding_external_message_id}"
    a_ids = world.a.evidence_ids_by_source_ref[ref]
    b_ids = world.b.evidence_ids_by_source_ref[ref]
    assert len(a_ids) == len(b_ids) == 1
    assert a_ids != b_ids
    for member in world.members.values():
        flattened = [e for ids in member.evidence_ids_by_source_ref.values() for e in ids]
        assert sorted(flattened) == sorted(member.evidence_ids)


def test_revoke_after_build_is_captured_by_fake_google(
    gmail_sync_world: GmailSyncWorld,  # noqa: F811
) -> None:
    """A follow-up revocation made after the world is built (member A
    disables the `email` domain -- `finish_gmail_revocation`'s path) still
    runs against the harness's fake Google: the revoke lands in
    `fake_google.revoked_tokens`, not on the network, where a failure would
    be swallowed silently."""
    world = gmail_sync_world
    assert world.fake_google.revoked_tokens == []
    response = world.client("a").post(
        "/api/v1/personal/domains/email/disable",
        headers=world.headers("a", idempotency_key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    assert world.fake_google.revoked_tokens == [FakeGoogle.refresh_token("a")]
    # B's mailbox is untouched.
    assert FakeGoogle.refresh_token("b") not in world.fake_google.revoked_tokens


@pytest.mark.parametrize(
    "gmail_sync_world", [{"ECC_EMAIL_ACTION_DETECTION_ENABLED": "false"}], indirect=True
)
def test_env_override_applies_to_the_sync(
    gmail_sync_world: GmailSyncWorld,  # noqa: F811
) -> None:
    world = gmail_sync_world
    assert get_settings().email_action_detection_enabled is False
    for member in world.members.values():
        assert member.ai_run_ids == ()
        assert member.recommendation_ids == ()
        assert member.detection_evidence_ids == ()
        # The deterministic half of the pipeline still ran.
        assert len(member.email_message_ids) == 2
        assert len(member.attention_item_ids) == 2
    with engine.begin() as connection:
        ai_runs = connection.execute(
            text("SELECT count(*) FROM ai_runs WHERE workspace_id = :workspace_id"),
            {"workspace_id": world.workspace_id},
        ).scalar_one()
    assert ai_runs == 0


def test_factory_builds_a_world_with_env_overrides(
    gmail_sync_world_factory: GmailSyncWorldFactory,  # noqa: F811
) -> None:
    world = gmail_sync_world_factory(env={"ECC_EMAIL_ACTION_DETECTION_ENABLED": "false"})
    assert world.a.recommendation_ids == ()
    assert world.a.sync_run_ids and world.b.sync_run_ids


def _settings_snapshot() -> tuple[object, ...]:
    settings = get_settings()
    return (
        settings.email_action_detection_enabled,
        settings.gmail_oauth_client_id,
        settings.gmail_oauth_client_secret,
        settings.gmail_oauth_redirect_uri,
        settings.gmail_oauth_allowlist,
    )


def test_harness_exception_restores_settings_and_patches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (review A1): an exception raised inside the harness must
    not leave the lru-cached `Settings` holding fixture values, nor leave
    any patched module attribute in place."""
    monkeypatch.delenv("ECC_EMAIL_ACTION_DETECTION_ENABLED", raising=False)
    get_settings.cache_clear()
    before = _settings_snapshot()
    assert before[0] is False
    adapters_before = [m._adapter for m in _GMAIL_ADAPTER_MODULES]
    registry_before = connector_accounts_module.connector_registry
    ollama_before = runtime_module.OllamaAdapter

    with pytest.raises(RuntimeError, match="boom"):
        with gmail_sync_harness(env={"ECC_GMAIL_OAUTH_ALLOWLIST": "x@example.test"}):
            assert get_settings().email_action_detection_enabled is True
            assert get_settings().gmail_oauth_client_id == "cid"
            raise RuntimeError("boom")

    # No explicit cache_clear here -- the harness must have done it.
    assert _settings_snapshot() == before
    assert all(
        m._adapter is before_adapter
        for m, before_adapter in zip(_GMAIL_ADAPTER_MODULES, adapters_before, strict=True)
    )
    assert connector_accounts_module.connector_registry is registry_before
    assert runtime_module.OllamaAdapter is ollama_before
