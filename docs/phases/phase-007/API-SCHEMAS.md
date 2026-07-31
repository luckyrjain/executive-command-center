---
id: PHASE-007-API-SCHEMAS
title: Phase 7 Personal Intelligence API
status: Approved for Implementation
version: 0.5.0
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
POST /personal/insights/generate
```

Requests declare domain and purpose. APIs enforce consent and field policy server-side. Insight responses include kind, evidence, confidence, limitations, freshness and policy version. Health/finance suggestions never use diagnostic or guaranteed-return language.

## Task 1 status

Ships every route above except `/personal/insights/{id}/feedback` (deferred with the AI-generated `trend`/`correlation` insight kinds it applies to -- `docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, Decision 1 item 5/Decision 4). List/summary responses for `sensitive`/`high_stakes` records redact encrypted fields per `DOMAIN-PRIVACY-CONTRACT.md`'s "Encryption fields (resolved)"; only `GET /personal/records/{id}` returns a decrypted value.

## Task 3 status

`GET /personal/records` gains two optional query parameters, `effective_from`/`effective_until` (inclusive on both bounds, either or both may be supplied) -- generic on the endpoint, not `travel`-specific, matching this activation's existing "a caller names a `domain_key`, never a domain-specific code path" convention. `travel` (`standard` classification) is the first domain with a real reason to use them: its own `trip` `record_type` convention (`DATA-MODEL.md`'s Task 3 section) sets `effective_at` to the trip's start date, so a caller can list trips within a date range.

## Task 5 status (part 1 of 2)

Ships `GET|POST /personal/grants`, `POST /personal/grants/{id}/revoke` (`ecc.domains.personal.grants`) -- the `cross_domain_grants` schema and grant lifecycle only, per the repository-owner-directed split (`DATA-MODEL.md`'s own Task 5 section). `POST /personal/grants` requires `source_domain_key` already be an enabled domain; `GET /personal/grants` supports optional `source_domain_key`/`active_only` query filters; revoke is idempotent. No route in this part reads or consumes a grant -- the `trend`/`correlation` AI-generated insight kinds that will are a separate, later PR (part 2).

## Task 5 status (part 2 of 2)

Ships `POST /personal/insights/generate` (`ecc.domains.personal.ai_insights`) -- the route that actually checks a grant before combining domains, gated behind `config.py:personal_ai_insight_generation_enabled` (default `False`). Request: `{source_domain_keys: [DomainKey, ...]}` (non-empty). Response: `{available: bool, insight: InsightResponse | null, error_code: string | null}` -- fail-open, matching `POST /meetings/{id}/prep`'s own enrichment response shape: a non-`completed` run (feature disabled, a requested domain lacking an active grant, no eligible model, a grounding/safety failure, ...) is `available=false` with that run's own `error_code`, never an HTTP error for a well-formed request. Idempotency-Key required, matching every other mutating route in this package.

`POST /personal/insights/{id}/feedback` (`ecc.domains.personal.habits`, the same router `GET /personal/insights`/`POST .../dismiss` already live on) ships here, once a `trend`/`correlation` insight exists for feedback to attach to. Request: `{useful: bool, comment: string | null}`. Response: `{id, insight_id, useful, comment, created_at}` -- deliberately no `kind` field anywhere in this request/response pair, matching `INSIGHT-CONTRACT.md`'s "never rewrites an insight's own `kind`" structurally (`personal_insight_feedback` has no such column to expose).

`InsightResponse` (`GET /personal/insights`'s existing shape) gains one field, `professional_referral_note: string | null` -- `null` for every deterministic insight and for any AI-generated insight whose sources are all `standard`/`sensitive`; non-empty specifically when a source is `high_stakes` (enforced before persistence, not by this response model).
