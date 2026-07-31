---
id: PHASE-007-API-SCHEMAS
title: Phase 7 Personal Intelligence API
status: Approved for Implementation
version: 0.4.0
owner: Lucky Jain
---

# Phase 7 API Schemas

```text
GET|POST /personal/domains
POST /personal/domains/{id}/enable|disable|export|delete
GET|POST /personal/records
GET|PATCH /personal/records/{id}
GET|POST /personal/goals
GET|POST /personal/routines
GET|POST /personal/check-ins
GET|POST /personal/consents
POST /personal/consents/{id}/revoke
GET|POST /personal/grants
POST /personal/grants/{id}/revoke
GET /personal/insights
POST /personal/insights/{id}/dismiss|feedback
```

Requests declare domain and purpose. APIs enforce consent and field policy server-side. Insight responses include kind, evidence, confidence, limitations, freshness and policy version. Health/finance suggestions never use diagnostic or guaranteed-return language.

## Task 1 status

Ships every route above except `/personal/insights/{id}/feedback` (deferred with the AI-generated `trend`/`correlation` insight kinds it applies to -- `docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, Decision 1 item 5/Decision 4). List/summary responses for `sensitive`/`high_stakes` records redact encrypted fields per `DOMAIN-PRIVACY-CONTRACT.md`'s "Encryption fields (resolved)"; only `GET /personal/records/{id}` returns a decrypted value.

## Task 3 status

`GET /personal/records` gains two optional query parameters, `effective_from`/`effective_until` (inclusive on both bounds, either or both may be supplied) -- generic on the endpoint, not `travel`-specific, matching this activation's existing "a caller names a `domain_key`, never a domain-specific code path" convention. `travel` (`standard` classification) is the first domain with a real reason to use them: its own `trip` `record_type` convention (`DATA-MODEL.md`'s Task 3 section) sets `effective_at` to the trip's start date, so a caller can list trips within a date range.

## Task 5 status (part 1 of 2)

Ships `GET|POST /personal/grants`, `POST /personal/grants/{id}/revoke` (`ecc.domains.personal.grants`) -- the `cross_domain_grants` schema and grant lifecycle only, per the repository-owner-directed split (`DATA-MODEL.md`'s own Task 5 section). `POST /personal/grants` requires `source_domain_key` already be an enabled domain; `GET /personal/grants` supports optional `source_domain_key`/`active_only` query filters; revoke is idempotent. No route in this part reads or consumes a grant -- the `trend`/`correlation` AI-generated insight kinds that will are a separate, later PR (part 2).
