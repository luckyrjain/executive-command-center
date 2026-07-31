---
id: PHASE-007-DOMAIN-PRIVACY
title: Personal Domain Privacy Contract
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Personal Domain Privacy Contract

Domains are separate privacy compartments. Enabling one does not grant another access. Consent is granular, purpose-bound, time-bound and revocable. Default search and meeting context exclude personal domains.

Exports are human-readable plus machine-readable. Deletion removes authoritative and derived content, embeddings and cached summaries, subject to minimal redacted audit integrity. Backups follow disclosed deletion windows. Sensitive data never leaves the device unless a named provider and exact data class are explicitly allowed.

## Privacy impact assessment (resolved)

No domain's data is visible to any other domain, to Phase 2/3/6's shared knowledge/attention/engineering surfaces, or to global search by default. Within a workspace, only the record's own owning user can access it -- no admin override exists for personal-domain content, this activation included (extends finding F-04's "Phase 9 enterprise administration does not bypass private-content boundaries" into working code now, not deferred). `domain_records`/`personal_insights` are never inserted into Phase 2's `retrieval_documents`/`embedding_projections` tables; no indexing pipeline touches personal-domain tables. No remote-provider egress risk this activation: Phase 4's only active model is local, and Task 1 makes zero model calls over personal-domain data (deterministic insights only). Residual, disclosed risk: direct database access bypasses `standard`-classified plaintext fields (accepted, matching Phase 6's equivalent disclosure); backup/restore of `sensitive`/`high_stakes` records is not yet independently exercised (tracked as a Task 1 follow-up, not assumed to work). Full detail: `docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, Decision 2.

## Encryption fields (resolved)

A dedicated `ECC_PERSONAL_DATA_ENCRYPTION_KEY` (Fernet, `RFC-005` v1.4.0's already-approved `cryptography` package -- no new technology), distinct from both `session_secret` and `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY`, matching `ecc.domains.engineering.crypto`'s own precedent for why a personal-data key rotation and a connector-token key rotation should not share one blast radius. `standard`-classified `domain_records.payload` fields stay plaintext (queryable/filterable). `sensitive`/`high_stakes`-classified narrative/free-text fields (e.g. `relationships.notes`, `health.symptom_description`) are Fernet-encrypted individually within `payload`; structured low-sensitivity fields on the same record (e.g. `relationships.contact_name`) stay plaintext. Decrypted value never logged, returned by an unrelated API response, or written into an audit/outbox payload. List/summary API responses return a redacted placeholder for an encrypted field; only a single-record fetch returns the decrypted value. Exact per-domain field list finalized in each domain's own `DATA-MODEL.md` addition when that domain's task lands.

**Task 4 confirmation.** `ecc.domains.personal.crypto` (Fernet, `ECC_PERSONAL_DATA_ENCRYPTION_KEY`, structurally validated outside development by `ecc.config.validate_production_settings`) now exists and is exercised for real by `relationships`' own `notes` field (`DATA-MODEL.md`'s Task 4 section). "Decrypted value never... written into an audit/outbox payload" is extended in practice to a persisted surface this contract did not name explicitly but the same principle plainly covers: `idempotency_records.response_body`, a store that would otherwise hold a decrypted response verbatim across a replayed request -- the write path builds that stored response from the still-encrypted row, decrypting only the copy actually returned to the caller. Verified against real Postgres, not just asserted: `notes` is genuinely ciphertext in `domain_records.payload` at rest, and the idempotency-records row itself never holds the decrypted value either.
