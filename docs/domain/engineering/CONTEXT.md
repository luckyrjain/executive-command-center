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

`docs/domain/DOMAIN-MODEL.md` names SourceRecord and SyncCursor as first-class entities this context owns.
SourceRecord doesn't exist anywhere in code — synced data lives in named per-provider tables instead
(repositories, changes, reviews, work items, and Datadog's own monitor/service/dashboard tables). SyncCursor
does have a real table, but no dedicated class — it's manipulated as raw rows, not a first-class object the
way ConnectorAccount is. Whether SyncCursor deserves promotion to a real class, and whether SourceRecord
should be built or dropped from the canonical model, are open decisions.
