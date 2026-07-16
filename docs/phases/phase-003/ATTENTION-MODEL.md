---
id: PHASE-003-ATTENTION-MODEL
title: Human Attention Model
status: Draft
version: 0.1.0
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
