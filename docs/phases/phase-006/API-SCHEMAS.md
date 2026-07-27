---
id: PHASE-006-API-SCHEMAS
title: Phase 6 Engineering Workspace API
status: Approved for Implementation
version: 0.6.1
owner: Lucky Jain
---

# Phase 6 API Schemas

```text
GET|POST /engineering/connectors
POST /engineering/connectors/{id}/sync|disable
GET /engineering/sync-runs
GET /engineering/overview
GET /engineering/repositories
GET /engineering/work-items
GET /engineering/changes
GET /engineering/deployments
GET|POST /engineering/incidents
POST /engineering/incidents/{id}/resolve
GET|POST /engineering/decisions
POST /engineering/decisions/{id}/decide
GET /engineering/metrics
```

**Task 1 status**: the first three routes above (`/engineering/connectors`, `/engineering/connectors/{id}/sync|disable`, `/engineering/sync-runs`) are implemented (`ecc.domains.engineering.connector_accounts`). The remaining routes do not exist yet -- each is added by the task that first needs it (`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`, Tasks 2-6).

**Task 5 status**: `GET /engineering/metrics` is implemented (`get_metrics_endpoint`). It is a deliberate, disclosed departure from pure REST `GET` semantics -- this phase has no periodic computation scheduler yet, so the `GET` call is itself the trigger, computing and persisting a fresh, immutable `delivery_metric_snapshots` row per metric on every call rather than only reading already-stored ones (mirroring `POST /connectors/{id}/sync`'s own "manual trigger only" reality since Task 1); see that endpoint's own docstring and `DELIVERY-INTELLIGENCE-CONTRACT.md`'s matching "Accepted limitation" section. `GET /engineering/repositories`/`/work-items`/`/changes`/`/deployments`, `GET /engineering/overview` remain unimplemented query surfaces -- this task's own scope was the sync/computation layer, not a general query API; see `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`'s Task 5 section.

**Task 6 status**: `GET|POST /engineering/incidents`, `POST /engineering/incidents/{id}/resolve`, `GET|POST /engineering/decisions` and `POST /engineering/decisions/{id}/decide` are all implemented (`ecc.domains.engineering.decisions_incidents`). `POST /engineering/incidents` is a real addition beyond this doc's original route sketch above (which named only the `GET`) -- no incident-management provider connector exists in this phase's scope (GitHub/GitLab/Jira are not incident-management tools), so manual capture is the only feasible source for `time_to_restore`, the same kind of disclosed real-addition-beyond-the-sketch `GET /engineering/metrics` itself already set a precedent for in Task 5. Both `incidents` and `engineering_decisions` are workspace-authored records correlated to `changes` only (via `incident_changes`/`decision_changes`); correlation to `deployments` or work items, and wiring their raw provider-identifier columns into Phase 2's real identity-resolution machinery, are both explicitly deferred -- see `backend/ecc/domains/engineering/decisions_incidents.py`'s own module docstring and migration `0049_phase6_decisions_incidents.py`'s. `GET /engineering/repositories`/`/work-items`/`/changes`/`/deployments` and `GET /engineering/overview` remain the only unimplemented query surfaces after this task.

**Task 7 status**: no new route -- this doc's own closing line ("Optional mutations route through approved automation policies") is exactly this task's own shape: three write actions (`github.add_issue_comment`, `gitlab.add_note`, `jira.add_comment`) registered as `ecc.domains.automation.adapters.ActionAdapter`s (`ecc.domains.engineering.write_actions`), reachable only through a workflow's own `action_ref` and Phase 5's existing `POST /automations/workflows`/`POST /automations/runs`/`POST /automations/approvals/{id}/approve` surface -- no second authority mechanism, no new HTTP endpoint. See that module's own docstring for the scope (one action concept -- add a comment to an existing issue/PR/MR -- across all three providers), containment, and retry-safety reasoning.

**Task 8 status**: `GET /engineering/repositories` and `GET /engineering/work-items` are now implemented (`ecc.domains.engineering.connector_accounts.list_repositories_endpoint`/`list_work_items_endpoint`) -- real, disclosed additions beyond this task's own plan-doc scope ("Executive UX and browser acceptance"), matching the identical "add the query endpoint the UX genuinely needs" precedent Tasks 5-7 each already set once. Neither table lacked data (`repositories` since Task 2, `engineering_work_items` since Task 4) -- only a query surface, which the Repositories and Source Coverage frontend views have nothing to read without. `GET /engineering/changes`, `GET /engineering/deployments`, and `GET /engineering/overview` remain unimplemented query surfaces: `changes`/`deployments` have no frontend consumer in this task's own eight required views (Repository/Incident/Decision detail views reference `change_ids` as opaque UUIDs, never a `changes` list), and "overview" is served entirely client-side by composing the connectors/incidents/decisions/metrics responses already returned by existing endpoints, needing no dedicated aggregate route of its own.

Connector creation returns required scopes and authorization state, never token values. Queries expose source coverage, freshness, definitions and evidence. Optional mutations route through approved automation policies. Signed cursors, isolation, redaction, idempotency and concurrency rules apply.
