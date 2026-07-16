---
id: PHASE-001-SEARCH-CONTRACT
title: Phase 1 Search Contract
status: Approved
version: 1.0.1
owner: Lucky Jain
---

# Search Contract

## Boundary

Phase 1 search is deterministic and PostgreSQL-only. It does not require embeddings, an external vector database, or an AI service.

## Indexed entities

Task, Commitment, Note, Meeting, CalendarEvent, and Risk. Archived entities are excluded by default.

The API filter enum is exactly:

```text
task | commitment | note | meeting | calendar_event | risk
```

## Query normalization

Trim whitespace, Unicode-normalize, case-fold, collapse repeated spaces, and reject empty queries. Maximum query length is 500 characters. Raw queries are never emitted as metric labels or logs.

## Ranking

Scores are normalized to 0..1 and expose components:

- exact normalized title/name match: 1.00,
- title prefix match: 0.85,
- title trigram similarity: up to 0.75,
- PostgreSQL full-text rank: up to 0.70,
- body/description prefix or full-text: up to 0.55,
- recency boost within 7/30/90 days: +0.12/+0.06/+0.02,
- pinned: +0.08,
- archived penalty when explicitly included: -0.20.

Clamp to 1.0. Tie-break by score, updated_at descending, entity type order task, commitment, meeting, note, risk, calendar_event, then UUID.

## API result

Each result returns entity type/id, title, sanitized snippet, matched fields, score, score components, updated_at, timestamp context, source type, archived state, and evidence summaries. Snippets are at most 240 characters and escape markup.

## Filters and pagination

Filters: entity types, updated-from/to, due-from/to, statuses, include_archived. Cursor encodes score, updated_at, type, and ID and is opaque/signed. Page size defaults to 20 and maxes at 100.

## Evidence and permissions

Evidence access follows the original source. The canonical evidence access enum is `available|missing|permission_denied|deleted`. Results may identify that evidence exists while returning any non-available state. Search never leaks cross-workspace existence; inaccessible and absent entities both behave as not found.

## Performance

With the representative fixture of 10,000 entities and 50,000 notes, search p95 must be below 500 ms locally and under 800 ms in CI. Query plans for supported paths must use approved indexes.

## Degraded behavior

Search remains available when AI is disabled. Index projection failure falls back to normalized prefix matching and returns `degraded=true` without returning stale cross-workspace data.

## Tests

Exact/prefix/full-text ranking, all six entity types, stable pagination, archived handling, HTML escaping, timezone timestamp context, all four evidence access states, cross-workspace isolation, malformed cursors, performance budget, and deterministic snapshots.
