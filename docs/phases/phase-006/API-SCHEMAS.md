---
id: PHASE-006-API-SCHEMAS
title: Phase 6 Engineering Workspace API
status: Approved for Implementation
version: 0.10.0
owner: Lucky Jain
updated: 2026-10-01
---

# Phase 6 API Schemas

```text
GET|POST /engineering/connectors
POST /engineering/connectors/{id}/sync|disable
GET /engineering/sync-runs
GET /engineering/overview
GET /engineering/repositories
POST /engineering/repositories/{id}/team
GET /engineering/work-items
POST /engineering/work-items/{id}/team
GET /engineering/team-suggestions
POST /engineering/team-suggestions/confirm
POST /engineering/team-suggestions/dismiss
GET /engineering/changes
GET /engineering/deployments
GET|POST /engineering/incidents
POST /engineering/incidents/{id}/resolve
GET|POST /engineering/decisions
POST /engineering/decisions/{id}/decide
GET /engineering/metrics
GET /engineering/monitors
GET /engineering/service-definitions
GET /engineering/dashboards
```

**Task 1 status**: the first three routes above (`/engineering/connectors`, `/engineering/connectors/{id}/sync|disable`, `/engineering/sync-runs`) are implemented (`ecc.domains.engineering.connector_accounts`). The remaining routes do not exist yet -- each is added by the task that first needs it (`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`, Tasks 2-6).

**Task 5 status**: `GET /engineering/metrics` is implemented (`get_metrics_endpoint`). It is a deliberate, disclosed departure from pure REST `GET` semantics -- this phase has no periodic computation scheduler yet, so the `GET` call is itself the trigger, computing and persisting a fresh, immutable `delivery_metric_snapshots` row per metric on every call rather than only reading already-stored ones (mirroring `POST /connectors/{id}/sync`'s own "manual trigger only" reality since Task 1); see that endpoint's own docstring and `DELIVERY-INTELLIGENCE-CONTRACT.md`'s matching "Accepted limitation" section. `GET /engineering/repositories`/`/work-items`/`/changes`/`/deployments`, `GET /engineering/overview` remain unimplemented query surfaces -- this task's own scope was the sync/computation layer, not a general query API; see `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`'s Task 5 section.

**Task 6 status**: `GET|POST /engineering/incidents`, `POST /engineering/incidents/{id}/resolve`, `GET|POST /engineering/decisions` and `POST /engineering/decisions/{id}/decide` are all implemented (`ecc.domains.engineering.decisions_incidents`). `POST /engineering/incidents` is a real addition beyond this doc's original route sketch above (which named only the `GET`) -- no incident-management provider connector exists in this phase's scope (GitHub/GitLab/Jira are not incident-management tools), so manual capture is the only feasible source for `time_to_restore`, the same kind of disclosed real-addition-beyond-the-sketch `GET /engineering/metrics` itself already set a precedent for in Task 5. Both `incidents` and `engineering_decisions` are workspace-authored records correlated to `changes` only (via `incident_changes`/`decision_changes`); correlation to `deployments` or work items, and wiring their raw provider-identifier columns into Phase 2's real identity-resolution machinery, are both explicitly deferred -- see `backend/ecc/domains/engineering/decisions_incidents.py`'s own module docstring and migration `0049_phase6_decisions_incidents.py`'s. `GET /engineering/repositories`/`/work-items`/`/changes`/`/deployments` and `GET /engineering/overview` remain the only unimplemented query surfaces after this task. Both mutating endpoints reject a temporally-inverted timestamp: `POST .../incidents/{id}/resolve` 422s `RESOLVED_AT_BEFORE_DETECTED_AT` when `resolved_at < detected_at`, and `POST .../decisions/{id}/decide` 422s `DECIDED_AT_BEFORE_CREATED_AT` when `decided_at < created_at` (added by a whole-phase review as `resolve`'s mirror; see `IMPLEMENTATION-STATUS.md`'s matching review section) -- undocumented here until now, a gap the same review round found.

**Task 7 status**: no new route -- this doc's own closing line ("Optional mutations route through approved automation policies") is exactly this task's own shape: three write actions (`github.add_issue_comment`, `gitlab.add_note`, `jira.add_comment`) registered as `ecc.domains.automation.adapters.ActionAdapter`s (`ecc.domains.engineering.write_actions`), reachable only through a workflow's own `action_ref` and Phase 5's existing `POST /automations/workflows`/`POST /automations/runs`/`POST /automations/approvals/{id}/approve` surface -- no second authority mechanism, no new HTTP endpoint. See that module's own docstring for the scope (one action concept -- add a comment to an existing issue/PR/MR -- across all three providers), containment, and retry-safety reasoning.

**Task 8 status**: `GET /engineering/repositories` and `GET /engineering/work-items` are now implemented (`ecc.domains.engineering.connector_accounts.list_repositories_endpoint`/`list_work_items_endpoint`) -- real, disclosed additions beyond this task's own plan-doc scope ("Executive UX and browser acceptance"), matching the identical "add the query endpoint the UX genuinely needs" precedent Tasks 5-7 each already set once. Neither table lacked data (`repositories` since Task 2, `engineering_work_items` since Task 4) -- only a query surface, which the Repositories and Source Coverage frontend views have nothing to read without. `GET /engineering/changes`, `GET /engineering/deployments`, and `GET /engineering/overview` remain unimplemented query surfaces: `changes`/`deployments` have no frontend consumer in this task's own eight required views (Repository/Incident/Decision detail views reference `change_ids` as opaque UUIDs, never a `changes` list), and "overview" is served entirely client-side by composing the connectors/incidents/decisions/metrics responses already returned by existing endpoints, needing no dedicated aggregate route of its own.

**Team linkage status (migration `0050_phase6_team_linkage.py`)**: `POST /engineering/repositories/{id}/team` and `POST /engineering/work-items/{id}/team` (`assign_repository_team_endpoint`/`assign_work_item_team_endpoint`) are new -- the "human confirms" half of this migration's own hybrid auto-suggest design (see `CONNECTOR-CONTRACT.md`'s "Team linkage status" section for the "auto-suggest" half). Body: `{"expected_version": <int>, "team_entity_id": "<uuid>" | null}` -- the `{id}` path parameter must itself resolve to a real repository/work-item in the caller's own workspace (404 `REPOSITORY_NOT_FOUND`/`WORK_ITEM_NOT_FOUND` otherwise); a non-null `team_entity_id` must reference a real, active, `kind="team"` `pkos_nodes` row in the caller's own workspace (404 `TEAM_ENTITY_NOT_FOUND` / 422 `TEAM_ENTITY_KIND_MISMATCH` otherwise); `null` clears an existing assignment. **Requires `Idempotency-Key`** and checks `expected_version` against `team_assignment_version` (409 `VERSION_CONFLICT` on a stale read) -- the first PR draft of this endpoint had neither, which review correctly flagged as leaving the first human-editable field either table has ever had with none of `update_entity`'s optimistic-concurrency, idempotency, or `audit_events`/`event_outbox` discipline every other mutating endpoint in this router has. Both `GET /engineering/repositories` and `GET /engineering/work-items` also gained an optional `team_entity_id` query filter, and both response bodies gained `team_entity_id`/`suggested_team_name`/`team_assignment_version`/`team_assignment_updated_by` fields.

**Datadog connector status (migration `0051_phase6_datadog_connector.py`)**: `GET /engineering/monitors`, `GET /engineering/service-definitions` and `GET /engineering/dashboards` are new (`list_monitors_endpoint`/`list_service_definitions_endpoint`/`list_dashboards_endpoint`) -- read-only, mirroring `GET /engineering/repositories`'s own shape exactly: workspace-scoped, optional `connector_account_id` and `team_entity_id` query filters, no pagination. Each response body includes `team_entity_id`/`suggested_team_name` (read-only on these three routes) but **no `team_assignment_version`/`team_assignment_updated_by`** -- unlike `repositories`/`work-items`, no `POST .../team` confirm endpoint exists yet for monitors/service definitions/dashboards; see `CONNECTOR-CONTRACT.md`'s "Datadog connector status" and "Team linkage status" sections for why writing a confirmed link for these three resource types is deliberately deferred to its own follow-up task, not an oversight of this one.

**Team suggestions review page status (migration `0072_team_suggestion_dismissal.py`, later addition)**: `GET /engineering/team-suggestions`, `POST /engineering/team-suggestions/confirm`, `POST /engineering/team-suggestions/dismiss` are new -- a grouped, bulk-action sibling of the per-item `POST .../{id}/team` endpoints above, not a replacement (see `CONNECTOR-CONTRACT.md`'s matching "Team suggestions review page" section for why). `GET` returns `{"items": [{"suggested_team_name", "repository_count", "work_item_count", "sample_items": [{"id", "resource_type": "repository"|"work_item", "name"}]}]}`, one entry per distinct pending `suggested_team_name` across `repositories`/`engineering_work_items` combined (dismissed and already-confirmed rows excluded). `POST .../confirm` body: `{"suggested_team_name": "<str>", "team_entity_id": "<uuid>"}`; `POST .../dismiss` body: `{"suggested_team_name": "<str>"}`. Both mutations respond `{"updated": ["<uuid>", ...], "skipped_unauthorized": ["<uuid>", ...]}` -- every currently-eligible row sharing that suggested name is locked and re-authorized individually for `action="write"` inside one transaction; a row the caller cannot write to is silently excluded from `updated` and reported in `skipped_unauthorized` instead of failing the whole batch. **Requires `Idempotency-Key`**, matching every other mutating endpoint in this router; no `expected_version` -- a set-based confirm has no single row version to check against, unlike the per-item endpoint.

Connector creation returns required scopes and authorization state, never token values. Queries expose source coverage, freshness, definitions and evidence. Optional mutations route through approved automation policies. Signed cursors, isolation, redaction, idempotency and concurrency rules apply.

## Security remediation (Spec A) status codes

Codes added or changed on the connector routes by security remediation Spec A (raise sites in `ecc.domains.engineering.connector_accounts`). "Flag" means `ECC_PERSONAL_DATA_ISOLATION`; the rest is unflagged. Gmail-specific detail: `docs/phases/phase-010/API-SCHEMAS.md`; rollout: `docs/runbooks/SPEC-A-ROLLOUT.md`.

| Route | Status / code | When |
|---|---|---|
| `POST /engineering/connectors` | `404 CONNECTOR_NOT_FOUND` | The request would reactivate an existing `disconnected` row the caller cannot read (S1.7). Audited as `connector_account.enrollment_refused` (reason `not_found`), counted in `ecc_connector_enrollment_refused_total` |
| `POST /engineering/connectors` | `403 INSUFFICIENT_ROLE` | Reactivation of a row the caller can read but not write (reason `access_denied`, audited and counted the same way); also any caller demoted below write while waiting for the membership lock (ADR-0014, not audited as a refusal) |
| `POST /engineering/connectors` | `500 CONNECTOR_ACCOUNT_PERSIST_FAILED` | An `IntegrityError` other than the duplicate-account unique violation (`uq_connector_accounts_workspace_provider_external_id`); previously reported as a `409` duplicate. Only SQLSTATE and constraint name are logged. The pre-existing `409 CONNECTOR_ALREADY_CONNECTED` for an active row is unchanged |
| `POST /engineering/connectors/{id}/sync`, `.../disable` | `404 CONNECTOR_NOT_FOUND` | Flag on, a personal (`gmail`) connector, and the caller is not its owner, whatever their role or grants; counted in `ecc_connector_access_denied_total{provider,route}`. `sync`: checked before any transaction (`_deny_non_owner_personal_connector`) and again on the row locked in phase 1 (`_locked_connector_denial`). `disable`: one check, on the locked row, ahead of the Gmail `409 GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT` -- so a non-owner's Gmail `disable` gets `404` where it used to get that `409` |
| `POST /engineering/connectors/{id}/sync` | `403 MEMBERSHIP_INACTIVE` | The acting member, or the personal connector's owner, is no longer an active workspace member (re-checked under the shared membership lock in phases 1 and 3). A phase-1 skip happens before any `sync_runs` row is reserved, so it writes nothing; a phase-3 skip closes the already-reserved run `failed`. Either way no outcome, cursor or audit is written |
| `POST /engineering/connectors/{id}/sync` | `403 EMAIL_CONSENT_NOT_ACTIVE` | Gmail only: the mailbox owner's `email` consent is not active, or the connector was disconnected, at any point during the sync (FX5). Nothing is written, except that a run already reserved is closed `failed` |
| `POST /engineering/connectors/{id}/sync`, `.../disable` | `403 INSUFFICIENT_ROLE` | Read but not write on the (locked) row, as before; also a role lowered while the request waited for the membership lock (ADR-0014) |

`409 MEMBERSHIP_CHANGE_BUSY` is **not** returned by these routes: it is the member-removal / role-change response when such a change gives up waiting (3 s) for writers holding the membership lock, for example a sync refreshing an OAuth token. A connector write that queues behind a waiting removal can instead hit the 5 s statement timeout and return a generic `500` (safe to retry).

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 0.10.0 | 2026-10-01 | Security remediation Spec A (T19; review wording: phase-1 vs phase-3 skip, sync pre-check vs disable locked-row check): documented the connector routes' new codes -- reactivation `404`/`403`, `500 CONNECTOR_ACCOUNT_PERSIST_FAILED`, non-owner Gmail `sync`/`disable` `404`, sync `403 MEMBERSHIP_INACTIVE` / `EMAIL_CONSENT_NOT_ACTIVE`, the ADR-0014 role re-check -- and that `MEMBERSHIP_CHANGE_BUSY` does not apply here | Lucky Jain |
