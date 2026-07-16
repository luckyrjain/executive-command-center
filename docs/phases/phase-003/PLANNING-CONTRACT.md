---
id: PHASE-003-PLANNING
title: Planning Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Planning Contract

## Goal

Produce a realistic daily or weekly plan from accepted constraints and attention items while keeping the user in control.

## Inputs

Workspace timezone, planning period, capacity profile, hard calendar blocks, protected focus windows, deadlines, estimated effort, attention policy/version, pins, deferrals and source snapshot.

## Deterministic planning order

1. Validate timezone and source freshness.
2. Reserve hard calendar and unavailable time.
3. Reserve protected user-defined focus constraints.
4. Place deadline-critical work in feasible windows.
5. Place pinned items.
6. Allocate remaining items by score, effort fit and stable tie-breakers.
7. Return unscheduled work and conflicts explicitly.

The planner does not invent effort. Items without estimates use a visible default bucket and lower confidence.

## Proposal and acceptance

Generated plans start as draft/proposed. Moving or removing blocks produces a new version. Accept is explicit, idempotent and audited. Acceptance updates ECC planning state only; external calendar writes are deferred.

## Conflicts

Never hide over-capacity, missed-deadline or hard-constraint conflicts. The response suggests alternatives such as reduce scope, move a flexible block or leave unscheduled, but never silently violates a hard constraint.

## Replanning

Source changes mark a proposal stale. Accepted plans are not silently rewritten. Replanning creates a new proposal with a diff: added, removed, moved, unchanged and newly conflicted blocks.

## Evaluation

Scenario tests cover full calendars, no capacity, timezone/DST, overdue work, equal scores, missing estimates, fixed meetings, stale sources and manual edits. Measure feasible-block rate, critical deadline coverage, churn and user acceptance.
