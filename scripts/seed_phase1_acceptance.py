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
# resolution.py, and 0014_phase2_retrieval.py; the Phase 3 tables added by
# 0022_phase3_attention_policy.py, 0023_phase3_waiting.py, 0024_phase3_
# risk_reviews.py, 0025_phase3_capacity_planning.py, 0026_phase3_plans.py,
# and 0027_phase3_meetings.py; and the Phase 4 tables added by this branch's
# own 0030_phase4_ai_runs.py and 0031_phase4_evaluation.py -- verify_restore.
# sh's workspace-isolation check discovers workspace_id-bearing tables
# generically, so any such table without seeded rows here fails that check
# regardless of whether it's listed in ALL_PHASE1_TABLES's name.
#
# The Phase 3 tables above were never added here when Phase 3 shipped --
# a pre-existing gap, not something this pass introduced -- and went
# undetected because nothing had exercised scripts/verify_restore.sh's
# generic workspace-scoped-table discovery against a Phase-3-and-later
# schema until this branch's own CI run surfaced it (workspace isolation
# check failed for ai_run_steps, the first Phase 4 table alphabetically;
# fixing only the Phase 4 tables would have then failed on the first Phase 3
# table alphabetically instead, e.g. attention_feedback or capacity_
# profiles). This pass closes both gaps together since both block the same
# CI job. `model_definitions`, `routing_policies`, `prompt_versions`,
# `tool_definitions`, and `evaluation_sets` (migrations 0028/0029/0031) are
# deliberately absent from this tuple: per those migrations' own docstrings
# they are global platform catalog tables with no `workspace_id` column at
# all, seeded once by the migration itself, not per-workspace fixture data.
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
    # Phase 3 (migrations 0022-0027) -- pre-existing gap, closed here.
    "waiting_links",
    "risk_reviews",
    "capacity_profiles",
    "planning_constraints",
    "plans",
    "plan_blocks",
    "attention_feedback",
    "meeting_participants",
    "meeting_packs",
    # Phase 4 (migrations 0030-0031) -- this branch's own new tables.
    "ai_runs",
    "ai_run_steps",
    "evaluation_runs",
    "generated_artifacts",
    # Phase 5 (migration 0038) -- workspace-scoped, unlike Phase 4's global
    # prompt_versions/tool_definitions/model_definitions/routing_policies/
    # evaluation_sets (see 0038_phase5_workflow_schema.py's own docstring);
    # discovered generically by verify_restore.sh's workspace-isolation
    # check the same way the Phase 3/4 tables above were, so seeding them
    # here from day one avoids repeating that gap for a fifth time.
    "workflow_definitions",
    "workflow_versions",
    "automation_policies",
    "triggers",
    # Phase 5 Task 2 (migration 0039) -- workflow_runs/workflow_run_steps,
    # also workspace-scoped; seeded (not merely migrated) here from day one
    # for the identical reason the four Task 1 tables above are, closing
    # this exact gap before it repeats a third time.
    "workflow_runs",
    "workflow_run_steps",
    # Phase 5 Task 3 (migration 0040) -- approval_requests, also
    # workspace-scoped; seeded here from day one for the same reason,
    # closing this exact gap before it repeats a fourth time.
    "approval_requests",
    # Phase 5 Task 6 (migration 0042) -- compensation_steps/
    # automation_kill_switches, also workspace-scoped; seeded here from
    # day one for the same reason, closing this exact gap before it
    # repeats a fifth time.
    "compensation_steps",
    "automation_kill_switches",
    # Phase 6 Task 1 (migration 0044) -- connector_accounts/sync_cursors/
    # sync_runs, also workspace-scoped; seeded here from day one for the
    # same reason, closing this exact gap before it repeats a sixth time.
    "connector_accounts",
    "sync_cursors",
    "sync_runs",
    # Phase 6 Task 2 (migration 0045) -- repositories, also workspace-
    # scoped; seeded here from day one for the same reason, closing this
    # exact gap before it repeats a seventh time.
    "repositories",
    # Phase 6 Task 4 (migration 0047) -- engineering_work_items, also
    # workspace-scoped; seeded here from day one for the same reason,
    # closing this exact gap before it repeats an eighth time.
    "engineering_work_items",
    # Phase 6 Task 5 (migration 0048) -- changes/reviews/delivery_metric_
    # snapshots, also workspace-scoped; seeded here from day one for the
    # same reason, closing this exact gap before it repeats a ninth time.
    "changes",
    "reviews",
    "delivery_metric_snapshots",
    # Phase 6 Task 6 (migration 0049) -- incidents/engineering_decisions
    # and their change-correlation join tables, also workspace-scoped;
    # seeded here from day one for the same reason, closing this exact
    # gap before it repeats a tenth time.
    "incidents",
    "engineering_decisions",
    "incident_changes",
    "decision_changes",
    # Phase 6 Datadog connector follow-up (migration 0051) --
    # datadog_monitors/datadog_service_definitions/datadog_dashboards, also
    # workspace-scoped; seeded here from day one for the same reason,
    # closing this exact gap before it repeats an eleventh time.
    "datadog_monitors",
    "datadog_service_definitions",
    "datadog_dashboards",
    # Phase 7 Task 1 (migration 0054) -- personal_domains/domain_consents/
    # domain_sources/domain_records/goals/routines/check_ins/deletion_jobs/
    # personal_insights, also workspace-scoped; seeded here from day one
    # for the same reason, closing this exact gap before it repeats a
    # twelfth time (this exact class of gap was first found on this same
    # PR's own CI run against the check_ins table -- see _seed_personal's
    # own docstring).
    "personal_domains",
    "domain_consents",
    "domain_sources",
    "domain_records",
    "goals",
    "routines",
    "check_ins",
    "deletion_jobs",
    "personal_insights",
    # Phase 7 Task 5 part 1 (migration 0056) -- also workspace-scoped,
    # same reason as the row above.
    "cross_domain_grants",
    # Phase 7 Task 5 part 2 (migration 0059) -- also workspace-scoped, same
    # reason as the row above (this exact gap, found on this same PR's own
    # CI run this time).
    "personal_insight_feedback",
    # Phase 8 Task 2 (migration 0062) -- invitations, also workspace-scoped,
    # same reason as the row above (this exact gap, found on this same PR's
    # own CI run against the invitations table -- a thirteenth occurrence of
    # the identical class of gap).
    "invitations",
    # Phase 8 Task 3 (migration 0063) -- resource_grants, also workspace-
    # scoped, same reason as the row above (this exact gap, found on this
    # same PR's own CI run against the resource_grants table -- a
    # fourteenth occurrence of the identical class of gap).
    "resource_grants",
    # Phase 8 Task 6 (migration 0064) -- delegations, also workspace-scoped,
    # same reason as the row above (this exact gap, found on this same PR's
    # own CI run against the delegations table -- a fifteenth occurrence of
    # the identical class of gap). delegation_evidence/delegation_events are
    # deliberately absent from this tuple: neither carries a workspace_id
    # column (both are scoped indirectly via delegation_id), so verify_
    # restore.sh's generic workspace_id-column discovery never reaches them.
    "delegations",
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
        "account": seed_id(label, "account", "owner"),
        "user": seed_id(label, "user", "owner"),
        "workspace_membership": seed_id(label, "workspace_membership", "owner"),
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
        "waiting_link": seed_id(label, "waiting_link", "task_active", "person"),
        "risk_review": seed_id(label, "risk_review", "risk_active"),
        "capacity_profile": seed_id(label, "capacity_profile", "monday"),
        "planning_constraint": seed_id(label, "planning_constraint", "preference"),
        "plan": seed_id(label, "plan", "week1"),
        "plan_block": seed_id(label, "plan_block", "task_active"),
        "attention_feedback": seed_id(label, "attention_feedback", "useful"),
        "meeting_participant": seed_id(label, "meeting_participant", "person"),
        "meeting_pack": seed_id(label, "meeting_pack", "fresh"),
        "ai_run": seed_id(label, "ai_run", "explain_item"),
        "ai_run_step": seed_id(label, "ai_run_step", "model_call"),
        "evaluation_run": seed_id(label, "evaluation_run", "attention_explain_item"),
        "generated_artifact": seed_id(label, "generated_artifact", "explain_item"),
        "workflow_version": seed_id(label, "workflow_version", "acceptance"),
        "automation_policy": seed_id(label, "automation_policy", "acceptance"),
        "trigger": seed_id(label, "trigger", "acceptance"),
        "workflow_run": seed_id(label, "workflow_run", "acceptance"),
        "workflow_run_step": seed_id(label, "workflow_run_step", "acceptance"),
        "approval_request": seed_id(label, "approval_request", "acceptance"),
        "compensation_step": seed_id(label, "compensation_step", "acceptance"),
        "kill_switch": seed_id(label, "kill_switch", "acceptance"),
        "connector_account": seed_id(label, "connector_account", "acceptance"),
        "sync_cursor": seed_id(label, "sync_cursor", "acceptance"),
        "sync_run": seed_id(label, "sync_run", "acceptance"),
        "repository": seed_id(label, "repository", "acceptance"),
        "work_item": seed_id(label, "work_item", "acceptance"),
        "change": seed_id(label, "change", "acceptance"),
        "review": seed_id(label, "review", "acceptance"),
        "delivery_metric_snapshot": seed_id(label, "delivery_metric_snapshot", "acceptance"),
        "incident": seed_id(label, "incident", "acceptance"),
        "engineering_decision": seed_id(label, "engineering_decision", "acceptance"),
        "incident_change": seed_id(label, "incident_change", "acceptance"),
        "decision_change": seed_id(label, "decision_change", "acceptance"),
        "datadog_connector_account": seed_id(label, "datadog_connector_account", "acceptance"),
        "datadog_monitor": seed_id(label, "datadog_monitor", "acceptance"),
        "datadog_service_definition": seed_id(label, "datadog_service_definition", "acceptance"),
        "datadog_dashboard": seed_id(label, "datadog_dashboard", "acceptance"),
        "personal_domain": seed_id(label, "personal_domain", "habits"),
        "domain_consent": seed_id(label, "domain_consent", "habits"),
        "domain_source": seed_id(label, "domain_source", "habits"),
        "domain_record": seed_id(label, "domain_record", "habits"),
        "personal_goal": seed_id(label, "personal_goal", "habits"),
        "personal_routine": seed_id(label, "personal_routine", "habits"),
        "personal_check_in": seed_id(label, "personal_check_in", "habits"),
        "personal_deletion_job": seed_id(label, "personal_deletion_job", "habits"),
        "personal_insight": seed_id(label, "personal_insight", "habits"),
        "cross_domain_grant": seed_id(label, "cross_domain_grant", "habits"),
        "personal_insight_feedback": seed_id(label, "personal_insight_feedback", "habits"),
        "personal_domain_relationships": seed_id(label, "personal_domain", "relationships"),
        "domain_record_relationships": seed_id(label, "domain_record", "relationships"),
        "personal_domain_health": seed_id(label, "personal_domain", "health"),
        "domain_record_health": seed_id(label, "domain_record", "health"),
        "invitation": seed_id(label, "invitation", "acceptance"),
        "resource_grant": seed_id(label, "resource_grant", "acceptance"),
        # Phase 8 Task 6 -- delegations.ck_delegations_not_self forbids a
        # self-delegation, unlike resource_grants above (no such
        # restriction there), so this needs a second, genuinely distinct
        # identity within the same workspace rather than reusing "account"/
        # "user" as both parties.
        "recipient_account": seed_id(label, "account", "recipient"),
        "recipient_user": seed_id(label, "user", "recipient"),
        "recipient_workspace_membership": seed_id(label, "workspace_membership", "recipient"),
        "delegation": seed_id(label, "delegation", "acceptance"),
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
    # Phase 8 Task 1 (docs/superpowers/specs/2026-08-01-phase-8-multi-user-
    # design.md Decision 1): identity is now `accounts` (workspace-
    # independent) + `users` (unchanged FK anchor, gains account_id) +
    # `workspace_memberships` (mutable role/status). This seed identity is
    # never logged into for real (same "no-login fixture" placeholder
    # password_hash as before), only referenced by id as every other
    # fixture row's owner_id/created_by/updated_by.
    cur.execute(
        """
        INSERT INTO accounts (id, email, password_hash, display_name, created_at)
        VALUES (%(id)s, %(email)s, %(password_hash)s, %(display_name)s, %(created_at)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["account"],
            "email": f"phase1-seed-{label}@example.test",
            "password_hash": "phase1-seed-fixture-no-login",
            "display_name": f"Phase1 Seed {label.capitalize()}",
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO users (id, workspace_id, account_id, created_at)
        VALUES (%(id)s, %(workspace_id)s, %(account_id)s, %(created_at)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["user"],
            "workspace_id": ids["workspace"],
            "account_id": ids["account"],
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO workspace_memberships (
            id, workspace_id, account_id, users_id, role, status, invited_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(account_id)s, %(users_id)s, 'owner', 'active',
            %(users_id)s, %(created_at)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["workspace_membership"],
            "workspace_id": ids["workspace"],
            "account_id": ids["account"],
            "users_id": ids["user"],
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


def _seed_waiting_links(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    # counterparty_entity_id has a composite (workspace_id, entity) FK into
    # pkos_nodes (migration 0023); node_person is already seeded by
    # _seed_pkos, which runs before this function in seed()'s call order.
    cur.execute(
        """
        INSERT INTO waiting_links (
            id, workspace_id, subject_type, subject_id, counterparty_entity_id,
            direction, status, note, since_at, expected_at, superseded_by,
            created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, 'task', %(subject_id)s, %(counterparty)s,
            'waiting_on_me', 'open', %(note)s, %(since_at)s, %(expected_at)s, NULL,
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
            "expected_at": SEED_EPOCH + timedelta(days=5),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )


def _seed_risk_reviews(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    # risk_id has a composite (workspace_id, risk) RESTRICT FK into risks
    # (migration 0024); risk_active is already seeded by _seed_risks, which
    # runs before this function in seed()'s call order.
    cur.execute(
        """
        INSERT INTO risk_reviews (
            id, workspace_id, risk_id, outcome, notes, evidence_refs,
            reviewed_at, next_review_at, actor_id
        ) VALUES (
            %(id)s, %(workspace_id)s, %(risk_id)s, 'no_change', %(notes)s,
            %(evidence_refs)s, %(reviewed_at)s, %(next_review_at)s, %(actor_id)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["risk_review"],
            "workspace_id": ids["workspace"],
            "risk_id": ids["risk_active"],
            "notes": "Deterministic phase1 acceptance seed fixture risk review.",
            "evidence_refs": [f"seed://{label}/risk-review/evidence"],
            "reviewed_at": SEED_EPOCH,
            "next_review_at": SEED_EPOCH + timedelta(days=30),
            "actor_id": ids["user"],
        },
    )


def _seed_capacity_and_planning_constraints(
    cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]
) -> None:
    cur.execute(
        """
        INSERT INTO capacity_profiles (
            id, workspace_id, user_id, weekday, available_minutes, focus_minutes,
            timezone, version, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, 0, 480, 240,
            %(timezone)s, 1, %(now)s, %(now)s
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
            %(id)s, %(workspace_id)s, %(user_id)s, 'preference', NULL, NULL,
            %(label)s, NULL, NULL, 'soft', 0, %(now)s, %(now)s, 1, NULL
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["planning_constraint"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "label": f"Deterministic phase1 acceptance seed fixture constraint {label}.",
            "now": SEED_EPOCH,
        },
    )


def _seed_plans_and_blocks(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO plans (
            id, workspace_id, user_id, period_start, period_end, status,
            policy_version, capacity_minutes, source_versions, conflicts,
            unscheduled, superseded_by, accepted_at, created_by, updated_by,
            created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(user_id)s, %(period_start)s, %(period_end)s,
            'accepted', 1, 2400, '{}', '[]', '[]', NULL, %(accepted_at)s,
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["plan"],
            "workspace_id": ids["workspace"],
            "user_id": ids["user"],
            "period_start": SEED_EPOCH.date(),
            "period_end": (SEED_EPOCH + timedelta(days=6)).date(),
            "accepted_at": SEED_EPOCH,
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    # plan_id has a composite (workspace_id, plan) CASCADE FK into plans
    # (migration 0026), inserted immediately above.
    cur.execute(
        """
        INSERT INTO plan_blocks (
            id, workspace_id, plan_id, source_type, source_id, starts_at,
            ends_at, status, rationale, is_default_effort, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(plan_id)s, 'task', %(source_id)s,
            %(starts_at)s, %(ends_at)s, 'accepted', %(rationale)s, FALSE,
            %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["plan_block"],
            "workspace_id": ids["workspace"],
            "plan_id": ids["plan"],
            "source_id": ids["task_active"],
            "starts_at": SEED_EPOCH + timedelta(days=1, hours=9),
            "ends_at": SEED_EPOCH + timedelta(days=1, hours=10),
            "rationale": "Deterministic phase1 acceptance seed fixture plan block.",
            "now": SEED_EPOCH,
        },
    )


def _seed_attention_feedback(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    # target_id is a free-standing uuid column with no FK constraint
    # (migration 0022), but points at the attention_item already seeded by
    # _seed_attention_and_briefs for realism, matching this file's existing
    # convention of pointing seed FKs at other real seeded rows even where
    # the database itself would not require it.
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
            "reason": "Deterministic phase1 acceptance seed fixture attention feedback.",
            "actor_id": ids["user"],
            "created_at": SEED_EPOCH,
        },
    )


def _seed_meeting_participants_and_packs(
    cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]
) -> None:
    # meeting_id has a composite (workspace_id, meeting) RESTRICT FK into
    # meetings (migration 0027); meeting_linked is already seeded by
    # _seed_calendar_and_meetings, which runs before this function in
    # seed()'s call order. entity_id has the same shape into pkos_nodes.
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
            %(stale_at)s, '{}', %(content)s, %(actor)s, %(actor)s, %(now)s,
            %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["meeting_pack"],
            "workspace_id": ids["workspace"],
            "meeting_id": ids["meeting_linked"],
            "generated_at": SEED_EPOCH,
            "stale_at": SEED_EPOCH + timedelta(days=1),
            "content": (
                '{"participants": [], "timeline": [], "commitments": [], '
                '"decisions": [], "notes": [], "risks": [], "dependencies": [], '
                '"evidence_gaps": []}'
            ),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )


def _seed_ai_runs_and_steps(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    cur.execute(
        """
        INSERT INTO ai_runs (
            id, workspace_id, actor_id, task_type, data_class, status,
            policy_version, model_id, provider, prompt_id, prompt_version,
            input_ref, output, evidence, error_code, prompt_tokens,
            output_tokens, cost, attempts, started_at, completed_at,
            created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(actor_id)s, 'attention.explain_item',
            'internal', 'completed', 1, %(model_id)s, 'ollama',
            'attention.explain_item.v1', 1, %(input_ref)s, %(output)s,
            %(evidence)s, NULL, 120, 40, 0.0000, 1, %(started_at)s,
            %(completed_at)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["ai_run"],
            "workspace_id": ids["workspace"],
            "actor_id": ids["user"],
            "model_id": "qwen2.5:1.5b-instruct-q4_K_M",
            "input_ref": f'{{"attention_item_id": "{ids["attention_item"]}"}}',
            "output": (
                '{"explanation_text": "Deterministic phase1 acceptance seed fixture '
                'explanation.", "cited_factor_codes": ["manual_priority"]}'
            ),
            "evidence": '["manual_priority"]',
            "started_at": SEED_EPOCH,
            "completed_at": SEED_EPOCH + timedelta(seconds=5),
            "now": SEED_EPOCH,
        },
    )
    # run_id has a composite (workspace_id, run) CASCADE FK into ai_runs
    # (migration 0030), inserted immediately above.
    cur.execute(
        """
        INSERT INTO ai_run_steps (
            id, workspace_id, run_id, sequence, kind, status, trace, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(run_id)s, 1, 'model_call', 'succeeded',
            %(trace)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["ai_run_step"],
            "workspace_id": ids["workspace"],
            "run_id": ids["ai_run"],
            "trace": ('{"model_id": "qwen2.5:1.5b-instruct-q4_K_M", "duration_ms": 850}'),
            "created_at": SEED_EPOCH,
        },
    )


def _seed_evaluation_run(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    # evaluation_set_id is a plain (non-composite) FK into the single global
    # evaluation_sets row migration 0031 seeds for task_type
    # 'attention.explain_item' -- that row's id is generated with uuid4() at
    # migration time, not derivable via seed_id(), so it is looked up here
    # rather than hardcoded. Both workspaces reference the same global row.
    cur.execute(
        "SELECT id, version FROM evaluation_sets WHERE task_type = %s AND status = 'active'",
        ("attention.explain_item",),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            "no active evaluation_sets row for task_type='attention.explain_item' -- "
            "expected migration 0031_phase4_evaluation.py to have seeded exactly one"
        )
    evaluation_set_id, dataset_version = row

    cur.execute(
        """
        INSERT INTO evaluation_runs (
            id, workspace_id, actor_id, task_type, evaluation_set_id,
            dataset_version, prompt_id, prompt_version, model_id, provider,
            total_examples, schema_validity_rate, grounding_rate,
            prohibited_fact_count, latency_p95_seconds, passed, failures,
            status, started_at, completed_at, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(actor_id)s, 'attention.explain_item',
            %(evaluation_set_id)s, %(dataset_version)s, 'attention.explain_item.v1',
            1, %(model_id)s, 'ollama', 20, 1.0000, 1.0000, 0, 1.250, TRUE, '[]',
            'completed', %(started_at)s, %(completed_at)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["evaluation_run"],
            "workspace_id": ids["workspace"],
            "actor_id": ids["user"],
            "evaluation_set_id": evaluation_set_id,
            "dataset_version": dataset_version,
            "model_id": "qwen2.5:1.5b-instruct-q4_K_M",
            "started_at": SEED_EPOCH,
            "completed_at": SEED_EPOCH + timedelta(minutes=2),
            "created_at": SEED_EPOCH,
        },
    )


def _seed_generated_artifact(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    # ai_run_id has a composite (workspace_id, ai_run) CASCADE FK into
    # ai_runs (migration 0031); ai_run is already seeded by
    # _seed_ai_runs_and_steps, which runs before this function in seed()'s
    # call order.
    cur.execute(
        """
        INSERT INTO generated_artifacts (
            id, workspace_id, ai_run_id, task_type, source_versions,
            schema_version, output, evidence, status, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(ai_run_id)s, 'attention.explain_item',
            %(source_versions)s, 'attention.explain_item.output.v1', %(output)s,
            %(evidence)s, 'proposed', %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["generated_artifact"],
            "workspace_id": ids["workspace"],
            "ai_run_id": ids["ai_run"],
            "source_versions": (
                f'{{"attention_item_id": "{ids["attention_item"]}", "source_entity_version": 1}}'
            ),
            "output": (
                '{"explanation_text": "Deterministic phase1 acceptance seed fixture '
                'explanation.", "cited_factor_codes": ["manual_priority"]}'
            ),
            "evidence": '["manual_priority"]',
            "created_at": SEED_EPOCH,
        },
    )


_AUTOMATION_WORKFLOW_ID = "phase1-seed.automation.acceptance"


def _seed_automation(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    """Phase 5 (migrations 0038-0040) -- workflow_definitions/workflow_
    versions/automation_policies/triggers (Task 1), workflow_runs/
    workflow_run_steps (Task 2), plus approval_requests (Task 3), all
    workspace-scoped (unlike Phase 4's global catalog tables).
    ``workflow_definitions`` is the family identity row every other table
    here FK's against; it must be inserted first. ``workflow_versions.
    policy_ref`` carries no FK constraint (migration 0038's own docstring),
    so referencing ``ids["automation_policy"]`` here is safe regardless of
    insert order relative to the policy row itself. ``workflow_runs`` pins
    ``workflow_version = 1`` via a real composite FK (migration 0039) --
    the seeded version row above must exist first, but its ``status`` does
    not need to be ``active`` for that FK to hold, so this seeds one
    ``succeeded`` run against the still-``draft`` version seeded above
    rather than also seeding a second, ``active`` version solely for this
    run to reference. ``approval_requests`` composite-FK's ``(workspace_id,
    run_id)`` to ``workflow_runs`` (migration 0040) -- the seeded run above
    must exist first; this seeds one ``approved`` (fully decided, not
    ``pending``) request bound to the exact same ``action_digest`` the
    seeded ``workflow_run_steps`` row above carries, representing a
    step that genuinely required and received approval before it
    dispatched -- a more representative fixture than a still-``pending``
    row that would look like a permanently-stuck seed. This function is
    Task 1's own ``_seed_automation`` extended in place (Task 2 and now
    Task 3's own instruction), following the exact ``workflow_runs``/
    ``workflow_run_steps``/``approval_requests`` naturally-follows-
    ``workflow_definitions``/``workflow_versions`` sequencing Task 1's fix
    commit established, not a new, duplicate ``_seed_*`` function.
    """
    graph = (
        '{"steps": [{"step_id": "notify", "step_type": "action", '
        '"action_ref": "local.send_test_notification", "input_mapping": {}, '
        '"on_success": "succeeded", "on_failure": "failed"}]}'
    )
    trigger_refs = "[]"
    definition_hash = sha256(
        (graph + trigger_refs + str(ids["automation_policy"])).encode("utf-8")
    ).hexdigest()

    cur.execute(
        """
        INSERT INTO workflow_definitions (
            id, workspace_id, workflow_id, created_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(workflow_id)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": seed_id(label, "workflow_definition", "acceptance"),
            "workspace_id": ids["workspace"],
            "workflow_id": _AUTOMATION_WORKFLOW_ID,
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO workflow_versions (
            id, workspace_id, workflow_id, version, graph, trigger_refs,
            policy_ref, definition_hash, status, created_by, updated_by,
            created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(workflow_id)s, 1, %(graph)s, %(trigger_refs)s,
            %(policy_ref)s, %(definition_hash)s, 'draft', %(actor)s, %(actor)s,
            %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["workflow_version"],
            "workspace_id": ids["workspace"],
            "workflow_id": _AUTOMATION_WORKFLOW_ID,
            "graph": graph,
            "trigger_refs": trigger_refs,
            "policy_ref": ids["automation_policy"],
            "definition_hash": definition_hash,
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO automation_policies (
            id, workspace_id, workflow_id, action_types, data_classes,
            value_limit, count_limit, rate_limit, schedule, approval_mode,
            expires_at, revoked_at, version, created_by, updated_by,
            created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(workflow_id)s, %(action_types)s, %(data_classes)s,
            0, 1, %(rate_limit)s, NULL, 'preview_only', %(expires_at)s, NULL, 1,
            %(actor)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["automation_policy"],
            "workspace_id": ids["workspace"],
            "workflow_id": _AUTOMATION_WORKFLOW_ID,
            "action_types": ["bounded"],
            "data_classes": ["internal"],
            "rate_limit": '{"runs_per_workflow_per_hour": 10}',
            "expires_at": SEED_EPOCH + timedelta(days=90),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO triggers (
            id, workspace_id, workflow_id, trigger_type, event_type_filter,
            schedule_expression, timezone, skip_missed, created_by, updated_by,
            created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(workflow_id)s, 'manual', NULL, NULL, NULL,
            FALSE, %(actor)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["trigger"],
            "workspace_id": ids["workspace"],
            "workflow_id": _AUTOMATION_WORKFLOW_ID,
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )

    # Phase 5 Task 2 (migration 0039) -- workflow_runs/workflow_run_steps.
    # A single completed run pinned to the ``workflow_versions`` row seeded
    # above (version 1 -- the FK only requires the row to exist, not to be
    # ``active``, so pointing at the still-``draft`` seed version is valid
    # and avoids seeding a second, ``active`` version just for this run to
    # reference). ``action_digest`` is computed inline with the same
    # ``sha256``-over-canonical-material shape ``worker.compute_action_
    # digest`` uses at runtime, not by importing that function -- mirrors
    # how ``definition_hash`` above is computed inline rather than via
    # ``ecc.domains.automation.workflows.compute_definition_hash``.
    step_input = '{"note": "phase1 acceptance seed fixture -- no secret material"}'
    step_output = '{"result": "ok"}'
    action_digest = sha256(
        (_AUTOMATION_WORKFLOW_ID + "1" + "notify" + step_input).encode("utf-8")
    ).hexdigest()
    cur.execute(
        """
        INSERT INTO workflow_runs (
            id, workspace_id, workflow_id, workflow_version, policy_id,
            trigger_ref, status, current_step_index, leased_by, leased_until,
            lease_heartbeat_at, cancel_requested_at, queued_at, started_at,
            finished_at, created_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(workflow_id)s, 1, %(policy_ref)s,
            'manual:phase1-seed', 'succeeded', 1, NULL, NULL, NULL, NULL,
            %(now)s, %(now)s, %(now)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["workflow_run"],
            "workspace_id": ids["workspace"],
            "workflow_id": _AUTOMATION_WORKFLOW_ID,
            "policy_ref": ids["automation_policy"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO workflow_run_steps (
            id, workspace_id, run_id, step_index, step_type, status,
            action_digest, input, output, started_at, finished_at,
            error_class, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(run_id)s, 0, 'action', 'succeeded',
            %(action_digest)s, %(input)s, %(output)s, %(now)s, %(now)s,
            NULL, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["workflow_run_step"],
            "workspace_id": ids["workspace"],
            "run_id": ids["workflow_run"],
            "action_digest": action_digest,
            "input": step_input,
            "output": step_output,
            "now": SEED_EPOCH,
        },
    )

    # Phase 5 Task 3 (migration 0040) -- approval_requests. Bound to the
    # exact same action_digest as the workflow_run_steps row above (the
    # "approval is bound to the exact digest it approved" property,
    # DATA-MODEL.md) -- represents a high-impact step that genuinely
    # required and received approval before its adapter's execute() ran.
    # 'approved', not 'pending': decided_at/decision/decided_by are all
    # NOT NULL together (ck_approval_requests_decided_matches_decision,
    # migration 0040), the more representative fixture for a step that
    # already ran to 'succeeded'.
    cur.execute(
        """
        INSERT INTO approval_requests (
            id, workspace_id, run_id, step_index, action_digest,
            high_impact_categories, status, requested_at, expires_at,
            decided_at, decision, decided_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(run_id)s, 0, %(action_digest)s,
            %(high_impact_categories)s, 'approved', %(now)s, %(expires_at)s,
            %(now)s, 'approved', %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["approval_request"],
            "workspace_id": ids["workspace"],
            "run_id": ids["workflow_run"],
            "action_digest": action_digest,
            "high_impact_categories": ["person-directed"],
            "expires_at": SEED_EPOCH + timedelta(hours=24),
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )

    # Phase 5 Task 6 (migration 0042) -- compensation_steps/automation_
    # kill_switches. ``compensation_steps`` represents one already-recorded
    # compensation for the seeded run's own step 0 (``compensates_step_
    # index=0``), bound to the same ``action_digest`` computed above,
    # ``status='succeeded'`` -- a representative "compensation already ran
    # and completed" fixture rather than a still-``pending`` row.
    # ``automation_kill_switches`` seeds one *deactivated* global switch
    # (``workflow_id IS NULL``, ``active=false``) -- a genuine historical
    # audit-trail row (the append-only-ish shape migration 0042 documents)
    # that does not leave an active kill switch in seeded data able to
    # affect any other acceptance check that might separately enqueue or
    # claim a run against these fixtures.
    cur.execute(
        """
        INSERT INTO compensation_steps (
            id, workspace_id, run_id, compensates_step_index, action_digest,
            status, started_at, finished_at, error_class, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(run_id)s, 0, %(action_digest)s,
            'succeeded', %(now)s, %(now)s, NULL, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["compensation_step"],
            "workspace_id": ids["workspace"],
            "run_id": ids["workflow_run"],
            "action_digest": action_digest,
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO automation_kill_switches (
            id, workspace_id, workflow_id, active, reason, activated_by,
            activated_at, deactivated_by, deactivated_at, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, NULL, FALSE, %(reason)s, %(actor)s,
            %(now)s, %(actor)s, %(deactivated_at)s, %(now)s, %(deactivated_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["kill_switch"],
            "workspace_id": ids["workspace"],
            "reason": "phase1 acceptance seed fixture -- deactivated historical switch",
            "actor": ids["user"],
            "now": SEED_EPOCH,
            "deactivated_at": SEED_EPOCH + timedelta(hours=1),
        },
    )


def _seed_engineering(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    """Phase 6 Task 1 (migration 0044) -- connector_accounts/sync_cursors/
    sync_runs, workspace-scoped like every Phase 5 automation table above.
    ``encrypted_credentials`` holds fixture bytes only -- never a real
    Fernet ciphertext derived from an actual secret, and never decrypted by
    anything this drill exercises -- mirroring how the Phase 5 seed above
    uses fixture ``input``/``output`` JSON rather than any real payload.
    One ``sync_cursors`` row and one completed ``sync_runs`` row reference
    the seeded ``connector_accounts`` row via its composite
    ``(workspace_id, connector_account_id)`` FK.

    Phase 6 Task 2 (migration 0045) extends this same function with one
    ``repositories`` row, for the identical reason: workspace-scoped,
    discovered generically by ``verify_restore.sh``'s workspace-isolation
    check, so it must be seeded here from day one rather than repeat the
    Task 1 gap CI found on this table's own first PR.

    Phase 6 Task 4 (migration 0047) extends this same function with one
    ``engineering_work_items`` row, for the identical reason.

    Phase 6 Task 6 (migration 0049) extends this same function with one
    resolved ``incidents`` row, one decided ``engineering_decisions``
    row, and one join row each in ``incident_changes``/
    ``decision_changes`` linking both back to the seeded ``changes`` row
    above -- workspace-authored (not connector-synced), but still
    workspace-scoped and therefore discovered generically by
    ``verify_restore.sh``'s workspace-isolation check, the same reason
    every table above must be seeded here.
    """
    cur.execute(
        """
        INSERT INTO connector_accounts (
            id, workspace_id, provider, external_account_id, display_name,
            granted_scopes, encrypted_credentials, status, status_detail,
            last_synced_at, last_error, disconnected_at, version,
            created_by, updated_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'sandbox', %(external_account_id)s, %(display_name)s,
            %(granted_scopes)s, %(encrypted_credentials)s, 'active', NULL,
            %(now)s, NULL, NULL, 1,
            %(actor)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["connector_account"],
            "workspace_id": ids["workspace"],
            "external_account_id": f"sandbox-{label}-acceptance",
            "display_name": "Phase 1 acceptance seed connector",
            "granted_scopes": ["contents:read", "metadata:read"],
            "encrypted_credentials": b"phase1-seed-fixture-not-a-real-credential",
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO sync_cursors (
            id, workspace_id, connector_account_id, resource_type, cursor_value, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'repository', '1', %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["sync_cursor"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["connector_account"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO sync_runs (
            id, workspace_id, connector_account_id, run_type, status,
            items_processed, error_summary, started_at, completed_at, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'backfill', 'succeeded',
            3, NULL, %(now)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["sync_run"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["connector_account"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO repositories (
            id, workspace_id, connector_account_id, provider, external_id,
            name, source_url, default_branch, permission_state, freshness_state,
            content_hash, provider_updated_at, observed_at, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'sandbox', %(external_id)s,
            %(name)s, %(source_url)s, 'main', 'active', 'fresh',
            %(content_hash)s, %(now)s, %(now)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["repository"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["connector_account"],
            "external_id": f"acceptance-repo-{label}",
            "name": f"acceptance/{label}-repo",
            "source_url": f"https://example.test/acceptance/{label}-repo",
            "content_hash": sha256(f"acceptance-repo-{label}".encode()).hexdigest(),
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO engineering_work_items (
            id, workspace_id, connector_account_id, provider, external_id,
            title, source_url, item_type, status, reporter_external_id,
            assignee_external_id, permission_state, freshness_state,
            content_hash, provider_updated_at, observed_at, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'sandbox', %(external_id)s,
            %(title)s, %(source_url)s, 'Task', 'To Do', %(reporter_external_id)s,
            %(assignee_external_id)s, 'active', 'fresh',
            %(content_hash)s, %(now)s, %(now)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["work_item"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["connector_account"],
            "external_id": f"acceptance-work-item-{label}",
            "title": f"Acceptance work item ({label})",
            "source_url": f"https://example.test/acceptance/{label}-work-item",
            "reporter_external_id": f"acceptance-reporter-{label}",
            "assignee_external_id": f"acceptance-assignee-{label}",
            "content_hash": sha256(f"acceptance-work-item-{label}".encode()).hexdigest(),
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO changes (
            id, workspace_id, connector_account_id, repository_id, provider,
            external_id, provider_number, title, source_url, status, merged_at,
            permission_state, freshness_state, content_hash, provider_updated_at,
            observed_at, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, %(repository_id)s, 'sandbox',
            %(external_id)s, %(provider_number)s, %(title)s, %(source_url)s, 'closed', %(now)s,
            'active', 'fresh', %(content_hash)s, %(now)s,
            %(now)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["change"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["connector_account"],
            "repository_id": ids["repository"],
            "external_id": f"acceptance-change-{label}",
            "provider_number": "1",
            "title": f"Acceptance change ({label})",
            "source_url": f"https://example.test/acceptance/{label}-change",
            "content_hash": sha256(f"acceptance-change-{label}".encode()).hexdigest(),
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO reviews (
            id, workspace_id, connector_account_id, change_id, provider,
            external_id, source_url, reviewer_external_id, review_state,
            requested_at, submitted_at, permission_state, freshness_state,
            content_hash, provider_updated_at, observed_at, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, %(change_id)s, 'sandbox',
            %(external_id)s, %(source_url)s, %(reviewer_external_id)s, 'approved',
            %(now)s, %(now)s, 'active', 'fresh',
            %(content_hash)s, %(now)s, %(now)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["review"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["connector_account"],
            "change_id": ids["change"],
            "external_id": f"acceptance-review-{label}",
            "source_url": f"https://example.test/acceptance/{label}-review",
            "reviewer_external_id": f"acceptance-reviewer-{label}",
            "content_hash": sha256(f"acceptance-review-{label}".encode()).hexdigest(),
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO delivery_metric_snapshots (
            id, workspace_id, metric_key, definition_version, aggregation_scope,
            window_label, population, numerator, denominator, value,
            coverage_status, coverage_percentage, computed_at, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'blocked_work', 1, 'system',
            'point_in_time', 0, 0, 0, 0,
            'insufficient_coverage', 0, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["delivery_metric_snapshot"],
            "workspace_id": ids["workspace"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO incidents (
            id, workspace_id, title, description, severity, status,
            detected_at, resolved_at, version, created_by, updated_by,
            created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(title)s, %(description)s, 'high', 'resolved',
            %(detected_at)s, %(resolved_at)s, 1, %(actor)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["incident"],
            "workspace_id": ids["workspace"],
            "title": f"Acceptance incident ({label})",
            "description": "Phase 1 acceptance seed fixture incident",
            "detected_at": SEED_EPOCH - timedelta(hours=2),
            "resolved_at": SEED_EPOCH,
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO engineering_decisions (
            id, workspace_id, title, description, rationale, status,
            decided_at, version, created_by, updated_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(title)s, %(description)s, %(rationale)s, 'decided',
            %(decided_at)s, 1, %(actor)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["engineering_decision"],
            "workspace_id": ids["workspace"],
            "title": f"Acceptance decision ({label})",
            "description": "Phase 1 acceptance seed fixture decision",
            "rationale": "Because the acceptance drill needs a decided row",
            "decided_at": SEED_EPOCH,
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO incident_changes (id, workspace_id, incident_id, change_id, created_at)
        VALUES (%(id)s, %(workspace_id)s, %(incident_id)s, %(change_id)s, %(now)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["incident_change"],
            "workspace_id": ids["workspace"],
            "incident_id": ids["incident"],
            "change_id": ids["change"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO decision_changes (id, workspace_id, decision_id, change_id, created_at)
        VALUES (%(id)s, %(workspace_id)s, %(decision_id)s, %(change_id)s, %(now)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["decision_change"],
            "workspace_id": ids["workspace"],
            "decision_id": ids["engineering_decision"],
            "change_id": ids["change"],
            "now": SEED_EPOCH,
        },
    )

    # Datadog connector follow-up (migration 0051) -- a separate
    # connector_account (provider='datadog') from the 'sandbox' one seeded
    # above, since a real Datadog row would genuinely reference one of its
    # own rather than another provider's; one row each in the three new
    # projection tables, for the identical reason repositories/engineering_
    # work_items are seeded here: workspace-scoped tables discovered
    # generically by verify_restore.sh's workspace-isolation check.
    cur.execute(
        """
        INSERT INTO connector_accounts (
            id, workspace_id, provider, external_account_id, display_name,
            granted_scopes, encrypted_credentials, status, status_detail,
            last_synced_at, last_error, disconnected_at, version,
            created_by, updated_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, 'datadog', %(external_account_id)s, %(display_name)s,
            %(granted_scopes)s, %(encrypted_credentials)s, 'active', NULL,
            %(now)s, NULL, NULL, 1,
            %(actor)s, %(actor)s, %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["datadog_connector_account"],
            "workspace_id": ids["workspace"],
            "external_account_id": f"datadog-{label}-acceptance",
            "display_name": "Phase 1 acceptance seed Datadog connector",
            "granted_scopes": [],
            "encrypted_credentials": b"phase1-seed-fixture-not-a-real-credential",
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO datadog_monitors (
            id, workspace_id, connector_account_id, provider, external_id,
            source_url, permission_state, freshness_state, content_hash,
            provider_updated_at, observed_at, created_at, updated_at,
            name, monitor_type, query, overall_state
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'datadog', %(external_id)s,
            %(source_url)s, 'active', 'fresh', %(content_hash)s,
            %(now)s, %(now)s, %(now)s, %(now)s,
            %(name)s, 'metric alert', 'avg(last_5m):avg:system.load.1{*} > 1', 'OK'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["datadog_monitor"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["datadog_connector_account"],
            "external_id": f"acceptance-monitor-{label}",
            "source_url": f"https://app.datadoghq.com/monitors/acceptance-{label}",
            "content_hash": sha256(f"acceptance-monitor-{label}".encode()).hexdigest(),
            "name": f"acceptance-{label}-monitor",
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO datadog_service_definitions (
            id, workspace_id, connector_account_id, provider, external_id,
            source_url, permission_state, freshness_state, content_hash,
            observed_at, created_at, updated_at,
            name, team, tier, description
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'datadog', %(external_id)s,
            %(source_url)s, 'active', 'fresh', %(content_hash)s,
            %(now)s, %(now)s, %(now)s,
            %(name)s, %(team)s, '1', 'Phase 1 acceptance seed fixture service'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["datadog_service_definition"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["datadog_connector_account"],
            "external_id": f"acceptance-service-{label}",
            "source_url": f"https://app.datadoghq.com/services/acceptance-{label}-service",
            "content_hash": sha256(f"acceptance-service-{label}".encode()).hexdigest(),
            "name": f"acceptance-{label}-service",
            "team": f"team-acceptance-{label}",
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO datadog_dashboards (
            id, workspace_id, connector_account_id, provider, external_id,
            source_url, permission_state, freshness_state, content_hash,
            provider_updated_at, observed_at, created_at, updated_at,
            title, description
        ) VALUES (
            %(id)s, %(workspace_id)s, %(connector_account_id)s, 'datadog', %(external_id)s,
            %(source_url)s, 'active', 'fresh', %(content_hash)s,
            %(now)s, %(now)s, %(now)s, %(now)s,
            %(title)s, 'Phase 1 acceptance seed fixture dashboard'
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["datadog_dashboard"],
            "workspace_id": ids["workspace"],
            "connector_account_id": ids["datadog_connector_account"],
            "external_id": f"acceptance-dashboard-{label}",
            "source_url": f"https://app.datadoghq.com/dashboard/acceptance-{label}",
            "content_hash": sha256(f"acceptance-dashboard-{label}".encode()).hexdigest(),
            "title": f"Acceptance dashboard ({label})",
            "now": SEED_EPOCH,
        },
    )


def _seed_personal(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    """Phase 7 Task 1 (migration 0054) -- personal_domains/domain_consents/
    domain_sources/domain_records/goals/routines/check_ins/deletion_jobs/
    personal_insights, workspace-scoped like every table above. This exact
    gap (a new workspace-scoped table with no seed row here) was found by
    this same PR's own CI run, failing ``verify_restore.sh``'s generic
    workspace-isolation check on ``check_ins`` -- closed here for the whole
    table set at once, matching every prior phase's own precedent for the
    identical class of gap.

    One row per table, all under the ``habits`` reference domain: a
    ``personal_domains`` row (enabled), one ``domain_consents`` grant, one
    ``domain_sources``/``domain_records`` pair, one ``goals`` row, one
    ``routines`` row (referencing the goal), one ``check_ins`` row
    (referencing the routine), one completed ``deletion_jobs`` row (an
    audit record of a hypothetical prior deletion -- its existence does not
    imply the domain above is currently deleted, matching how this table's
    own real callers write it purely as an append-only log), and one
    ``personal_insights`` row (dismissed, so this fixture does not depend
    on any particular gap-computation threshold holding true against
    ``SEED_EPOCH`` at verification time), one ``cross_domain_grants``
    row (Phase 7 Task 5 part 1, migration 0056 -- an indefinite,
    ``insight_generation``-purposed grant against the same ``habits``
    domain; no consumer reads it yet, this fixture only needs to prove the
    table itself round-trips through backup/restore), and one
    ``personal_insight_feedback`` row (Phase 7 Task 5 part 2, migration
    0059 -- referencing the ``personal_insights`` row above; this fixture
    only needs to prove the table round-trips, not that it reflects real
    user feedback).

    Also: one ``personal_domains``/``domain_records`` pair each for
    ``relationships`` (``sensitive``) and ``health`` (``high_stakes``),
    closing a residual risk ``DOMAIN-PRIVACY-CONTRACT.md`` had disclosed
    since Task 1 and never actually closed: "backup/restore of sensitive/
    high_stakes records is not yet independently exercised." Every row
    seeded above this comment is under ``habits`` (``standard``, no
    encrypted field at all), so it never actually proved a ``sensitive``/
    ``high_stakes`` row -- specifically, an *encrypted* JSONB payload field
    -- survives ``pg_dump``/``pg_restore`` intact. The ``notes``/``symptom_
    description`` values below are NOT real Fernet ciphertext (this script
    stays psycopg-plus-stdlib-only, deliberately not importing ``ecc.
    domains.personal.crypto`` or ``ecc.config``, both of which pull in
    pydantic); they are fixed strings shaped like a real Fernet token
    (URL-safe base64 text) instead. That is sufficient for what this drill
    actually needs: proving an opaque JSONB text value in a ``sensitive``/
    ``high_stakes``-classified row round-trips byte-for-byte through
    backup and restore, and stays workspace-isolated -- backup/restore
    itself operates on raw bytes with no awareness of encryption, so a
    placeholder with the right shape verifies the identical property real
    ciphertext would. Real encrypt/decrypt *correctness* (as opposed to
    byte-preservation across a backup/restore cycle) is separately and
    thoroughly proven by ``tests/test_personal_relationships_postgres.py``/
    ``tests/test_personal_health_postgres.py``'s own direct-SQL
    verification -- this script was never the right place for that.
    """
    cur.execute(
        """
        INSERT INTO personal_domains (
            id, workspace_id, owner_id, domain_key, classification,
            enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', 'standard',
            true, %(now)s, %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_domain"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO domain_consents (
            id, workspace_id, owner_id, domain_key, granted_at, created_at
        ) VALUES (%(id)s, %(workspace_id)s, %(owner_id)s, 'habits', %(now)s, %(now)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["domain_consent"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO domain_sources (
            id, workspace_id, owner_id, domain_key, source_type, label, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', 'manual',
            'Phase 1 acceptance seed source', %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["domain_source"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO domain_records (
            id, workspace_id, owner_id, domain_key, record_type, payload,
            domain_source_id, effective_at, created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', 'note', '{}'::jsonb,
            %(domain_source_id)s, %(now)s, %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["domain_record"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "domain_source_id": ids["domain_source"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO goals (
            id, workspace_id, owner_id, domain_key, title, status,
            created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits',
            'Phase 1 acceptance seed goal', 'active',
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_goal"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO routines (
            id, workspace_id, owner_id, domain_key, goal_id, title, cadence,
            created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', %(goal_id)s,
            'Phase 1 acceptance seed routine', 'daily',
            %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_routine"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "goal_id": ids["personal_goal"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO check_ins (
            id, workspace_id, owner_id, domain_key, routine_id, occurred_at, created_by, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', %(routine_id)s,
            %(now)s, %(actor)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_check_in"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "routine_id": ids["personal_routine"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO deletion_jobs (
            id, workspace_id, owner_id, domain_key, scope, status,
            requested_at, completed_at, created_by
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', 'domain', 'completed',
            %(now)s, %(now)s, %(actor)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_deletion_job"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO personal_insights (
            id, workspace_id, owner_id, domain_key, insight_key, kind, title,
            evidence, source_period_start, source_period_end, missing_data,
            confidence, limitations, computed_at, dismissed_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', %(insight_key)s, 'observation',
            'Phase 1 acceptance seed insight', '{}'::jsonb, %(now)s, %(now)s, NULL,
            'high', 'Fixture data only, not a real computed observation.', %(now)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_insight"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "insight_key": f"acceptance-{label}",
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO cross_domain_grants (
            id, workspace_id, owner_id, source_domain_key, purpose,
            granted_categories, granted_at, expires_at, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'habits', 'insight_generation',
            '["habit_check_in"]'::jsonb, %(now)s, NULL, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["cross_domain_grant"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO personal_insight_feedback (
            id, workspace_id, owner_id, insight_id, useful, comment, created_by, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, %(insight_id)s, true,
            'Phase 1 acceptance seed feedback', %(actor)s, %(now)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_insight_feedback"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "insight_id": ids["personal_insight"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )

    # `relationships` (`sensitive`) and `health` (`high_stakes`) -- see this
    # function's own docstring for why these two extra domain/record pairs
    # exist and why their "encrypted" field is a placeholder, not real
    # Fernet ciphertext.
    cur.execute(
        """
        INSERT INTO personal_domains (
            id, workspace_id, owner_id, domain_key, classification,
            enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'relationships', 'sensitive',
            true, %(now)s, %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_domain_relationships"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    # `notes` is shaped like a real Fernet token (URL-safe base64 text), not
    # actually produced by `encrypt_field` -- see this function's own
    # docstring for why.
    cur.execute(
        """
        INSERT INTO domain_records (
            id, workspace_id, owner_id, domain_key, record_type, payload,
            effective_at, retention_acknowledged_at, created_by, updated_by,
            created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'relationships', 'contact',
            '{"contact_name": "Phase 1 acceptance seed contact",
              "notes": "gAAAAABphase1acceptanceplaceholdernotrealciphertext=="}'::jsonb,
            %(now)s, NULL, %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["domain_record_relationships"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO personal_domains (
            id, workspace_id, owner_id, domain_key, classification,
            enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'health', 'high_stakes',
            true, %(now)s, %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["personal_domain_health"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )
    # Same placeholder-shape convention as `relationships` above;
    # `symptom_log` has two encrypted fields.
    cur.execute(
        """
        INSERT INTO domain_records (
            id, workspace_id, owner_id, domain_key, record_type, payload,
            effective_at, retention_acknowledged_at, created_by, updated_by,
            created_at, updated_at, version
        ) VALUES (
            %(id)s, %(workspace_id)s, %(owner_id)s, 'health', 'symptom_log',
            '{"severity": "mild",
              "symptom_description": "gAAAAABphase1acceptanceplaceholdernotrealciphertext01==",
              "notes": "gAAAAABphase1acceptanceplaceholdernotrealciphertext02=="}'::jsonb,
            %(now)s, %(now)s, %(actor)s, %(actor)s, %(now)s, %(now)s, 1
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["domain_record_health"],
            "workspace_id": ids["workspace"],
            "owner_id": ids["user"],
            "actor": ids["user"],
            "now": SEED_EPOCH,
        },
    )


def _seed_invitations(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    """Phase 8 Task 2 (migration 0062) -- invitations, workspace-scoped like
    every table above. This exact gap (a new workspace-scoped table with no
    seed row here) was found by this same PR's own CI run, failing
    ``verify_restore.sh``'s generic workspace-isolation check on
    ``invitations`` -- closed here the same way every prior phase's own
    identical gap was.

    One still-pending row per workspace: ``invited_by`` references the
    workspace's own owner-role ``users`` row seeded in ``_seed_workspace``
    (the same composite-FK-anchor role ``owner_id``/``created_by`` already
    play elsewhere), and ``token_hash`` is a fixed placeholder digest -- this
    fixture is never actually accepted through the real endpoint, it only
    needs to prove the table round-trips through backup/restore and stays
    workspace-isolated. ``expires_at`` uses the same far-future horizon as
    the fixture ``sessions`` row in ``_seed_workspace``, so this row never
    reads as expired regardless of when the acceptance drill runs.
    """
    cur.execute(
        """
        INSERT INTO invitations (
            id, workspace_id, email, role, token_hash, invited_by,
            expires_at, accepted_at, rejected_at, revoked_at, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(email)s, 'member', %(token_hash)s,
            %(invited_by)s, %(expires_at)s, NULL, NULL, NULL, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["invitation"],
            "workspace_id": ids["workspace"],
            "email": f"phase1-seed-invitee-{label}@example.test",
            "token_hash": sha256(f"phase1-seed-invitation-{label}".encode()).hexdigest(),
            "invited_by": ids["user"],
            "expires_at": SEED_EPOCH + timedelta(days=36500),
            "created_at": SEED_EPOCH,
        },
    )


def _seed_resource_grants(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    """Phase 8 Task 3 (migration 0063) -- resource_grants, workspace-scoped
    like every table above. This exact gap (a new workspace-scoped table
    with no seed row here) was found by this same PR's own CI run, failing
    ``verify_restore.sh``'s generic workspace-isolation check on
    ``resource_grants`` -- closed here the same way every prior phase's own
    identical gap was.

    One still-active grant row per workspace, naming the workspace's own
    seeded ``incident`` fixture (``_seed_engineering``, already present by
    the time this runs) as the resource and the workspace's own owner
    account as both granter and grantee -- ``resource_grants`` has no
    self-grant restriction at the schema level, and this fixture is never
    read through the real `authorize()`/`/sharing/grants` endpoints, only
    needs to prove the table round-trips through backup/restore and stays
    workspace-isolated, the same reasoning ``_seed_invitations``'s own
    docstring gives for its placeholder token_hash.
    """
    cur.execute(
        """
        INSERT INTO resource_grants (
            id, workspace_id, grantee_account_id, resource_type, resource_id,
            actions, granted_by, expires_at, revoked_at, created_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(grantee_account_id)s, 'incidents',
            %(resource_id)s, %(actions)s, %(granted_by)s, %(expires_at)s,
            NULL, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["resource_grant"],
            "workspace_id": ids["workspace"],
            "grantee_account_id": ids["account"],
            "resource_id": ids["incident"],
            "actions": ["read"],
            "granted_by": ids["user"],
            "expires_at": SEED_EPOCH + timedelta(days=36500),
            "created_at": SEED_EPOCH,
        },
    )


def _seed_delegations(cur: psycopg.Cursor[Any], label: str, ids: Mapping[str, UUID]) -> None:
    """Phase 8 Task 6 (migration 0064) -- delegations, workspace-scoped like
    every table above. This exact gap (a new workspace-scoped table with no
    seed row here) was found by this same PR's own CI run, failing
    ``verify_restore.sh``'s generic workspace-isolation check on
    ``delegations`` -- closed here the same way every prior phase's own
    identical gap was.

    Unlike ``_seed_resource_grants`` (which reuses the workspace's own
    single seeded owner identity as both granter and grantee, since
    ``resource_grants`` has no self-grant restriction), ``delegations.
    ck_delegations_not_self`` forbids delegator and recipient from being the
    same account -- so this seeds a genuinely second identity (``member``
    role, active) first, then one still-``proposed`` delegation row naming
    the workspace's own seeded ``incident`` fixture (``_seed_engineering``,
    already present by the time this runs) as the obligation. Never read
    through the real ``ecc.domains.collaboration.delegations`` endpoints,
    only needs to prove the table round-trips through backup/restore and
    stays workspace-isolated -- the same reasoning ``_seed_resource_grants``'s
    own docstring gives.
    """
    cur.execute(
        """
        INSERT INTO accounts (id, email, password_hash, display_name, created_at)
        VALUES (%(id)s, %(email)s, %(password_hash)s, %(display_name)s, %(created_at)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["recipient_account"],
            "email": f"phase1-seed-{label}-recipient@example.test",
            "password_hash": "phase1-seed-fixture-no-login",
            "display_name": f"Phase1 Seed {label.capitalize()} Recipient",
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO users (id, workspace_id, account_id, created_at)
        VALUES (%(id)s, %(workspace_id)s, %(account_id)s, %(created_at)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["recipient_user"],
            "workspace_id": ids["workspace"],
            "account_id": ids["recipient_account"],
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO workspace_memberships (
            id, workspace_id, account_id, users_id, role, status, invited_by, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(account_id)s, %(users_id)s, 'member', 'active',
            %(invited_by)s, %(created_at)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["recipient_workspace_membership"],
            "workspace_id": ids["workspace"],
            "account_id": ids["recipient_account"],
            "users_id": ids["recipient_user"],
            "invited_by": ids["user"],
            "created_at": SEED_EPOCH,
        },
    )
    cur.execute(
        """
        INSERT INTO delegations (
            id, workspace_id, delegator_account_id, recipient_account_id,
            obligation_type, obligation_resource_id, expected_outcome, due_at,
            status, created_at, updated_at
        ) VALUES (
            %(id)s, %(workspace_id)s, %(delegator_account_id)s, %(recipient_account_id)s,
            'incidents', %(obligation_resource_id)s, %(expected_outcome)s, %(due_at)s,
            'proposed', %(created_at)s, %(created_at)s
        )
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": ids["delegation"],
            "workspace_id": ids["workspace"],
            "delegator_account_id": ids["account"],
            "recipient_account_id": ids["recipient_account"],
            "obligation_resource_id": ids["incident"],
            "expected_outcome": "Resolve the seeded acceptance incident",
            "due_at": SEED_EPOCH + timedelta(days=36500),
            "created_at": SEED_EPOCH,
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
            _seed_recommendations(cur, label, ids)
            _seed_idempotency(cur, label, ids)
            # Phase 3 (migrations 0022-0027) -- each depends only on rows
            # already seeded above: waiting_links on pkos_nodes/tasks,
            # risk_reviews on risks, capacity/planning on users,
            # plan_blocks on plans (seeded immediately before it),
            # attention_feedback on attention_items, meeting_participants/
            # meeting_packs on meetings/pkos_nodes.
            _seed_waiting_links(cur, label, ids)
            _seed_risk_reviews(cur, label, ids)
            _seed_capacity_and_planning_constraints(cur, label, ids)
            _seed_plans_and_blocks(cur, label, ids)
            _seed_attention_feedback(cur, label, ids)
            _seed_meeting_participants_and_packs(cur, label, ids)
            # Phase 4 (migrations 0030-0031) -- ai_run_steps/generated_
            # artifacts depend on ai_runs (seeded immediately before them);
            # evaluation_runs depends on users plus the global evaluation_
            # sets row migration 0031 seeds directly (looked up by query,
            # not seeded here).
            _seed_ai_runs_and_steps(cur, label, ids)
            _seed_evaluation_run(cur, label, ids)
            _seed_generated_artifact(cur, label, ids)
            # Phase 5 (migration 0038) -- workflow_versions/automation_
            # policies/triggers all reference workflow_definitions, seeded
            # first inside _seed_automation itself.
            _seed_automation(cur, label, ids)
            # Phase 6 Task 1 (migration 0044) -- connector_accounts/
            # sync_cursors/sync_runs; sync_cursors/sync_runs reference
            # connector_accounts, seeded first inside _seed_engineering
            # itself.
            _seed_engineering(cur, label, ids)
            # Phase 7 Task 1 (migration 0054) -- personal_domains/domain_
            # consents/domain_sources/domain_records/goals/routines/
            # check_ins/deletion_jobs/personal_insights; every dependent
            # row (domain_consents/domain_sources/domain_records/goals on
            # personal_domains, routines on goals, check_ins on routines)
            # is seeded in FK-safe order inside _seed_personal itself.
            _seed_personal(cur, label, ids)
            # Phase 8 Task 2 (migration 0062) -- invitations; depends only
            # on the owner-role users row seeded first inside
            # _seed_workspace itself.
            _seed_invitations(cur, label, ids)
            # Phase 8 Task 3 (migration 0063) -- resource_grants; depends on
            # the incident fixture seeded inside _seed_engineering above.
            _seed_resource_grants(cur, label, ids)
            # Phase 8 Task 6 (migration 0064) -- delegations; depends on the
            # incident fixture (obligation) seeded inside _seed_engineering
            # above and the owner-role users row seeded inside _seed_
            # workspace (delegator) -- seeds its own second, distinct
            # recipient identity first.
            _seed_delegations(cur, label, ids)


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
