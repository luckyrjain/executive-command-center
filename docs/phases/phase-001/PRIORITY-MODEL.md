---
id: PHASE-001-PRIORITY-MODEL
title: Phase 1 Priority Model
status: Approved
version: 1.0.1
owner: Lucky Jain
---

# Deterministic Priority Model

## Purpose

Phase 1 ranking is rule-based and works without AI. Scores recompute on relevant entity change, workspace-day rollover, and at least every 15 minutes while active.

## Score

Start at 0 and clamp to 0..100.

| Factor | Points |
|---|---:|
| Manual priority critical/high/medium/low | +35/+25/+15/+5 |
| Overdue | +35 |
| Due today | +25 |
| Due within 48 hours | +15 |
| Commitment importance critical/high/medium/low | +25/+18/+10/+4 |
| Risk score probability×impact: 20–25 / 12–19 / 6–11 | +25/+15/+8 |
| Meeting starts within 2 hours / today | +20/+10 |
| Explicitly pinned | +20 |
| Age without movement ≥14d / ≥7d | +8/+4 |
| Waiting on another person | +8 |
| Blocked | -12 |
| Deferred until future | excluded |
| Completed, cancelled, archived, dismissed | excluded |

“Waiting on another person” is deterministic only when either a commitment has `direction=made_to_me` or a blocked task has `blocked_on_person_id`. Phase 1 never infers this factor from free text.

No single non-pinned item may exceed 95. Pinned items may score 100.

## Confidence

Confidence is 1.0 for authoritative local fields, the minimum evidence confidence for derived factors, 0.8 for date-only due precision, and 0.6 when evidence or an optional counterparty reference is incomplete. Overall confidence is the minimum applicable value, rounded to two decimals.

## Tie-breaking

Pinned first, then higher score, earlier due/start, higher manual priority, older created_at, and stable UUID lexical order.

## Explanation

Every attention item exposes ordered factor objects: code, label, points, source_field and optional evidence IDs. Human explanation is generated from these factors, never opaque model text.

## Overrides, dismissal and feedback

Pin is an explicit score boost. Defer excludes until `deferred_until`. Dismiss stores `dismissed_at` and `dismissed_entity_version`; it suppresses only that source entity version. Any later entity version is a material change and causes regenerated attention to become eligible again. Manual priority is authoritative. Feedback never silently mutates the underlying entity.

## Expiry and performance

Attention projections expire after 30 minutes, workspace-day rollover, or source mutation. Recommendations have independent expiry. Ranking 10,000 representative entities must complete under 500 ms in CI and dashboard assembly p95 remains below two seconds.
