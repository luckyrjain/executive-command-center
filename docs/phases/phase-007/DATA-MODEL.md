---
id: PHASE-007-DATA-MODEL
title: Phase 7 Personal Intelligence Data Model
status: Approved for Implementation
version: 0.2.0
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
