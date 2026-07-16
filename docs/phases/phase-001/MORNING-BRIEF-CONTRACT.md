---
id: PHASE-001-MORNING-BRIEF
title: Phase 1 Morning Brief Contract
status: Approved
version: 1.0.2
owner: Lucky Jain
---

# Morning Brief Contract

## Purpose

The morning brief is a persisted, explainable snapshot for one workspace date. It is deterministic by default and remains fully usable when AI is disabled or unavailable.

## Sections and limits

1. Today's Schedule — maximum 8 meetings, ordered by start time.
2. Top Priorities — maximum 7 attention items, ordered by the priority model.
3. Overdue Commitments — maximum 5, ordered by score then oldest due date/time.
4. Risks — maximum 5 open risks, ordered by probability×impact then review date.
5. Waiting On — maximum 5 commitments with `direction=made_to_me` or blocked tasks with `blocked_on_person_id`.
6. Recently Changed — maximum 5 audited changes from the previous 24 hours.
7. Recommended Actions — maximum 3 eligible `pending_confirmation` rule/AI recommendations.

Duplicate underlying entities appear only in the highest-priority applicable section, except meetings may also be evidence.

## Generation lifecycle

A brief is keyed by `(workspace_id,user_id,briefing_date,generation_version)`. Generation occurs on first request after 04:00 local time or explicit refresh.

After a material source event, a brief older than 15 minutes becomes **refresh-eligible**. Independently, any brief older than 30 minutes becomes **stale-by-age**. These are distinct states: refresh eligibility permits regeneration; staleness requires the response to set `stale=true` or use live projections.

Explicit refresh is idempotent. The deterministic generator reads a transactionally consistent snapshot and stores section payloads, source versions, evidence IDs, generated_at, timezone, algorithm version, optional AI enrichment status and stale reason.

## AI behavior

AI enrichment is disabled by default behind `phase1.ai_brief_enrichment`. It may rewrite summaries but may not change inclusion, order, score or proposed action. AI failure returns the deterministic brief with `ai_status=unavailable`.

## Evidence and explanation

Every non-calendar item includes why, score factors, source entity reference/version and evidence summaries. Evidence status uses exactly `available|missing|permission_denied|deleted`. Missing, inaccessible or deleted evidence is shown explicitly and never silently omitted.

## Staleness and degraded behavior

A brief is stale when an included source version changes, the workspace date changes, or it is stale-by-age. Stale briefs may be returned immediately with `stale=true`. A synchronous deterministic refresh occurs only when it can finish within the two-second dashboard budget; otherwise the dashboard uses live projections.

Empty sections are omitted except Today's Schedule and Top Priorities, which return explicit empty messages. Database unavailability returns 503; AI unavailability never blocks the brief.

## Observability and tests

Record generation duration, section counts, refresh eligibility, stale returns, AI fallback count, refresh reason and failure category without private content or high-cardinality labels. Snapshot tests cover empty data, all-day meetings, cross-midnight events, timezone rollover, duplicate suppression, AI unavailable, stale versions, refresh-eligible versus stale-by-age behavior, recommendation publication/confirmation links, all four evidence states and representative-data p95.
