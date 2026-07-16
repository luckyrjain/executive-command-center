---
id: PHASE-003-API-SCHEMAS
title: Phase 3 Human Attention API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 3 API Schemas

## Proposed surface

```text
GET /attention
GET /attention/{id}
POST /attention/{id}/pin|dismiss|defer|restore
POST /attention/{id}/feedback
GET|POST /waiting
PATCH /waiting/{id}
POST /waiting/{id}/fulfil|cancel
GET /risks/review-queue
POST /risks/{id}/review
GET|PUT /planning/capacity
GET|POST /plans
GET /plans/{id}
POST /plans/{id}/propose|accept|supersede
POST /plans/{id}/blocks/{block_id}/move|remove
GET|POST /meetings/{id}/prep
POST /meetings/{id}/prep/refresh
```

## Conventions

Session-derived actor/workspace, UUIDs, ISO-8601 with workspace timezone, signed cursors, idempotency keys and optimistic concurrency are mandatory. Mutations return the current representation and write audit/outbox atomically.

## Attention result

Includes source reference, state, score, confidence, policy version, ordered factors, evidence, freshness, waiting direction, risk indicators and applicable user override. It must never expose a hidden person score.

## Planning

Plan creation identifies date/range, capacity profile version, optional fixed constraints and source snapshot. Proposal returns blocks, unscheduled items, conflicts, capacity summary and rationale. Accept requires the current plan version and is a durable human confirmation. It does not write to external calendars.

## Meeting preparation

Returns objective, attendees as knowledge entity refs, prior decisions, open commitments, risks, recent timeline, open questions, suggested agenda, evidence and source versions. Optional enrichment is marked separately from deterministic sections.

## Errors

Required codes: `version_conflict`, `constraint_conflict`, `capacity_exceeded`, `invalid_waiting_direction`, `stale_plan`, `stale_meeting_pack`, `evidence_unavailable`, `feature_disabled`, `cursor_invalid`.
