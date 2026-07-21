---
id: PHASE-002-UX-STATES
title: Phase 2 Knowledge UX States
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 2 UX States

## Primary surfaces

Knowledge explorer, entity detail, relationship view, timeline, resolution inbox, merge review and global retrieval.

## Required states

Each surface defines loading, empty, no-results, degraded, stale, recoverable error, offline, permission-denied and version-conflict states. Skeletons must not imply facts. Degraded hybrid retrieval remains usable and identifies lexical fallback.

## Entity detail

Show canonical identity, aliases, active claims, relationships, timeline and provenance. Facts display source and confidence. Missing, deleted and permission-denied evidence are distinct. Corrections create new versions; history remains inspectable.

## Resolution review

Show records side-by-side, shared and conflicting attributes, score factors, evidence and impact. Primary actions are confirm match, reject match and defer. Merge requires explicit confirmation and reason. Keyboard and screen-reader flows must expose the same information.

## Relationship and timeline

Do not depend on a graph visualization for access. Provide an accessible list/table equivalent, filters and deterministic ordering. Historical and current relationships are visually distinct.

## Retrieval

Highlight matching text safely, explain match mode, support filters and preserve query state. Empty results offer filter reset, not fabricated suggestions.

## Accessibility

Core flows meet WCAG 2.2 AA, support 200% zoom, visible focus, non-color status cues, reduced motion and full keyboard operation.
