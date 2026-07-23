---
id: PHASE-003-DATA-MODEL
title: Phase 3 Human Attention Data Model
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 3 Data Model

## Reconciliation with Phase 1's shipped `attention_items`

Phase 1 already shipped and runs `attention_items` (migration `0006_phase1_risks_priority.py`) and `governance/attention.py`. This document's `attention_items` row below is that same table, extended in place, not a new table — and there is no separate `attention_overrides` table. Approved by the repository owner (2026-07-23), per `docs/superpowers/specs/2026-07-22-phase-3-human-attention-engine-design.md`'s Open decision 1:

| This document's concept | Reconciled shape |
|---|---|
| `attention_items.source_type` / `source_id` | Kept as shipped: `entity_type` / `entity_id`. `entity_type` is an unconstrained `String(32)` (no `CHECK`), widened at the application layer to accept `waiting_link`, `risk_review`, `meeting` alongside `task`/`commitment`/`risk`. |
| `attention_items.state` | Not a stored column. Status stays derived at query time from `dismissed_at`/`dismissed_entity_version`/`deferred_until`, matching Phase 1's shipped design. |
| `attention_items.policy_version` | New column: `SMALLINT NOT NULL DEFAULT 1`. Every row shipped before this phase is implicitly policy v1. |
| `attention_items.factors_json` | Kept as shipped: `factors` (`JSONB`), same shape (list of `{code, label, points, source_field}`). |
| `attention_overrides` (dropped) | `audit_events` already records actor + timestamp + action for every dismiss/defer/restore (`_mutate_attention`'s existing insert). The one genuinely missing piece — free-text reason capture — is added as a new nullable `override_reason TEXT` column directly on `attention_items` instead of a second table. `effective_until` is redundant with the existing `deferred_until` column. Pin stays derived from the source entity's own `pinned` column (read-through `LEFT JOIN`), not a stored override — no second source of truth for pin state. `POST /attention/{id}/pin` is dropped from `API-SCHEMAS.md`'s endpoint list; pinning continues exclusively through the source entity's own `PATCH`. |

## Core records

| Record | Purpose | Required fields |
|---|---|---|
| `attention_items` | Rebuildable attention projection (Phase 1's shipped table, extended) | entity_type, entity_id, source_entity_version, score, confidence, policy_version, factors, override_reason |
| `waiting_links` | Directional obligation/dependency | subject_type/id, counterparty_entity_id, direction, status, since_at, expected_at |
| `risk_reviews` | Risk review and escalation history | risk_id, reviewed_at, outcome, next_review_at, evidence_refs |
| `capacity_profiles` | User-declared planning boundaries | weekday, available_minutes, focus_minutes, timezone, version |
| `planning_constraints` | Fixed time, deadlines and preferences | kind, source_id, starts_at, ends_at, hardness, priority |
| `plans` | Versioned daily/weekly plan snapshot | period, status, policy_version, capacity_minutes, version |
| `plan_blocks` | Proposed or accepted allocation | plan_id, source_type/id, starts_at, ends_at, status, rationale |
| `meeting_packs` | Persisted preparation snapshot | meeting_id, generated_at, source_versions, stale_at, status |
| `attention_feedback` | Explicit usefulness/correction label | target_type/id, label, reason, actor_id, policy_version |

## Invariants

All records are workspace scoped. Projections point to authoritative sources but do not own their lifecycle. Scores are integers [0,100] (matching the shipped `SmallInteger` column); confidence is decimal [0,1]. Manual pins are read through from the source entity's own `pinned` column, not stored on `attention_items`; hard constraints are stored separately from derived score. Waiting direction is relative to the accountable user and cannot be null. Accepted plan blocks do not overlap hard calendar constraints. A meeting pack stores source versions so staleness is reproducible.

## Lifecycle

Attention: `active -> dismissed|deferred|resolved`, with regeneration rules versioned. Waiting: `open -> fulfilled|cancelled|superseded`. Plan: `draft -> proposed -> accepted -> completed|superseded`. Pack: `fresh -> stale -> refreshed|archived`.

## Retention and rebuild

Attention items and suggested plans are rebuildable. User overrides, accepted plans, feedback and audit history are authoritative and retained. Rebuilds must preserve overrides and never duplicate waiting links.
