---
id: PHASE-003-MEETING-PREP
title: Meeting Preparation Contract
status: Approved for Implementation
version: 0.4.0
owner: Lucky Jain
updated: 2026-10-03
---

# Meeting Preparation Contract

## Goal

Provide concise, evidence-backed context before a meeting without mixing facts, unanswered questions and suggestions.

## Required deterministic sections

- Meeting objective and timing.
- Participants and known roles.
- Relevant recent timeline.
- Open commitments by direction.
- Prior decisions and unresolved questions.
- Active risks and dependencies.
- Documents or notes worth reviewing.
- Evidence gaps and source freshness.

Suggested agenda or talking points are separate and clearly labelled.

## Source selection

Use meeting links, participant entities, project/topic relationships and bounded recent history. Respect source permissions at query and render time. Deduplicate by canonical entity and source. Prefer user-confirmed, recent and directly linked evidence.

## Snapshot and staleness

A pack stores source IDs and versions, generation time and stale threshold. Material meeting, commitment, decision, risk or participant changes mark it stale. Refresh creates a new snapshot; history remains available.

A pack is stored once per meeting and served to every reader of that meeting, so the stored snapshot holds only `workspace`-visible sources: participants whose entity is workspace-visible, and workspace-visible timeline entries, commitments, notes, risks, dependencies and (with `ECC_PERSONAL_DATA_ISOLATION` on) evidence. Its fingerprint, and therefore its stale state, is computed from those shared sources only, so it is identical for every reader and no reader's private data can flip it.

Every response (generate, read, refresh) adds a live overlay of the caller's own readable rows that the snapshot excludes: their private rows, rows explicitly shared with them, and rows about participant entities only they can see. The overlay is recomputed per request, merged in each section's own order and limit, and never stored in the pack. One exception: a create or refresh response is saved in that caller's own idempotency record, so a replay with the same idempotency key within its window returns the overlay as it was on the first call, minus any row the caller can no longer read, and with the stored summary withheld when any row of that pack's stored snapshot is no longer readable (see below). Near the evidence limit the merged view can list slightly more evidence gaps than one capped query would, but only gaps the caller may read.

Every response also re-checks the snapshot's own rows against the live rows and drops any the caller can no longer read: a row narrowed to someone else (private, or explicitly shared without this caller), deleted, or a note since marked `restricted`, and rows keyed to a participant entity the caller can no longer read (with that participant). Only visibility is re-checked (plus, for a participant-keyed row, that its key still names a readable participant of the pack, so a re-keyed row is dropped too); status, archive and text changes are still served as stored until refresh. A filtered section can hold fewer rows than its limit until refresh. Such a change also changes the fingerprint, so the pack reads `stale`. When anything is dropped, the stored AI summary is withheld (`enrichment.available = false`, `error_code = evidence_unavailable`), because it may quote the dropped rows. The stored pack itself is unchanged until refresh.

The AI tool `meeting.get_prep_pack` and the stored AI enrichment use only the shared snapshot, never a caller's overlay, because both feed artifacts every meeting reader sees. During a generate or refresh, the enrichment run summarizes exactly the snapshot that request stores (never a fresh regeneration), so the per-read check above covers every row its summary can quote.

## Optional enrichment

AI may summarize retrieved authorized evidence behind a feature flag. The deterministic pack remains available when AI is disabled. Enrichment may not introduce uncited facts or change authoritative records.

## Safety

Notes marked `restricted` are always excluded. A caller's own `visibility='private'` notes (and notes explicitly shared with them) appear only in that caller's overlay, never in the stored snapshot. Deleted and permission-denied evidence appears only as an availability state. Prompt-injection content from sources is treated as data and never as instruction.

Rollout: packs generated before the workspace-only snapshot rule (0.3.0) may contain other members' private rows (participants, timeline, commitments, risks, dependencies, notes) and private text in their stored AI enrichment summary, and in the related workspace-visible `ai_runs` / `ai_run_steps` rows. After deploy such packs flip to `stale`; until refreshed they are served with every row the reader cannot read removed (0.4.0), and with the stored summary withheld for such readers. Before enabling `ECC_PERSONAL_DATA_ISOLATION` (Spec A rollout step R5), archive or refresh every active pack generated before the deploy, and purge or narrow to their actor every `meeting.prep_summary` `ai_runs` row (with its `ai_run_steps`) created before the deploy: refreshing a pack leaves those runs in place, and `GET /api/v1/ai/runs/{run_id}` still serves them to any workspace member.

## Evaluation

Versioned meeting scenarios measure factual support, source coverage, missed critical commitments, stale detection, citation correctness and concise length. Any unsupported factual statement blocks release.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 0.4.0 | 2026-10-03 | Responses drop snapshot rows the caller can no longer read (narrowed, deleted, restricted, or keyed to an unreadable participant), including idempotent replays; the stored AI summary is withheld when anything is dropped, and the enrichment run summarizes exactly the stored snapshot | Lucky Jain |
| 0.3.0 | 2026-09-28 | Stored snapshot holds only workspace-visible sources; per-caller live overlay; AI tool and enrichment use the shared snapshot; pre-deploy pack rollout requirement | Lucky Jain |
| 0.2.0 | 2026-07-23 | Earlier approved contract (commit 6fd807fb) | Lucky Jain |
