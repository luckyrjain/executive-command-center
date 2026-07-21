---
id: PHASE-002-RETRIEVAL
title: Hybrid Retrieval Contract
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Hybrid Retrieval Contract

## Modes

Lexical mode is mandatory and deterministic. Hybrid mode combines lexical and optional embedding candidates. AI generation is not part of retrieval and cannot alter authoritative records.

## Pipeline

1. Normalize query and filters.
2. Retrieve exact identifier and alias matches.
3. Retrieve PostgreSQL full-text and trigram candidates.
4. When enabled and healthy, retrieve embedding candidates.
5. Fuse ranked lists using a versioned deterministic method.
6. Apply workspace, permission, lifecycle and time filters.
7. Return evidence and match explanation.

## Ranking

Exact trusted identifier > exact canonical name > exact alias > lexical relevance > semantic relevance. Authority, recency and user-confirmed status may adjust scores within bounded versioned weights. Entity ID is the final tie-breaker.

## Result contract

Every result includes entity type and ID, title, snippet, score, matching mode, factor summary, evidence state and source freshness. Snippets derive only from authorized content and are escaped before display.

## Degradation

If embedding generation or retrieval fails, return lexical results with `degraded=true`; do not fail the request. Missing source permission removes content from candidates. Stale projections expose their source version and schedule a rebuild without blocking lexical retrieval.

## Evaluation

A versioned benchmark contains representative person, project, decision and topic queries with relevance judgements. Release gates include precision@5, recall@10, zero isolation leakage and latency targets. Ranking changes require before/after benchmark results.
