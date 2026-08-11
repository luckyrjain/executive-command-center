---
id: PHASE-010-DATA-MODEL
title: Phase 10 Gmail Data Model
status: Approved for Implementation
version: 1.5.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-006-CONNECTOR-CONTRACT
  - PHASE-007-DOMAIN-PRIVACY-CONTRACT
---

# Phase 10 Gmail Data Model

## Delivery boundary

- **Current (Tasks 1-2, 5-7):** OAuth connector account, 30-day metadata
  backfill, Gmail-history incremental sync, thread/message projections,
  sender/recipient entity linking, (Task 5) controlled body retrieval and
  caching for a message that triggers `email.detect_action`, (Task 6)
  on-demand human-facing body retrieval/caching for an explicit thread
  open plus a per-thread "forget this" write path that nulls the same
  cached fields back out, and (Task 7) the consent-revocation cascade
  (see "Ownership, retention, and deletion" below) plus its own new
  `email_message_id_purge_log` table. Task 6 adds no new table or column
  to the ones below -- it reuses Task 5's own `body`/`snippet`/
  `body_fetched_at` columns for both writes; its one schema change
  (`deletion_jobs.scope`/`resource_id`) belongs to Phase 7's own
  `deletion_jobs` table, documented in `docs/phases/phase-007/DATA-
  MODEL.md`'s cross-phase amendment, not here. Attention projections
  (Task 3) and recommendation creation (Task 4) also shipped, but through
  tables this document does not own (`attention_items`, `recommendations`)
  rather than a change to the tables below.
- **Current (Task 8):** executive UX (`GmailPanel`) and its two supporting
  backend additions -- both read-shaped, neither needing a migration.
  `GET /api/v1/personal/gmail/threads` computes every field it returns
  (`last_sender`/`last_direction`/`message_count`/`body_cached`) live from
  the existing `email_threads`/`email_messages` columns above; `SyncRequest.
  since` (`connector_accounts.py`) is a new request field threaded into
  `GmailAdapter.backfill`'s own `since` parameter, which has accepted it
  since migration `0069`'s own `connectors.py` Protocol widening (Task 1) --
  Task 8 is the first real caller to pass a non-`None` value.

## Current tables

### `connector_accounts`

The existing Phase 6 table accepts `provider = 'gmail'`. One row represents
one Google account inside a workspace. `owner_id` is the connecting ECC user;
`external_account_id` is Gmail's account email. Access token, refresh token,
and expiry are serialized together and encrypted with
`ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY`. Credentials never appear in response
models.

### `email_threads`

| Field | Contract |
|---|---|
| `id`, `workspace_id`, `owner_id` | UUID identity and personal-owner boundary |
| `domain_key` | Always `email`; composite FK to `personal_domains` |
| `connector_account_id` | Composite workspace FK; cascade on connector deletion |
| `external_thread_id` | Gmail thread ID; unique per workspace/account |
| `subject` | Nullable plaintext structural field |
| `last_message_at` | Greatest observed message time, never moved backward |
| `created_at`, `updated_at` | UTC lifecycle timestamps |

Index `ix_email_threads_owner_last_message` supports owner-scoped recency
queries. `(workspace_id, id)` is unique for the message composite FK.

### `email_messages`

| Field | Contract |
|---|---|
| `id`, `workspace_id`, `owner_id`, `thread_id` | UUID identity and workspace/owner/thread boundary |
| `external_message_id` | Unique within workspace/thread; deduplication key |
| `sender`, `recipients` | Normalized plaintext addresses, each at most 320 characters |
| `sent_at`, `direction` | UTC time and `inbound` or `outbound` |
| `snippet`, `body` | Nullable narrative fields; `body` holds Fernet ciphertext (`ECC_PERSONAL_DATA_ENCRYPTION_KEY`) once fetched |
| `body_fetched_at` | Null until a body fetch (Task 5's proactive path or Task 6's on-demand `GET`) populates it; renulled by Task 6's own "forget this" |
| `created_at`, `updated_at` | UTC lifecycle timestamps |

Task 2 writes `snippet`, `body`, and `body_fetched_at` as `NULL`; it fetches
headers only. Task 5's `gmail_adapter.py:_detect_action_for_message` fetches
one message's full body (`gmail.readonly`, `format=full`) the first time it
becomes eligible for `email.detect_action`, encrypts it with `crypto.
encrypt_field`, and writes `body`/`body_fetched_at` together in the same
`UPDATE ... WHERE body IS NULL` -- every other message in a thread keeps
`body IS NULL` until its own turn. Index `ix_email_messages_thread_sent`
supports thread chronology; the partial index `ix_email_messages_detect_
action_eligible` (`workspace_id, owner_id, direction WHERE body IS NULL`,
migration `0074`) supports Task 5's own eligibility scan. Task 6's `GET
/api/v1/personal/gmail/threads/{thread_id}` calls the same `fetch_and_
store_body` method (extracted from `_detect_action_for_message` for this
reuse) for every still-`body IS NULL` message in a thread an owner
explicitly opens, up to `MAX_THREAD_MESSAGES`; its own `POST .../forget`
is the only write path that ever nulls `snippet`/`body`/`body_fetched_at`
back out once set, via `UPDATE ... WHERE thread_id = :thread_id` scoped to
one thread (`docs/phases/phase-010/PRIVACY-CONSENT-CONTRACT.md`'s own
entry covers why nulling, not row deletion).

### `sync_cursors` and `sync_runs`

The existing connector tables accept resource type `message`. A cursor is an
opaque Gmail history position; a bounded partial history record may use
`historyId:recordId:skipCount`. `sync_runs` records `running`, `succeeded`,
`partial`, or `failed` and never stores email content.

## Provenance and entity resolution

For a new normalized participant address, Task 2 creates a workspace-visible
`pkos_nodes(node_type='person')`, a `pkos_evidence(source_type='gmail_sync')`
row containing only a source reference and its SHA-256, and an `email`
`entity_aliases` row. Exact normalized email is Phase 2 match level 3.
Concurrent creation converges on the unique alias row.

## Ownership, retention, and deletion

All email queries must include both `workspace_id` and `owner_id`.
`email_threads`/`email_messages` carry `ondelete=CASCADE` FKs to their
personal domain and connector account, but neither of those parent rows is
ever actually hard-deleted by this application (`personal_domains`/
`connector_accounts` are always kept, only `enabled`/`status` flipped) --
so, in practice, those schema-level cascades never fire on their own.
**Task 7** closes the gap this previously left open: `ecc.domains.
personal.gmail_revocation.cascade_email_revocation` performs the explicit
purge Phase 7's own `_disable_domain`/`_delete_domain_data` write paths
now run for `domain_key = "email"` -- disconnecting every one of the
owner's own Gmail connector account(s) and deleting `email_threads`/
`email_messages` plus the `attention_items`/`pkos_evidence` rows this
owner's own Gmail sync produced (an evidence row is left alone rather
than purged if its `external_message_id` happens to collide with a
different owner's own message -- see `gmail_revocation.py`'s own module
docstring), rather than relying on a cascade that would only ever fire if
a parent row were hard-deleted, which none of this application's own code
paths do. `email_action_detected` `recommendations` not yet `executed`
are deleted the same way; an already-`executed` one is redacted in place
instead, not deleted (its confirmed downstream `tasks`/`commitments`/
`risks` row is the owner's own work product, not Gmail data). See
`PRIVACY-CONSENT-CONTRACT.md`'s own "Consent revocation cascade (Task 7)"
section for the full description.

### `email_message_id_purge_log`

Added by migration `0076` (Task 7 Loop 2 round 8 review), not part of the
original Task 7 delivery. One row per `(workspace_id, external_message_id,
owner_id)` an owner's own cascade has ever processed, written by
`cascade_email_revocation` before it deletes that owner's own `email_
messages` rows -- an append-only ledger that outlives those rows,
closing a sequential cross-owner `pkos_evidence` leak the round-4 fix
below could not: that fix's own ambiguity check only ever compared
against still-*live* `email_messages` rows, so it missed the case where a
first owner's own colliding row is already gone by the time a second
owner's cascade runs. Never itself purged by any code path in this
codebase (a `workspace_id` `ON DELETE CASCADE` FK is its only removal
path) -- see migration `0076`'s own docstring for the full reasoning,
including why that is the correct call privacy-wise (no message content,
only an opaque id).

## Planned model changes

None. Task 8 -- the plan's final task -- shipped its executive UX and the
two backend additions above needing no new migration, closing this
section out. Task 7's own consent revocation cascade similarly shipped
needing no new migration -- this document's own prior "Tasks 7-8 may add
further schema for consent-revocation purge" claim went stale in that
particular respect -- but Loop 2 round 8 review added one after all:
`email_message_id_purge_log` (migration `0076`), described above, closing
a gap the original cascade design could not close without persisted
state.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented the Task 1-2 Gmail persistence contract | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 5 review (Loop 2 round 16): documented body/body_fetched_at now populated by `email.detect_action`'s own body fetch, and the new `ix_email_messages_detect_action_eligible` partial index (migration `0074`); this document had gone stale after Tasks 3-5 shipped without a contract-version update | Lucky Jain |
| 1.2.0 | 2026-08-06 | Task 6 review: documented on-demand human-facing body retrieval and the new "forget this" write path that renulls `snippet`/`body`/`body_fetched_at`; this document had gone stale after Task 6 shipped without a contract-version update | Lucky Jain |
| 1.3.0 | 2026-08-10 | Task 7: documented the consent revocation cascade, correcting the "cascade is planned for Task 7" claim and the "Tasks 7-8 may add further schema" claim (no new migration was needed) | Lucky Jain |
| 1.3.1 | 2026-08-10 | Task 7 Loop 2 round 5 review: this file was never revisited across four review rounds -- corrected the "every derived ... row" overclaim (round 4's cross-owner collision fix leaves an ambiguous evidence row unpurged) and the imprecise "recommendations ... row" language (executed ones are redacted in place, not deleted); also noted an owner can hold more than one Gmail connector account | Lucky Jain |
| 1.4.0 | 2026-08-10 | Task 7 Loop 2 round 8 review: documented the new `email_message_id_purge_log` table (migration `0076`), closing a sequential cross-owner evidence leak the round-4 fix could not close without persisted state -- correcting this document's own prior "no new migration was needed" claim | Lucky Jain |
| 1.4.1 | 2026-08-10 | Task 7 Loop 2 round 16 review: the "Delivery boundary" section at the top of this file was never reconciled after Task 7 shipped -- still read "Current (Tasks 1-2, 5-6)" / "Planned (Tasks 7-8): consent-revocation purge" while the rest of this same document (as of 1.3.0-1.4.0) already described Task 7's cascade as fully shipped; corrected | Lucky Jain |
| 1.5.0 | 2026-08-11 | Task 8: documented the thread-list endpoint and `since` param as read-shaped additions needing no migration, and closed "Planned model changes" out now that the plan's final task has shipped | Lucky Jain |
