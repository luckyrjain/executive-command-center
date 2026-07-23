---
id: PHASE-003-ATTENTION-MODEL
title: Human Attention Model
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Human Attention Model

## Principle

The score ranks work, not people. It is a transparent decision aid, not an objective truth. Manual safety constraints and explicit user choices remain authoritative.

## Eligible inputs

Deadline proximity, overdue duration, commitment accountability, blocked dependents, waiting direction, risk severity/likelihood, meeting proximity, strategic importance explicitly set by the user, evidence freshness and prior deferral count.

## Excluded inputs

Protected characteristics, inferred emotion or personality, employee activity volume, message response speed as a performance proxy, private-source content without permission and opaque model-only scores.

## Deterministic score

```text
score = clamp(0, 100,
  urgency + commitment + dependency + risk + meeting + importance
  + bounded_recency - bounded_deferral_penalty
)
```

Each component, cap and weight is stored in a versioned policy. Components use decimal arithmetic and entity ID is the final tie-breaker. Confidence is separate from priority and declines with missing, conflicting or stale evidence.

## Policy v1 (approved 2026-07-23)

Policy v1 is defined as Phase 1's exact current point values, carried forward byte-for-byte with zero ranking change on the day this phase ships, then extended additively with new factors (not a v2 bump — new terms join the same v1 config):

- `urgency` (due timing): overdue `35`; due/reviewable within 48h `15`; due today `25` (tasks only, date-only due).
- `commitment` (manual priority/importance): task manual_priority — critical `35`, high `25`, medium `15`, low `5`; commitment importance — critical `25`, high `18`, medium `10`, low `4`.
- `risk`: impact (probability × impact) — `25` if ≥20, `15` if ≥12, `8` if ≥6, else `0`; review overdue `35`; review due within 48h `15`.
- Pin: `+20` (uncapped item), score capped at `95` unpinned / `100` pinned (existing shipped cap behavior, unchanged).
- Existing task-only factors carried forward unchanged: waiting-on-person `+8`, blocked `-12`, stale 7d `+4`, stale 14d `+8`.
- New in Phase 3, additive to policy v1: `dependency` (blocked-by/waiting factors extending the existing waiting-on-person concept to `waiting_link` rows), `meeting` (meeting proximity), `importance` (explicit user-set strategic importance, distinct from task/commitment priority), `bounded_recency`, `bounded_deferral_penalty` — each implemented and scenario-tested in Task 1 before shipping, with exact weights set during implementation and captured in `policy.py`'s versioned config (not restated here to avoid two sources of truth once code exists).

Confidence values carry forward unchanged: task confidence `0.8` with a due date else `1.0`; commitment confidence from `row.confidence` (default `0.6`), capped at `0.8` when a due date exists; risk confidence fixed at `1.0`.

## Critical-item definition (approved 2026-07-23)

For the two-week dogfood's "no missed critical item" exit criterion, an item is **critical** if it is: overdue, due or reviewable within 48 hours, or blocking a dependent item. This reuses the existing `overdue`/`due_48h`/`review_due_soon`/`review_overdue` factor codes `attention.py` already computes — no new concept, just naming the existing high-weight factors as the critical set.

## Overrides

Pin places an item in the protected section without changing its score. Dismiss hides the current source version. Defer hides until a timestamp or source change. Restore removes the override. Safety-critical overdue commitments may remain visible in a conflicts section even when deferred.

## Waiting semantics

- `waiting_on_me`: the accountable user owes the next action.
- `waiting_on_them`: a counterparty owes the next action.
- `blocked_by`: progress depends on another entity/event.
- `delegated`: another accountable owner has accepted responsibility.

Direction changes create history; they do not overwrite the original obligation.

## Evaluation

Use versioned scenarios covering deadlines, risk, missing evidence, deferral, ties and timezone boundaries. Measure critical-item recall, top-five usefulness, override rate and false urgency. Policy changes require scenario diff review.
