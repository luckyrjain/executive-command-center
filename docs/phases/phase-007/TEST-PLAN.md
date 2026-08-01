---
id: PHASE-007-TEST-PLAN
title: Phase 7 Test Plan
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Phase 7 Test Plan

Test domain opt-in/out, records, goals, routines, consent scopes/expiry/revocation, cross-domain denial, insight evidence, export and deletion propagation. Adversarial cases cover diagnosis, guaranteed returns, sensitive inference, coercive wording and prompt injection.

Verify encryption, local-only policy, remote egress denial, audit redaction, backups and workspace isolation. Browser acceptance enables a domain, captures data, grants/revokes access, inspects/dismisses an insight, exports and deletes.

## Task 1 status

Covers: `habits` domain opt-in/out, disabled-domain-blocks-all-access, cross-workspace/cross-user isolation, encrypted-field-never-returned-in-list-view, export completeness and deletion propagation, and the always-in-scope adversarial fixtures (no diagnostic/scoring language leaking even from a `standard` domain). Cross-domain denial, remote-egress denial and the `health`/`finance` adversarial rubric are exercised by the tasks that introduce a second domain, an AI-generated insight, and the `high_stakes` tier respectively -- not yet reachable in Task 1's own scope (zero AI calls, one domain).

## Task 2 status

Covers: `learning` (`standard`) domain enable/disable/list, `course`/`resource` record CRUD, whole-domain export/deletion, and confirms enabling `learning` does not leak into or implicitly enable `habits` (`tests/test_personal_learning_postgres.py`).

## Task 3 status

Covers: `travel` (`standard`) domain enable/isolation, `trip` record CRUD, and the new `effective_from`/`effective_until` date-range query filter on `GET /personal/records` (a range covering one trip, an inclusive exact-instant boundary match, from-only/until-only bounds, an empty-result range), plus export/deletion (`tests/test_personal_travel_postgres.py`).

## Task 4 status

Covers: `relationships` (`sensitive`) domain enable/isolation, `contact`/`interaction` record CRUD, and the first real field-level-encryption proof -- `notes` genuinely ciphertext in `domain_records.payload` at rest (verified via direct SQL, not just the API response shape), list views redact it to a placeholder, PATCH re-encrypts new content, export returns it decrypted, and an idempotency-record replay never persists or returns a decrypted value from the cached row itself (`tests/test_personal_relationships_postgres.py`).

## Task 5 status (cross-domain grants and AI-generated insights)

Part 1 covers: `cross_domain_grants` create/list/revoke, `active_only`/`source_domain_key` filtering (excluding both revoked and expired grants), revoke idempotency, unknown-grant 404, cross-workspace isolation (`tests/test_personal_grants_postgres.py`). Part 2 covers: `personal.get_insight_sources`' grant/category enforcement (a missing or expired grant on even one requested domain fails the whole call), citation grounding and the conditional `professional_referral_note` check (a fully-grounded pass; ungrounded-citation, prohibited-fact and missing-note failures each isolated to their own floor), the 10-example adversarial safety-rubric fixture set at the required 100%-floor rigor, and the production `POST /personal/insights/generate`/`.../feedback` endpoints (feature-flag-disabled fail-open, missing-grant fail-open, feedback creation leaving `kind` unchanged, unknown-insight 404) (`tests/test_ai_runtime_personal_insight_evaluation_postgres.py`, `tests/test_personal_ai_insights_postgres.py`).

## Task 6 status

Covers: `health` (`high_stakes`) domain enable/isolation, per-record retention-acknowledgement enforcement (rejection with zero rows written when unacknowledged; `habits`, `standard`, unaffected by the requirement), `vital_reading`/`symptom_log` field-level encryption at rest (direct SQL) and list-view redaction, and the first real (non-synthetic) end-to-end proof that `professional_referral_note` is required and enforced against a genuine `high_stakes` grant and cited record (`tests/test_personal_health_postgres.py`).

## Task 7 status

Covers: `finance`, the second and last `high_stakes` domain this phase's scope names -- re-runs Task 6's exact retention-acknowledgement and `professional_referral_note` proof shape against `account`/`transaction` records to confirm the mechanism generalizes rather than being `health`-specific, plus `account_notes`/`memo` field-level encryption at rest (`tests/test_personal_finance_postgres.py`).

## Task 8 status

Covers browser acceptance end to end: enable a domain, capture a record, create and revoke a cross-domain grant (alongside a pre-seeded already-expired grant), inspect a `high_stakes`-sourced insight's `professional_referral_note` boundary and its `missing_data` notice, dismiss it, attempt a live generate-insight call with no active grant and confirm the fail-open response, export a domain's data, and delete it -- with zero serious/critical accessibility violations checked after every tab switch (`frontend/e2e/scenarios/personal-domain-lifecycle.mjs`), plus 35 component-test cases across the five panel components (`frontend/src/features/personal/*.test.tsx`).

## Review round 1 (post-Task-8, whole-phase deep review)

A six-persona review of the entire merged phase (backend, frontend, tests, migrations, docs) found and closed several test-coverage gaps this document had not yet named: workspace isolation for `personal_insights`/`personal_insight_feedback` (`tests/test_personal_ai_insights_postgres.py`); a PATCH stale-`expected_version` conflict against a real encrypted `relationships` record, confirming the encrypted field survives a rejected write unchanged (`tests/test_personal_relationships_postgres.py`); `personal.get_insight_sources` returning a genuinely `encrypt_record_payload`-encrypted narrative field decrypted, not just the shape of decryption (`tests/test_personal_insight_tools_postgres.py`); and three new frontend tests per panel (`RecordsPanel`/`GrantsPanel`/`ExportDeletePanel`) proving a stale in-flight mutation resolving after the user has since switched domains does not clear or overwrite the newly-selected domain's own in-progress state -- the `useMutation`/`TanStack Query` rebinding-to-latest-render-closures class of bug this round's own root-cause analysis identified. The same round also closed the "backup/restore of `sensitive`/`high_stakes` records is not yet independently exercised" gap `DOMAIN-PRIVACY-CONTRACT.md` had disclosed since Task 1 -- see that document's own "Backup/restore confirmation" paragraph.
