---
id: PHASE-001-PRIORITY-MODEL
title: Phase 1 Priority Model
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Deterministic Priority Model

## Purpose

Phase 1 ranking is rule-based and must work without AI. Scores are recomputed when a relevant entity changes, at workspace-day rollover, and at least every 15 minutes while the application is active.

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

No single non-pinned item may exceed 95. Pinned items may score 100.

## Confidence

Confidence is deterministic:

- 1.0 for authoritative local fields,
- minimum evidence confidence for derived commitment/risk factors,
- 0.8 when due-date precision is date-only,
- 0.6 when owner or evidence is incomplete.

Overall confidence is the minimum of applicable factors, rounded to two decimals.

## Tie-breaking

1. pinned first,
2. higher score,
3. earlier due/start time,
4. higher manual priority,
5. older `created_at`,
6. stable UUID lexical order.

## Explanation

Every attention item exposes ordered factor objects: `code`, `label`, `points`, `source_field`, and optional evidence IDs. The human explanation is generated from these factors, never from opaque model text.

## Overrides and feedback

Pin is an explicit score boost. Defer excludes the item until `deferred_until`. Dismiss excludes the current projection until a material entity change occurs. Manual priority changes are authoritative. User feedback never silently mutates the underlying entity.

## Expiry

Attention projections expire after 30 minutes, at workspace-day rollover, or immediately on entity mutation. Recommendations have independent expiry and approval semantics.

## Performance

Ranking 10,000 representative entities must complete in under 500 ms in CI integration tests and dashboard assembly p95 must remain below two seconds.