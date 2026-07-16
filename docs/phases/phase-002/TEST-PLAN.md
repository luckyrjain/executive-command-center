---
id: PHASE-002-TEST-PLAN
title: Phase 2 Test Plan
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 2 Test Plan

## Functional coverage

- Entity, alias, claim and relationship lifecycle.
- Resolution candidate generation, confirmation, rejection and deduplication.
- Atomic merge, redirects, duplicate-edge handling, split/reversal and lineage.
- Timeline projection ordering and deterministic rebuild.
- Lexical retrieval, hybrid fusion, filters, signed cursors and degradation.
- Source permission, deletion and evidence-state propagation.

## Property and adversarial tests

Generate alias collisions, Unicode normalization cases, circular relationships, concurrent merges, stale versions, invalid intervals and cross-workspace identifiers. Verify no silent merge, no orphan edge and no information leakage.

## Retrieval evaluation

Run a versioned labelled benchmark for precision@5, recall@10, MRR and false-positive rate. Compare ranking versions. Test embedding-disabled, missing model, corrupt projection and stale projection behavior.

## Performance

Measure entity lookup, candidate generation, 10,000-entry timelines, projection rebuild and retrieval at acceptance dataset size. Record p50/p95/p99 and query plans for regressions.

## Security and privacy

Workspace isolation for every table and endpoint; injection and unsafe snippet tests; audit redaction; embedding redaction; source deletion; export; backup/restore and cursor tampering.

## Browser acceptance

Create an entity, add evidence, review a resolution candidate, merge and reverse safely, inspect a relationship/timeline, retrieve context and exercise degraded lexical mode with keyboard-only navigation.

## Exit gate

All tests and migrations pass; deterministic rebuild matches source state; benchmark and latency thresholds pass; zero Critical, High or Medium findings remain.
