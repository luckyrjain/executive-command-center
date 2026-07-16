---
id: PHASE-006
title: Engineering Workspace
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on: [PHASE-005]
contracts:
  - phase-006/DATA-MODEL.md
  - phase-006/API-SCHEMAS.md
  - phase-006/CONNECTOR-CONTRACT.md
  - phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md
  - phase-006/UX-STATES.md
  - phase-006/TEST-PLAN.md
---

# PHASE-006 — Engineering Workspace

## Objective

Provide an evidence-backed executive view of engineering delivery, reliability, architecture and decisions without replacing source systems or scoring individuals.

## In scope

Read-first GitHub/GitLab/Jira connectors; normalized repositories, work items, changes, releases, incidents and decisions; sync cursors; entity linking; delivery/risk views; architecture decision memory; evidence-backed summaries; optional approved write actions through Phase 5.

## Out of scope

Employee productivity scores, surveillance, mandatory cloud sync, source-control replacement, autonomous issue/PR mutation, code generation platform and compensation/performance decisions.

## Requirements

- Source systems remain authoritative and every projection retains provenance.
- Sync is incremental, idempotent, rate-limited, resumable and permission aware.
- Delivery metrics are team/system signals, never individual performance judgements.
- Definitions and denominators accompany every metric.
- Missing or partial source coverage is visible.
- Cross-system identity linking follows Phase 2 resolution and requires review when ambiguous.
- Write actions use Phase 5 approvals and connector-specific scopes.
- Decision and incident context can be retrieved for meetings and planning.

## Exit criteria

Approved connector/data contracts; sandbox sync; backfill/resume; deletion/permission propagation; metric validation; non-surveillance review; browser acceptance; performance and backup/restore gates; zero Critical/High/Medium findings.

## Rollback

Disable connectors independently; retain last-known projections with freshness state; revoke tokens; rebuild projections from source; disable writes without affecting read views.
