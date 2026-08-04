# Executive Command Center Roadmap

## Current status

**Foundation:** Phase 0 baseline approved and implemented  
**Current delivery:** [Phase 1 — Executive Dashboard MVP](phases/PHASE-001-executive-dashboard-mvp.md) — engineering delivery complete on `feature/phase-1-production-hardening` (Tasks 1-11 of `superpowers/plans/2026-07-16-phase-1-completion.md`, each independently reviewed with zero Critical or Important findings); Phase 1 exit remains open pending the seven-day daily-use validation gate and human change review — see [Phase 1 Implementation Status](phases/phase-001/IMPLEMENTATION-STATUS.md)  
**Future specifications:** Phase 2 Approved for Implementation and in progress (parallel to Phase 1's open exit gates); Phase 3 Approved for Implementation and beginning (parallel-start exception granted, same as Phase 2's); Phase 4 engineering delivery complete, exit gates open (known-limitation floor misses accepted, parallel-start exception granted, same as Phase 2's and Phase 3's); Phase 5 engineering delivery complete through Task 7 (parallel-start exception granted, same as Phase 2/3/4's), exit gate open pending `docs/runbooks/PHASE-5-DOGFOOD.md`'s real, human-operator-driven staged dogfood record (0 of 14 required days logged); Phase 6 Approved for Implementation, engineering delivery complete through Task 8 (connector framework, GitHub/GitLab read sync, Jira work-item sync, delivery/reliability metrics, decisions/incidents and change-correlation, approved write actions, and the executive UX/browser-acceptance frontend surface -- partial scope disclosed on several tasks; parallel-start exception granted, same as Phase 2/3/4/5's), proceeding alongside Phase 5's own still-open exit gate, entering its own final cross-task review; Phase 7 Approved for Implementation, engineering delivery complete through Task 8 across all six domains plus a whole-phase deep-review-and-fix round (`luckyrjain/executive-command-center#99`, merged, zero open findings on re-review), proceeding alongside Phase 6's own still-open exit gate -- no promotion/exit decision made yet; Phase 8 Approved for Implementation, engineering delivery complete through Task 9 of 9 (account/membership/session framework, invitations, authorization engine, workspace-wide authorization, sharing-review UX, delegation, notifications/shared activity, ownership transfer/member removal, and the executive UX/multi-identity browser-acceptance frontend -- parallel-start exception granted, same as Phase 2/3/4/5/6/7's), plus three whole-phase deep-review-and-fix rounds complete with a fourth in progress, proceeding alongside Phase 7's own not-yet-made exit/promotion decision; Phase 9 published as Draft / Planned

The [canonical phase index](phases/README.md) lists every primary specification and supporting contract. The [Phase 0–9 documentation review](phases/PHASE-REVIEW.md) records completeness findings and approval gates.

## Delivery principles

Every phase must:

- deliver independently usable value
- preserve local-first ownership and deterministic fallback
- compile, migrate, test and remain recoverable
- preserve architecture, privacy and authorization boundaries
- define measurable acceptance and exit criteria before implementation
- identify rollback and deferred scope
- receive explicit approval after all dependency exit gates pass
- avoid implementation outside the approved phase

## Delivery sequence

```text
Phase 0 — Repository Foundation         [Implemented]
  -> Phase 1 — Executive Dashboard MVP [Engineering delivery complete; exit gates open]
  -> Phase 2 — Knowledge Platform      [Approved for Implementation; in progress, parallel to open Phase 1 exit gates]
  -> Phase 3 — Human Attention Engine  [Approved for Implementation; in progress, parallel-start exception granted]
  -> Phase 4 — AI Runtime              [Engineering delivery complete; exit gates open, known limitations accepted]
  -> Phase 5 — Automation              [Engineering delivery complete through Task 7; exit gate open pending staged dogfood]
  -> Phase 6 — Engineering Workspace   [Approved for Implementation; engineering delivery complete through Task 8]
  -> Phase 7 — Personal Intelligence   [Approved for Implementation; engineering delivery complete through Task 8 + review round]
  -> Phase 8 — Multi-user Workspaces   [Approved for Implementation; engineering delivery complete through Task 9 of 9 + three review rounds, fourth in progress]
  -> Phase 9 — Enterprise              [Draft]
```

A later phase may be designed or reviewed early, but implementation begins only after its dependencies satisfy exit criteria and its status is changed to Approved for Implementation.

## Phase 0 — Repository Foundation

**Status:** Approved baseline; implemented.

Primary outcomes:

- reproducible local development and CI
- modular-monolith architecture enforcement
- PostgreSQL persistence and migrations
- authentication, workspace isolation and security baseline
- durable event/outbox foundation
- observability, backup and restore

Specification: [PHASE-000](phases/PHASE-000-repository-foundation.md)

## Phase 1 — Executive Dashboard MVP

**Status:** Approved for Implementation; every capability below is delivered and independently reviewed on `feature/phase-1-production-hardening`. Phase 1 is not yet closed: it exits only after the seven-day daily-use validation (`docs/runbooks/PHASE-1-DAILY-USE.md`) and human change-review sign-off, both still open.

Primary outcomes:

- Today dashboard and Morning Brief
- tasks, commitments, notes, meetings and risks
- deterministic attention ranking and local search
- immutable audit
- explainable recommendations with durable human confirmation
- executive frontend and browser acceptance
- production hardening: security/config validation, structured observability, verified backup/restore, representative-scale performance gates, and dependency/container/secret scanning

Specification: [PHASE-001](phases/PHASE-001-executive-dashboard-mvp.md)  
Delivery status: [Phase 1 Implementation Status](phases/phase-001/IMPLEMENTATION-STATUS.md)  
Release gate: [Phase 1 Production Release Gate](runbooks/PHASE-1-RELEASE-GATE.md)  
Deployment runbook: [Phase 1 Deployment](runbooks/PHASE-1-DEPLOYMENT.md)  
Daily-use validation record: [Phase 1 Daily-Use Validation Record](runbooks/PHASE-1-DAILY-USE.md)

## Phase 2 — Knowledge Platform

**Status:** Approved for Implementation; contracts moved from Draft after resolving the PKOS-reconciliation decision in `docs/superpowers/specs/2026-07-21-phase-2-knowledge-platform-design.md` (extend the existing `pkos_nodes`/`pkos_edges`/`pkos_evidence` tables rather than fork independent ones). Implementation began by explicit repository-owner authorization to proceed in parallel with Phase 1's still-open exit gates (seven-day daily-use validation, human change review) — a deliberate exception to this document's own "implementation begins only after dependencies satisfy exit criteria" principle above, not a claim that Phase 1 has exited.

Tasks 1-6 and 8 (entities/claims/provenance, typed relationships, timeline, resolution, reversible merge/split, lexical retrieval, and the executive knowledge UX consuming all of it) are implemented. **Task 7 (optional embeddings and hybrid fusion), the design doc's Open decision 2, is now also authorized by the repository owner to proceed**, closing that decision's "repository owner decides embeddings are worth pursuing" precondition. This authorization covers starting the work, not the RFC-005/ADR gate itself — `RFC-005.md`'s "Retrieval benchmark and ADR" activation requirement for `pgvector` is satisfied separately, by RFC-005 v1.2.0's amendment and ADR-0011, both produced as part of this same authorization.

Persistent entities, claims, relationships, entity resolution, reversible merge/split, timelines and lexical-first hybrid retrieval, now extended with optional local embeddings for semantic recall.

Specification: [PHASE-002](phases/PHASE-002-knowledge-platform.md)  
Implementation plan: [Phase 2 Knowledge Platform Implementation Plan](superpowers/plans/2026-07-21-phase-2-knowledge-platform.md)

## Phase 3 — Human Attention Engine

**Status:** Approved for Implementation; contracts moved from Draft after resolving the `attention_items`-reconciliation decision in `docs/superpowers/specs/2026-07-22-phase-3-human-attention-engine-design.md` (extend Phase 1's shipped `attention_items` table rather than fork a new one plus a separate `attention_overrides` table). The three named approval gates (attention policy weights/caps, critical-item definition, dogfood success thresholds) are resolved in `phase-003/ATTENTION-MODEL.md` and `PHASE-003-human-attention-engine.md`. Implementation begins by explicit repository-owner authorization to proceed in parallel with Phase 1/2's own open exit gates — the same kind of exception Phase 2 received.

Explainable attention, waiting direction, risk review, capacity-aware planning and evidence-backed meeting preparation.

Specification: [PHASE-003](phases/PHASE-003-human-attention-engine.md)  
Implementation plan: [Phase 3 Human Attention Engine Implementation Plan](superpowers/plans/2026-07-22-phase-3-human-attention-engine.md)

## Phase 4 — AI Runtime

**Status:** Approved for Implementation; contracts moved from Draft after the repository owner reviewed and accepted `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md`'s proposed resolution for the first activation (local model: Ollama serving `qwen2.5:1.5b-instruct-q4_K_M`, no remote provider registered; deterministic routing algorithm; immutable prompt/tool versioning; Pydantic-based structured-output validation; two read-only first tools; concrete budget/timeout/circuit-breaker numbers; a 20-example first evaluation dataset for `attention.explain_item`) and the four named approval gates (approved local/remote models and providers, data-class egress matrix, evaluation floors, trace retention), resolved in `PHASE-004-ai-runtime.md`'s "Approved models, providers and evaluation floors" section. Ollama's RFC-005 technology-activation gate ("AI-runtime phase specification and ADR review") is satisfied by this design doc plus `docs/adr/ADR-0012-ollama-local-inference.md` and `docs/RFC-005.md` v1.3.0. Implementation begins by explicit repository-owner authorization to proceed in parallel with Phase 3's own open exit gate (the two-week dogfood window is still open) -- the same kind of exception Phase 2 and Phase 3 each received. This authorization does not itself close Phase 3's dogfood gate.

Implementation has landed through Task 19 of the first activation slice (post-launch audit fixes and closure) -- see `phase-004/IMPLEMENTATION-STATUS.md`. The originally scoped first activation was deliberately narrow: one local model, no remote provider, two read-only tools, one evaluated task type. Two parts of that scope have since been explicitly reopened and repository-owner-approved within this same activation, not silently inherited: a second local model and a second evaluated task type, `meeting.prep_summary` (`PHASE-004-ai-runtime.md`'s "Approved models, providers and evaluation floors" section has the current detail). A remote provider and any mutating tool remain explicitly deferred to a later Phase 4 slice this design pass did not schedule.

Phase 4's own exit gate remains open, by repository-owner acceptance rather than by oversight: two evaluation-floor misses (`attention.explain_item`'s stable `prohibited_fact` count, `meeting.prep_summary`'s p95 latency) are documented, evidence-backed known limitations of this activation's small local model, not pursued further as of 2026-07-25, and no promotion decision has been made for either task type (`PHASE-004-ai-runtime.md`'s "Phase 4 exit status and Phase 5 parallel-start" section has the full detail). The repository owner's own independent full-repo re-verification, also named in Phase 4's exit criteria, has not yet been performed.

Specification: [PHASE-004](phases/PHASE-004-ai-runtime.md)  
Design doc: [Phase 4 AI Runtime Design](superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md)  
Implementation plan: [Phase 4 AI Runtime Implementation Plan](superpowers/plans/2026-07-23-phase-4-ai-runtime.md)  
Technology activation: [RFC-005 v1.3.0](RFC-005.md), [ADR-0012](adr/ADR-0012-ollama-local-inference.md)

## Phase 5 — Automation

**Status:** Approved for Implementation; contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:136`'s four named approval-gate items (PostgreSQL worker/lease design, high-impact action taxonomy, approval expiry/rate limits, recovery runbook) in `docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`, per its own "Approved decision gates" section, resolved in `PHASE-005-automation.md`'s "Approved decisions" section. `RFC-005.md`'s pre-registered Temporal activation gate ("Durable workflows | Automation phase and ADR") is evaluated and explicitly declined for this first activation -- `docs/adr/ADR-0013-durable-workflow-execution.md` records a PostgreSQL-backed lease worker instead, so `RFC-005.md` itself is not amended (Temporal's row is unchanged, still un-activated). Design work began by explicit repository-owner authorization to proceed in parallel with Phase 4's own open exit gate (`PHASE-004-ai-runtime.md`'s "Phase 4 exit status and Phase 5 parallel-start" section) -- the same kind of exception Phase 2, Phase 3 and Phase 4 each received; this authorization does not itself close Phase 4's exit gate. **Engineering delivery complete as of Task 7** (`docs/phases/phase-005/IMPLEMENTATION-STATUS.md` has the full task-by-task evidence: Task 1 workflow/policy/trigger schema, Task 2 durable local worker and crash recovery, Task 3 approval inbox and revocation enforcement, Task 4 schedule triggers and run management, Task 5 connector action adapters, Task 6 compensation/observability/kill switches, Task 7a simulate/kill-switch-status/compensation-ledger endpoints, Task 7 the frontend surface and browser acceptance tests) -- this status line was not kept current task-by-task through the phase and is corrected here rather than left stale now that the phase's engineering work is done. The one remaining exit item is real, human-operator-driven: `docs/runbooks/PHASE-5-DOGFOOD.md`'s staged dogfood record (Open, 0 of 14 required days logged) -- not further engineering work.

Versioned workflows, simulation, explicit approval, durable execution, schedules, cancellation, compensation and kill switches.

Specification: [PHASE-005](phases/PHASE-005-automation.md)  
Design doc: [Phase 5 Automation Design](superpowers/specs/2026-07-25-phase-5-automation-design.md)  
Technology decision: [ADR-0013](adr/ADR-0013-durable-workflow-execution.md)  
Recovery runbook: [Phase 5 Durable Worker Recovery](runbooks/PHASE-5-RECOVERY.md)  
Implementation status: [Phase 5 Implementation Status](phases/phase-005/IMPLEMENTATION-STATUS.md)  
Dogfood record: [Phase 5 Dogfood Validation Record](runbooks/PHASE-5-DOGFOOD.md)

## Phase 6 — Engineering Workspace

**Status:** Approved for Implementation; contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:137`'s three named approval-gate items (provider scopes and retention, connector release set, metric definitions and source-coverage thresholds) in `docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md`, per that document's Decision 1-3 sections, resolved in `PHASE-006-engineering-workspace.md`'s "Approved decisions" section. Implementation begins by explicit repository-owner authorization to proceed in parallel with Phase 5's own open exit gate (the staged dogfood record, 0 of 14 required days logged) -- the same kind of exception Phase 2, Phase 3, Phase 4 and Phase 5 each received; this authorization does not itself close Phase 5's exit gate.

Engineering delivery is complete through Task 8: Task 1 (connector framework and source projections: the `ConnectorAdapter` contract, encrypted credential storage, connector lifecycle API and a sandbox adapter exercising the full contract shape), Task 2 (GitHub read sync: `github_adapter.GitHubAdapter`, the first real non-sandbox adapter, repository backfill/incremental sync only), Task 3 (GitLab read sync: `gitlab_adapter.GitLabAdapter`, the second real adapter against the identical contract, no new migration needed since `repositories` already supported the `gitlab` provider), Task 4 (Jira work-item sync: `jira_adapter.JiraAdapter`, the third real adapter, work items only since Jira is not a source-control provider, adding `engineering_work_items` via migration `0047`), Task 5 (delivery and reliability metrics: `github_adapter.py` extended with `change`/`review` sync, `backend/ecc/domains/engineering/metrics.py`'s coverage-threshold engine, `GET /engineering/metrics` -- only three of the seven approved metrics were genuinely computable at that point, since `deployments`/`incidents` data didn't exist yet), Task 6 (decisions, incidents and change-correlation: `incidents`/`engineering_decisions` and their `changes`-correlation join tables via migration `0049`, `ecc.domains.engineering.decisions_incidents`'s capture/resolve/decide/list endpoints, and `time_to_restore` becoming the fourth genuinely computable metric -- `delivery_frequency`/`lead_time_for_changes`/`change_failure_rate` remain `insufficient_coverage`, still blocked on `deployments`, which has no task assigned to it at all; correlation to `deployments`/work items and wiring raw provider identifiers into Phase 2's identity resolution are both disclosed follow-ups, not silent gaps), and Task 7 (approved write actions: `ecc.domains.engineering.write_actions`'s three `github.add_issue_comment`/`gitlab.add_note`/`jira.add_comment` `ActionAdapter`s, dispatched through Phase 5's unchanged `worker.py` gate -- one action concept, add-a-comment, chosen for being uniformly reversible and creating no new external identity; every adapter declares `high_impact_categories={"public"}` unconditionally, the real access control given `automation/policy.py`'s own documented unenforced-`action_types` gap; no new migration or HTTP endpoint), and Task 8 (executive UX and browser acceptance: `frontend/src/features/engineering/` implements all eight `UX-STATES.md`-named surfaces as one tabbed `EngineeringWorkspace`, every required degraded state covered including first sync, backfill, partial permissions, stale connector, rate limited, disconnected, provider unavailable and conflicting identities -- the last a disclosure of raw unresolved provider identifiers rather than an interactive resolution flow, since none exists for engineering data; two disclosed additive query endpoints, `GET /engineering/repositories`/`/work-items`, close the one real gap the frontend work found; verified via component tests and two Playwright browser-acceptance scenarios with accessibility checks) -- see `docs/phases/phase-006/IMPLEMENTATION-STATUS.md` for task-by-task evidence.

GitHub, GitLab and Jira connectors; delivery/reliability intelligence; incidents; decisions; evidence and source coverage without person scoring.

Specification: [PHASE-006](phases/PHASE-006-engineering-workspace.md)  
Design doc: [Phase 6 Engineering Workspace Design](superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md)  
Technology activation: [RFC-005 v1.4.0](RFC-005.md)  
Implementation plan: [Phase 6 Engineering Workspace Implementation Plan](superpowers/plans/2026-07-27-phase-6-engineering-workspace.md)  
Implementation status: [Phase 6 Implementation Status](phases/phase-006/IMPLEMENTATION-STATUS.md)

## Phase 7 — Personal Intelligence

**Status:** Approved for Implementation; contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:138`'s four named approval-gate items (per-domain schema/retention, privacy impact assessment, encryption fields, high-stakes safety rubric) in `docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, per that document's Decision 1-4 sections, resolved in `PHASE-007-personal-intelligence.md`'s "Approved decisions" section. Engineering delivery complete through Task 8 (all six domains, executive UX and browser acceptance) plus a whole-phase deep-review-and-fix round (`luckyrjain/executive-command-center#99`, merged; a second independent review of that round's own diff found zero further issues before merge) -- proceeding in parallel with Phase 6's own open exit gate, the same kind of exception Phase 2, 3, 4, 5 and 6 each received; this does not itself close Phase 6's exit gate, and no separate promotion/exit decision has been made for Phase 7 itself yet.

Opt-in private domains for health, finance, learning, travel, habits and relationships, with consent, bounded insights, export and deletion.

Specification: [PHASE-007](phases/PHASE-007-personal-intelligence.md)  
Design doc: [Phase 7 Personal Intelligence Design](superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md)  
Implementation plan: [Phase 7 Personal Intelligence Implementation Plan](superpowers/plans/2026-07-31-phase-7-personal-intelligence.md)  
Implementation status: [Phase 7 Implementation Status](phases/phase-007/IMPLEMENTATION-STATUS.md)

## Phase 8 — Multi-user Workspaces

**Status:** Approved for Implementation; contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:139`'s four named approval-gate items (identity migration, complete authorization matrix, invitation verification, revocation propagation SLO) in `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md`, per that document's Decision 1-4 sections, resolved in `PHASE-008-multi-user.md`'s "Approved decisions" section. Implementation began with Task 1 (account/membership/session framework) once Phase 7's own whole-phase review round converged with zero open findings -- the same kind of repository-owner-directed sequencing every phase 2-7 transition has used; this did not itself make Phase 7's own promotion/exit decision. **Engineering delivery is now complete through Task 9 of 9** (`docs/phases/phase-008/IMPLEMENTATION-STATUS.md` has the full task-by-task evidence: Task 2 invitations, Task 3 the authorization engine plus a schema-wide migration and the engineering reference domain, Task 4 authorization widened to every remaining domain, Task 5 sharing-review UX and effective permissions, Task 6 delegation, Task 7 notifications and shared activity, Task 8 ownership transfer and member removal, Task 9 the executive UX and multi-identity browser-acceptance frontend), followed by three whole-phase deep-review-and-fix rounds (each finding and fixing real issues before the next round starts clean) with a fourth in progress -- no promotion/exit decision has been made for Phase 8 itself yet.

Membership, invitations, least-privilege permissions, explicit sharing, delegation acceptance, ownership transfer and privacy-preserving collaboration.

Specification: [PHASE-008](phases/PHASE-008-multi-user.md)  
Design doc: [Phase 8 Multi-user Design](superpowers/specs/2026-08-01-phase-8-multi-user-design.md)  
Implementation plan: [Phase 8 Multi-user Implementation Plan](superpowers/plans/2026-08-01-phase-8-multi-user.md)  
Implementation status: [Phase 8 Implementation Status](phases/phase-008/IMPLEMENTATION-STATUS.md)

## Phase 9 — Enterprise

**Status:** Draft / Planned.

Tenant isolation, SSO/SCIM, policy administration, keys/residency, retention/legal hold, audit export, compliance evidence and disaster recovery.

Specification: [PHASE-009](phases/PHASE-009-enterprise.md)

## Approval gates

Before a Draft phase becomes Approved for Implementation:

1. dependency exit criteria are evidenced
2. phase scope and supporting contracts are reviewed
3. technology additions are approved through RFC-005 and an ADR where required
4. threat model and privacy boundaries are approved
5. measurable acceptance, performance and recovery datasets are frozen
6. rollback and operational runbooks are reviewable
7. zero Critical, High or Medium findings remain

Phase-specific decisions are recorded in [PHASE-REVIEW](phases/PHASE-REVIEW.md).

## Roadmap governance

A material phase change requires an explicit version update and reviewed pull request. Implementation status documents report evidence but never override normative contracts. No phase may silently skip dependencies or expand approved scope.

## Long-term goal

Build a local-first executive operating system trusted as the first application opened each morning for decisions, commitments, knowledge and attention.
