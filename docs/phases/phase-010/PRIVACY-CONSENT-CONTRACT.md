---
id: PHASE-010-PRIVACY-CONSENT-CONTRACT
title: Phase 10 Gmail Privacy and Consent Contract
status: Approved for Implementation
version: 1.5.0
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
colliding row is long gone -- and against a *genuinely concurrent* second
cascade too (neither has committed yet): a `pg_advisory_xact_lock` keyed
on `workspace_id` serializes every gmail cascade in the same workspace on
this section, which trivially includes two colliding owners' (Loop 2
round 17 review, closing the one window neither the round-4 nor round-8
fix covered; round 19 review widened the key from one lock per candidate
id to one per workspace, since the former could exhaust Postgres's shared
advisory-lock pool for a large mailbox -- a database-wide resource, so
this traded a small amount of unnecessary cross-owner serialization,
acceptable for an infrequent action, for eliminating that resource-
exhaustion risk entirely). `email`-sourced `recommendations` not
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
to the domain-level endpoints above). That rejection is itself gated on
the owner having a `personal_domains` row for `email` in the first place
(Loop 2 round 25 review): an owner who completed the Gmail OAuth flow
without ever calling `POST /domains`/`POST /consents` for `email` has no
such row, so there is nothing for the cascade to purge or revoke, and the
generic endpoint falls through to disconnect the account the same way any
other provider's is -- without this carve-out, that owner's connector had
no HTTP-reachable way to disconnect it at all, since the domain-level
endpoints 404 `DOMAIN_NOT_FOUND` for an owner with no domain row.

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

**`delete_domain_endpoint` gained a `for_update=True` lock round 11
review -- corrected round 12 review to describe accurately what it does
and does not do.** `export_deletion.py:delete_domain_endpoint` read
`personal_domains` without `for_update=True` while its own trailing
writes still landed unconditionally; round 11 added the lock, mirroring
`_disable_domain`'s own. Round 11's own framing claimed this closed "the
identical TOCTOU gap" round 10 closed for `_disable_domain` -- it does
not. `_disable_domain`'s round-10 fix rejects (`404`) when a caller-
supplied `consent_id` has been superseded by a later grant;
`delete_domain_endpoint` takes no `consent_id` at all, so it has nothing
to validate freshness against and cannot reject on one. A concurrent
re-grant that commits and releases the lock before this endpoint
acquires it is not rejected -- the endpoint proceeds against the fresh
state and reverts it anyway. This is not a new gap: `disable_domain_
endpoint` has had the identical characteristic since round 10 (it also
never passes a `consent_id`), already disclosed in `_disable_domain`'s
own docstring. The lock still has real value -- it serializes this
endpoint's whole read-cascade-write sequence against `_enable_domain`'s
own lock, preventing torn/interleaved writes within that sequence -- it
just does not provide staleness rejection. Corrected `export_deletion.
py`'s own comment and this document's wording accordingly; no code
behavior changed.

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

**Reusing the same `Idempotency-Key` after a genuine re-grant is rejected
outright, round 21 review.** The paragraph above describes retrying a
*failed* attempt (no `idempotency_records` row is ever written for one,
so a retry genuinely re-attempts the cascade). A *succeeded* disable/
delete does write one, and it lives 365 days -- reusing that same key
later, after the owner genuinely re-enabled `email` and resynced Gmail,
used to match the first call's cached row (`req_hash` depends only on
`domain_key`, not on domain state) and return its stale "success"
response immediately, without `cascade_email_revocation` ever running a
second time. The caller was told the new consent was revoked and the
freshly-synced data purged when neither had happened -- exactly the
silent "disconnected but data remains" state this document's own Task 7
section exists to make unreachable, reachable here through ordinary
idempotency-key reuse alone, no attacker or cross-tenant access
required. Fixed by checking, on a cache hit, whether the domain is
currently enabled again -- one fact that distinguishes a genuine retry
(nothing has changed) from a reused key applied to a materially
different, later request -- and rejecting with `409 IDEMPOTENCY_
CONFLICT` (the same code this idempotency framework already returns for
a request-hash mismatch) rather than silently serving the stale
response. (Round 22 review found `domain.enabled` alone is not the
*only* such fact -- see the paragraph below.)

**A genuine Gmail reconnect, not only a domain re-enable, must also
invalidate a stale cached response, round 22 review.** `gmail_oauth.py:
gmail_oauth_callback_endpoint` -- the sole reconnect path for a `gmail`-
provider `connector_accounts` row (the generic engineering connector-
create endpoint never registers `gmail`; the generic disable endpoint
rejects it when the owner has a `personal_domains` row for `email`, round
25 review) -- reactivates that row straight back to `status=
'active'` with zero reference to `personal_domains`/`domain_consents`.
A real disable/delete followed by a genuine reconnect through that
unrelated OAuth flow (no domain re-enable at all) left `domain.enabled`
still `False`, so round 21's own check alone would still have served
the stale cached response on a same-key replay -- leaving the freshly-
reconnected, still-live connector row untouched. No fresh email content
can resync through this specific gap (`gmail_adapter._email_consent_
active` independently re-checks `domain_consents.revoked_at IS NULL`
before every sync write, and that stays set from the original
disable); what leaks past round 21's fix alone is the connector's live/
undisconnected status and its unrevoked Google grant. Fixed by widening
the cache-hit check to also query for a reactivated `gmail` connector
account for the owner, mirroring `cascade_email_revocation`'s own
identical query, rather than gating the OAuth reconnect flow itself
(Task 1 code this document's Task 7 section does not otherwise touch).

**A genuinely fresh disable request, not only a replayed one, must also
reach a reconnected Gmail account, round 24 review.** Rounds 21-22 close
the case of a *replayed* `Idempotency-Key`. `_disable_domain` separately
gated `cascade_email_revocation` behind `if domain.enabled:`, so a
genuinely fresh disable request (a new key, the ordinary shape of a real
client call) against a domain that was already disabled was
unconditionally a no-op -- even with a reconnected Gmail account from
the same gap above. The freshly-reconnected connector and its unrevoked
Google grant were left untouched indefinitely, with a plain success
response indistinguishable from a routine no-op.
`delete_domain_endpoint`/`_delete_domain_data` already call the cascade
unconditionally for `email`, so this gap was specific to `disable`; that
asymmetry was itself the tell. Fixed by triggering the cascade on
`domain.enabled or reconnected` (the same check rounds 21-22
introduced), not merely `domain.enabled`, matching `_delete_domain_
data`'s own unconditional shape.

**The generic engineering `/disable` endpoint had the identical stale-
cache-after-reconnect gap rounds 21-22 closed for the domain-level
endpoints, round 27 review.** `disable_connector_endpoint`'s own
idempotency cache-hit check (added round 1 for this endpoint's `gmail`-
specific guard) returned the first call's cached `"disconnected"`
response unconditionally, with no re-check of the account's current
state -- so a real disable, followed by a genuine Gmail reconnect (same
row, same `id`), followed by the same `Idempotency-Key` replayed against
this endpoint, served the stale cached success while the account stayed
live, still syncing, with an unrevoked Google grant. Fixed the same way
as rounds 21-22: reject a cache hit with `409 IDEMPOTENCY_CONFLICT`
whenever the account is no longer `disconnected`.

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
`email_consent_active`), and `POST .../forget` nulls a single thread's
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

Task 8 -- the plan's final task -- shipped `GmailPanel`, the executive UX
layer, per `docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`'s
own Task 8 scope (a frontend-only bullet: "GmailPanel inside the existing
PersonalWorkspace shell ... wired into App.tsx/WorkspaceNavigation.tsx").
This section's own three items were never actually part of that scope --
an earlier draft of this document attributed them to "Task 8" without that
attribution ever appearing in the authoritative plan. Explicitly resolved,
not silently dropped: Gmail-specific export, deletion verification,
redacted audit events for connect/sync/body-access/revoke/delete, an
explicit retention period and cache expiry before body storage is
enabled, and key rotation all remain genuinely unimplemented and are not
scheduled against any further task in this phase's plan. **Gmail stays
internal-development only** until this list and recovery evidence exist,
whenever a future phase or task takes it up.

## Deferred controls (open, not scheduled)

Renamed from "Planned controls (Task 8)" -- these were never actually in
Task 8's own scope (see "Unsupported — production blocker" above), so
naming a task here would misstate when, or whether, they ship:

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
| 1.3.7 | 2026-08-10 | Task 7 Loop 2 round 11 review: documented `delete_domain_endpoint`'s own `for_update=True` fix, closing the identical TOCTOU gap round 10 left open at that call site | Lucky Jain |
| 1.3.8 | 2026-08-10 | Task 7 Loop 2 round 12 review: corrected the 1.3.7 entry -- the round-11 fix serializes `delete_domain_endpoint`'s sequence but does not reject a losing race the way `_disable_domain`'s `consent_id` check does, since this endpoint has no such identifier; the same already-accepted self-race behavior `disable_domain_endpoint` has had since round 10 | Lucky Jain |
| 1.3.9 | 2026-08-10 | Task 7 Loop 2 round 17 review: documented the new advisory-lock fix closing the genuine-concurrency gap in the cross-owner colliding-`external_message_id` evidence check (neither the round-4 nor round-8 fix covered two truly-overlapping, not-yet-committed cascades) | Lucky Jain |
| 1.4.0 | 2026-08-10 | Task 7 Loop 2 round 19 review: the round-17 advisory lock was rekeyed from one lock per candidate id to one per workspace, closing a resource-exhaustion risk the per-id version had for large mailboxes (Postgres's shared advisory-lock pool is a database-wide, not per-session, resource) | Lucky Jain |
| 1.4.1 | 2026-08-10 | Task 7 Loop 2 round 21 review: documented the new `409 IDEMPOTENCY_CONFLICT` fix closing an idempotency-key-reuse-after-regrant gap in `disable_domain_endpoint`/`delete_domain_endpoint` -- a reused key used to return the first call's stale cached "success" without re-running the cascade against freshly re-granted, freshly-synced data | Lucky Jain |
| 1.4.2 | 2026-08-10 | Task 7 Loop 2 round 22 review: `domain.enabled` alone (round 21) was not sufficient -- a genuine Gmail reconnect through the separate OAuth callback flow also needed to invalidate a cached idempotency response, since that flow never touches `personal_domains`/`domain_consents` at all; widened the cache-hit check to also detect a reactivated `gmail` connector account | Lucky Jain |
| 1.4.3 | 2026-08-10 | Task 7 Loop 2 round 24 review (HIGH): a genuinely fresh (non-replayed) disable request against an already-disabled domain was unconditionally a no-op, even with a Gmail account reconnected in the meantime -- `cascade_email_revocation` is now triggered on `domain.enabled or reconnected`, not merely `domain.enabled`, matching `delete_domain_endpoint`'s own already-unconditional cascade call | Lucky Jain |
| 1.4.4 | 2026-08-10 | Task 7 Loop 2 round 25 review (MEDIUM): the generic engineering `/disable` endpoint's `gmail`-provider rejection is now gated on the owner having a `personal_domains` row for `email` at all -- without it, an OAuth-connected owner who never enabled the `email` domain had no HTTP-reachable way to disconnect their `gmail` account, since the domain-level endpoints 404 for them too | Lucky Jain |
| 1.4.5 | 2026-08-10 | Task 7 Loop 2 round 27 review (MEDIUM-HIGH): the generic engineering `/disable` endpoint now also rejects a reused `Idempotency-Key` with `409 IDEMPOTENCY_CONFLICT` if the account is no longer `disconnected` when replayed -- the same stale-cache-after-reconnect gap rounds 21-22 closed for the domain-level endpoints, never propagated to this sibling endpoint | Lucky Jain |
| 1.5.0 | 2026-08-11 | Task 8: explicit user decision -- Task 8 shipped exactly plan.md's own frontend-only scope (`GmailPanel`); export/audit-event/retention-policy were never actually in that scope despite this document's own prior "Planned controls (Task 8)" heading implying otherwise. Renamed to "Deferred controls (open, not scheduled)" and reworded "Unsupported — production blocker" to state plainly that Gmail stays internal-development-only with these three items open, not blocked on a task that will close them | Lucky Jain |
