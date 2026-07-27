# Phase 6 Engineering Workspace Design

**Status of this document:** planning artifact only. It does not by itself change `docs/phases/PHASE-006-engineering-workspace.md`'s status, and does not close Phase 5's own open exit gate (`docs/runbooks/PHASE-5-DOGFOOD.md`'s staged dogfood record, 0 of 14 required days logged as of this writing). Per `docs/ROADMAP.md`'s approval gates, Phase 6 implementation may not begin until this document's decisions are reviewed and accepted by the repository owner and `docs/phases/PHASE-006-engineering-workspace.md`'s contracts move from Draft to Approved for Implementation. This document resolves the three approval-gate items `docs/phases/PHASE-REVIEW.md:137` names for Phase 6 ("Provider scopes and retention; connector release set; metric definitions and source-coverage thresholds"). The repository owner's request to begin Phase 6 implementation now is treated as the same kind of parallel-start exception every prior phase (2, 3, 4, 5) received to begin work before its predecessor's exit gates closed -- it does not itself close Phase 5's dogfood gate.

## Outcome

Design and begin the first slice of Phase 6: a connector platform abstraction (authorize/validate/backfill/incremental-sync/webhook/permission-refresh/disconnect) that production GitHub, GitLab and Jira adapters implement against, encrypted-at-rest OAuth/token storage, normalized engineering projections, and delivery/reliability metrics with disclosed definitions and coverage. This document resolves the phase's three named decision gates and lays out the task sequence; it does not implement every task in one slice -- **Task 1 (connector framework and source projections)** is this activation's engineering-delivery scope, matching `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`'s task table. Later tasks (real GitHub/GitLab/Jira API calls, delivery/reliability metric computation, decision/incident linking, approved write actions, frontend) are queued, not attempted here.

## Why this isn't a green field

- `docs/phases/PHASE-REVIEW.md` finding F-03 already draws the Phase 5/6 connector boundary: "Phase 5 defines connector-independent action interfaces and validates with local/fake adapters. Phase 6 owns production GitHub/GitLab/Jira adapters; every write uses Phase 5 approval semantics." Phase 6's own write actions must therefore be registered as `ecc.domains.automation.adapters.ActionAdapter`s and dispatched through the existing `worker.py` gate -- this document does not invent a second authority mechanism.
- Finding F-04 ("Engineering intelligence and multi-user phases needed an explicit prohibition on person scoring") and the cross-phase invariant "Work and system risk may be ranked; people may not be scored" bind every metric decision below: no per-engineer composite score, ranking, or activity leaderboard is designed here, matching `DELIVERY-INTELLIGENCE-CONTRACT.md`'s existing text verbatim.
- Phase 2's `resolution_candidates`/`entity_operations.merge_entities` machinery (`backend/ecc/domains/knowledge/resolution.py`, `entity_operations.py`) is the established idiom for "an ambiguous external identity (a GitHub username, a Jira reporter) becomes a `Person` entity link" -- this design reuses it rather than building a second resolution mechanism, per `PHASE-006-engineering-workspace.md`'s own "Phase 2 identity linking" scope line.
- No connector/OAuth/webhook/token-encryption precedent exists anywhere in this codebase yet (confirmed by direct repo survey before writing this document) -- the mechanism below is new, not an extension of prior-phase code, unlike most of Phase 6's other dependencies.
- Phase 5's `AdapterRegistry`/`ActionAdapter` Protocol precedent (structural typing, `@runtime_checkable`, per-test private registries, one shared production `registry` instance) is reused in kind for this phase's own `ConnectorAdapter` Protocol/`ConnectorRegistry` -- a second registry, because connectors and action-adapters are different concerns (a connector owns read sync/lifecycle; an action-adapter owns one write), not a shared one.

## Decision 1 (approval gate): connector release set

**GitHub ships first, as the reference adapter; GitLab and Jira are explicitly sequenced next within this phase, not descoped.** All three remain in `PHASE-006-engineering-workspace.md`'s scope -- this decision only orders delivery, per the phase's own acceptance criterion ("All three read connectors pass the common contract or are explicitly descoped before approval"): GitHub is not a substitute for the other two, it is the first one built against the shared `ConnectorAdapter` contract so GitLab/Jira can follow the same shape with less design risk. Task sequence (see `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`):

1. Connector framework and source projections (this activation) -- the `ConnectorAdapter` Protocol, registry, encrypted token storage, connector lifecycle API, and one deliberately-fake sandbox adapter exercising the full contract shape (mirrors Phase 5 Decision 8's `fake.external_action`, precedent above) -- no real network call to any provider yet.
2. GitHub read sync (backfill + incremental + webhook) against the real GitHub REST/webhook API.
3. GitLab read sync, same contract.
4. Jira work-item sync, same contract.
5. Delivery and reliability metrics (Decision 3 below).
6. Decisions, incidents and knowledge linking.
7. Approved write actions (registered as Phase 5 `ActionAdapter`s).
8. Executive UX and browser acceptance.

A provider that cannot pass the common contract by its scheduled task is explicitly descoped at that point, in the implementation-status document, rather than silently dropped.

## Decision 2 (approval gate): provider scopes and retention

**Scopes -- least privilege, read-only for this phase's default write posture.** GitHub: fine-grained personal-access-token or GitHub App scopes limited to `contents:read`, `metadata:read`, `pull_requests:read`, `issues:read`; write scopes (`contents:write`, `pull_requests:write` for an approved Phase 5-gated action) are requested only when a specific approved write action needs them, never bundled into the default read connection. GitLab: `read_api` (or `read_repository`+`read_api` combination) for read sync; `api` only for an approved write action. Jira: `read:jira-work` for read sync; `write:jira-work` only for an approved write action. Every scope actually granted is recorded on the `connector_accounts` row and surfaced by `GET /engineering/connectors` (never the token itself) so an operator can see exactly what a connection can do.

**Token storage.** OAuth/PAT credentials are encrypted at rest with a dedicated, distinct-from-the-session-secret application key (`ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY`, Fernet/AES-128-CBC+HMAC via the `cryptography` package -- new dependency, RFC-005 amendment below) before being written to `connector_accounts.encrypted_credentials`; the decrypted value is held only in memory for the duration of an authorized sync/API call and is never logged, returned by any API response, or included in an audit/outbox payload. Connector creation and status endpoints return authorization state and granted scopes, never the credential.

**Retention.** No raw provider payload is retained beyond what a normalized projection needs (`CONNECTOR-CONTRACT.md`'s "Connector payloads are untrusted"; `PHASE-006-engineering-workspace.md`'s out-of-scope line, "unrestricted raw provider data retention"). Every projection row stores a `content_hash` (for dedupe/change detection) instead of a raw payload blob. On disconnect: credentials are revoked at the provider when the provider's API supports revocation, future sync stops immediately, and previously-synced projections are retained with `freshness_state = 'disconnected'` by default (visible, not deleted, matching `CONNECTOR-CONTRACT.md`: "locally retained records follow configured retention") until an operator explicitly purges them via a separate, disclosed delete action -- disconnect and delete are two distinct, separately confirmed operations, never combined into one.

## Decision 3 (approval gate): metric definitions and source-coverage thresholds

**Adopts `DELIVERY-INTELLIGENCE-CONTRACT.md`'s seven approved metrics verbatim** (delivery frequency, lead time for changes, change failure rate, time to restore, work ageing, blocked work, review latency) -- this decision makes each one concrete, not additive:

| Metric | Population | Window | Numerator / denominator |
|---|---|---|---|
| Delivery frequency | Deployments to a tracked service | Rolling 7/30/90-day, selectable | Count of successful deployments / window length |
| Lead time for changes | Changes merged in window with a linked deployment | Rolling 30-day | Median (commit-to-merge) + (merge-to-deploy) duration |
| Change failure rate | Deployments in window | Rolling 30-day | Deployments followed by a linked incident within 24h / total deployments |
| Time to restore | Incidents resolved in window | Rolling 30-day | Median (detected-at to resolved-at) duration |
| Work ageing | Open work items | Point-in-time snapshot | Distribution of (now - created-at) for still-open items, bucketed |
| Blocked work | Open work items flagged blocked | Point-in-time snapshot | Count and median age of items in a blocked state |
| Review latency | Merged changes with at least one review | Rolling 30-day | Median (review-requested-at to first-review-at) duration |

Every snapshot stores `metric_definition_id`, `version`, `window`, `numerator`, `denominator`, `population` and `coverage` (`DATA-MODEL.md`'s existing field list, made concrete here) -- a later redefinition creates a new `metric_definition_id`/`version` pair and never rewrites a historical snapshot, per `DELIVERY-INTELLIGENCE-CONTRACT.md`.

**Source-coverage threshold.** A metric is presented as `complete` coverage only when at least 95% of its population's source records have synced within their connector's freshness SLO (backfill fully complete, and incremental lag under the phase's own five-minute NFR) at snapshot time; below that, it is presented as `partial` with the exact coverage percentage and the specific gap (e.g. "GitLab backfill 62% complete") displayed alongside the number, never silently included in an aggregate as if it were complete (`PHASE-006-engineering-workspace.md`: "Partial coverage is visible and never presented as complete"). No metric is computed or displayed below 50% coverage at all -- it is shown as `insufficient_coverage` with zero numeric value, to avoid a number that would mislead more than an explicit gap would.

**No individual-engineer aggregation.** Every metric above aggregates at system/service, team, or workstream level only (`aggregation_scope` on the metric definition is a closed enum of exactly those three values) -- there is no `person_id`-scoped variant of any metric in this schema, no leaderboard endpoint, and no route or field anywhere in `phase-006/API-SCHEMAS.md` that returns a ranked list of people. This is a hard schema-level constraint, not a UI-layer omission a later task could quietly work around.

## Task 1 scope for this activation

Per the sequence above, this activation delivers:

- `connector_accounts`, `sync_cursors` and `sync_runs` -- the connector-platform tables this task's own scope needs, workspace-scoped with the same FK/audit conventions every prior-phase table uses. The remaining `DATA-MODEL.md` projection tables (`repositories`, `engineering_work_items`, `changes`, `reviews`, `deployments`, `incidents`, `engineering_decisions`, `service_links`, `delivery_metric_snapshots`, `source_tombstones`) are added by the task that first populates them (Tasks 2-6 below) rather than all upfront in one migration -- matching Phase 5 Task 1's own precedent (`workflow_definitions`/`workflow_versions`/`automation_policies`/`triggers` only; `workflow_runs` waited for Task 2's migration `0039_phase5_workflow_runs.py`), not a divergence from it.
- `ecc.domains.engineering.connectors.ConnectorAdapter` (a `typing.Protocol`, mirroring `ecc.domains.automation.adapters.ActionAdapter`'s shape) declaring `authorize`, `backfill`, `incremental_sync`, `handle_webhook`, `refresh_permissions`, `disconnect` -- six methods, not seven: "validate" is deliberately folded into `authorize`'s own return value rather than a distinct method, since no adapter in this task's scope has a post-authorization revalidation step from a fresh credential that `authorize` could not already perform (see `connectors.py`'s own module docstring). Plus `ConnectorRegistry`.
- `ecc.domains.engineering.crypto` -- Fernet-based encrypt/decrypt helpers over `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY`.
- `ecc.domains.engineering.sandbox_adapter` -- one deliberately-fake, in-memory `sandbox.github` adapter (no network call), exercising the full `ConnectorAdapter` contract shape end to end, mirroring Phase 5's `fake.external_action` precedent (Decision 8 there; this document's own "why this isn't a green field" section above).
- `ecc.domains.engineering.connector_accounts` -- `GET|POST /api/v1/engineering/connectors`, `POST .../{id}/sync`, `POST .../{id}/disable`, `GET /api/v1/engineering/sync-runs`, per `API-SCHEMAS.md`. Secrets never appear in a response body.
- Cross-workspace isolation, encrypted-credential-never-returned, and sandbox-adapter backfill/incremental/disconnect coverage tests.

Real GitHub API calls, delivery/reliability metric computation, and every other later task remain out of this activation's scope, tracked as "Not started" in `docs/phases/phase-006/IMPLEMENTATION-STATUS.md` until their own task lands.
