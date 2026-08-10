---
id: PHASE-010-PRIVACY-CONSENT-CONTRACT
title: Phase 10 Gmail Privacy and Consent Contract
status: Approved for Implementation
version: 1.3.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-007-DOMAIN-PRIVACY-CONTRACT
---

# Phase 10 Gmail Privacy and Consent Contract

## Current controls (Tasks 1-2, 5-7)

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
- `snippet` and `body` hold personal-data Fernet ciphertext using
  `ECC_PERSONAL_DATA_ENCRYPTION_KEY`; Task 2 leaves both null, Task 5
  populates `body` for the one message that triggers `email.detect_action`
  (feature-flagged off by default, `ECC_EMAIL_ACTION_DETECTION_ENABLED`).
- Subject, sender, recipients, direction, and timestamps remain plaintext
  structural fields required for deterministic server-side processing.
- Responses and logs never expose OAuth credentials or message bodies.
- Entity-link evidence stores a source reference and SHA-256, not body text.

### Consent enforcement

Every sync requires an active owner-scoped `email` domain consent and rechecks
it before each write. Connector access is workspace-visible under the Phase 8
authorization model, while email rows remain strictly workspace-and-owner
scoped under the Phase 7 personal-domain model.

### Consent revocation cascade (Task 7)

Disabling the `email` domain (`POST /personal/domains/email/disable`,
`POST /personal/consents/{id}/revoke` -- both already funnel through
`domains.py`'s single `_disable_domain` write path) and deleting it
(`POST /personal/domains/email/delete`) now all reach `ecc.domains.
personal.gmail_revocation.cascade_email_revocation` in the same request:
best-effort Google-side token revoke (deferred until after the local
transaction commits, matching `connector_accounts.py:disable_connector_
endpoint`'s own established split), disconnecting the owner's `gmail`
connector account, and purging `email_threads`/`email_messages`, the
owner's own `entity_type='email_thread'` `attention_items`, and every
Gmail-sourced `pkos_evidence` row. `email`-sourced `recommendations` not
yet `executed` are deleted outright; an already-`executed` one (which now
has an independent, confirmed `tasks`/`commitments`/`risks` row) is
redacted in place instead -- its own Gmail-derived `rationale`/`proposed_
action`/`evidence_ids` nulled, matching `gmail_threads.py:forget_thread_
endpoint`'s own "null the content, keep the audit skeleton" convention.
`pkos_nodes` (the resolved person entities) are deliberately not deleted
-- they deduplicate at workspace, not owner, scope, so deleting one could
destroy state a different owner or domain still depends on; only the
evidence pointing at it is removed. This is the single action the plan's
own Task 7 bullet describes: revoking consent, disconnecting Google, and
purging every email-derived record all happen together, with no code path
that does only one of the three.

**"Retryable" is the existing idempotency-key mechanism, not a new
`deletion_jobs` state.** Every step above runs inside the same transaction
`_disable_domain`/`_delete_domain_data` already opens, so a failure at any
point rolls back the whole cascade -- there is no reachable partially-
purged state, and no `deletion_jobs` row is written for a failed attempt.
A client retry with the same `Idempotency-Key` simply re-attempts the
cascade from scratch (every `DELETE`/`UPDATE` in it is independently
idempotent). `deletion_jobs.status` therefore still only needs `'pending'`/
`'completed'` (migration `0054`'s original shape) -- see `gmail_
revocation.py`'s own module docstring for the full reasoning against
adding `'failed'`/`'retrying'` states with nothing that would ever
populate them for longer than one transaction.

### On-demand thread reading and per-thread "forget" (Tasks 5-6)

On-demand AI body access shipped with Task 5: `email.get_thread_content`
reads a thread's already-fetched, decrypted message bodies for the
`email.detect_action` model call only, scoped to the caller's own
`workspace_id`/`owner_id`, behind the same feature flag above.

On-demand human-facing thread reading, and a real (if narrower than Task
7's own domain-wide cascade above) deletion control, both shipped with
Task 6: `GET /api/v1/personal/gmail/threads/{thread_id}` fetches and
returns a thread's decrypted content to the authenticated owner directly
(not gated behind the AI feature flag above -- consent-gated only, per
`_email_consent_active`), and `POST .../forget` nulls a single thread's
own cached `snippet`/`body`/`body_fetched_at`, recorded in `deletion_jobs`
with `scope='thread'` (migration `0075`, widening the Phase 7 `deletion_
jobs` table `docs/phases/phase-007/DATA-MODEL.md` documents). **Explicitly
narrower than Task 7's own cascade, not a substitute for it**: Task 6's
"forget this" does not disconnect Google, does not touch any other
thread, and does not propagate to that thread's own derived `pkos_
evidence`/`recommendations` rows (a recommendation created from a since-
forgotten thread's content keeps citing evidence that itself still
exists, unaffected) -- a deliberate, plan-scoped limitation ("deletes the
cached body/message content for that thread only"), not an oversight.
Task 7's own cascade above reaches evidence/recommendation propagation
only at the domain-wide scope consent revocation actually operates at; a
single forgotten thread's own evidence still is not independently
reachable by any endpoint (a real, disclosed gap -- there is no per-
thread evidence-propagation action, only the domain-wide one).

## Unsupported — production blocker

Task 8 does **not** yet provide Gmail-specific export, deletion
verification, redacted audit events for connect/sync/body-access/revoke/
delete, an explicit retention period and cache expiry before body storage
is enabled, or key rotation. Until Task 8 and recovery evidence exist,
Gmail is internal-development only.

## Planned controls (Task 8)

- export with decrypted owner-authorized content and no credential material;
- redacted audit events for connect, sync, body access, revoke, and delete;
- explicit retention period and cache expiry before body storage is enabled.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented current controls and explicit Task 7 privacy blocker | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 5 review (Loop 2 round 16): documented Task 5's body population and on-demand AI body access (`email.get_thread_content`), moved out of "Planned"; this document had gone stale after Tasks 3-5 shipped without a contract-version update | Lucky Jain |
| 1.2.0 | 2026-08-06 | Task 6: documented on-demand human-facing thread reading and per-thread "forget this," explicitly scoped narrower than Task 7's own eventual revocation cascade | Lucky Jain |
| 1.3.0 | 2026-08-10 | Task 7: documented the consent revocation cascade (disconnect + domain-wide purge, one action), moved it out of "Unsupported"/"Planned" into "Current controls"; only Task 8's export/audit-event/retention items remain planned | Lucky Jain |
