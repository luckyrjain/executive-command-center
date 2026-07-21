---
id: PHASE-002-ENTITY-RESOLUTION
title: Entity Resolution Contract
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Entity Resolution Contract

## Goal

Resolve references to stable entities without silently collapsing distinct people, organizations, projects or topics.

## Match hierarchy

1. Existing user-confirmed mapping.
2. Exact trusted external identifier.
3. Exact normalized workspace-scoped identifier such as verified email.
4. Exact alias plus compatible entity kind.
5. Weighted fuzzy candidate generation.

Lower levels may propose but cannot auto-confirm a merge.

## Candidate scoring

Factors may include normalized name, alias overlap, verified identifiers, organization/project context, relationship neighborhood and temporal compatibility. Every candidate stores factor contributions, resolver version and source versions. Protected or sensitive attributes must not be used.

## Decision thresholds

- Deterministic trusted identifier match may attach an alias to the existing entity.
- A unique high-scoring inferred match creates a review candidate.
- Multiple plausible matches remain unresolved.
- Absence of a match creates a new entity only when the caller explicitly requests creation.

Threshold values are typed configuration and require benchmark evidence before change.

## Human review

Review shows both entities, conflicting attributes, shared evidence, score factors and consequences. Confirm and reject are idempotent. Confirmation records actor and reason. Rejection prevents the same unchanged pair from being proposed again.

## Merge and split

Merge selects or creates a canonical target, redirects source IDs, rehomes aliases and active edges, resolves duplicates deterministically and records full lineage. No authoritative source record is deleted. Reversal restores prior identities unless a later dependent operation requires manual split.

## Quality metrics

Measure pairwise precision/recall, false-merge rate, unresolved rate and reviewer override rate on a versioned labelled dataset. False merges are the highest-severity failure and block release.
