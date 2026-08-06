---
id: PHASE-7-PERSONAL-DATA-RECOVERY
title: Phase 7 Personal Data Recovery Runbook
status: Active
version: 1.0.0
owner: Lucky Jain
updated: 2026-08-06
---

# Phase 7 Personal Data Recovery Runbook

This runbook covers implemented per-domain export and deletion. Production recovery remains blocked by [`../operations/PRODUCTION-READINESS.md#blockers`](../operations/PRODUCTION-READINESS.md#blockers) PR-005 and PR-007.

## Export and deletion

1. Authenticate as the owning user and select the exact personal domain.
2. Before deletion, call `POST /api/v1/personal/domains/<domain_key>/export` and store the returned machine-readable JSON in a user-controlled protected location. It contains decrypted owner data and must be treated at the domain's highest privacy class.
3. Call `POST /api/v1/personal/domains/<domain_key>/delete` with the required CSRF and idempotency protections. The implementation transactionally removes the domain's authoritative and derived records while retaining only the redacted audit integrity defined by the privacy contract.
4. Verify through supported list/fetch APIs that domain records and insights are absent. Do not query or delete tables directly.

Deletion from historical backups is not immediate. The production backup-retention/deletion window is **Unsupported — production blocker**. Contain by not using real personal data in an unapproved production deployment and by protecting any local backup containing such data. See PR-007.

## Backup and restore

The general PostgreSQL backup/restore process covers personal-domain tables, but an operator-grade production restore with real encrypted records is **Unsupported — production blocker**. Use [`../operations/PHASE-0-BACKUP-RESTORE.md`](../operations/PHASE-0-BACKUP-RESTORE.md) only with synthetic/development data until PR-007 closes. Never restore over the only copy of a database; restore into a clean target and verify authorization, checksums, migration head, encrypted-record access, and deleted-domain expectations before cutover.

Personal-data key rotation/re-encryption is **Unsupported — production blocker**. Keep `ECC_PERSONAL_DATA_ENCRYPTION_KEY` stable. If decryption fails, stop access, preserve ciphertext and configuration, and do not invent a replacement key or rewrite JSON payloads. See PR-005.

## Evidence to retain

Record domain key, export/delete request IDs, timestamps, row-count/checksum assertions without content, backup identifier, restore target, authorization checks, and reviewer. Never retain exported personal content in CI artifacts or issue/PR logs.
