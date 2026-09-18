---
id: CONTEXT-ENGINEERING
title: Engineering Context
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Engineering

Authorizing and syncing data from external engineering-tool providers.

## Language

**ConnectorAccount**:
One authorized credential for one provider (GitHub, GitLab, Jira, Datadog, or Gmail).

**ConnectorAdapter**:
One provider's full account lifecycle: authorize, backfill, incremental sync, handle webhook, refresh
permissions, disconnect.
_Avoid_: ActionAdapter (see [Automation](../automation/CONTEXT.md)) — a different concept, already correctly
disambiguated in code: a ConnectorAdapter owns read-sync and account lifecycle; an ActionAdapter owns one
write action. "Adapter" alone is used for at least three unrelated things across this codebase — always
qualify it.

**OAuth2ConnectorAdapter**:
A ConnectorAdapter variant for providers needing three-legged OAuth. Gmail today.

## Open question

`docs/domain/DOMAIN-MODEL.md` names SourceRecord as a first-class entity this context owns. SourceRecord
doesn't exist anywhere in code — synced data lives in named per-provider tables instead (repositories,
changes, reviews, work items, and Datadog's own monitor/service/dashboard tables). Whether SourceRecord
should be built or dropped from the canonical model is an open decision.

**Resolved (architecture review, 2026-09-18): SyncCursor stays raw rows, not a class.** `DOMAIN-MODEL.md`
also names SyncCursor as first-class; this doc previously left promoting it to a real class as an open
question, on the theory that its "manipulated as raw rows" status meant read/write logic was duplicated
somewhere. Verified directly: every read and write of `sync_cursors` already lives in one function,
`_run_connector_sync` (`connector_accounts.py`) — not scattered across the 4+ provider adapters. The real,
narrower duplication (two near-identical UPSERTs for `cursor_value` and `backfill_resume_cursor` inside that
one function) was collapsed into a small `_save_sync_cursor` helper. A `SyncCursor` class would have nothing
left to concentrate — it would gain a second adapter (a real seam) only if some other write path emerged,
which none has.
