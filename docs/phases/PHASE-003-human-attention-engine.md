---
id: PHASE-003
title: Human Attention Engine
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
depends_on:
  - PHASE-002
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
contracts:
  - phase-003/DATA-MODEL.md
  - phase-003/API-SCHEMAS.md
  - phase-003/ATTENTION-MODEL.md
  - phase-003/PLANNING-CONTRACT.md
  - phase-003/MEETING-PREP-CONTRACT.md
  - phase-003/UX-STATES.md
  - phase-003/TEST-PLAN.md
---

# PHASE-003 — Human Attention Engine

## Objective

Convert trusted commitments, risks, meetings and knowledge into an explainable, user-controlled system for allocating executive attention.

## User value

The user understands what matters, why it matters, who owes the next action, what can wait, how to plan available capacity and how to prepare for meetings.

## In scope

Unified attention projection; deterministic priority; waiting-on-me/them and blocked-by; risk review cadence; daily/weekly planning; capacity and focus windows; meeting-preparation packs; pin/dismiss/defer/feedback; scenario and replan diffs; deterministic AI-disabled operation.

## Out of scope

Autonomous scheduling, external calendar writes, background agents, predictive ML risk, person/employee scoring, performance surveillance, automatic messaging, multi-user delegation and cross-domain optimization.

## Functional requirements

- Attention items expose factors, evidence, confidence, freshness and policy version.
- Hard safety constraints and explicit pins cannot be overridden by inference.
- Waiting direction and accountable owner are explicit and history preserving.
- Plans show every capacity/deadline/constraint conflict.
- Plan proposals require explicit acceptance and do not mutate external calendars.
- Replanning produces a user-reviewable diff; accepted plans are never silently rewritten.
- Meeting packs cite sources and separate fact, unresolved question and suggestion.
- Feedback is explicit labelled evidence and never rewrites history automatically.
- Missing/permission-denied evidence lowers confidence visibly.

## Non-functional requirements

Attention query p95 <500 ms for 10,000 inputs; deterministic daily plan p95 <1 second; meeting pack p95 <2 seconds excluding optional enrichment. Equivalent input/policy/time gives equivalent output. Core flows work without AI/internet and meet WCAG 2.2 AA.

## Architecture impact

Add attention projection, waiting/dependency, risk-review, planning and meeting-preparation modules. Phase 1 remains authoritative for work; Phase 2 provides knowledge. Overrides and accepted plans are authoritative; scores and draft plans are rebuildable.

## Data changes

Extend Phase 1's shipped `attention_items` in place (`policy_version`, `override_reason`; no separate overrides table — `audit_events` already covers override history); add waiting links, risk reviews, capacity profiles, constraints, plans/blocks, meeting packs and feedback, all defined in `phase-003/DATA-MODEL.md`.

## API changes

Add attention, waiting, risk-review, capacity, plan and meeting-preparation endpoints defined in `phase-003/API-SCHEMAS.md`. Plan acceptance and accountable-state changes are idempotent, concurrency checked and audited.

## Frontend changes

Add Attention Queue, Waiting views, Risk Review, daily/weekly Planner, conflict/replan review and Meeting Preparation. Scores are secondary to plain-language explanation. Accessible list views accompany timelines.

## Security and privacy

No protected characteristics, inferred personality, activity-volume or response-speed performance proxies. Private/restricted evidence is used only when authorized. The system ranks work, never people. Cross-workspace IDs return 404.

## Observability

Measure projection lag, policy version distribution, score duration/input count, critical-item recall fixtures, dismiss/defer/pin rates, waiting ageing, plan feasibility/conflicts/churn, meeting-pack staleness/coverage and fallback. Telemetry contains no private content or person scores.

## Test strategy

Policy scenario tests, deterministic/property tests, prohibited-signal checks, waiting lifecycle, planning constraints/timezone/DST, meeting citation/staleness, isolation/redaction, accessibility, performance, backup/restore and two-week dogfood.

## Acceptance criteria

- Every attention result has inspectable factors/evidence.
- Determinism, stable tie and prohibited-signal tests pass.
- No plan silently violates a hard constraint or hides unscheduled work.
- Waiting/risk lifecycle and meeting citations pass.
- Performance, AI-disabled, isolation, accessibility and browser gates pass.
- Fairness/ethics review confirms work-not-people ranking.

## Exit criteria

- Contracts explicitly approved before implementation.
- All slices and migrations merged with benchmark evidence.
- Two-week dogfood (`docs/runbooks/PHASE-3-DOGFOOD.md`) records top-five usefulness, critical misses, false urgency, plan acceptance/churn and meeting corrections against the approved thresholds below.
- No missed critical item or unsupported meeting fact remains unresolved.
- Zero open Critical, High or Medium findings.
- Phase 4 can consume stable context and proposal contracts.

### Dogfood success thresholds (approved 2026-07-23)

Zero missed critical items (per `phase-003/ATTENTION-MODEL.md`'s critical-item definition) across the two-week window; ≥80% top-five usefulness rating; plan acceptance rate ≥60% (below that, the planner is proposing infeasible plans); false-urgency rate <10%.

## Dependency exit posture (approved 2026-07-23)

Phase 3 implementation begins now, in parallel with Phase 1/2's own ongoing dogfood/validation windows, under the same kind of parallel-start exception the repository owner granted Phase 2 (`docs/ROADMAP.md`'s Phase 2 status note) — not gated on formally closing every Phase 1/2 exit gate first.

## Rollback plan

Disable planning and meeting enrichment independently. Rebuild projections from Phase 1/2 sources. Preserve overrides, feedback and accepted plans. Revert policy versions without rewriting history.

## Deferred backlog

External calendar writes, automatic delegation, predictive risk, agentic replanning, team capacity optimization and cross-domain planning.
