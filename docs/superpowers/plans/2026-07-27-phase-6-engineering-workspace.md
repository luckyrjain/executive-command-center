# Phase 6 Engineering Workspace Implementation Plan

Companion to `docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md` (the decisions) -- this document is the task sequence and per-task scope, mirroring the shape of `docs/superpowers/plans/2026-07-25-phase-5-automation-design.md`'s own task breakdown.

## Task 1 — Connector framework and source projections (this activation)

- Migration: `connector_accounts`, `sync_cursors`, `sync_runs` (`docs/phases/phase-006/DATA-MODEL.md`). The remaining projection tables (`repositories`, `engineering_work_items`, `changes`, `reviews`, `deployments`, `incidents`, `engineering_decisions`, `service_links`, `delivery_metric_snapshots`, `source_tombstones`) are added by Tasks 2-6 below, the task that first populates each one -- matching Phase 5 Task 1's identical precedent (`workflow_runs` wasn't created until Task 2's own migration).
- `ecc.domains.engineering.connectors`: `ConnectorAdapter` Protocol (`authorize`/`backfill`/`incremental_sync`/`handle_webhook`/`refresh_permissions`/`disconnect` -- "validate" is folded into `authorize`'s return value, not a separate method) and `ConnectorRegistry`, mirroring `ecc.domains.automation.adapters.ActionAdapter`'s structural-typing shape.
- `ecc.domains.engineering.crypto`: Fernet-based `encrypt_credential`/`decrypt_credential` over `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY`.
- `ecc.domains.engineering.sandbox_adapter`: one deliberately-fake `sandbox.github` adapter (no network call) registered into the shared production registry, exercising the full contract shape end to end.
- `ecc.domains.engineering.connector_accounts`: `GET|POST /api/v1/engineering/connectors`, `POST .../{id}/sync`, `POST .../{id}/disable`, `GET /api/v1/engineering/sync-runs`.
- Tests: workspace isolation, credential-never-returned, backfill/incremental/disconnect lifecycle against the sandbox adapter, encrypted-storage round-trip.

## Task 2 — GitHub read sync

**Repositories complete.** Real GitHub REST API backfill/incremental sync through the same `ConnectorAdapter` contract Task 1 defines (`github_adapter.GitHubAdapter`, migration `0045_phase6_repositories.py`). Task 1's disclosed pool-exhaustion risk is resolved (`sync_connector_endpoint`/`create_connector_endpoint` restructured into phases across separate pooled connections, with a partial-unique-index-backed guard (`uq_sync_runs_running_per_account`, migration `0046`) serializing concurrent syncs per account after review found the initial restructuring alone reintroduced idempotency/cursor races -- see `connector_accounts.py`'s own module docstring).

**Deferred to a Task 2 follow-up, not yet done:** work items (issues), changes (commits/PRs), reviews, deployments (`DATA-MODEL.md`'s remaining projection tables); webhook ingestion has no receiving HTTP endpoint or webhook-secret storage yet (`GitHubAdapter.handle_webhook` implements the parsing/upsert logic but is not wired to a public route -- see that module's own docstring for why).

## Task 3 — GitLab read sync

**Complete.** Real GitLab REST API backfill/incremental sync through the same `ConnectorAdapter` contract Task 2 implements for GitHub (`gitlab_adapter.GitLabAdapter`) -- no new migration, since `repositories` (migration `0045_phase6_repositories.py`) already names `gitlab` in its provider CHECK constraint. Applies every lesson Task 2's own review rounds found from the first version: real scope checking (`GET /personal_access_tokens/self` always returns actual granted scopes, no fabricated-fallback risk), a still-rate-limited retry degrading to `partial` rather than raising, the `_MAX_PAGES_PER_CALL` bound reporting `partial` rather than a silent `succeeded`, and `response.links` for `Link`-header pagination. Genuine (not no-op) best-effort `disconnect()` via `DELETE /personal_access_tokens/self`, realistically expected to fail at this connector's own default read-only scopes. Same deferrals as Task 2: work items/changes/reviews/deployments and webhook ingestion's receiving endpoint.

## Task 4 — Jira work-item sync

Same contract, Jira REST/webhook API; work items only (Jira is not a source-control provider).

## Task 5 — Delivery and reliability metrics

Implements the seven metric definitions and windows from the design doc's Decision 3 table, snapshot computation, and the coverage-threshold policy (95% complete / 50% insufficient).

## Task 6 — Decisions, incidents and knowledge linking

`engineering_decisions` capture, incident correlation to changes/deployments, and ambiguous-identity resolution raised against Phase 2's existing `resolution_candidates`/`merge_entities` endpoints (design doc's "why this isn't a green field" section) rather than a second resolution mechanism.

## Task 7 — Approved write actions

GitHub/GitLab/Jira write actions registered as `ecc.domains.automation.adapters.ActionAdapter`s, dispatched through the existing Phase 5 `worker.py` gate under an `automation_policies` row -- no second authority mechanism.

## Task 8 — Executive UX and browser acceptance

Engineering Overview, Delivery, Reliability, Repository, Incident, Decision, Connector Health and Coverage frontend views per `UX-STATES.md`, plus Playwright acceptance coverage of the required degraded states (first sync, backfill, partial permissions, stale connector, rate limited, disconnected, provider unavailable, conflicting identities).
