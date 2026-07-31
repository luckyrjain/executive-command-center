---
id: PHASE-007-DATA-MODEL
title: Phase 7 Personal Intelligence Data Model
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Phase 7 Data Model

Core records: `personal_domains`, `domain_consents`, `domain_records`, `domain_sources`, `goals`, `routines`, `check_ins`, `cross_domain_grants`, `personal_insights` and `deletion_jobs`.

Each record includes domain, classification, provenance, effective time and retention policy. Sensitive payloads use field-level encryption where defined. Cross-domain grants name source domain, target purpose, fields/categories and expiry. Insights are derived, versioned and deletable; source records remain authoritative.

## Domain/classification/schema (resolved)

`personal_domains.domain_key`: closed enum `habits|learning|travel|relationships|health|finance`. `personal_domains.classification`: closed enum `standard|sensitive|high_stakes` -- `habits`/`learning`/`travel` are `standard`, `relationships` is `sensitive`, `health`/`finance` are `high_stakes`. Every domain is disabled (`enabled=false`) by default, per user, independently of every other domain.

`domain_records.payload` (JSONB) holds per-`record_type` fields; sensitive/high_stakes-classified fields inside `payload` are individually Fernet-encrypted (`DOMAIN-PRIVACY-CONTRACT.md`'s "Encryption fields (resolved)"), not the whole column -- a `standard`-classified field on an otherwise `sensitive` record (e.g. a `relationships` record's `contact_name`) stays plaintext/queryable. `domain_sources.source_type`: `manual|imported_file` this activation -- no connector-based personal-domain source exists yet.

## Retention (resolved)

`standard`: retained until the user deletes the record or the design doc's own export/deletion mechanism runs -- disabling a domain never deletes its data by default. `sensitive`: same indefinite default, plus a non-blocking keep/export/delete nudge after 18 months of no access to a record. `high_stakes`: same nudge at 12 months, plus every `high_stakes` `domain_records` row requires `retention_acknowledged_at` set at creation time (consent affirmed per record, not only once at domain enablement).

## Task 1 status

Ships `personal_domains`, `domain_consents`, `domain_records`, `domain_sources`, `goals`, `routines`, `check_ins`, `deletion_jobs`, scoped to the `habits` reference domain (`standard` classification) only. `cross_domain_grants` and `personal_insights` beyond deterministic (non-model-call) kinds are added by the task that first needs them (`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, Decision 1, item 5).

## Task 2 status

**No new table, migration or endpoint.** `learning` is `standard`-classified, identical to `habits` -- Task 1's own domain-enablement and generic `domain_records` CRUD (`ecc.domains.personal.domains`) take `domain_key` as a runtime value already validated against the closed `DomainKey` enum, not a `habits`-specific code path. Verified directly (`tests/test_personal_learning_postgres.py`): enabling `learning`, creating/listing/patching/exporting/deleting `domain_records` under it all work unmodified. This is the concrete confirmation the design doc's own "why `habits` first" reasoning (Decision 1: prove the generic framework before a `sensitive`/`high_stakes` domain needs it) was aiming for -- a second `standard` domain onboarded with zero new backend code, not merely asserted to be possible.

`learning`'s own `domain_records.record_type` conventions (documented here since nothing else fixes them): `course` (`payload`: `title`, `provider`, `url` optional, `progress_pct` integer 0-100 optional, `completed_at` optional), `resource` (`payload`: `title`, `resource_type` string e.g. `"book"`/`"article"`/`"video"`, `url` optional, `notes` optional). No `goals`/`routines`/`check_ins` convention is defined for `learning` in this task -- those three tables are proven domain-agnostic by schema (no FK or CHECK constraint ties them to `habits`), but `learning`'s own natural shape (course progress, not a repeating daily/weekly/monthly cadence) is better served by `domain_records.payload.progress_pct` directly than by forcing a `routines`/`check_ins` pair onto content that isn't actually a recurring habit -- not attempted here without a real UI/usage need driving it.

## Task 3 status

**No new table or migration -- one small, generic API addition.** `travel` is `standard`-classified, identical in enablement/CRUD to `habits`/`learning`. What Task 3 actually needed: `travel` is the design doc's own "first domain with meaningfully time-ranged records, exercising retention-by-date-range" -- a single `domain_records.effective_at` timestamp already exists per record, but nothing previously let a caller *query* by it. `GET /personal/records` (`ecc.domains.personal.domains.list_records_endpoint`) gains two optional query parameters, `effective_from`/`effective_until` (inclusive on both bounds), filtering on `effective_at` -- generic on the endpoint, not `travel`-specific, matching this activation's existing "a caller names a `domain_key`, never a domain-specific code path" convention (`API-SCHEMAS.md`'s Task 3 section has the endpoint-level detail). Retention itself is unchanged from the "Retention (resolved)" section above -- `standard` classification is still retained indefinitely until the user deletes it; "retention-by-date-range" here means the record's own effective date range is now queryable, not that records expire on a timer.

`travel`'s own `domain_records.record_type` convention: `trip` (`payload`: `destination`, `start_date` (ISO date string), `end_date` optional, `notes` optional; `effective_at` is set to the trip's `start_date` at creation, per the module's existing `effective_at` parameter on `POST /personal/records`). No `goals`/`routines`/`check_ins` convention is defined for `travel` in this task, for the same reason as `learning`'s own Task 2 finding above -- a trip is not a recurring habit.
