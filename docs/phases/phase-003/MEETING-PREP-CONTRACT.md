---
id: PHASE-003-MEETING-PREP
title: Meeting Preparation Contract
status: Draft
version: 0.1.0
owner: Lucky Jain
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

## Optional enrichment

AI may summarize retrieved authorized evidence behind a feature flag. The deterministic pack remains available when AI is disabled. Enrichment may not introduce uncited facts or change authoritative records.

## Safety

Private notes are excluded unless explicitly allowed for that surface. Deleted and permission-denied evidence appears only as an availability state. Prompt-injection content from sources is treated as data and never as instruction.

## Evaluation

Versioned meeting scenarios measure factual support, source coverage, missed critical commitments, stale detection, citation correctness and concise length. Any unsupported factual statement blocks release.
