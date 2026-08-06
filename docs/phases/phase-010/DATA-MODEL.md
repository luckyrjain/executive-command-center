---
id: PHASE-010-DATA-MODEL
title: Phase 10 Gmail Data Model
status: Approved for Implementation
version: 1.1.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-006-CONNECTOR-CONTRACT
  - PHASE-007-DOMAIN-PRIVACY-CONTRACT
---

# Phase 10 Gmail Data Model

## Delivery boundary

- **Current (Tasks 1-2, 5):** OAuth connector account, 30-day metadata
  backfill, Gmail-history incremental sync, thread/message projections,
  sender/recipient entity linking, and (Task 5) controlled body retrieval
  and caching for a message that triggers `email.detect_action`.
  Attention projections (Task 3) and recommendation creation (Task 4) also
  shipped, but through tables this document does not own (`attention_items`,
  `recommendations`) rather than a change to the tables below.
- **Planned (Tasks 6-8):** consent-revocation purge and executive UX.

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
| `body_fetched_at` | Null until Task 5's own body fetch populates it |
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
migration `0074`) supports Task 5's own eligibility scan.

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

All email queries must include both `workspace_id` and `owner_id`. Database
cascades remove messages with a thread and threads with their personal domain
or connector account. **Current limitation:** Task 2 does not connect a
`domain_consents` revocation to connector disable and deletion. That cascade
is planned for Task 7 and remains a production blocker; operators must not
claim consent revocation already purges Gmail data.

## Planned model changes

Tasks 6-8 may add further schema for consent-revocation purge and executive
UX. They require their own migration and contract-version update; this
document does not describe those rows as present.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented the Task 1-2 Gmail persistence contract | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 5 review (Loop 2 round 16): documented body/body_fetched_at now populated by `email.detect_action`'s own body fetch, and the new `ix_email_messages_detect_action_eligible` partial index (migration `0074`); this document had gone stale after Tasks 3-5 shipped without a contract-version update | Lucky Jain |
