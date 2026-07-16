---
id: PHASE-003
title: Human Attention Engine
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on:
  - PHASE-001
  - PHASE-002
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

Convert trusted commitments, risks, meetings and connected knowledge into an explainable, user-controlled system for allocating executive attention.

## User value

The user understands what deserves attention, why it matters, who is waiting on whom, what can safely wait, how the day should be planned and what context is needed before each meeting.

## In scope

- Unified attention items derived from authoritative work and knowledge.
- Explainable deterministic priority scoring and bounded policy configuration.
- Waiting-on-me, waiting-on-them and blocked-by tracking.
- Risk escalation, review cadence and staleness.
- Daily and weekly planning with capacity and protected focus windows.
- Meeting preparation packs built from authorized knowledge and current obligations.
- Dismiss, defer, pin, accept and correct feedback loops.
- Scenario preview before a planning change is applied.
- Audit, provenance, workspace isolation and deterministic AI-disabled operation.

## Out of scope

Autonomous scheduling; external calendar writes; background agents; predictive ML risk models; employee scoring; performance surveillance; automatic messaging; multi-user delegation; optimization across personal domains.

## Functional requirements

- Every attention item exposes factors, evidence, confidence, freshness and policy version.
- Hard safety and user-pinned constraints cannot be overridden by inferred priority.
- Waiting direction and accountable owner are explicit and history preserving.
- Plans never exceed declared capacity without showing an unresolved conflict.
- Plan suggestions remain proposals until the user accepts them.
- Meeting packs cite sources and distinguish fact, unresolved question and suggestion.
- Missing or permission-denied evidence lowers confidence and remains visible.
- Feedback updates user-controlled preferences or labelled evaluation data; it does not silently rewrite history.

## Non-functional requirements

- Today attention query p95 <500 ms for 10,000 active inputs.
- Daily plan generation p95 <1 second using deterministic planning.
- Meeting pack retrieval p95 <2 seconds excluding optional enrichment.
- Equivalent inputs, policy and time produce equivalent deterministic output.
- Core workflows function without AI or internet access.

## Architecture impact

Add attention projection, dependency/waiting, planning and meeting-preparation modules to the modular monolith. Phase 2 knowledge remains the context source. Phase 1 records remain authoritative for tasks, commitments, meetings and risks.

## Security and ethics

No ranking by protected characteristics, inferred personality or employee productivity. Private notes and restricted sources appear only when authorized. The UI must not present the score as an objective judgement of a person.

## Acceptance and exit criteria

- Contracts and policy versions approved.
- Deterministic score, waiting, risk, plan and meeting-pack tests pass.
- Explainability, feedback, staleness and AI-disabled flows pass.
- Fairness review confirms excluded signals and absence of person-ranking behavior.
- Browser acceptance, isolation, redaction, backup/restore and performance gates pass.
- Two weeks of daily-use validation show useful plans without missed critical commitments.

## Rollback

Disable planning and meeting-enrichment flags independently. Rebuild projections from authoritative Phase 1/2 records. Preserve manual pins, deferrals and accepted plans through forward fixes.

## Deferred backlog

External calendar writes, automatic delegation, predictive risk, agentic replanning, team capacity optimization and cross-domain personal planning.
