---
id: PHASE-003-DATA-MODEL
title: Phase 3 Human Attention Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 3 Data Model

## Core records

| Record | Purpose | Required fields |
|---|---|---|
| `attention_items` | Rebuildable attention projection | source_type, source_id, state, score, confidence, policy_version, factors_json |
| `attention_overrides` | User pin, dismiss or defer | attention_item_id, action, reason, effective_until, actor_id |
| `waiting_links` | Directional obligation/dependency | subject_type/id, counterparty_entity_id, direction, status, since_at, expected_at |
| `risk_reviews` | Risk review and escalation history | risk_id, reviewed_at, outcome, next_review_at, evidence_refs |
| `capacity_profiles` | User-declared planning boundaries | weekday, available_minutes, focus_minutes, timezone, version |
| `planning_constraints` | Fixed time, deadlines and preferences | kind, source_id, starts_at, ends_at, hardness, priority |
| `plans` | Versioned daily/weekly plan snapshot | period, status, policy_version, capacity_minutes, version |
| `plan_blocks` | Proposed or accepted allocation | plan_id, source_type/id, starts_at, ends_at, status, rationale |
| `meeting_packs` | Persisted preparation snapshot | meeting_id, generated_at, source_versions, stale_at, status |
| `attention_feedback` | Explicit usefulness/correction label | target_type/id, label, reason, actor_id, policy_version |

## Invariants

All records are workspace scoped. Projections point to authoritative sources but do not own their lifecycle. Scores are decimal [0,100]; confidence is [0,1]. Manual pins and hard constraints are stored separately from derived score. Waiting direction is relative to the accountable user and cannot be null. Accepted plan blocks do not overlap hard calendar constraints. A meeting pack stores source versions so staleness is reproducible.

## Lifecycle

Attention: `active -> dismissed|deferred|resolved`, with regeneration rules versioned. Waiting: `open -> fulfilled|cancelled|superseded`. Plan: `draft -> proposed -> accepted -> completed|superseded`. Pack: `fresh -> stale -> refreshed|archived`.

## Retention and rebuild

Attention items and suggested plans are rebuildable. User overrides, accepted plans, feedback and audit history are authoritative and retained. Rebuilds must preserve overrides and never duplicate waiting links.
