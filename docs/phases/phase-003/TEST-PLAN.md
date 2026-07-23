---
id: PHASE-003-TEST-PLAN
title: Phase 3 Test Plan
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 3 Test Plan

## Functional coverage

- Attention projection and factor explanations.
- Pin, dismiss, defer, restore and source-version regeneration.
- Waiting direction, lifecycle and history.
- Risk review queues and escalation cadence.
- Capacity, constraints, plan proposal, conflict, edit, acceptance and replan diff.
- Meeting pack generation, citations, staleness, refresh and optional enrichment fallback.

## Determinism and property tests

Freeze time and policy version; verify identical output and stable ties. Generate overlapping constraints, zero capacity, invalid timezones, missing effort, circular dependencies, concurrent plan edits and stale sources. Verify no hard-constraint violation.

## Security and ethics

Workspace isolation on all endpoints/tables; audit and source redaction; restricted-note exclusion; prompt-injection fixtures; prohibited-signal static checks; confirm no API exposes person-ranking fields.

## Performance

Benchmark 10,000 attention inputs, dense weekly plans and large meeting histories. Record p50/p95/p99, memory and query plans. Validate projection rebuild equivalence.

## Browser acceptance

Review explanations, defer/restore, resolve waiting, review a risk, generate/edit/accept a plan, inspect conflicts, replan with diff and refresh a stale meeting pack in AI-disabled mode. Repeat core flows using keyboard only.

## Product validation

Two-week dogfood log records top-five usefulness, missed critical items, false urgency, plan acceptance/churn and meeting-pack corrections. Critical misses or unsupported meeting facts block exit.

## Exit gate

All CI, migration, benchmark, accessibility, isolation, backup/restore and review gates pass with zero Critical, High or Medium findings.
