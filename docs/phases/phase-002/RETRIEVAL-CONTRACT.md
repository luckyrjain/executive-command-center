---
id: PHASE-002-RETRIEVAL
title: Hybrid Retrieval Contract
status: Approved for Implementation
version: 0.3.0
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

Exact canonical name > exact alias > lexical relevance (bounded above by a hybrid semantic-agreement bonus, still capped below the next tier up) > semantic-only relevance. Entity ID is the final tie-breaker.

The schema has no separate "trusted identifier" column beyond an alias's free-form `alias_type`, so an exact-identifier tier above exact canonical name is not distinguishable from an exact alias match and is not implemented as a separate level. Authority, recency and user-confirmed status are not scoring factors in the shipped implementation -- nothing in `retrieval.py` reads a confirmation flag, a document age, or a source-authority weight into the ranking formula. A future iteration that wants these factors needs its own versioned scoring-formula change and benchmark re-run per the Evaluation section below, not a documentation-only claim.

## Result contract

Every result includes entity type and ID, title, snippet, score, matching mode, factor summary, evidence state and source freshness. Snippets derive only from authorized content and are escaped before display.

## Degradation

If embedding generation or retrieval fails, return lexical results with `degraded=true`; do not fail the request. Missing source permission removes content from candidates. Stale projections expose their source version and schedule a rebuild without blocking lexical retrieval.

## Evaluation

A versioned benchmark contains representative person, project, decision and topic queries with relevance judgements. Release gates include precision@5, recall@10, zero isolation leakage and latency targets. Ranking changes require before/after benchmark results.
