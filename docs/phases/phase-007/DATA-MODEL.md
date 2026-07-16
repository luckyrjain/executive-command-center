---
id: PHASE-007-DATA-MODEL
title: Phase 7 Personal Intelligence Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 7 Data Model

Core records: `personal_domains`, `domain_consents`, `domain_records`, `domain_sources`, `goals`, `routines`, `check_ins`, `cross_domain_grants`, `personal_insights` and `deletion_jobs`.

Each record includes domain, classification, provenance, effective time and retention policy. Sensitive payloads use field-level encryption where defined. Cross-domain grants name source domain, target purpose, fields/categories and expiry. Insights are derived, versioned and deletable; source records remain authoritative.
