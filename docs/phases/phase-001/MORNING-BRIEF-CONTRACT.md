---
id: PHASE-001-MORNING-BRIEF
 title: Phase 1 Morning Brief Contract
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Morning Brief Contract

## Purpose

The morning brief is a persisted, explainable snapshot for one workspace date. It is deterministic by default and remains fully usable when AI is disabled or unavailable.

## Sections and limits

1. Today's Schedule — maximum 8 meetings, ordered by start time.
2. Top Priorities — maximum 7 attention items, ordered by the priority model.
3. Overdue Commitments — maximum 5, ordered by score then oldest due date.
4. Risks — maximum 5 open risks, ordered by probability×impact then review date.
5. Waiting On — maximum 5 commitments made to the user or blocked tasks.
6. Recently Changed — maximum 5 audited changes from the previous 24 hours.
7. Recommended Actions — maximum 3 approved rule/AI proposals requiring confirmation.

Duplicate underlying entities appear in only the highest-priority applicable section, except meetings may also be referenced as evidence.

## Generation lifecycle

A brief is keyed by `(workspace_id,user_id,briefing_date,generation_version)`. Generation occurs on first request after 04:00 local time, on explicit refresh, or after a material event when the existing brief is older than 15 minutes. Explicit refresh is idempotent.

The deterministic generator reads a transactionally consistent snapshot. It stores section payloads, source entity versions, evidence IDs, generated_at, timezone, algorithm version, optional AI enrichment status, and stale reason.

## AI behavior

AI enrichment is disabled by default behind `phase1.ai_brief_enrichment`. It may rewrite summaries but may not change inclusion, order, score, or proposed action. If AI fails, the deterministic brief is returned with `ai_status=unavailable` and no error to the user-facing dashboard.

## Evidence and explanation

Every non-calendar item includes `why`, score factors, source entity reference, source version, and evidence summaries. Missing or inaccessible evidence is shown explicitly and never silently omitted.

## Staleness

A brief is stale when any included entity version changes, the workspace date changes, or it is older than 30 minutes. Stale briefs may be returned immediately with `stale=true` while a deterministic refresh runs synchronously only when it can complete within the two-second dashboard budget; otherwise the dashboard uses live projections.

## Empty and degraded states

Empty sections are omitted except Today's Schedule and Top Priorities, which return an explicit empty-state message. Database unavailability returns `503`; AI unavailability never blocks the brief.

## Observability

Record generation duration, section counts, stale returns, AI fallback count, refresh reason, and failure category. Do not record private content, raw note text, entity IDs as metric labels, or raw search text.

## Acceptance tests

Snapshot tests freeze ordering and explanations. Tests cover empty data, all-day meetings, cross-midnight events, timezone rollover, duplicate suppression, AI unavailable, stale entity versions, recommendation confirmation links, and representative-data p95 under two seconds.