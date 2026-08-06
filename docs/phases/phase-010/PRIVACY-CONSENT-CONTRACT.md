---
id: PHASE-010-PRIVACY-CONSENT-CONTRACT
title: Phase 10 Gmail Privacy and Consent Contract
status: Approved for Implementation
version: 1.0.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-007-DOMAIN-PRIVACY-CONTRACT
---

# Phase 10 Gmail Privacy and Consent Contract

## Current controls (Tasks 1-2)

### Scopes and rollout boundary

The OAuth flow requests `gmail.metadata` and `gmail.readonly`; both must be
returned. Task 2 calls metadata endpoints only. The application enforces a
comma-separated, case-insensitive internal account allowlist both before
redirect and after Google identifies the authorized account. An empty
allowlist denies every account.

This allowlist is the current rollout control. Public/general availability is
unsupported and blocked on Google verification/CASA requirements and a new
security/privacy review.

### Encryption and minimization

- OAuth grant material is encrypted with
  `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY`.
- `snippet` and `body` are reserved for personal-data Fernet ciphertext using
  `ECC_PERSONAL_DATA_ENCRYPTION_KEY`; Task 2 leaves both null.
- Subject, sender, recipients, direction, and timestamps remain plaintext
  structural fields required for deterministic server-side processing.
- Responses and logs never expose OAuth credentials or message bodies.
- Entity-link evidence stores a source reference and SHA-256, not body text.

### Consent enforcement

Every sync requires an active owner-scoped `email` domain consent and rechecks
it before each write. Connector access is workspace-visible under the Phase 8
authorization model, while email rows remain strictly workspace-and-owner
scoped under the Phase 7 personal-domain model.

## Unsupported — production blocker

Tasks 1-2 do **not** yet provide a single consent-revocation action that
disconnects Google, purges threads/messages/body cache, removes derived
attention/recommendations/evidence, and records completion. They also do not
provide Gmail-specific export, deletion verification, or key rotation. Until
Task 7 and recovery evidence exist, Gmail is internal-development only.

## Planned controls (Tasks 3-8)

- on-demand and AI body access with purpose/audit boundaries;
- export with decrypted owner-authorized content and no credential material;
- revocation cascade with retryable deletion job and completion evidence;
- deletion propagation to derived PKOS/attention/recommendation records;
- redacted audit events for connect, sync, body access, revoke, and delete;
- explicit retention period and cache expiry before body storage is enabled.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented current controls and explicit Task 7 privacy blocker | Lucky Jain |
