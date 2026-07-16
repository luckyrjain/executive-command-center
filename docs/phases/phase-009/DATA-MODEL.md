---
id: PHASE-009-DATA-MODEL
title: Phase 9 Enterprise Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 9 Data Model

Core records: `organizations`, `tenants`, `tenant_domains`, `identity_providers`, `directory_mappings`, `enterprise_policies`, `policy_assignments`, `key_references`, `residency_profiles`, `retention_policies`, `legal_holds`, `audit_exports`, `compliance_controls`, `control_evidence`, `quotas` and `break_glass_events`.

Tenant ID participates in every enterprise uniqueness/reference boundary. Key material is external; only references and rotation state are stored. Policies and control evidence are versioned. Legal hold preserves matching records without granting read access. Audit exports store manifest/hash/signature state.
