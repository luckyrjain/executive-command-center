"""Seed deterministic, idempotent Phase 1 acceptance fixtures.

This script inserts representative rows into every Phase 1 table under two
genuinely isolated workspaces ("alpha" and "bravo"). It exists so the backup
and restore drill (``scripts/backup.sh`` / ``scripts/restore.sh`` /
``scripts/verify_restore.sh``) can be exercised against populated data
instead of an empty schema.

Design notes:

* All identifiers are derived deterministically from fixed names via
  ``uuid5`` (see ``seed_id``) -- never ``uuid4()``. Re-running this script
  against the same database is therefore idempotent: every insert uses
  ``ON CONFLICT ... DO NOTHING`` keyed on the table's real primary key, and
  because the derived ids never change, a second run inserts nothing new.
* All timestamps are derived from a fixed ``SEED_EPOCH`` constant rather than
  ``datetime.now()``, so the seeded rows -- and any checksum computed over
  them -- are identical across repeated runs.
* Two independent workspaces are created with no shared rows and no
  cross-workspace foreign keys, so genuine workspace isolation is testable
  after a restore.
* This script only depends on ``psycopg`` (already a project dependency used
  the same way by ``scripts/bootstrap_dev.py``) and the standard library --
  no new third-party dependency is introduced.

CLI usage:

    uv run python scripts/seed_phase1_acceptance.py
        Seed the fixtures into ECC_DATABASE_URL (or DATABASE_URL, or the
        local default).

    uv run python scripts/seed_phase1_acceptance.py --database-url URL
        Seed the fixtures into an explicit database.

    uv run python scripts/seed_phase1_acceptance.py --checksums --database-url URL
        Print a deterministic ``<table>\\t<checksum>`` line per fixture-scoped
        table instead of seeding. Used by scripts/verify_restore.sh to
        compare the source and restored-target databases.

    uv run python scripts/seed_phase1_acceptance.py --print-workspace-ids
        Print ``<label>\\t<uuid>`` lines for the two seeded workspaces. Used
        by scripts/verify_restore.sh to check per-workspace representation
        without duplicating the id derivation logic in bash.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg import sql

SEED_NAMESPACE = uuid5(NAMESPACE_URL, "https://ecc.local/phase1-acceptance-seed")
SEED_MARKER = "Phase1SeedMarker"
SEED_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
WORKSPACE_LABELS: tuple[str, ...] = ("alpha", "bravo")

# Tables that carry a `workspace_id` column directly (every Phase 1 table
# except `workspaces` itself, `event_inbox`, and `event_dead_letters`, plus
# the Phase 2 tables added by backend/migrations/versions/0011_phase2_
# knowledge_entities.py, 0012_phase2_timeline.py, 0013_phase2_
# resolution.py, and 0014_phase2_retrieval.py, plus the Phase 3 tables added
# by 0022_phase3_attention_policy.py, 0023_phase3_waiting.py, 0024_phase3_
# risk_reviews.py, 0025_phase3_capacity_planning.py, 0026_phase3_plans.py,
# and 0027_phase3_meetings.py -- verify_restore.sh's workspace-isolation
# check discovers workspace_id-bearing tables generically, so any such
# table without seeded rows here fails that check regardless of whether
# it's listed in ALL_PHASE1_TABLES's name. This list itself is NOT
# discovered generically and must be kept in sync by hand whenever a new
# workspace_id-bearing table is added -- Phase 3 added nine such tables
# (attention_feedback, waiting_links, risk_reviews, capacity_profiles,
# planning_constraints, plans, plan_blocks, meeting_participants,
# meeting_packs) without updating this list, which is what made the
# backup-restore drill's workspace-isolation check fail).
_WORKSPACE_ID_TABLES: tuple[str, ...] = (
    "users",
    "sessions",
    "pkos_nodes",
    "pkos_edges",
    "pkos_evidence",
    "event_outbox",
    "tasks",
    "audit_events",
    "idempotency_records",
    "commitments",
    "notes",
    "calendar_events",
    "meetings",
    "risks",
    "attention_items",
    "morning_briefs",
    "recommendations",
    "recommendation_feedback",
    "entity_aliases",
    "knowledge_claims",
    "timeline_entries",
    "resolution_candidates",
    "entity_operations",
    "retrieval_documents",
    "embedding_projections",
    "attention_feedback",
    "waiting_links",
    "risk_reviews",
    "capacity_profiles",
    "planning_constraints",
    "plans",
    "plan_blocks",
    "meeting_participants",
    "meeting_packs",
)
# `workspaces` is scoped by its own `id`, not a `workspace_id` column.
_WORKSPACE_TABLE = "workspaces"
# These two tables have no `workspace_id` column at all; they are scoped by
# the seeded `event_outbox.event_id` they reference instead.
_EVENT_SCOPED_TABLES: tuple[str, ...] = ("event_inbox", "event_dead_letters")

# Every Phase 1 table, enumerated from backend/migrations/versions/*.py.
ALL_PHASE1_TABLES: tuple[str, ...] = (
    (_WORKSPACE_TABLE,) + _WORKSPACE_ID_TABLES + _EVENT_SCOPED_TABLES
)


def seed_id(*parts: str) -> UUID:
    """Derive a stable, deterministic UUID for a named fixture row."""
    return uuid5(SEED_NAMESPACE, ":".join(parts))


def _fixture_ids(label: str) -> dict[str, UUID]:
    return {
        "workspace": seed_id(label, "workspace"),
        "user": seed_id(label, "user", "owner"),
        "session": seed_id(label, "session", "primary"),
        "node_person": seed_id(label, "pkos_node", "person"),
        "node_topic": seed_id(label, "pkos_node", "topic"),
        "edge": seed_id(label, "pkos_edge", "person_topic"),
        "evidence": seed_id(label, "pkos_evidence", "person"),
        "entity_alias": seed_id(label, "entity_alias", "person"),
        "knowledge_claim": seed_id(label, "knowledge_claim", "person"),
        "timeline_entry": seed_id(label, "timeline_entry", "person"),
        "resolution_candidate": seed_id(label, "resolution_candidate", "person_topic"),
        "entity_operation": seed_id(label, "entity_operation", "merge"),
        "retrieval_document": seed_id(label, "retrieval_document", "person"),
        "embedding_projection": seed_id(label, "embedding_projection", "person"),
        "outbox_event": seed_id(label, "event_outbox", "marker"),
        "dead_letter": seed_id(label, "event_dead_letter", "marker"),
        "task_active": seed_id(label, "task", "active"),
        "task_archived": seed_id(label, "task", "archived"),
        "audit_task_created": seed_id(label, "audit", "task_active", "created"),
        "audit_task_archived": seed_id(label, "audit", "task_archived", "archived"),
        "commitment_active": seed_id(label, "commitment", "active"),
        "commitment_archived": seed_id(label, "commitment", "archived"),
        "note_active": seed_id(label, "note", "active"),
        "note_archived": seed_id(label, "note", "archived"),
        "calendar_event_active": seed_id(label, "calendar_event", "active"),
        "calendar_event_archived": seed_id(label, "calendar_event", "archived"),
        "meeting_linked": seed_id(label, "meeting", "linked"),
        "meeting_standalone": seed_id(label, "meeting", "standalone_archived"),
        "risk_active": seed_id(label, "risk", "active"),
        "risk_archived": seed_id(label, "risk", "archived"),
        "attention_item": seed_id(label, "attention_item", "task_active"),
        "morning_brief": seed_id(label, "morning_brief", "today"),
        "recommendation_active": seed_id(label, "recommendation", "proposed"),
        "recommendation_archived": seed_id(label, "recommendation", "archived"),
        "recommendation_feedback": seed_id(label, "recommendation_feedback", "pin"),
        "correlation_marker": seed_id(label, "correlation", "marker"),
        "request_created": seed_id(label, "request", "task_active", "created"),
        "request_archived": seed_id(label, "request", "task_archived", "archived"),
        "attention_feedback": seed_id(label, "attention_feedback", "useful"),
        "waiting_link": seed_id(label, "waiting_link", "task_active"),
        "risk_review": seed_id(label, "risk_review", "risk_active"),
        "capacity_profile": seed_id(label, "capacity_profile", "monday"),
        "planning_constraint": seed_id(label, "planning_constraint", "task_active"),
        "plan": seed_id(label, "plan", "week"),
        "plan_block": seed_id(label, "plan_block", "task_active"),
        "meeting_participant": seed_id(label, "meeting_participant", "node_person"),
        "meeting_pack": seed_id(label, "meeting_pack", "meeting_linked"),
    }


FIXTURE_IDS: dict[str, dict[str, UUID]] = {label: _fixture_ids(label) for label in WORKSPACE_LABELS}
WORKSPACE_IDS: dict[str, UUID] = {
    label: FIXTURE_IDS[label]["workspace"] for label in WORKSPACE_LABELS
}


def _database_url(override: str | None = None) -> str:
    value = (
        override
        or os.getenv("ECC_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql://ecc:ecc@localhost:5432/ecc"
    )
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


def _tz(label: str) -> str:
    return "UTC" if label == "alpha" else "Asia/Kolkata"


def _seed_workspace(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO workspaces (id, name, created_at, timezone)
        VALUES (%(id)s, %(name)s, %(created_at)s, %(timezone)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["workspace"],
            "name": f"Phase1 Seed {label.capitalize()}",
            "created_at": SEED_EPOCH,
            "timezone": _tz(label),
        },
    )
    cur.execute(
        """
        INSERT INTO users (id, workspace_id, email, password_hash, created_at)
        VALUES (%(id)s, %(workspace_id)s, %(email)s, %(password_hash)s, %(created_at)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["user"],
            "workspace_id": ids["workspace"],
            "email": f"phase1-seed-{label}@example.test",
            "password_hash": "phase1-seed-fixture-no-login",
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO sessions (
            id, workspace_id, user_id, token_hash, expires_at, last_seen_at, revoked_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, %(token_hash)s,
            %(expires_at)s, %(last_seen_at)s, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["session"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "token_hash": sha256(f"phase1-seed-session-{label}".encode()).hexdigest(),
            "expires_at": SEED_EPOCH + timedelta(days=36500),
            "last_seen_at": SEED_EPOCH,
        },
    )


def _seed_pkos(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    for node_key, node_type, name in (
        ("node_person", "person", f"Seed Person {label.capitalize()}"),
        ("node_topic", "topic", f"Seed Topic {label.capitalize()}"),
    ):
        cur.execute(
            """
            INSERT INTO pkos_nodes (
                id, workspace_id, node_type, canonical_name, attributes,
                created_at, updated_at
            ) VALUES (
                %(id)s, %(workspace_id)s, %(node_type)s, %(canonical_name)s,
                '{}'::jsonb, %(created_at)s, %(updated_at)s
            )
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": ids[node_key],
                "workspace_id": ids["workspace"],
                "node_type": node_type,
                "canonical_name": name,
                "created_at": SEED_EPOCH,
                "updated_at": SEED_EPOCH,
            },
        )
    # Evidence must exist before the edge below: pkos_edges.evidence_id is
    # NOT NULL with a composite FK into pkos_evidence (migration 0016), and
    # ids["evidence"] is already deterministic at this point (seed_id), so
    # this insert only needs reordering ahead of the edge, not a new ID.
    cur.execute(
        """
        INSERT INTO pkos_evidence (
            id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(node_id)s, 'seed_fixture', %(source_ref)s,
            %(sha256)s, %(captured_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["evidence"],
            "workspace_id": ids["workspace"],
            "node_id": ids["node_person"],
            "source_ref": f"seed://{label}/evidence/person",
            "sha256": sha256(f"phase1-seed-evidence-{label}".encode()).hexdigest(),
            "captured_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO pkos_edges (
            id, workspace_id, source_node_id, target_node_id, edge_type,
            attributes, evidence_id
        ) VALUES (
            %(id)s, %(workspace_id)s, %(source)s, %(target)s, 'related_to',
            '{}'::jsonb, %(evidence_id)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["edge"],
            "workspace_id": ids["workspace"],
            "source": ids["node_person"],
            "target": ids["node_topic"],
            "evidence_id": ids["evidence"],
        },
    )
    cur.execute(
        """
        INSERT INTO entity_aliases (
            id, workspace_id, entity_id, alias_type, normalized_value, source_id,
            confidence, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(entity_id)s, 'external_id', %(normalized_value)s,
            %(source_id)s, 1.00, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["entity_alias"],
            "workspace_id": ids["workspace"],
            "entity_id": ids["node_person"],
            "normalized_value": f"seed-alias-{label}",
            "source_id": ids["evidence"],
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO knowledge_claims (
            id, workspace_id, subject_id, predicate, value_json, source_id,
            confidence, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(subject_id)s, 'seed_predicate',
            %(value_json)s, %(source_id)s, 1.00, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["knowledge_claim"],
            "workspace_id": ids["workspace"],
            "subject_id": ids["node_person"],
            "value_json": f'{{"seed": "{label}"}}',
            "source_id": ids["evidence"],
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO timeline_entries (
            id, workspace_id, entity_id, effective_at, recorded_at, event_type,
            source_id, summary
        ) VALUES (
            %(id)s, %(workspace_id)s, %(entity_id)s, %(effective_at)s, %(recorded_at)s,
            'knowledge_entity.created', %(source_id)s, %(summary)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["timeline_entry"],
            "workspace_id": ids["workspace"],
            "entity_id": ids["node_person"],
            "effective_at": SEED_EPOCH,
            "recorded_at": SEED_EPOCH,
            "source_id": ids["evidence"],
            "summary": f"seed timeline entry {label}",
        },
    )
    cur.execute(
        """
        INSERT INTO resolution_candidates (
            id, workspace_id, left_entity_id, right_entity_id, score,
            factors_json, resolver_version, status, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(left_id)s, %(right_id)s, %(score)s,
            %(factors_json)s, %(resolver_version)s, 'open', %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["resolution_candidate"],
            "workspace_id": ids["workspace"],
            "left_id": ids["node_person"],
            "right_id": ids["node_topic"],
            "score": "0.1000",
            "factors_json": (
                '{"name_similarity": 0.0, "alias_overlap": 0.0, '
                '"neighbor_overlap": 0.0, "temporal_compatibility": 1.0}'
            ),
            "resolver_version": "phase2-resolution-v1",
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO entity_operations (
            id, workspace_id, operation_type, status, inputs_json, outputs_json,
            actor_id, reason, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'merge', 'active', %(inputs_json)s,
            %(outputs_json)s, %(actor_id)s, %(reason)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["entity_operation"],
            "workspace_id": ids["workspace"],
            "inputs_json": f'{{"seed": "{label}"}}',
            "outputs_json": f'{{"seed": "{label}"}}',
            "actor_id": ids["user"],
            "reason": f"seed entity operation {label}",
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO retrieval_documents (
            id, workspace_id, entity_type, entity_id, title, body,
            source_version, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'person', %(entity_id)s, %(title)s, %(body)s,
            1, %(updated_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["retrieval_document"],
            "workspace_id": ids["workspace"],
            "entity_id": ids["node_person"],
            "title": f"Seed Person {label.capitalize()}",
            "body": f"seed retrieval document {label}",
            "updated_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO embedding_projections (
            id, workspace_id, document_id, model_id, model_version,
            dimensions, embedding, content_hash, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(document_id)s, %(model_id)s, '1',
            %(dimensions)s, CAST(%(embedding)s AS vector), %(content_hash)s,
            %(created_at)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["embedding_projection"],
            "workspace_id": ids["workspace"],
            "document_id": ids["retrieval_document"],
            "model_id": "sentence-transformers/all-MiniLM-L6-v2",
            "dimensions": 384,
            # A fixed, arbitrary unit-ish vector -- seed data only exercises
            # storage/isolation, never real similarity ranking, so no need
            # to run the actual model here.
            "embedding": "[" + ",".join(["0.01"] * 384) + "]",
            "content_hash": f"seed-{label}",
            "created_at": SEED_EPOCH,
        },
    )


def _seed_events(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO event_outbox (
            event_id, workspace_id, event_type, event_version, correlation_id,
            causation_id, payload, occurred_at, published_at, attempt_count
        ) VALUES (
            %(event_id)s, %(workspace_id)s, 'phase1.seed.marker.v1', 1,
            %(correlation_id)s, NULL, '{}'::jsonb, %(occurred_at)s, NULL, 0
        )
        ON CONFLICT (event_id) DO NOTHING
        """,
        {
            "event_id": ids["outbox_event"],
            "workspace_id": ids["workspace"],
            "correlation_id": ids["correlation_marker"],
            "occurred_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO event_inbox (consumer, event_id, processed_at)
        VALUES (%(consumer)s, %(event_id)s, %(processed_at)s)
        ON CONFLICT (consumer, event_id) DO NOTHING
        """,
        {
            "consumer": "phase1-seed-fixture-consumer",
            "event_id": ids["outbox_event"],
            "processed_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO event_dead_letters (id, event_id, consumer, reason, failed_at)
        VALUES (%(id)s, %(event_id)s, %(consumer)s, %(reason)s, %(failed_at)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["dead_letter"],
            "event_id": ids["outbox_event"],
            "consumer": "phase1-seed-fixture-consumer",
            "reason": "deterministic phase1 seed fixture dead-letter",
            "failed_at": SEED_EPOCH,
        },
    )


def _seed_tasks_and_audit(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO tasks (
            id, workspace_id, owner_id, title, description, status, manual_priority,
            due_date, due_at, pinned, source_type, source_ref, created_by, updated_by,
            created_at, updated_at, version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(title)s, %(description)s,
            'in_progress', 'high', NULL, %(due_at)s, TRUE, 'local', NULL,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["task_active"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "title": f"{SEED_MARKER} active task {label}",
            "description": "Deterministic phase1 acceptance seed fixture task.",
            "due_at": SEED_EPOCH + timedelta(days=7),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO tasks (
            id, workspace_id, owner_id, title, description, status, manual_priority,
            due_date, due_at, completed_at, pinned, source_type, source_ref,
            created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(title)s, %(description)s,
            'completed', 'medium', NULL, NULL, %(completed_at)s, FALSE, 'local', NULL,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 2, %(archived_at)s, 'completed'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["task_archived"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "title": f"Phase1 seed archived task {label}",
            "description": "Deterministic phase1 acceptance seed fixture archived task.",
            "completed_at": SEED_EPOCH,
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )
    cur.execute(
        """
        INSERT INTO audit_events (
            id, workspace_id, event_type, aggregate_type, aggregate_id, aggregate_version,
            actor_id, request_id, correlation_id, idempotency_key_hash, before, after,
            changed_fields, authorization_result, source, failure_code, metadata, occurred_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'task.created', 'task', %(aggregate_id)s, 1,
            %(actor_id)s, %(request_id)s, %(correlation_id)s, NULL, NULL,
            %(after)s, %(changed_fields)s, 'allowed', 'user', NULL, '{}'::jsonb, %(occurred_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["audit_task_created"],
            "workspace_id": ids["workspace"],
            "aggregate_id": ids["task_active"],
            "actor_id": ids["user"],
            "request_id": ids["request_created"],
            "correlation_id": ids["correlation_marker"],
            "after": '{"status": "in_progress"}',
            "changed_fields": ["title", "status"],
            "occurred_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO audit_events (
            id, workspace_id, event_type, aggregate_type, aggregate_id, aggregate_version,
            actor_id, request_id, correlation_id, idempotency_key_hash, before, after,
            changed_fields, authorization_result, source, failure_code, metadata, occurred_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'task.archived', 'task', %(aggregate_id)s, 2,
            %(actor_id)s, %(request_id)s, %(correlation_id)s, NULL, %(before)s,
            %(after)s, %(changed_fields)s, 'allowed', 'user', NULL, '{}'::jsonb, %(occurred_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["audit_task_archived"],
            "workspace_id": ids["workspace"],
            "aggregate_id": ids["task_archived"],
            "actor_id": ids["user"],
            "request_id": ids["request_archived"],
            "correlation_id": ids["correlation_marker"],
            "before": '{"status": "completed", "archived_at": null}',
            "after": '{"status": "completed", "archived_at": "2026-01-02T00:00:00+00:00"}',
            "changed_fields": ["archived_at", "pre_archive_status"],
            "occurred_at": SEED_EPOCH + timedelta(days=1),
        },
    )


def _seed_commitments(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO commitments (
            id, workspace_id, owner_id, summary, description, direction,
            counterparty_person_id, counterparty_name, status, due_date, due_at,
            importance, evidence_id, confidence, fulfilled_at, pinned,
            created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(summary)s, %(description)s,
            'made_by_me', %(counterparty)s, %(counterparty_name)s, 'active', NULL,
            %(due_at)s, 'high', %(evidence_id)s, 0.750, NULL, TRUE,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["commitment_active"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "summary": f"{SEED_MARKER} commitment {label}",
            "description": "Deterministic phase1 acceptance seed fixture commitment.",
            "counterparty": ids["node_person"],
            "counterparty_name": f"Seed Person {label.capitalize()}",
            "due_at": SEED_EPOCH + timedelta(days=14),
            "evidence_id": ids["evidence"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO commitments (
            id, workspace_id, owner_id, summary, description, direction,
            counterparty_person_id, counterparty_name, status, due_date, due_at,
            importance, evidence_id, confidence, fulfilled_at, pinned,
            created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(summary)s, %(description)s,
            'made_to_me', %(counterparty)s, %(counterparty_name)s, 'fulfilled', NULL,
            NULL, 'medium', %(evidence_id)s, 0.900, %(fulfilled_at)s, FALSE,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 2, %(archived_at)s, 'fulfilled'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["commitment_archived"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "summary": f"Phase1 seed archived commitment {label}",
            "description": "Deterministic phase1 acceptance seed fixture archived commitment.",
            "counterparty": ids["node_person"],
            "counterparty_name": f"Seed Person {label.capitalize()}",
            "evidence_id": ids["evidence"],
            "fulfilled_at": SEED_EPOCH,
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )


def _seed_notes(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO notes (
            id, workspace_id, owner_id, title, body, note_type, meeting_id,
            source_type, source_ref, created_by, updated_by, created_at, updated_at,
            version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(title)s, %(body)s, 'general',
            NULL, 'local', NULL, %(actor)s, %(actor)s, %(now)s, %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["note_active"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "title": f"{SEED_MARKER} note {label}",
            "body": (
                "Deterministic phase1 acceptance seed fixture note body used only "
                "for restore-verification hashing; contains no real user content."
            ),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO notes (
            id, workspace_id, owner_id, title, body, note_type, meeting_id,
            source_type, source_ref, created_by, updated_by, created_at, updated_at,
            version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(title)s, %(body)s, 'journal',
            NULL, 'local', NULL, %(actor)s, %(actor)s, %(now)s, %(now)s, 2,
            %(archived_at)s, 'active'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["note_archived"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "title": f"Phase1 seed archived note {label}",
            "body": "Deterministic phase1 acceptance seed fixture archived note body.",
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )


def _seed_calendar_and_meetings(
    cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]
) -> None:
    cur.execute(
        """
        INSERT INTO calendar_events (
            id, workspace_id, external_source, external_id, title, starts_at, ends_at,
            all_day, timezone, location, description, status, source_authoritative,
            created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, 'local', NULL, %(title)s, %(starts_at)s,
            %(ends_at)s, FALSE, %(timezone)s, 'Seed conference room', %(description)s,
            'confirmed', TRUE, %(actor)s, %(actor)s, %(now)s, %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["calendar_event_active"],
            "workspace_id": ids["workspace"],
            "title": f"{SEED_MARKER} calendar event {label}",
            "starts_at": SEED_EPOCH + timedelta(days=2),
            "ends_at": SEED_EPOCH + timedelta(days=2, hours=1),
            "timezone": _tz(label),
            "description": "Deterministic phase1 acceptance seed fixture calendar event.",
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO calendar_events (
            id, workspace_id, external_source, external_id, title, starts_at, ends_at,
            all_day, timezone, location, description, status, source_authoritative,
            created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, 'local', NULL, %(title)s, %(starts_at)s,
            %(ends_at)s, FALSE, %(timezone)s, NULL, %(description)s,
            'cancelled', TRUE, %(actor)s, %(actor)s, %(now)s, %(now)s, 2,
            %(archived_at)s, 'cancelled'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["calendar_event_archived"],
            "workspace_id": ids["workspace"],
            "title": f"Phase1 seed archived calendar event {label}",
            "starts_at": SEED_EPOCH + timedelta(days=3),
            "ends_at": SEED_EPOCH + timedelta(days=3, hours=1),
            "timezone": _tz(label),
            "description": "Deterministic phase1 acceptance seed fixture archived event.",
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )
    cur.execute(
        """
        INSERT INTO meetings (
            id, workspace_id, calendar_event_id, title, standalone_starts_at,
            standalone_ends_at, standalone_timezone, status, agenda, preparation,
            notes_summary, created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(calendar_event_id)s, %(title)s, NULL, NULL,
            NULL, 'planned', %(agenda)s, NULL, NULL, %(actor)s, %(actor)s, %(now)s,
            %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["meeting_linked"],
            "workspace_id": ids["workspace"],
            "calendar_event_id": ids["calendar_event_active"],
            "title": f"{SEED_MARKER} meeting {label}",
            "agenda": "Deterministic phase1 acceptance seed fixture meeting agenda.",
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO meetings (
            id, workspace_id, calendar_event_id, title, standalone_starts_at,
            standalone_ends_at, standalone_timezone, status, agenda, preparation,
            notes_summary, created_by, updated_by, created_at, updated_at, version,
            archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, NULL, %(title)s, %(starts_at)s, %(ends_at)s,
            %(timezone)s, 'completed', NULL, NULL, %(notes_summary)s, %(actor)s,
            %(actor)s, %(now)s, %(now)s, 2, %(archived_at)s, 'completed'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["meeting_standalone"],
            "workspace_id": ids["workspace"],
            "title": f"Phase1 seed archived standalone meeting {label}",
            "starts_at": SEED_EPOCH + timedelta(days=4),
            "ends_at": SEED_EPOCH + timedelta(days=4, hours=1),
            "timezone": _tz(label),
            "notes_summary": "Deterministic phase1 acceptance seed fixture meeting notes.",
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )


def _seed_risks(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO risks (
            id, workspace_id, description, probability, impact, status, owner_id,
            mitigation, trigger, review_at, project_id, pinned, created_by, updated_by,
            created_at, updated_at, version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(description)s, 3, 4, 'monitoring', %(owner_id)s,
            %(mitigation)s, %(trigger)s, %(review_at)s, NULL, TRUE, %(actor)s, %(actor)s,
            %(now)s, %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["risk_active"],
            "workspace_id": ids["workspace"],
            "description": f"{SEED_MARKER} risk {label}",
            "owner_id": ids["user"],
            "mitigation": "Deterministic phase1 acceptance seed fixture mitigation.",
            "trigger": "Deterministic phase1 acceptance seed fixture trigger.",
            "review_at": SEED_EPOCH + timedelta(days=30),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO risks (
            id, workspace_id, description, probability, impact, status, owner_id,
            mitigation, trigger, review_at, project_id, pinned, created_by, updated_by,
            created_at, updated_at, version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, %(description)s, 2, 2, 'closed', %(owner_id)s,
            %(mitigation)s, NULL, NULL, NULL, FALSE, %(actor)s, %(actor)s,
            %(now)s, %(now)s, 2, %(archived_at)s, 'closed'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["risk_archived"],
            "workspace_id": ids["workspace"],
            "description": f"Phase1 seed archived risk {label}",
            "owner_id": ids["user"],
            "mitigation": "Deterministic phase1 acceptance seed fixture closed mitigation.",
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )


def _seed_attention_and_briefs(
    cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]
) -> None:
    cur.execute(
        """
        INSERT INTO attention_items (
            id, workspace_id, entity_type, entity_id, source_entity_version, score,
            confidence, factors, explanation, generated_at, expires_at, pinned,
            dismissed_at, dismissed_entity_version, deferred_until
        ) VALUES (
            %(id)s, %(workspace_id)s, 'task', %(entity_id)s, 1, 42, 0.500, '{}'::jsonb,
            %(explanation)s, %(generated_at)s, %(expires_at)s, FALSE, NULL, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["attention_item"],
            "workspace_id": ids["workspace"],
            "entity_id": ids["task_active"],
            "explanation": "Deterministic phase1 acceptance seed fixture attention item.",
            "generated_at": SEED_EPOCH,
            "expires_at": SEED_EPOCH + timedelta(days=30),
        },
    )
    cur.execute(
        """
        INSERT INTO morning_briefs (
            id, workspace_id, user_id, briefing_date, generation_version, sections,
            source_versions, evidence_ids, generated_at, timezone, algorithm_version,
            ai_status, stale_reason, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, %(briefing_date)s, 1,
            %(sections)s, '{}'::jsonb, %(evidence_ids)s, %(generated_at)s,
            %(timezone)s, 'phase1-fixture-v1', 'disabled', NULL, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["morning_brief"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "briefing_date": SEED_EPOCH.date(),
            "sections": '{"tasks": [], "commitments": []}',
            "evidence_ids": [ids["evidence"]],
            "generated_at": SEED_EPOCH,
            "timezone": _tz(label),
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO attention_feedback (
            id, workspace_id, target_type, target_id, label, reason, actor_id,
            policy_version, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'attention_item', %(target_id)s, 'useful',
            %(reason)s, %(actor_id)s, 1, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["attention_feedback"],
            "workspace_id": ids["workspace"],
            "target_id": ids["attention_item"],
            "reason": "Deterministic phase1 acceptance seed fixture feedback.",
            "actor_id": ids["user"],
            "created_at": SEED_EPOCH,
        },
    )


def _seed_waiting_links(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO waiting_links (
            id, workspace_id, subject_type, subject_id, counterparty_entity_id,
            direction, status, note, since_at, expected_at, superseded_by,
            created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, 'task', %(subject_id)s, %(counterparty)s,
            'waiting_on_them', 'open', %(note)s, %(since_at)s, %(expected_at)s, NULL,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["waiting_link"],
            "workspace_id": ids["workspace"],
            "subject_id": ids["task_active"],
            "counterparty": ids["node_person"],
            "note": "Deterministic phase1 acceptance seed fixture waiting link.",
            "since_at": SEED_EPOCH,
            "expected_at": SEED_EPOCH + timedelta(days=7),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )


def _seed_risk_reviews(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO risk_reviews (
            id, workspace_id, risk_id, outcome, notes, evidence_refs, reviewed_at,
            next_review_at, actor_id
        ) VALUES (
            %(id)s, %(workspace_id)s, %(risk_id)s, 'no_change', %(notes)s,
            ARRAY[%(evidence_ref)s]::text[], %(reviewed_at)s, %(next_review_at)s, %(actor)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["risk_review"],
            "workspace_id": ids["workspace"],
            "risk_id": ids["risk_active"],
            "notes": "Deterministic phase1 acceptance seed fixture risk review.",
            "evidence_ref": f"seed://{label}/risk-review/evidence",
            "reviewed_at": SEED_EPOCH,
            "next_review_at": SEED_EPOCH + timedelta(days=30),
            "actor": ids["user"],
        },
    )


def _seed_capacity_and_constraints(
    cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]
) -> None:
    cur.execute(
        """
        INSERT INTO capacity_profiles (
            id, workspace_id, user_id, weekday, available_minutes, focus_minutes,
            timezone, version, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, 0, 480, 240, %(timezone)s, 1,
            %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["capacity_profile"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "timezone": _tz(label),
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO planning_constraints (
            id, workspace_id, user_id, kind, source_type, source_id, label,
            starts_at, ends_at, hardness, priority, created_at, updated_at,
            version, archived_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, 'deadline', 'task', %(source_id)s,
            %(label)s, NULL, %(ends_at)s, 'hard', 1, %(now)s, %(now)s, 1, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["planning_constraint"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "source_id": ids["task_active"],
            "label": f"{SEED_MARKER} planning constraint {label}",
            "ends_at": SEED_EPOCH + timedelta(days=2),
            "now": SEED_EPOCH,
        },
    )


def _seed_plans(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO plans (
            id, workspace_id, user_id, period_start, period_end, status,
            policy_version, capacity_minutes, source_versions, conflicts,
            unscheduled, superseded_by, accepted_at, created_by, updated_by,
            created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, %(period_start)s, %(period_end)s,
            'proposed', 1, 480, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, NULL, NULL,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["plan"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "period_start": SEED_EPOCH.date(),
            "period_end": SEED_EPOCH.date() + timedelta(days=6),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO plan_blocks (
            id, workspace_id, plan_id, source_type, source_id, starts_at, ends_at,
            status, rationale, is_default_effort, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(plan_id)s, 'task', %(source_id)s,
            %(starts_at)s, %(ends_at)s, 'proposed', %(rationale)s, FALSE,
            %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["plan_block"],
            "workspace_id": ids["workspace"],
            "plan_id": ids["plan"],
            "source_id": ids["task_active"],
            "starts_at": SEED_EPOCH + timedelta(hours=9),
            "ends_at": SEED_EPOCH + timedelta(hours=10),
            "rationale": "Deterministic phase1 acceptance seed fixture plan block.",
            "now": SEED_EPOCH,
        },
    )


def _seed_meeting_participants_and_packs(
    cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]
) -> None:
    cur.execute(
        """
        INSERT INTO meeting_participants (
            id, workspace_id, meeting_id, entity_id, role, created_by, updated_by,
            created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(meeting_id)s, %(entity_id)s, 'attendee',
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["meeting_participant"],
            "workspace_id": ids["workspace"],
            "meeting_id": ids["meeting_linked"],
            "entity_id": ids["node_person"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO meeting_packs (
            id, workspace_id, meeting_id, status, generated_at, stale_at,
            source_versions, content, created_by, updated_by, created_at,
            updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(meeting_id)s, 'fresh', %(generated_at)s,
            %(stale_at)s, '{}'::jsonb, '{}'::jsonb, %(actor)s, %(actor)s,
            %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["meeting_pack"],
            "workspace_id": ids["workspace"],
            "meeting_id": ids["meeting_linked"],
            "generated_at": SEED_EPOCH,
            "stale_at": SEED_EPOCH + timedelta(days=1),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )


def _seed_recommendations(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO recommendations (
            id, workspace_id, recommendation_type, target_type, target_id,
            proposed_action, expected_version, rationale, confidence, status,
            evidence_ids, expires_at, confirmed_by, confirmed_at, execution_result,
            source, pinned, deferred_until, created_by, updated_by, created_at,
            updated_at, version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, 'seed_fixture', 'task', %(target_id)s,
            %(proposed_action)s, 1, %(rationale)s, 0.8000, 'proposed',
            %(evidence_ids)s, %(expires_at)s, NULL, NULL, NULL,
            'rule', FALSE, NULL, %(actor)s, %(actor)s, %(now)s, %(now)s, 1, NULL, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["recommendation_active"],
            "workspace_id": ids["workspace"],
            "target_id": ids["task_active"],
            "proposed_action": '{"action": "seed_fixture"}',
            "rationale": "Deterministic phase1 acceptance seed fixture recommendation.",
            "evidence_ids": [ids["evidence"]],
            "expires_at": SEED_EPOCH + timedelta(days=30),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO recommendations (
            id, workspace_id, recommendation_type, target_type, target_id,
            proposed_action, expected_version, rationale, confidence, status,
            evidence_ids, expires_at, confirmed_by, confirmed_at, execution_result,
            source, pinned, deferred_until, created_by, updated_by, created_at,
            updated_at, version, archived_at, pre_archive_status
        ) VALUES (
            %(id)s, %(workspace_id)s, 'seed_fixture', 'task', %(target_id)s,
            %(proposed_action)s, 2, %(rationale)s, 0.6000, 'rejected',
            %(evidence_ids)s, %(expires_at)s, NULL, NULL, NULL,
            'rule', FALSE, NULL, %(actor)s, %(actor)s, %(now)s, %(now)s, 2,
            %(archived_at)s, 'rejected'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["recommendation_archived"],
            "workspace_id": ids["workspace"],
            "target_id": ids["task_archived"],
            "proposed_action": '{"action": "seed_fixture"}',
            "rationale": "Deterministic phase1 acceptance seed fixture rejected recommendation.",
            "evidence_ids": [ids["evidence"]],
            "expires_at": SEED_EPOCH + timedelta(days=30),
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "archived_at": SEED_EPOCH + timedelta(days=1),
        },
    )
    cur.execute(
        """
        INSERT INTO recommendation_feedback (
            id, workspace_id, recommendation_id, action, reason, defer_until,
            actor_id, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(recommendation_id)s, 'pin', NULL, NULL,
            %(actor_id)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["recommendation_feedback"],
            "workspace_id": ids["workspace"],
            "recommendation_id": ids["recommendation_active"],
            "actor_id": ids["user"],
            "created_at": SEED_EPOCH,
        },
    )


def _seed_idempotency(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO idempotency_records (
            workspace_id, actor_id, key, request_hash, response_status,
            response_body, created_at, expires_at
        ) VALUES (
            %(workspace_id)s, %(actor_id)s, %(key)s, %(request_hash)s, 201,
            %(response_body)s, %(created_at)s, %(expires_at)s
        )
        ON CONFLICT (workspace_id, actor_id, key) DO NOTHING
        """,
        {
            "workspace_id": ids["workspace"],
            "actor_id": ids["user"],
            "key": "phase1-seed-fixture",
            "request_hash": sha256(f"phase1-seed-request-{label}".encode()).hexdigest(),
            "response_body": '{"status": "in_progress"}',
            "created_at": SEED_EPOCH,
            "expires_at": SEED_EPOCH + timedelta(days=36500),
        },
    )


def seed(conn: psycopg.Connection[Any]) -> None:
    """Insert deterministic Phase 1 fixtures into every table for both workspaces.

    Idempotent: every insert uses ``ON CONFLICT ... DO NOTHING`` against a
    deterministically-derived id, so calling this twice against the same
    database changes nothing on the second call.
    """
    with conn.cursor() as cur:
        for label in WORKSPACE_LABELS:
            ids = FIXTURE_IDS[label]
            _seed_workspace(cur, label, ids)
            _seed_pkos(cur, label, ids)
            _seed_tasks_and_audit(cur, label, ids)
            _seed_events(cur, label, ids)
            _seed_commitments(cur, label, ids)
            _seed_notes(cur, label, ids)
            _seed_calendar_and_meetings(cur, label, ids)
            _seed_risks(cur, label, ids)
            _seed_attention_and_briefs(cur, label, ids)
            _seed_waiting_links(cur, label, ids)
            _seed_risk_reviews(cur, label, ids)
            _seed_capacity_and_constraints(cur, label, ids)
            _seed_plans(cur, label, ids)
            _seed_meeting_participants_and_packs(cur, label, ids)
            _seed_recommendations(cur, label, ids)
            _seed_idempotency(cur, label, ids)


def fixture_row_checksums(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """Compute a deterministic full-row checksum per Phase 1 table.

    Casting each row to ``text`` and aggregating the per-row md5 digests
    (ordered by digest, not physical row order) yields a single checksum per
    table that changes if -- and only if -- any column of any seeded row
    changed. This is used generically for the "representative record
    checksums" check, and by restricting the same technique to
    ``audit_events`` and the ``pkos_*`` tables it also proves audit
    append-only-ness and PKOS mapped-column survival across a restore: if
    any field of any row had been silently mutated, the checksum for that
    table would differ between source and target.
    """
    workspace_ids = list(WORKSPACE_IDS.values())
    event_ids = [FIXTURE_IDS[label]["outbox_event"] for label in WORKSPACE_LABELS]
    checksums: dict[str, str] = {}
    with conn.cursor() as cur:
        for table in (_WORKSPACE_TABLE,) + _WORKSPACE_ID_TABLES:
            filter_column = "id" if table == _WORKSPACE_TABLE else "workspace_id"
            query = sql.SQL(
                """
                SELECT coalesce(md5(string_agg(h, ',')), 'empty') FROM (
                    SELECT md5(t::text) AS h FROM {table} t
                    WHERE {filter_column} = ANY(%(ids)s)
                    ORDER BY md5(t::text)
                ) s
                """
            ).format(
                table=sql.Identifier(table),
                filter_column=sql.Identifier(filter_column),
            )
            cur.execute(query, {"ids": workspace_ids})
            row = cur.fetchone()
            checksums[table] = row[0] if row else "empty"
        for table in _EVENT_SCOPED_TABLES:
            query = sql.SQL(
                """
                SELECT coalesce(md5(string_agg(h, ',')), 'empty') FROM (
                    SELECT md5(t::text) AS h FROM {table} t
                    WHERE event_id = ANY(%(ids)s)
                    ORDER BY md5(t::text)
                ) s
                """
            ).format(table=sql.Identifier(table))
            cur.execute(query, {"ids": event_ids})
            row = cur.fetchone()
            checksums[table] = row[0] if row else "empty"
    return checksums


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--checksums",
        action="store_true",
        help="Print per-table checksums instead of seeding.",
    )
    parser.add_argument(
        "--print-workspace-ids",
        action="store_true",
        help="Print the two seeded workspace ids and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.print_workspace_ids:
        for label in WORKSPACE_LABELS:
            print(f"{label}\t{WORKSPACE_IDS[label]}")
        return 0

    database_url = _database_url(args.database_url)
    with psycopg.connect(database_url) as conn:
        if args.checksums:
            checksums = fixture_row_checksums(conn)
            for table in sorted(checksums):
                print(f"{table}\t{checksums[table]}")
            return 0
        seed(conn)
        conn.commit()
    print("Phase 1 acceptance fixtures seeded across two isolated workspaces.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
