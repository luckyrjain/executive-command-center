---
id: PHASE-005
title: Automation
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on: [PHASE-004]
contracts:
  - phase-005/DATA-MODEL.md
  - phase-005/API-SCHEMAS.md
  - phase-005/EXECUTION-CONTRACT.md
  - phase-005/APPROVAL-POLICY.md
  - phase-005/UX-STATES.md
  - phase-005/TEST-PLAN.md
---

# PHASE-005 — Automation

## Objective

Execute bounded, recoverable workflows on schedules or events only under explicit user-approved policies.

## In scope

Workflow definitions and versions; triggers and schedules; approval gates; durable execution; idempotency; retries; compensation; pause/cancel; secrets references; policy simulation; execution history; notifications; local background worker.

## Out of scope

Unbounded autonomous agents, silent external side effects, self-created policies, financial/legal/medical decisions, credential discovery, cross-workspace workflows and multi-user delegation.

## Requirements

- Every side effect belongs to an approved versioned workflow and policy.
- Default is preview/recommend; approval scopes specify action, target, limits and expiry.
- Execution is durable, idempotent and resumable after restart.
- Human approval cannot be inferred from conversation or prior unrelated actions.
- High-impact actions require per-run confirmation.
- Retries are bounded; compensation is explicit and never assumed.
- Users can pause, cancel, inspect and revoke future authority.
- Schedules use workspace timezone with defined DST and missed-run behavior.
- Every step records redacted inputs, outcome, actor/policy and correlation.

## Exit criteria

Approved contracts; durable worker recovery; approval and revocation tests; schedule/DST tests; connector sandbox tests; compensation; security review; browser acceptance; zero Critical/High/Medium findings; staged dogfood with no unauthorized side effects.

## Rollback

Global and workflow kill switches stop new runs. In-flight runs reach a safe checkpoint or cancel. Revoking a policy blocks future steps. Recovery procedures address partial external side effects.
