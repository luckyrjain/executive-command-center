---
id: PHASE-007
title: Personal Intelligence
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on: [PHASE-006]
contracts:
  - phase-007/DATA-MODEL.md
  - phase-007/API-SCHEMAS.md
  - phase-007/DOMAIN-PRIVACY-CONTRACT.md
  - phase-007/INSIGHT-CONTRACT.md
  - phase-007/UX-STATES.md
  - phase-007/TEST-PLAN.md
---

# PHASE-007 — Personal Intelligence

## Objective

Extend ECC to user-controlled personal domains—health, finance, learning, travel, habits and relationships—without crossing medical, financial or privacy boundaries.

## In scope

Optional domain vaults; manual/imported records; goals and routines; consented cross-domain links; evidence-backed descriptive insights; reminders and planning proposals; export/deletion; domain-specific retention and sensitivity controls.

## Out of scope

Diagnosis, treatment, regulated financial advice, trading execution, credit decisions, continuous surveillance, covert relationship scoring, sale of data, advertising and default cross-domain inference.

## Requirements

- Every domain is disabled by default and requires explicit opt-in.
- Domain data has classification, retention, export and deletion controls.
- Cross-domain retrieval requires explicit grants and is visible/revocable.
- Insights distinguish observation, correlation, suggestion and professional advice boundary.
- High-stakes health/finance outputs require prominent limitations and source freshness.
- No action is taken without Phase 5 policy and confirmation.
- Users can correct facts, exclude sources and inspect why an insight appeared.
- Local deterministic capture and retrieval work without AI.

## Exit criteria

Approved privacy and domain contracts; consent tests; high-stakes safety review; export/deletion; isolation and encryption validation; browser acceptance; staged opt-in dogfood; zero Critical/High/Medium findings.

## Rollback

Disable any domain independently, revoke cross-domain grants, stop import/insights and export/delete its data without affecting work-domain operation.
