---
id: PHASE-007
title: Personal Intelligence
status: Draft
version: 0.2.0
owner: Lucky Jain
depends_on:
  - PHASE-006
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
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

## User value

The user preserves personal context, plans goals and receives cautious evidence-backed observations while retaining granular control over collection, sharing, inference, export and deletion.

## In scope

Opt-in domain vaults; manual/imported records; goals/routines/check-ins; provenance; domain retention/classification; granular consent; consented cross-domain links; descriptive insights/reminders/planning proposals; local retrieval; export and verified deletion.

## Out of scope

Diagnosis/treatment; regulated financial advice; trading or money movement; credit/employment/insurance decisions; continuous surveillance; covert relationship scoring; advertising/data sale; default cross-domain inference; public sharing; dependent/minor profiles without a later safeguarding specification.

## Functional requirements

- Every domain is disabled by default and independently enabled/disabled.
- Data has domain, classification, provenance, retention and permission state.
- Cross-domain retrieval requires a purpose/field-scoped, expiring, revocable grant.
- Default global search, meeting prep and engineering views exclude personal domains.
- Insights identify type, evidence window, missing data, confidence, limitations and policy version.
- Observations/correlations never become diagnoses, prescriptions or guaranteed outcomes.
- Users can correct facts, exclude sources, dismiss insights, export and delete.
- Phase 5 approval governs any reminder/action; high-impact personal actions remain prohibited.
- Local capture/retrieval works without AI/internet.

## Non-functional requirements

Domain switching and record query p95 <500 ms locally. Consent revocation blocks new access immediately and derived-access jobs within 60 seconds. Export is complete/verifiable; deletion reports authoritative/derived/backup status. Sensitive fields are encrypted per approved threat model. Core flows meet WCAG 2.2 AA.

## Architecture impact

Add separate personal-domain modules/vault policies using shared identity, knowledge and audit foundations. Work and personal records remain separate authorization compartments. Phase 4 may generate bounded insights; Phase 5 may schedule low-risk user-approved actions.

## Data changes

Add personal domains, consents, domain records/sources, goals, routines, check-ins, cross-domain grants, insights and deletion jobs defined in `phase-007/DATA-MODEL.md`. Derived artifacts retain source versions and consent purpose.

## API changes

Add domain lifecycle, record, goal/routine/check-in, consent/grant, insight, export and deletion endpoints in `phase-007/API-SCHEMAS.md`. Every request is purpose/domain checked; APIs never expose encryption keys.

## Frontend changes

Add domain enablement/privacy setup, records, goals/routines, consent dashboard, insight evidence, export and deletion flows. Health/finance boundaries appear beside relevant output, not only in settings.

## Security and privacy

Compartmentalize domains; apply field-level encryption where threat-modelled; default deny cross-domain access; minimize remote egress; redact audit; propagate deletion to search, embeddings, summaries and caches. Consent is explicit, informed, granular and revocable. High-stakes data is excluded from remote providers unless separately allowed.

## Observability

Measure domain enablement state (without sensitive values), import/projection status, consent/grant decisions, revocation propagation, insight type/status, export/deletion lifecycle and policy violations. Never log health/finance values, relationship notes or raw insight evidence.

## Test strategy

Opt-in/out, records/goals/routines, consent scope/expiry/revocation, default exclusions, cross-domain denial, insight evidence/safety, export completeness, deletion propagation, encryption/egress, audit redaction, adversarial high-stakes outputs, accessibility and browser acceptance.

## Acceptance criteria

- Disabled domains produce no collection, retrieval or insight.
- Cross-domain access fails without an exact active grant.
- Consent revocation and deletion meet propagation targets.
- Insights contain evidence/limitations and pass medical/financial safety fixtures.
- Local-only and AI-disabled flows pass.
- Export/restore/deletion evidence is verifiable.
- No sensitive values appear in logs or unrelated surfaces.

## Exit criteria

- Privacy impact and high-stakes safety reviews approved.
- Each released domain has explicit scope/schema/retention and test fixtures.
- Export/deletion and backup treatment are documented and exercised.
- Staged opt-in dogfood completes with no unauthorized cross-domain disclosure.
- Zero open Critical, High or Medium findings.
- Phase 8 can add sharing without changing private-default behavior.

## Rollback plan

Disable a domain or insight policy independently; revoke grants; stop import/inference/actions; retain local authoritative data for export or delete on request. Work-domain functionality remains unaffected. Derived insights can be discarded/rebuilt.

## Deferred backlog

Regulated advice, transaction execution, dependent/minor profiles, caregiver access, clinical integrations, bank-account write access, public sharing and predictive sensitive-trait inference.
