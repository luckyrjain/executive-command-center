---
id: PHASE-010-PRIVACY-CONSENT-CONTRACT
title: Phase 10 Gmail Privacy and Consent Contract
status: Approved for Implementation
version: 1.3.6
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
connector account(s) (an owner can hold more than one), and purging
`email_threads`/`email_messages`, the owner's own `entity_type='email_
thread'` `attention_items`, and every Gmail-sourced `pkos_evidence` row
this owner's own Gmail sync produced -- except a row whose `external_
message_id` happens to collide with a *different* owner's own message
(only unique per `(workspace_id, thread_id)`, not per-workspace): that
row is deliberately left alone rather than risk deleting across the
ownership boundary (Loop 2 round 4 review finding; see `gmail_
revocation.py`'s own module docstring) -- durably, across separate
cascade runs, not only within one: `email_message_id_purge_log`
(migration `0076`, Loop 2 round 8 review) records every id a cascade
processes before that owner's own `email_messages` rows are deleted, so a
*second*, independent owner's own later cascade still finds the first
owner's record of the collision even after the first owner's own
colliding row is long gone. `email`-sourced `recommendations` not
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
purging the owner's own email-derived records (subject only to the three
deliberate exceptions above) all happen together, with no code path
that does only one of the three -- including the generic engineering
`POST /connectors/{account_id}/disable` endpoint, which could disconnect
a `gmail`-provider account (and revoke the live Google grant) without
touching `personal_domains`/data at all until Loop 2 round 1 review found
it and closed it (that endpoint now rejects `gmail`-provider accounts
outright, `409 GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT`, directing callers
to the domain-level endpoints above).

**`revoke_consent_endpoint` rejects a `consent_id` a later grant has
superseded (Loop 2 round 8 review, refined round 9).** Its own `domain_
consents` lookup resolves a `consent_id` to a `domain_key` only if the
consent is still active, or -- if revoked -- no *later* grant for the
same domain exists. Without that check, a stale/already-revoked
`consent_id` -- a replayed request, a double-submitted form, or a client
retry landing after the owner already re-granted `email` consent and
resynced Gmail -- would still trigger a fresh disable-and-purge cascade
against the *current* state, using an identifier for a grant that no
longer exists. Harmless for every other domain (disable/re-enable is
fully reversible); destructive for `email` specifically, since disabling
now cascades. Round 8's first attempt at this fix (a plain `AND revoked_
at IS NULL`) closed that but broke a legitimate case round 9 review
found: an `Idempotency-Key` retry of a revoke that had already succeeded,
with no re-grant in between, now 404'd instead of returning the cached
response, since the lookup runs before `_disable_domain`'s own
idempotency-cache check ever gets a chance to fire. The `NOT EXISTS (a
later grant)` condition distinguishes the two -- a revoked consent with
nothing superseding it still resolves normally. A truly stale id (one a
later grant has superseded) is rejected the same non-disclosing way
every other lookup in this module already fails closed (`404 CONSENT_
NOT_FOUND`).

**The `NOT EXISTS (a later grant)` re-check itself moved, round 10
review, to close a TOCTOU window the first two rounds left open.**
`revoke_consent_endpoint` originally ran that check in its own, separate
transaction that committed *before* `_disable_domain` was even called --
a concurrent re-grant could commit in the gap between that check and
`_disable_domain`'s own `FOR UPDATE` lock on `personal_domains`, and
`_disable_domain` would then disable-and-cascade-purge the freshly
re-granted state anyway, using a `consent_id` that had already gone
stale by the time the actual mutation ran (the same underlying failure
mode rounds 8-9 were built to prevent, just not under genuine
concurrency). Fixed by passing `consent_id` through to `_disable_domain`
and re-validating *after* it acquires the same `personal_domains` row
lock `_enable_domain`'s own re-grant path also acquires (`existing =
get_domain(..., for_update=True)`) -- whichever transaction gets that
lock first is authoritative, closing the window structurally rather than
by timing. The comparison itself was also widened from `>` to `>=`
(round 10 LOW finding): a strict `>` has a same-timestamp blind spot
under coarse clock resolution that `>=` fails closed on instead.

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
| 1.3.1 | 2026-08-10 | Task 7 Loop 2 round 1 review: documented and closed a cascade-bypassing third write path (the generic engineering connector-disable endpoint) and the multi-connector-account cascade crash | Lucky Jain |
| 1.3.2 | 2026-08-10 | Task 7 Loop 2 round 5 review: corrected the "every Gmail-sourced pkos_evidence row" overclaim -- round 4's fix deliberately leaves a cross-owner-colliding evidence row unpurged; this file was never revisited when that fix shipped | Lucky Jain |
| 1.3.3 | 2026-08-10 | Task 7 Loop 2 round 6 review: corrected a second, nearby "purging every email-derived record" overclaim that round 5's fix missed -- internally inconsistent with the three carve-outs this same section already discloses two sentences earlier | Lucky Jain |
| 1.3.4 | 2026-08-10 | Task 7 Loop 2 round 8 review: documented `email_message_id_purge_log` (migration `0076`, closing a sequential cross-owner evidence leak) and `revoke_consent_endpoint`'s new `revoked_at IS NULL` requirement (closing a stale-consent-id replay that could re-trigger the cascade against freshly re-granted state) | Lucky Jain |
| 1.3.5 | 2026-08-10 | Task 7 Loop 2 round 9 review: corrected the round-8 `revoke_consent_endpoint` description -- the plain `revoked_at IS NULL` check broke legitimate `Idempotency-Key` retries; replaced with a `NOT EXISTS (a later grant)` check that only rejects a consent a later grant has actually superseded | Lucky Jain |
| 1.3.6 | 2026-08-10 | Task 7 Loop 2 round 10 review: documented the TOCTOU fix moving the `NOT EXISTS` re-check inside `_disable_domain`'s own transaction (closing a race the round 8-9 fix left open under genuine concurrency) and widening the comparison from `>` to `>=` | Lucky Jain |
