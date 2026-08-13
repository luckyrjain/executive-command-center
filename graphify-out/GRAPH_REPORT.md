# Graph Report - executive-command-center  (2026-08-13)

## Corpus Check
- 672 files · ~1,110,255 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 10819 nodes · 28123 edges · 730 communities (372 shown, 358 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 1734 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ee26cdb8`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]
- recommendation_mutations.py
- Phase 1 Completion Design
- risks.py
- Task API & Contract Tests
- commitments.py
- Frontend Package Dependencies
- notes.py
- phases/README.md
- Calendar Events API
- Meeting Scheduling API
- retrieval.py
- attention.py
- Core entities
- Dev Bootstrap & Phase 1 Acceptance Tooling
- adr/README.md
- dashboard_briefs.py
- Frontend Autosave Controller & Note Workspace
- Phase 7 Personal Intelligence Implementation Plan
- Frontend Task Workspace
- Frontend Commitment Workspace
- Frontend Dashboard & Panel Components
- RFC-002: Engineering Philosophy
- README (Executive Command Center)
- Recommendation Postgres Integration Tests
- main.py
- DatadogAdapter
- Frontend Dashboard & Panel Components
- Dev Bootstrap Script
- Phase 5 Automation Data Model
- RFC-001: Product Definition
- Frontend API Client
- Architecture Ch.2b: Runtime
- Architecture Ch.3/8: AI Runtime & Data Platform
- Architecture Ch.2a: Core Platform
- Architecture Ch.5: Attention Engine
- Architecture Ch.6: Integration Platform
- bus.py & NonDurableInProcessEventBus Group
- GmailAdapter
- _get
- Architecture Ch.9: Security
- Architecture Ch.10: Operations
- test_engineering_gitlab_sync_postgres.py
- engineering/types.ts
- Architecture Ch.7: Frontend
- delegations.py
- test_gmail_connector_sync_postgres.py
- worker.py
- OllamaAdapter
- PHASE-001: Test Plan
- test_automation_worker_postgres.py
- meeting_prep.py
- RFC-000/RFC-003: Governance, Design Principles & Setup
- Frontend E2e Run
- Docs Phases Phase 001 Audit Contract
- Morning Brief Contract
- Docs Phases Phase 001 Priority Model
- PHASE-001: Search Contract
- test_engineering_write_actions_postgres.py
- execute_run
- JiraAdapter
- Docs Phases Phase 002 Entity Resolution Contract
- write_actions.py
- AuthContext
- Docs Phases Phase 002 Ux States
- personal/types.ts
- knowledge/types.ts
- record_idempotency_conflict
- _publish_workflow
- test_automation_compensation_postgres.py
- Docs Phases Phase 003 Planning Contract
- Docs Phases Phase 003 Test Plan
- test_ai_runtime_budgets_postgres.py
- test_engineering_metrics_postgres.py
- test_engineering_connectors_postgres.py
- test_automation_workflows_postgres.py
- Frontend TypeScript Config
- package.json & Package Name Group
- Backend Ecc Domains Calendar Init
- Backend Ecc Domains Communication Init
- Backend Ecc Domains Governance Init
- Backend Ecc Domains Platform Init
- AdapterRegistry
- docker-compose backend service (read-only, no-new-privileges, health-checked) & Setup and Usage Guide Group
- docker-compose frontend service (read-only, depends_on backend healthy)
- Docs 01 Product Definition
- ADR-0001 Repository Layout: monorepo with docs/backend/frontend/packages/tests/scripts/infrastructure/.github; rejected multi-repo (fragments context) and framework-first folders (weakens domain ownership)
- ADR-0002 Local-First Architecture: core workflows, storage, search, scheduling and default AI path operate locally; cloud is an optional adapter, never mandatory; rejected SaaS-first and offline-cache-with-cloud-authority for conflicting with product principles / subordinating local operation
- ADR-0003 PKOS (Personal Knowledge Operating System): canonical knowledge subsystem storing normalized entities, typed relationships, provenance, temporal validity; source artifacts remain immutable evidence, derived summaries/embeddings are replaceable projections; rejected vector-DB-as-primary-memory (no authoritative relationships) and notes-only model (no cross-domain reasoning)
- ADR-0004 AI Runtime: all model calls pass through a single AI Runtime (Model Router, prompt registry, structured-output validation, tool permission checks, evaluation hooks, audit logging); AI outputs are proposals until validated; rejected direct model calls per feature for fragmenting policy/evaluation/observability
- Model Router: centralized component through which all LLM/model calls must pass (provider routing, local/cloud selection, auditable versions)
- ADR-0005 Event Bus: versioned immutable domain events, past-tense named, standard envelope, published after commit, idempotent consumers, at-least-once delivery, ordering only within an aggregate stream; Phase 0 uses in-process durable implementation behind an event-bus contract; rejected direct service-to-service calls for synchronous coupling/cascading failure
- AI Runtime
- Knowledge Platform
- Connector Framework
- AI Contributions Policy: AI-generated code welcome but must never invent APIs, requirements, technologies or architecture; reviewed like human code
- Modular Monolith Architecture
- PKOS (Personal Knowledge Operating System) schema/repository
- Workspace Isolation Invariant
- Docs Phases Phase 001 Consistency Review
- Recommendation Lifecycle State Machine
- Phase 1 Final Acceptance
- Evidence State Model
- Recommendation Publication Lifecycle
- EvidenceRef Representation
- KnowledgeEntity Representation
- MatchExplanation Representation
- embedding_projections Record
- entity_aliases Record
- entity_operations Record
- knowledge_claims Record
- knowledge_entities Record
- relationships Record
- resolution_candidates Record
- retrieval_documents Record
- source_refs Record
- timeline_entries Record
- Hybrid Retrieval Pipeline
- Retrieval Ranking Rules
- Relationship and Timeline UX
- Resolution Review UX
- Meeting Preparation API
- Planning API Surface
- Deterministic Attention Score Formula
- attention_feedback Record
- attention_items Record
- attention_overrides Record
- capacity_profiles Record
- meeting_packs Record
- plan_blocks Record
- planning_constraints Record
- plans Record
- risk_reviews Record
- waiting_links Record
- Meeting Prep Deterministic Sections
- Meeting Pack Snapshot and Staleness
- Attention Queue UX
- Attention UX Ethics and Accessibility Constraints
- Meeting Preparation UX
- Planner UX
- AI Run Lifecycle
- Typed Internal Runtime Ports
- ai_run_steps Record
- ai_runs Record
- evaluation_runs Record
- evaluation_sets Record
- generated_artifacts Record
- model_definitions Record
- prompt_versions Record
- routing_policies Record
- tool_definitions Record
- AI Evaluation Metrics
- Evaluation Promotion Blocking Criteria
- Circuit Breakers and Fallback
- Deterministic Routing Eligibility
- AI-Enhanced Surface Required States
- Failure Never Blocks Deterministic Core Flow
- WCAG 2.2 AA Accessibility Standard
- Approval Responses Require Current Version and Exact Action Digest
- /automations/approvals Endpoints
- /automations/policies Endpoints
- /automations/runs Endpoints
- Workflow Simulation (Predicted Steps Without Executing)
- /automations/workflows Endpoints
- Approval Modes (preview_only / per_run / bounded_recurring)
- Least-Privilege, Explicit, Time-Bound, Revocable Authority
- Material Changes Invalidate Approval
- Mandatory Per-Run Approval for High-Risk Action Classes
- Phase 5 Automation Authority Semantics
- Stable Action Digest and Idempotency Key
- Bounded Exponential Backoff Only for Classified Transient Failures
- Compensation Executes Only Declared, Approved Steps
- Worker Persists State Before/After Each Side Effect
- Preview-Only Dogfood With Explicit Exit Review
- Automation Functional Test Scope
- Automation Security Test Scope
- Automation Required UX States
- Automation Primary Surfaces (Builder/Simulation/Policy/Approval/History)
- Optional Mutations Route Through Approved Automation Policies
- /engineering/connectors Endpoints
- /engineering/metrics Endpoint
- /engineering/overview and Query Endpoints
- Connector Creation Never Returns Token Values
- Cursor Persists Only After Durable Projection
- Provider Deletion, Access Loss, Rename and Disconnect Are Distinct States
- Least-Privilege Tokens With Encrypted Secret Storage
- Connector Lifecycle (authorize/validate/backfill/sync/webhook/refresh/disconnect)
- Connector Payloads Untrusted, Cannot Issue Runtime Instructions
- 006 Data Model Changes
- Data Model Connector Accounts
- Model Delivery Metric Snapshots
- 006 Data Model Deployments
- Data Model Engineering Decisions
- Model Engineering Work Items
- 006 Data Model Incidents
- Raw Provider Payload Retention Minimized
- People Link to Phase 2 Entities
- Provider/External-ID Scoped Unique Keys With Full Provenance
- 006 Data Model Repositories
- 006 Data Model Reviews
- Data Model Service Links
- Data Model Source Tombstones
- Data Model Sync Cursors
- Data Model Sync Runs
- Approved Delivery/Reliability Metrics Set
- No Composite Engineer Score, Ranking or Leaderboard
- Risk Signals Cite Underlying Evidence and State Confidence
- Metric-Definition Changes Create New Version, Preserve History
- Ethics Checks Prohibit Person Scores/Leaderboards
- Engineering Workspace Security Test Scope
- Connector Sync/Metric Functional Test Scope
- Never Display Person Rankings or Shame Language
- Engineering Workspace Required UX States
- Engineering Workspace Surfaces
- APIs Enforce Consent and Field Policy Server-Side
- /personal/domains Endpoints
- /personal/insights Endpoints
- Health/Finance Suggestions Never Use Diagnostic or Guaranteed-Return Language
- Data Model Check Ins
- Model Cross Domain Grants
- Data Model Deletion Jobs
- Data Model Domain Consents
- Data Model Domain Records
- Data Model Domain Sources
- Field-Level Encryption for Sensitive Payloads
- 007 Data Model Goals
- Insights Are Derived, Versioned, Deletable; Source Records Authoritative
- Data Model Personal Domains
- Data Model Personal Insights
- 007 Data Model Routines
- Domains Are Separate Privacy Compartments
- Granular, Purpose-Bound, Time-Bound, Revocable Consent
- Default Search and Meeting Context Exclude Personal Domains
- Deletion Removes Authoritative and Derived Content
- Sensitive Data Never Leaves Device Unless Explicitly Allowed
- Feedback Cannot Silently Turn Correlation Into Causation
- Cross-Domain Insight Requires Active Grant Covering Every Source
- Insights Must Be Evidence-Backed, Proportionate, Non-Manipulative
- Insight Types (observation/trend/correlation/reminder/planning_suggestion)
- System Does Not Diagnose, Prescribe, or Promise Financial Outcomes
- Adversarial Cases (diagnosis/guaranteed returns/sensitive inference/coercion/prompt injection)
- Personal Domain Privacy Test Scope
- Calm, Non-Judgmental Language; No Shame, Addiction Loops, or False Urgency
- Personal Intelligence Required UX States
- 404 Privacy Masking for Unauthorized Access
- Delegation Endpoints
- Effective Permissions Exposure
- Invitation Endpoints
- Server-validated Session Context
- Shared Activity Endpoint
- Sharing Grants Endpoints
- Workspace Endpoints
- Phase 8 Core Records
- Append-only Delegation History
- Membership Removal Cannot Orphan Records
- Resource Grants
- Resource Visibility Model (private/shared_explicitly/workspace)
- Accountability Transfers Only on Acceptance
- Recipient Access Limited to Required Evidence
- Delegation State Machine
- Dependency on Phase 7 Exit
- Authorization Evaluation Factors
- Background Job Re-check Before Side Effects
- Deny and Privacy Override Role Grants
- Checks at Service and Query Boundaries
- Adversarial Tests (IDOR, Privilege Escalation, Confused Deputy)
- Role/Resource/Action Authorization Matrix
- Multi-identity Browser Acceptance Tests
- Phase 8 Required UX States
- Phase 8 Multi-user Surfaces
- WCAG 2.2 AA Compliance
- Break-glass Endpoints
- Identity Provider Endpoints
- Enterprise Policy Endpoints
- Policy Simulation Before Publication
- SCIM Idempotent Provisioning
- Step-up Authentication for High-impact Actions
- Audit Event Properties (append-only, redacted, exportable)
- Control-to-Evidence Mapping
- Exceptions Expire and Require Risk Acceptance
- Legal Hold Prevents Deletion, Not Access Control
- Audit Export Manifest/Hash/Signature State
- Phase 9 Core Records
- External Key Material, Stored References Only
- Legal Hold Preserves Records Without Read Access
- Tenant ID Uniqueness/Reference Boundary
- Dependency on Phase 8 Exit
- Just-in-time Administrative Support Access
- Cross-tenant Identifiers Return 404
- No Tenant Content in Global Models Without Consent
- Tenant-scoped Storage, Cache, Jobs, AI Context
- Disaster Recovery Exercise (RPO/RTO)
- Independent Penetration/Security Review
- OIDC/SAML Interoperability Validation
- Tenant Isolation Tests Across Layers
- Phase 9 Admin Surfaces
- Phase 9 Required UX States
- WCAG 2.2 AA Compliance
- Local-first deterministic release floor
- Non-surveillance: rank work, never people
- Documentation Fitness Functions AFF-DOC-001..010: FR/test/ADR traceability, no drift, owned/versioned/changelogged docs, no circular dependencies
- GOV-001: The specification is authoritative; code implements the specification, never the reverse
- GOV-002: Every architectural decision is documented
- GOV-003: Every implementation traces back to requirements
- GOV-004: Every behaviour-changing change updates documentation
- GOV-005: Documentation is version controlled
- Secondary Persona: CTO
- Primary Persona: Director of Engineering
- Secondary Persona: Founder
- Secondary Persona: VP Engineering
- EP-001 Specification Before Code: development follows Understand->Specify->Review->Implement->Test->Deploy; no behavior-changing PR without spec update
- EP-002 Simplicity Wins: the simplest solution satisfying a requirement SHALL be preferred; complexity compounds
- EP-003 Delete Before Add: evaluate existing systems before introducing new ones; adding code is easy, removing is difficult
- EP-004 AI is a Junior Engineer: AI accelerates but does not replace engineering judgment; every AI change needs human review, architectural validation, tests, traceability
- EP-005 Local First: local execution is default for privacy, ownership, latency, resilience, cost; cloud SHALL NOT be mandatory for core workflows
- EP-006 Explainability Over Intelligence: an explainable recommendation beats an opaque one; unexplainable recommendations SHOULD NOT be shown
- EP-007 Human Authority: AI recommends, humans decide; AI SHALL NEVER silently send email, delete data, modify data, execute workflows or approve requests
- EP-008 Permanent Memory: information should never be intentionally discarded; system prefers preservation over deletion; summaries regenerate, memory persists
- EP-009 Modular Architecture: subsystems have one responsibility, communicate through contracts; dependency direction UI->Application->Domain->Infrastructure, never reversed
- EP-010 Evolution Over Perfection: choose solutions that evolve safely rather than predicting every future requirement; stability over perfection
- DP-001 Attention Is The Primary Resource: UI must prioritize action, decisions, risks, commitments and deprioritize stats/vanity metrics/decorative widgets
- DP-002 Local First: local execution is default architecture for privacy, speed, ownership, offline capability, lower cost; cloud extends but never owns the product
- DP-003 Explain Everything: every recommendation should answer why, what evidence, how confident, what happens if ignored
- DP-004 Memory Is Permanent: ECC continuously builds richer understanding of people, projects, orgs, commitments, meetings, decisions; deleting context is exceptional
- DP-005 Progressive Disclosure: users see only what is required now (Dashboard->Project->Meeting->Conversation->Original Email); present maximum relevance not maximum information
- DP-006 Calm Interfaces: avoid blinking, animations, unnecessary colors, repeated alerts, graph-filled dashboards; whitespace, silence and focus are features
- DP-007 Relationships Over Documents: ECC organizes around Meeting->People->Decisions->Projects->Action Items->Risks rather than files; documents are evidence, relationships are knowledge
- DP-008 Context Before Content: every piece of information is accompanied by participants, previous meeting, related decisions, risks, documents, action items
- DP-009 Human In Control: AI recommends, humans decide; all state-changing actions (send email, delete, schedule, delegate, update tasks) require explicit confirmation
- DP-010 AI Should Feel Invisible: users should experience outcomes, not AI; good AI is quiet infrastructure, not the product
- DP-011 Executive Dashboard First: every widget must answer what needs attention, what changed, what is blocked, what is at risk, what should happen next
- DP-012 Reduce Decisions: prefer automatic classification/linking/organization/prioritization over manual folder/category/tag/label choices
- DP-013 Search Is A Backup: information should arrive when relevant; search exists for recovery, not primary navigation
- DP-014 Time Is A First-Class Entity: ECC stores timelines not files; everything answers when, what changed, before/after
- DP-015 Design For Years: every feature must scale to 100k emails, 20k meetings, 5k documents, 50k tasks or be redesigned
- DP-016 Small Surfaces: interfaces expose only a few primary actions; configuration is not capability
- DP-017 One Source Of Truth: every concept has exactly one owner - Calendar->Calendar Service, Tasks->Task Engine, Knowledge->Knowledge Graph, Models->Model Router
- DP-018 Consistency Over Novelty: reuse interaction patterns, layouts, terminology; novelty increases learning cost
- DP-019 Defaults Matter: every default represents the recommended path; zero-configuration onboarding is the long-term goal
- DP-020 Every Screen Must Earn Its Place: before adding a screen ask if it could be integrated, contextual, automatic, or eliminated
- Approved-Later Technologies: Ollama, Neo4j, Qdrant, pgvector, Redis, NATS, Kafka, Temporal, Tauri, Kubernetes, S3-compatible storage - each gated by ADR/benchmark/phase activation
- Prohibited In Phase 0: floating versions, unpinned images, Neo4j, Qdrant, Redis, Kafka, NATS, Temporal, Kubernetes, cloud-only deps, JWT browser sessions, no-auth dev mode, LangChain, LangGraph, CrewAI, AutoGen, MongoDB, Firebase, Django, Flask, Express, Electron
- Secrets Management
- Article I: Human Judgment Is Sovereign
- Article II: Human Attention Is Primary Optimization Target
- Article III: Knowledge Is The Product
- Article IV: Local First By Default
- Article IX: Security Is Architectural
- Article V: Specification Before Code
- Article VI: Replace Technologies, Preserve Architecture
- Article VII: AI Is Infrastructure
- Article VIII: Explainability Is Mandatory
- Article X: Every Action Leaves Evidence
- Article XI: Simplicity Is A Feature
- Article XII: One Source Of Truth
- Article XIII: Evolution Without Reinvention
- Article XIV: Every Component Must Earn Its Place
- Article XV: Architecture Exists To Reduce Decisions
- Configuration Hierarchy (Default->Env->Workspace->User)
- Dependency Direction Rule (Application->Domain->Infrastructure)
- Repository Fitness Functions (AFF-STD-001..010)
- Structured Logging Requirements
- Module and Function Size Limits
- RFC-002 (referenced dependency)
- RFC-003 (referenced dependency)
- RFC-004 (referenced dependency)
- RFC-005 (referenced dependency)
- Executive Command Center Frontend Entry (index.html) & frontend/src/main.tsx (module entry, referenced) Group
- domains.py
- Github Issue Template Specification Change Request
- PR Template
- CI Backend Job: ruff, mypy, alembic upgrade, pytest, pip-audit against Postgres 18 service
- CI Containers Job: docker build backend/frontend images
- CI Frontend Job: typecheck, vitest, build, playwright e2e
- CI Security Job: gitleaks secret scan, SBOM (anchore/syft), trivy critical vuln scan
- Acceptance-Contract Job: runs scripts/check_phase1_acceptance.py
- Accessibility-Smoke Job: playwright e2e accessibility checks
- Backup-Restore Job: migrate, backup.sh, verify_restore.sh, phase1 acceptance pytest
- Scripts Init
- seed_phase1_acceptance.py
- test_engineering_decisions_incidents_postgres.py
- validate_repository
- test_engineering_query_endpoints_postgres.py
- test_ai_runtime_evaluation_postgres.py
- test_ai_runtime_routing_postgres.py
- test_ai_runtime_runtime_postgres.py
- test_attention_capacity_postgres.py
- ActionAdapter
- test_gmail_revocation_postgres.py
- test_ai_runtime_validation_postgres.py
- router.py
- record_audit_outbox_failure
- auth.py
- prompts.py
- TransientAdapterError
- test_engineering_authz_postgres.py
- ConnectorAccountContext
- gmail_adapter.py
- test_automation_simulate_postgres.py
- scheduler.py
- test_gmail_action_detection_sync_postgres.py
- automation/policy.py
- test_engineering_github_sync_postgres.py
- ROADMAP.md
- kill_switches.py
- test_attention_meeting_prep_postgres.py
- runs.py
- test_ai_runtime_versioning_postgres.py
- test_attention_planning_postgres.py
- test_automation_approvals_postgres.py
- accounts.py
- relationships.py
- waiting.py
- decisions_incidents.py
- entities.py
- test_automation_scheduler_postgres.py
- test_identity_invitations_postgres.py
- create_identity
- test_automation_runs_postgres.py
- test_engineering_team_suggestions_postgres.py
- entity_operations.py
- test_mutation_brief_performance_postgres.py
- test_knowledge_entity_operations_postgres.py
- test_check_phase3_prohibited_signals.py
- planning.py
- test_automation_kill_switches_postgres.py
- test_observability.py
- test_ai_runtime_meeting_prep_evaluation_postgres.py
- test_automation_policy_postgres.py
- TaskCreate
- test_identity_accounts_postgres.py
- test_gmail_threads_postgres.py
- test_identity_membership_removal_postgres.py
- test_personal_domains_postgres.py
- test_attention_email_awaiting_reply_postgres.py
- Delivery tasks
- _chained_graph
- test_knowledge_embeddings_postgres.py
- claims.py
- AttentionQueue.tsx
- test_collaboration_delegations_postgres.py
- test_phase1_acceptance.py
- test_personal_insight_tools_postgres.py
- move_block
- EchoInput
- assertNoSeriousAccessibilityViolations
- ScheduleWorkspace.tsx
- score_candidate
- gitlab_adapter.py
- test_knowledge_entities_postgres.py
- capacity.py
- entities_mutations.py
- observability.py
- ConnectorHealthPanel.tsx
- test_attention_risk_reviews_postgres.py
- test_platform_notifications_postgres.py
- test_ai_runtime_personal_insight_evaluation_postgres.py
- risk_reviews.py
- evidence.py
- MonkeyPatch
- test_knowledge_relationships_postgres.py
- get_ollama_adapter
- check_promotion_floors
- database.py
- gmail_threads.py
- test_ai_runtime_email_detect_action_evaluation_postgres.py
- test_knowledge_resolution_postgres.py
- validate_production_settings
- test_ai_runtime_tools_postgres.py
- notifications.py
- test_knowledge_retrieval_postgres.py
- test_personal_finance_postgres.py
- test_personal_health_postgres.py
- invitations.py
- search
- RiskWorkspace.tsx
- config.py
- propose_plan
- membership_removal.py
- createFixtureApi
- _insert_attention_item
- test_knowledge_claims_postgres.py
- test_personal_relationships_postgres.py
- _project
- test_phase1_evidence.py
- PHASE-010 — Gmail Connector
- test_ai_runtime_evaluation_live_ollama.py
- http_security.py
- Documentation Governance Repair
- test_knowledge_retrieval_benchmark_postgres.py
- _parse_credential
- test_personal_grants_postgres.py
- test_production_security.py
- github_adapter.py
- create_run
- Phase 3 Human Attention Engine Design
- Phase 4 AI Runtime Design
- Phase 5 Automation Design
- test_evidence_postgres.py
- test_knowledge_resolution_visibility_postgres.py
- Executive Command Center Roadmap
- MeetingPrep.tsx
- Planner.tsx
- WaitingView.tsx
- test_personal_travel_postgres.py
- Executive Command Center — Phase Documentation
- Phase 2 Knowledge Platform Design
- test_phase1_performance_evidence.py
- test_knowledge_retrieval_performance_postgres.py
- Phase 10 Gmail API Schemas
- test_automation_triggers_http_postgres.py
- test_dashboard_briefs_postgres.py
- test_knowledge_entity_operations_performance_postgres.py
- test_personal_learning_postgres.py
- Phase 10 Implementation Status
- Planned file structure
- Planned file structure
- Team suggestions review page
- MorningBrief.tsx
- test_automation_triggers_postgres.py
- UUID
- test_identity_person_organizations_postgres.py
- test_seed_phase1_acceptance.py
- list_plans
- Phase 1 API Schemas
- Phase 10 Gmail Data Model
- Global Constraints
- check_result_evidence
- recommendation_queries.py
- recommendation_targets.py
- Planned file structure
- Architecture Constraints
- Architecture Fitness Functions
- Current controls (Tasks 1-2, 5-7)
- Current behavior (Tasks 2-3, 5-8)
- Phase 8 Multi-user Workspaces Implementation Plan
- File map
- GitLab Self-Managed Instance Support — Design
- test_calendar_meetings_postgres.py
- test_knowledge_resolution_performance_postgres.py
- test_task_postgres.py
- .__call__
- PHASE-000-repository-foundation.md
- Phase 0 Backup and Restore
- Phase 6 Engineering Workspace Implementation Plan
- Phase 10 Gmail Connector Design
- phase1_performance_evidence.py
- GitHubAddIssueCommentAdapter
- test_note_postgres.py
- ._maybe_cool_down
- MonkeyPatch
- 0053_phase4_explain_item_prompt_v2.py
- UUID
- test_sync_backfill_writes_repositories_then_incremental_only_writes_newer
- ADR-0013 — Durable Workflow Execution
- Architecture Constraints
- Architecture Constraints
- CandidateEntity
- audit_queries.py
- Phase 7 Personal Intelligence Design
- Phase 8 Multi-user Workspaces Design
- 0055_phase4_expl_item_prompt_v3.py
- 0063_phase8_authz_visibility.py
- Domain Ownership
- Architecture Constraints
- Security Principles
- adapters.py
- Phase 10 Gmail Connector Implementation Plan
- GitLab Self-Managed Instance Support Implementation Plan
- gmail-panel-states.mjs
- _insert_meeting_with_participant
- _deterministic_alias_match
- ._request_with_rate_limit_retry
- 0029_phase4_prompt_tool_versions.py
- 0033_phase4_reflection.py
- 0034_phase4_meeting_prep.py
- 0035_phase4_meeting_eval.py
- 0057_phase7_insight.py
- 0072_phase10_email_detect_action.py
- Architecture Constraints
- Design Goals
- Priority Signals
- Architecture Constraints
- Design Goals
- Durable Engineering Evidence Policy
- Phase 2 Deployment Runbook (Delta from Phase 1)
- seed_large_meeting_history
- multi-identity-collaboration-lifecycle.mjs
- check_ollama_models.py
- get_settings
- _GmailHistoryCursor
- 0036_phase4_meeting_prep_timeout.py
- 0037_phase4_meeting_timeout2.py
- 0052_phase4_meeting_timeout3.py
- 0077_phase4_expl_item_timeout.py
- Phase 1 Implementation Status
- Architectural Goals
- Architectural Goals
- Operational Philosophy
- ._reject_private_host
- Team suggestions: create team inline
- _reset_mutation_rate_limiters
- _normalize_email
- 0042_phase5_compensation_retry_kill_switch.py
- Runtime Philosophy
- State Management
- Phase Evolution
- Team suggestions: create team inline — Implementation Plan
- GitLab suggested team name: full path, not immediate subgroup
- attention-queue.mjs
- engineering-connector-states.mjs
- knowledge-resolution.mjs
- recommendation-terminals.mjs
- gmail_revocation_context
- test_client_host_ignores_forwarded_for_when_trusted_proxy_count_is_zero
- .check
- 0019_phase2_mutable_versioning.py
- 0027_phase3_meetings.py
- 0028_phase4_model_registry.py
- 0030_phase4_ai_runs.py
- 0031_phase4_evaluation.py
- 0032_phase4_second_model.py
- 0038_phase5_workflow_schema.py
- 0039_phase5_workflow_runs.py
- 0040_phase5_approval_requests.py
- 0041_phase5_scheduler_and_pause.py
- 0043_phase5_preview_blocked_status.py
- 0044_phase6_connector_platform.py
- 0045_phase6_repositories.py
- 0046_phase6_active_sync_guard.py
- 0047_phase6_work_items.py
- 0048_phase6_delivery_metrics.py
- 0049_phase6_decisions_incidents.py
- 0050_phase6_team_linkage.py
- 0051_phase6_datadog_connector.py
- 0054_phase7_personal_domains.py
- 0056_phase7_cross_domain_grants.py
- 0058_phase7_insight_eval.py
- 0059_phase7_insight_feedback.py
- 0060_phase7_insight_eval_v2.py
- 0061_phase8_accounts_memberships.py
- 0062_phase8_invitations.py
- 0064_phase8_delegations.py
- 0066_phase8_ownership_transfers.py
- 0067_phase8_grant_delegation_id.py
- 0069_phase10_gmail_connector.py
- 0070_gmail_sync_cursor_type.py
- 0073_phase10_email_detect_eval.py
- 0075_phase10_thread_forget.py
- 0076_phase10_email_id_purge_log.py
- Deployment Strategy
- attention-explanation.mjs
- automation-lifecycle.mjs
- conflict-audit-keyboard.mjs
- engineering-lifecycle.mjs
- knowledge-entities.mjs
- personal-domain-lifecycle.mjs
- Settings
- Product KPI Contract
- attention-meeting-prep.mjs
- verify_restore.sh
- test_settings_is_actually_constructible_in_development_with_no_session_secret
- collaboration/__init__.py
- Phase 5 Dogfood Validation Record
- vite.config.ts
- backup.sh
- restore.sh
- test_violated_constraint_extracts_psycopg_diag_constraint_name
- Phase 4 AI UX States
- Phase 5 Automation API Schemas
- Phase 5 Automation UX States
- Phase 6 Engineering Workspace API
- Phase 6 Engineering Workspace Data Model
- Phase 6 Engineering UX States
- Phase 7 Personal Intelligence API
- Phase 7 Personal Intelligence Data Model
- Phase 7 Personal Intelligence UX States
- Phase 9 Enterprise API
- Phase 9 Enterprise Data Model
- Phase 9 Enterprise UX States
- executive-command-center
- Phase 6 Connector Recovery Runbook
- test_github_add_issue_comment_rejects_connector_account_in_different_workspace
- _publish_workflow
- phase4_evaluation_meeting_prep.py
- test_execute_run_repair_retry_provider_error_fails_run_gracefully
- test_preview_only_never_dispatches_even_after_an_approved_digest
- BaseTransport
- _estimate_tokens
- .__init__
- test_execute_run_repair_retry_timeout_fails_run_gracefully
- test_execute_run_reflection_schema_invalid_response_skipped_no_repair_attempted
- test_execute_run_reflection_provider_error_is_skipped_run_still_completes

## God Nodes (most connected - your core abstractions)
1. `AuthContext` - 751 edges
2. `ConnectorAccountContext` - 296 edges
3. `create_identity()` - 276 edges
4. `GmailAdapter` - 176 edges
5. `OllamaAdapter` - 137 edges
6. `apiRequest()` - 133 edges
7. `AdapterRegistry` - 117 edges
8. `SyncOutcome` - 107 edges
9. `_get()` - 95 edges
10. `get_settings()` - 89 edges

## Surprising Connections (you probably didn't know these)
- `test_note_body_bounds_are_enforced_by_schema()` --calls--> `NoteCreate`  [INFERRED]
  tests/test_note_contract.py → backend/ecc/domains/knowledge/notes.py
- `PR18 Bootstrap Dev Test Session Report` --semantically_similar_to--> `PR7 Mypy Type-Check Report`  [INFERRED] [semantically similar]
  pr18-bootstrap-report.txt → pr7-mypy.txt
- `_TimeoutSpyAdapter` --uses--> `AuthContext`  [INFERRED]
  tests/test_ai_runtime_runtime_postgres.py → backend/ecc/auth.py
- `_EchoAdapter` --uses--> `AuthContext`  [INFERRED]
  tests/test_automation_kill_switches_postgres.py → backend/ecc/auth.py
- `_Input` --uses--> `AuthContext`  [INFERRED]
  tests/test_automation_kill_switches_postgres.py → backend/ecc/auth.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Evidence-Driven Phase 1 Closure Gate** — docs_runbooks_phase_1_release_gate_exit_criteria, docs_superpowers_plans_2026_07_16_phase_1_completion_task_12_operations_status_synchronization_and_full_proof, docs_superpowers_specs_2026_07_16_phase_1_completion_design_completion_boundary, docs_superpowers_specs_2026_07_16_phase_1_completion_design_documentation_consistency [INFERRED 0.85]
- **Seven-Day Human-Duration Daily-Use Gate** — docs_superpowers_plans_2026_07_16_phase_1_completion_global_constraints, docs_superpowers_specs_2026_07_16_phase_1_completion_design_outcome, docs_runbooks_phase_1_daily_use, docs_superpowers_plans_2026_07_16_phase_1_completion_task_12_operations_status_synchronization_and_full_proof [INFERRED 0.85]
- **Production Hardening and Security Gate Pipeline** — docs_superpowers_plans_2026_07_16_phase_1_completion_task_7_production_configuration_and_http_protections, docs_superpowers_plans_2026_07_16_phase_1_completion_task_8_structured_observability_and_phase_1_metrics, docs_superpowers_plans_2026_07_16_phase_1_completion_task_11_dependency_filesystem_and_container_security_gates, docs_superpowers_specs_2026_07_16_phase_1_completion_design_backend_production_hardening, docs_superpowers_specs_2026_07_16_phase_1_completion_design_ci_and_security [INFERRED 0.85]
- **Phase 5 run lifecycle state machine** — docs_phases_phase_005_data_model_run_states, docs_phases_phase_005_data_model_workflow_runs, docs_phases_phase_005_data_model_approval_requests, docs_phases_phase_005_data_model_compensation_steps [INFERRED 0.85]
- **README governance onboarding reading order** — readme, docs_00_document_control, docs_setup, docs_specifications_spec_000_doc [EXTRACTED 1.00]
- **CI Verification Reports for executive-command-center PRs** — pr18_bootstrap_report_test_report, pr7_mypy_report, concept_dev_bootstrap_feature, concept_mypy_type_safety_gate [INFERRED 0.75]

## Communities (730 total, 358 thin omitted)

### Community 0 - "Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]"
Cohesion: 0.49
Nodes (9): commitment_test_context(), _headers(), fixture, TestClient, UUID, test_commitment_lifecycle_is_transactional_and_workspace_scoped(), test_commitment_list_uses_signed_cursor_pagination(), test_cross_workspace_references_are_not_disclosed() (+1 more)

### Community 1 - "recommendation_mutations.py"
Cohesion: 0.14
Nodes (46): Any, datetime, Request, Session, UUID, record_event(), record_feedback(), RecommendationResponse (+38 more)

### Community 2 - "Phase 1 Completion Design"
Cohesion: 0.06
Nodes (53): PHASE-001 Executive Dashboard MVP, Phase 1 Production Release Gate, Accessibility and UX Checks, Application Correctness Checks, Backup and Recovery Checks, Exit Criteria, Observability Checks, Operations Checks (+45 more)

### Community 3 - "risks.py"
Cohesion: 0.09
Nodes (58): _archive_action(), archive_risk(), _get_row(), _load_cached(), _lock_idempotency(), Any, AuthDep, BaseModel (+50 more)

### Community 4 - "Task API & Contract Tests"
Cohesion: 0.18
Nodes (43): archive_task(), cancel_task(), complete_task(), create_task(), _decode_cursor(), _encode_cursor(), get_task(), _get_task_row() (+35 more)

### Community 5 - "commitments.py"
Cohesion: 0.17
Nodes (47): archive_commitment(), cancel_commitment(), _check_version(), CommitmentAction, CommitmentLinks, CommitmentListResponse, CommitmentPatch, CommitmentResponse (+39 more)

### Community 6 - "Frontend Package Dependencies"
Cohesion: 0.05
Nodes (43): @axe-core/playwright, dependencies, react, react-dom, react-router, @tanstack/react-query, zustand, devDependencies (+35 more)

### Community 7 - "notes.py"
Cohesion: 0.17
Nodes (42): archive_note(), _body_checksum(), _check_version(), create_note(), _decode_cursor(), _encode_cursor(), get_note(), _get_row() (+34 more)

### Community 8 - "phases/README.md"
Cohesion: 0.03
Nodes (37): Phase 1 Data Model, Tables, UX States, Phase 2 API Schemas, Phase 2 Data Model, Exit evidence, Known gaps, Phase 2 Implementation Status (+29 more)

### Community 9 - "Calendar Events API"
Cohesion: 0.16
Nodes (38): archive_calendar_event(), CalendarEventAction, CalendarEventCreate, CalendarEventListResponse, CalendarEventPatch, CalendarEventResponse, create_calendar_event(), _decode_cursor() (+30 more)

### Community 10 - "Meeting Scheduling API"
Cohesion: 0.13
Nodes (45): archive_meeting(), _calendar_event(), create_meeting(), _decode_cursor(), _encode_cursor(), get_meeting(), _get_row(), _lifecycle() (+37 more)

### Community 11 - "retrieval.py"
Cohesion: 0.06
Nodes (55): _content_hash(), EmbeddingProvider, EmbeddingRebuildReport, EmbeddingUnavailable, EmbeddingWriteResult, get_provider(), datetime, Protocol (+47 more)

### Community 12 - "attention.py"
Cohesion: 0.06
Nodes (90): AttentionAction, AttentionFeedback, AttentionFeedbackCreate, AttentionItem, AttentionList, defer_attention(), dismiss_attention(), _due_points() (+82 more)

### Community 13 - "Core entities"
Cohesion: 0.07
Nodes (26): AttentionItem, AuditEvent, Brief, CalendarEvent, Canonical Domain Model, Commitment, Conversation and Message, Core entities (+18 more)

### Community 14 - "Dev Bootstrap & Phase 1 Acceptance Tooling"
Cohesion: 0.14
Nodes (17): Executive Command Center backend package., Local Dev Bootstrap Environment Guard, Mypy Static Type-Check CI Gate, PR18 Bootstrap Dev Test Session Report, PR7 Mypy Type-Check Report, _load_bootstrap_module(), ModuleType, MonkeyPatch (+9 more)

### Community 15 - "adr/README.md"
Cohesion: 0.03
Nodes (48): ADR-0001 — Repository Layout, ADR-0002 — Local-First Architecture, ADR-0004 — AI Runtime, ADR-0005 — Event Bus, ADR-0006 — Storage Strategy, Alternatives considered, Consequences, Context (+40 more)

### Community 16 - "dashboard_briefs.py"
Cohesion: 0.15
Nodes (32): _bounds(), _brief_staleness(), _build_sections(), dashboard_today(), DashboardResponse, _entity_ref(), _generate(), get_morning_brief() (+24 more)

### Community 17 - "Frontend Autosave Controller & Note Workspace"
Cohesion: 0.10
Nodes (22): AutosaveController, AutosaveOptions, AutosaveState, AutosaveStatus, createAutosaveController(), createNoteDraftRecoveryStore(), NoteDraftRecovery, NoteDraftRecoveryStore (+14 more)

### Community 18 - "Phase 7 Personal Intelligence Implementation Plan"
Cohesion: 0.20
Nodes (9): Phase 7 Personal Intelligence Implementation Plan, Task 1 — Domain/consent/vault framework and the `habits` reference domain (this activation), Task 2 — `learning` domain (complete), Task 3 — `travel` domain (complete), Task 4 — `relationships` domain (complete), Task 5 — Cross-domain grants and the first AI-generated insights (split into two PRs), Task 6 (complete) — `health` domain, Task 7 (complete) — `finance` domain (+1 more)

### Community 19 - "Frontend Task Workspace"
Cohesion: 0.16
Nodes (14): Action, duePayload(), EditState, emptyDraft, errorMessage(), filters, listTasks(), pad() (+6 more)

### Community 20 - "Frontend Commitment Workspace"
Cohesion: 0.16
Nodes (13): Action, Commitment, CommitmentList, CommitmentWorkspace(), Draft, duePayload(), EditState, emptyDraft (+5 more)

### Community 21 - "Frontend Dashboard & Panel Components"
Cohesion: 0.05
Nodes (45): cookieValue(), currentState(), requestHeaders(), SAFE_METHODS, ApiErrorEnvelope, ApiRequestOptions, WorkspaceView, api() (+37 more)

### Community 22 - "RFC-002: Engineering Philosophy"
Cohesion: 0.14
Nodes (13): AI Engineering Philosophy, Architectural Fitness Functions, Engineering Principles, EP-001, EP-002, EP-003, EP-004, EP-005 (+5 more)

### Community 23 - "README (Executive Command Center)"
Cohesion: 0.03
Nodes (50): 00 - Document Control, Golden Rule, Architecture Decision Records (docs/adr), PHASE-002 Knowledge Platform, PHASE-003 Human Attention Engine, PHASE-004 AI Runtime, PHASE-005 Automation, workflow_definitions / workflow_versions (+42 more)

### Community 24 - "Recommendation Postgres Integration Tests"
Cohesion: 0.27
Nodes (26): _generate(), _generate_create(), _headers(), datetime, fixture, Response, TestClient, UUID (+18 more)

### Community 25 - "main.py"
Cohesion: 0.10
Nodes (22): _correlation_id(), Request, Response, UUID, _record_rejected_task_mutation(), rejected_mutation_audit_middleware(), _task_id_from_path(), configure_logging() (+14 more)

### Community 26 - "DatadogAdapter"
Cohesion: 0.04
Nodes (95): _dashboard_content_hash(), DatadogAdapter, _external_account_id(), _InvalidCredentialError, _monitor_content_hash(), _monitor_team_tag(), _parse_credential(), Any (+87 more)

### Community 27 - "Frontend Dashboard & Panel Components"
Cohesion: 0.11
Nodes (23): ActionName, actionPayload(), ActionRequest, actionSummary(), confidenceLabel(), EvidenceItem, EvidenceList, EvidencePreview() (+15 more)

### Community 28 - "Dev Bootstrap Script"
Cohesion: 0.35
Nodes (10): _allow_remote_database(), _create_identity(), _database_url(), _existing_identity(), main(), Cursor, datetime, UUID (+2 more)

### Community 29 - "Phase 5 Automation Data Model"
Cohesion: 0.24
Nodes (11): Phase 5 Automation Data Model, approval_requests, automation_policies, compensation_steps, notifications, Run states enum, secret_references, triggers (+3 more)

### Community 30 - "RFC-001: Product Definition"
Cohesion: 0.18
Nodes (10): Functional Requirements, Jobs To Be Done, Non-Functional Requirements, Primary Persona, Product Evolution, Product Maturity Model, Product Principles, Secondary Persona (+2 more)

### Community 31 - "Frontend API Client"
Cohesion: 0.03
Nodes (82): ApiError, ApprovalCard(), ApprovalInbox(), errorMessage(), isExpired(), FUTURE_EXPIRY, mockFetchByPath(), pendingApproval (+74 more)

### Community 32 - "Architecture Ch.2b: Runtime"
Cohesion: 0.05
Nodes (37): AI, AI Cache, Asynchronous, Background Workers, Caching Strategy, Configuration, Deployment Architecture, Domain (+29 more)

### Community 33 - "Architecture Ch.3/8: AI Runtime & Data Platform"
Cohesion: 0.05
Nodes (36): Architecture Fitness Functions, Backup Strategy, Context Package, CQRS, Data Lifecycle, Data Ownership, Data Platform & Storage Architecture, Data Retention (+28 more)

### Community 34 - "Architecture Ch.2a: Core Platform"
Cohesion: 0.06
Nodes (31): AI Events, AI Platform, Application Gateway, Attention Engine, Capture Events, Connector Framework, Core Platform & Service Architecture, Does NOT Own (+23 more)

### Community 35 - "Architecture Ch.5: Attention Engine"
Cohesion: 0.05
Nodes (36): Architecture Fitness Functions, Attention Pipeline, Cognitive Load Model, Commitment Engine, Commitment Lifecycle, Core Architecture, Daily Brief Generator, End-of-Day Reflection (+28 more)

### Community 36 - "Architecture Ch.6: Integration Platform"
Cohesion: 0.07
Nodes (22): Architectural Goals, Architectural Principles, Architecture Fitness Functions, RFC-004: System Architecture, AI Runtime Goals, Runtime Constraints, Architecture Constraints, Core Principles (+14 more)

### Community 37 - "bus.py & NonDurableInProcessEventBus Group"
Cohesion: 0.24
Nodes (5): NonDurableInProcessEventBus, Test and development adapter for synchronous in-process dispatch. This adapter…, EventEnvelope, BaseModel, EventHandler

### Community 38 - "GmailAdapter"
Cohesion: 0.04
Nodes (149): decrypt_credential(), Inverse of `encrypt_credential`. Raises `cryptography.fernet. InvalidToken` if…, GmailAdapter, BaseTransport, `email` empty/blank never matches -- an unset allowlist entry (`""` between two…, A still-valid access token, or one this call successfully refreshes via the…, Real provider-side revocation -- unlike every existing PAT-based adapter (none…, Shared by `disconnect` and the single `try/except` guarding every post-token-… (+141 more)

### Community 39 - "_get"
Cohesion: 0.07
Nodes (91): assign_repository_team_endpoint(), assign_work_item_team_endpoint(), confirm_team_suggestion_endpoint(), ConnectorAccount, ConnectorAccountResponse, create_connector_endpoint(), disable_connector_endpoint(), dismiss_team_suggestion_endpoint() (+83 more)

### Community 40 - "Architecture Ch.9: Security"
Cohesion: 0.04
Nodes (45): AI, AI Access Control, AI Safety, Architecture Fitness Functions, At Rest, Audit Architecture, Authentication, Authorization (+37 more)

### Community 41 - "Architecture Ch.10: Operations"
Cohesion: 0.05
Nodes (39): AI Metrics, Alerting, Architecture Fitness Functions, Backup Strategy, Configuration Management, Container Architecture, Continuous Delivery, Continuous Integration (+31 more)

### Community 42 - "test_engineering_gitlab_sync_postgres.py"
Cohesion: 0.06
Nodes (56): GitLabAdapter, _account_context(), Phase 6 Engineering Workspace Task 3: GitLab read sync…, `_rate_limit_wait_seconds` returns `None` when `Retry-After` is absent entirely…, `_rate_limit_wait_seconds`'s `except ValueError: return None` branch for a non-…, Deliberate fail-open (matches `GitHubAdapter.refresh_permissions`'s identical…, Unlike GitHub's hard no-op, GitLab's self-revocation endpoint is real -- and,…, `404` (token already deleted/revoked out-of-band, e.g. through GitLab's own… (+48 more)

### Community 43 - "engineering/types.ts"
Cohesion: 0.03
Nodes (86): ConnectorCoverageRow(), countStates(), CoveragePanel(), StateCounts, response(), stubFetch(), DecisionRow(), DecisionsPanel() (+78 more)

### Community 44 - "Architecture Ch.7: Frontend"
Cohesion: 0.05
Nodes (36): Accessibility, Architecture Fitness Functions, Command Palette, Component Hierarchy, Context Panel, Dashboard, Dashboard Composition, Design Philosophy (+28 more)

### Community 45 - "delegations.py"
Cohesion: 0.05
Nodes (126): accept_delegation_endpoint(), _account_id_for(), cancel_delegations_for_removed_member(), complete_delegation_endpoint(), create_delegation_endpoint(), DelegationCreateRequest, DelegationListResponse, DelegationResponse (+118 more)

### Community 46 - "test_gmail_connector_sync_postgres.py"
Cohesion: 0.04
Nodes (130): _comment_nesting_bomb(), _cursor_fields(), _json_response(), _message_body(), Any, fixture, MonkeyPatch, Response (+122 more)

### Community 47 - "worker.py"
Cohesion: 0.04
Nodes (113): compensable(), Whether `adapter` declares a genuine compensating action (Decision 9) --…, ActorMembershipInactive, ActorScopeMismatch, cancel_run(), cancel_runs_for_removed_member(), claim_next_run(), _compensation_policy_usable() (+105 more)

### Community 48 - "OllamaAdapter"
Cohesion: 0.06
Nodes (154): CancellationToken, check_input_token_budget(), check_output_token_budget(), CircuitBreaker, Exception, Budgets, timeouts, cancellation and circuit breakers (design doc Decision 5's…, One run's Decision 5 budget: 80s total wall clock, 30s per-model- call, 5s per-…, Reject a prompt **before** the model call is attempted if its pre-call estimate… (+146 more)

### Community 49 - "PHASE-001: Test Plan"
Cohesion: 0.18
Nodes (10): API contract tests, Audit, security and privacy tests, Backup and restore, CI and exit gates, Database integration tests, Domain unit tests, Frontend and end-to-end tests, Performance tests (+2 more)

### Community 50 - "test_automation_worker_postgres.py"
Cohesion: 0.10
Nodes (36): EchoAdapter, _make_registry(), Phase 5 Automation Task 2: the durable local worker and crash recovery…, A `bounded` (non-high-impact) step under a usable `bounded_recurring` policy…, `policy-limit-exceeding` (Decision 5): a `bounded_recurring` policy whose…, The other half of "the approval can still be decided": rejecting a…, Closes the one path from `preview_only` to a real side effect that a naive…, The explicit no-regression counterpart to the `preview_only` fix: `per_run`… (+28 more)

### Community 51 - "meeting_prep.py"
Cohesion: 0.07
Nodes (90): add_participant(), build_pack(), CommitmentOut, CommitmentRow, _compute_enrichment(), _content_to_snapshot(), create_prep(), _current_pack_row() (+82 more)

### Community 52 - "RFC-000/RFC-003: Governance, Design Principles & Setup"
Cohesion: 0.10
Nodes (20): 1. Configure the repository, 2. Start PostgreSQL and migrate, 3. Create a local authenticated session, `401 Authentication required`, `403 CSRF_TOKEN_REQUIRED` or `CSRF_TOKEN_INVALID`, 4. Start the backend, 5. Start the frontend, Bootstrap code is invalid or expired (+12 more)

### Community 53 - "Frontend E2e Run"
Cohesion: 0.19
Nodes (10): main(), scenarios, pendingApproval, run(), tabTo(), run(), seedTask, BASE_URL (+2 more)

### Community 54 - "Docs Phases Phase 001 Audit Contract"
Cohesion: 0.22
Nodes (8): Access and retention, API and tests, Audit Contract, Immutability and consistency, Normative action mapping, Record fields, Redaction, Scope

### Community 55 - "Morning Brief Contract"
Cohesion: 0.22
Nodes (8): AI behavior, Evidence and explanation, Generation lifecycle, Morning Brief Contract, Observability and tests, Purpose, Sections and limits, Staleness and degraded behavior

### Community 56 - "Docs Phases Phase 001 Priority Model"
Cohesion: 0.22
Nodes (8): Confidence, Deterministic Priority Model, Expiry and performance, Explanation, Overrides, dismissal and feedback, Purpose, Score, Tie-breaking

### Community 57 - "PHASE-001: Search Contract"
Cohesion: 0.17
Nodes (11): API result, Boundary, Degraded behavior, Evidence and permissions, Filters and pagination, Indexed entities, Performance, Query normalization (+3 more)

### Community 58 - "test_engineering_write_actions_postgres.py"
Cohesion: 0.17
Nodes (28): GitLabAddNoteAdapter, GitLabAddNoteInput, _insert_connector_account(), parametrize, Phase 6 Engineering Workspace Task 7: "Approved write actions"…, Disclosed asymmetry, made explicit (not merely incidental): unlike…, A `connector_accounts` row written before self-managed support shipped stores a…, `_parse_gitlab_credential` raises `gitlab_adapter.py`'s own private… (+20 more)

### Community 59 - "execute_run"
Cohesion: 0.17
Nodes (29): execute_run(), _adapter_with_responses(), `call_model`'s own `guard.check_total_budget(token)` call (checked before every…, The repair prompt (`rendered_prompt + "\\n\\n" + repair_instruction`) is…, A reflection response is a second surface an injected instruction could target…, Sibling proof for `attention.explain_item`'s own timeout (30.0s, raised from…, A real `OllamaAdapter` over `httpx.MockTransport` (Task 1's own testing…, Phase 10 Task 3: `attention.explain_item` extended to handle a Gmail-sourced… (+21 more)

### Community 60 - "JiraAdapter"
Cohesion: 0.05
Nodes (80): _content_hash(), JiraAdapter, _parse_credential(), _parse_jira_timestamp(), Any, BaseTransport, datetime, Response (+72 more)

### Community 61 - "Docs Phases Phase 002 Entity Resolution Contract"
Cohesion: 0.22
Nodes (8): Candidate scoring, Decision thresholds, Entity Resolution Contract, Goal, Human review, Match hierarchy, Merge and split, Quality metrics

### Community 62 - "write_actions.py"
Cohesion: 0.12
Nodes (20): Connector credential encryption at rest (design doc Decision 2:…, _classify_and_raise(), GitHubAddIssueCommentOutput, GitLabAddNoteOutput, JiraAddCommentOutput, _load_credential(), BaseModel, field_validator (+12 more)

### Community 63 - "AuthContext"
Cohesion: 0.05
Nodes (98): AuthContext, _aggregate(), _classify_outcome(), create_evaluation_run(), _delete_synthetic_email_thread(), _delete_synthetic_items(), _delete_synthetic_meeting(), _delete_synthetic_personal_insight_sources() (+90 more)

### Community 64 - "Docs Phases Phase 002 Ux States"
Cohesion: 0.05
Nodes (66): apiRequest(), ActivityPanel(), activityQueryKey(), CollaborationView, TABS, DelegationRow(), DelegationsPanel(), EMPTY_PROPOSE_FORM (+58 more)

### Community 65 - "personal/types.ts"
Cohesion: 0.05
Nodes (62): classificationLabel(), classificationNote(), DomainRow(), DomainsPanel(), formatTimestamp(), personalErrorMessage(), ExportDeletePanel(), exportRecordCount() (+54 more)

### Community 66 - "knowledge/types.ts"
Cohesion: 0.03
Nodes (70): ClaimDraft, claimValueText(), emptyClaimDraft, emptyMemberDraft, emptyRelationshipDraft, EntityDetail(), EntityDetailProps, evidenceLabel() (+62 more)

### Community 67 - "record_idempotency_conflict"
Cohesion: 0.16
Nodes (42): _candidate_entity(), confirm_candidate(), create_candidate(), _decide_candidate(), _decode_cursor(), defer_candidate(), _encode_cursor(), _entity_row() (+34 more)

### Community 68 - "_publish_workflow"
Cohesion: 0.10
Nodes (26): _create_policy(), HighImpactAdapter, _publish_workflow(), Any, WorkflowVersion, The other permanently-unactionable state the same review found. Once a…, `person-directed` (Decision 5) -- always requires per-run approval regardless…, The deliberate negative half of the fix above: a `'waiting_approval'` run whose… (+18 more)

### Community 69 - "test_automation_compensation_postgres.py"
Cohesion: 0.07
Nodes (54): _action_step(), _cleanup_workspace(), CompensatableAdapter, _compensation_step(), compensation_test_context(), DedicatedUndoAdapter, EchoInput, EchoOutput (+46 more)

### Community 70 - "Docs Phases Phase 003 Planning Contract"
Cohesion: 0.22
Nodes (8): Conflicts, Deterministic planning order, Evaluation, Goal, Inputs, Planning Contract, Proposal and acceptance, Replanning

### Community 71 - "Docs Phases Phase 003 Test Plan"
Cohesion: 0.22
Nodes (8): Browser acceptance, Determinism and property tests, Exit gate, Functional coverage, Performance, Phase 3 Test Plan, Product validation, Security and ethics

### Community 72 - "test_ai_runtime_budgets_postgres.py"
Cohesion: 0.04
Nodes (83): candidate_state_for(), Build a `router.py:CandidateState` whose `health_state` is read live off…, Build a `RunBudget` from a task type's active `routing_policies` row…, Whether the first-slice Reflection Engine (an additional, bounded, fail-open…, reflection_enabled(), AI Runtime domain package (Phase 4). Owns the provider-neutral Model Router,…, _ctx(), _FakeClock (+75 more)

### Community 73 - "test_engineering_metrics_postgres.py"
Cohesion: 0.08
Nodes (76): _bucket_ages_days(), compute_and_store_metrics(), _compute_blocked_work(), compute_metrics(), _compute_review_latency(), _compute_time_to_restore(), _compute_work_ageing(), _coverage_for() (+68 more)

### Community 74 - "test_engineering_connectors_postgres.py"
Cohesion: 0.08
Nodes (76): _ActionDetectionSpyAdapter, _cleanup_workspace(), engineering_test_context(), _headers(), _insert_connector_account(), _insert_personal_email_domain(), Any, fixture (+68 more)

### Community 75 - "test_automation_workflows_postgres.py"
Cohesion: 0.08
Nodes (66): _cleanup_workspace(), _graph_with_compensation(), _graph_with_high_impact_compensation(), _headers(), _insert_version_row(), _insert_workflow_family(), Any, fixture (+58 more)

### Community 76 - "Frontend TypeScript Config"
Cohesion: 0.09
Nodes (22): compilerOptions, allowJs, allowSyntheticDefaultImports, esModuleInterop, forceConsistentCasingInFileNames, isolatedModules, jsx, lib (+14 more)

### Community 77 - "package.json & Package Name Group"
Cohesion: 0.18
Nodes (10): name, nanoid, packageManager, pnpm, overrides, private, scripts, build (+2 more)

### Community 82 - "AdapterRegistry"
Cohesion: 0.07
Nodes (75): AdapterRegistry, A small in-process `dict[str, ActionAdapter]`-backed registry (design doc…, activate_workflow_version(), compute_definition_hash(), create_workflow_draft(), create_workflow_endpoint(), disable_workflow_endpoint(), disable_workflow_version() (+67 more)

### Community 108 - "Docs Phases Phase 001 Consistency Review"
Cohesion: 0.33
Nodes (5): Closed critical findings, Closed high findings, Closed medium findings, Final re-review result, Phase 1 Consistency Review Closure

### Community 110 - "Phase 1 Final Acceptance"
Cohesion: 0.29
Nodes (6): Accessibility and product evidence, Automated gates, Backup and restore evidence, Phase 1 Final Acceptance, Product validation outside the merge gate, Purpose

### Community 377 - "domains.py"
Cohesion: 0.05
Nodes (101): decrypt_field(), encrypt_field(), _fernet(), Personal-domain field-level encryption at rest (design doc Decision 3:…, Cached per-process, mirroring `ecc.domains.engineering.crypto. _fernet`'s…, `plaintext` is a single field's value -- a `domain_records.payload` field for…, Inverse of `encrypt_field`. Raises `cryptography.fernet.InvalidToken` if…, _classification_for() (+93 more)

### Community 378 - "Github Issue Template Specification Change Request"
Cohesion: 0.20
Nodes (9): Acceptance criteria, Affected documents, Alternatives considered, Ambiguity, conflict or missing requirement, Architecture impact, Current specification, Proposed change, Reason and user impact (+1 more)

### Community 379 - "PR Template"
Cohesion: 0.18
Nodes (10): Architecture impact, Breaking changes, Checklist, Observability impact, Problem, Rollback plan, Security and privacy impact, Solution (+2 more)

### Community 388 - "seed_phase1_acceptance.py"
Cohesion: 0.08
Nodes (71): _alembic_revision(), _archive_checksum_status(), build_report(), evaluate_invariants(), _git_head_sha(), main(), _parse_args(), _pg_url() (+63 more)

### Community 389 - "test_engineering_decisions_incidents_postgres.py"
Cohesion: 0.09
Nodes (71): _cleanup_workspace(), _create_decision(), _create_incident(), engineering_test_context(), _headers(), _insert_change(), _other_workspace_client(), datetime (+63 more)

### Community 390 - "validate_repository"
Cohesion: 0.08
Nodes (65): _clean_destination(), github_anchor(), _governed_class(), _heading_anchors(), _lines_outside_fences(), main(), _markdown_files(), parse_frontmatter() (+57 more)

### Community 391 - "test_engineering_query_endpoints_postgres.py"
Cohesion: 0.12
Nodes (68): _cleanup_workspace(), _headers(), _insert_change(), _insert_connector_account(), _insert_datadog_dashboard(), _insert_datadog_monitor(), _insert_datadog_service_definition(), _insert_repository() (+60 more)

### Community 392 - "test_ai_runtime_evaluation_postgres.py"
Cohesion: 0.08
Nodes (46): Versioned, checked-in labelled dataset for Phase 4 Task 5's evaluation harness…, _adapter_with_responses(), _flat_responses(), _headers(), http_client(), fixture, MonkeyPatch, TestClient (+38 more)

### Community 393 - "test_ai_runtime_routing_postgres.py"
Cohesion: 0.05
Nodes (64): ai_runtime_test_context(), _ctx(), _model(), _ndjson_response(), _p95(), fixture, MonkeyPatch, Response (+56 more)

### Community 394 - "test_ai_runtime_runtime_postgres.py"
Cohesion: 0.12
Nodes (24): Test-only escape hatch: the module-level breaker registry otherwise persists…, reset_circuit_breakers(), http_client(), fixture, Phase 4 Task 4: the bounded tool runtime and orchestration loop…, Flips `routing_policies.constraints.reflection_enabled` to `true` for…, The second registered model (Task 7) is temporarily marked `disabled` for this…, A defense-in-depth check found missing during PR review: the reflection call's… (+16 more)

### Community 395 - "test_attention_capacity_postgres.py"
Cohesion: 0.08
Nodes (62): archive_constraint(), archive_constraint_endpoint(), create_constraint(), create_constraint_endpoint(), list_active_constraints(), list_constraints_endpoint(), _load_cached(), _lock_idempotency() (+54 more)

### Community 396 - "ActionAdapter"
Cohesion: 0.07
Nodes (69): ActionAdapter, call_compensate(), BaseModel, Protocol, Must not perform the real side effect, by contract (Decision 4) -- returns a…, The real side effect. `ecc.domains.automation.worker.run_step` is this…, Invoke `adapter.compensate(action_input)` -- raises `AttributeError` if…, Structural contract every registered action adapter satisfies (design doc… (+61 more)

### Community 397 - "test_gmail_revocation_postgres.py"
Cohesion: 0.11
Nodes (53): _connector_status(), _headers(), _insert_attention_item(), _insert_enabled_email_domain(), _insert_gmail_connector_account(), _insert_message(), _insert_pkos_evidence(), _insert_pkos_node() (+45 more)

### Community 398 - "test_ai_runtime_validation_postgres.py"
Cohesion: 0.05
Nodes (62): Validate `raw_response` (raw JSON text from a model or tool call) against…, `outcome` is the final validation result after at most one repair retry;…, Validate `first_raw_response`; if (and only if) it is `schema_invalid`, call…, Small, instruct-tuned models (this activation's `qwen2.5:1.5b` included)…, RepairAttemptResult, _strip_markdown_fence(), _summarize_validation_error(), validate_output() (+54 more)

### Community 399 - "router.py"
Cohesion: 0.09
Nodes (44): get_model(), list_models(), list_models_endpoint(), ModelDefinition, ModelDefinitionResponse, ModelListResponse, Any, AuthDep (+36 more)

### Community 400 - "record_audit_outbox_failure"
Cohesion: 0.10
Nodes (28): CreateNoteInput, CreateNoteOutput, _fake_external_id(), FakeExternalActionAdapter, FakeExternalActionCompensationOutput, FakeExternalActionInput, FakeExternalActionOutput, BaseModel (+20 more)

### Community 401 - "auth.py"
Cohesion: 0.06
Nodes (91): _cited_record_period(), generate_insight_endpoint(), InsightGenerateRequest, InsightGenerateResponse, AuthDep, BaseModel, CsrfDep, datetime (+83 more)

### Community 402 - "prompts.py"
Cohesion: 0.07
Nodes (54): activate_policy(), activate_prompt_version(), compute_template_hash(), _current_active_version(), get_active_prompt(), get_prompt_version(), PolicyActivateRequest, PolicyActivateResponse (+46 more)

### Community 403 - "TransientAdapterError"
Cohesion: 0.09
Nodes (49): Exception, Task 6's own addition to the adapter contract surface (`docs/phases/…, TransientAdapterError, BaseException, _action_step(), AlwaysTransientAdapter, _chained_graph(), _cleanup_workspace() (+41 more)

### Community 404 - "test_engineering_authz_postgres.py"
Cohesion: 0.10
Nodes (57): _Actor, authz_context(), _AuthzContext, _cleanup_workspace(), _create_connector(), _create_decision(), _create_incident(), _create_session() (+49 more)

### Community 405 - "ConnectorAccountContext"
Cohesion: 0.03
Nodes (97): ConnectorAccountListResponse, ConnectorAccountNotFound, ConnectorCreateRequest, DashboardListResponse, DashboardResponse, _EmptyBody, MetricsListResponse, MetricSnapshotResponse (+89 more)

### Community 406 - "gmail_adapter.py"
Cohesion: 0.07
Nodes (52): _detect_action_for_message(), detect_actions_since(), datetime, Session, UUID, Phase 10 Task 5: proactive `email.detect_action` AI-task-type wiring for Gmail.…, Called from `connector_accounts.sync_connector_endpoint`'s phase 3, after a…, One fresh `pkos_evidence` row citing the specific message that triggered a… (+44 more)

### Community 407 - "test_automation_simulate_postgres.py"
Cohesion: 0.11
Nodes (48): _action_step(), _cleanup_workspace(), _CompensatableAdapter, _compensation_step(), _count(), _EchoInput, _EchoOutput, _FailingAdapter (+40 more)

### Community 408 - "scheduler.py"
Cohesion: 0.06
Nodes (56): _Due, _evaluate_schedule(), _MisfireSkip, _next_occurrence_after(), _NotDue, datetime, The schedule-trigger evaluation tick (`docs/superpowers/specs/…, This trigger's next scheduled occurrence has not yet arrived -- the ordinary… (+48 more)

### Community 409 - "test_gmail_action_detection_sync_postgres.py"
Cohesion: 0.08
Nodes (54): _BodyParseOutcome, _parse_message_body_response(), What parsing one `messages.get(format=full)` response concluded.…, Pure parsing, no I/O -- `fetch_and_store_body` owns the actual Gmail request…, backlog_context(), _build_deeply_nested_mime_payload_json(), detection_context(), _evidence_source_refs() (+46 more)

### Community 410 - "automation/policy.py"
Cohesion: 0.12
Nodes (44): create_policy(), create_policy_endpoint(), _EmptyBody, get_policy(), is_policy_usable(), list_policies(), list_policies_endpoint(), _load_cached() (+36 more)

### Community 411 - "test_engineering_github_sync_postgres.py"
Cohesion: 0.05
Nodes (88): GitHubAdapter, BaseTransport, _account_context(), _cleanup_workspace(), engineering_test_context(), _headers(), _insert_github_connector_account(), _json_response() (+80 more)

### Community 412 - "ROADMAP.md"
Cohesion: 0.03
Nodes (48): Blockers, Production Readiness and Blocker Register, Review rule, Phase 5 Implementation Slices, Phase 6 Implementation Slices, Phase 7 Implementation Slices, Consent or sync failure, Evidence to retain (+40 more)

### Community 413 - "kill_switches.py"
Cohesion: 0.09
Nodes (55): activate_kill_switch(), deactivate_kill_switch(), get_active_kill_switch(), get_kill_switch(), get_latest_kill_switch(), global_kill_switch_endpoint(), _handle_kill_switch_request(), is_workflow_killed() (+47 more)

### Community 414 - "test_attention_meeting_prep_postgres.py"
Cohesion: 0.13
Nodes (49): _add_participant(), _headers(), meeting_prep_test_context(), _mocked_ollama_adapter(), fixture, MonkeyPatch, TestClient, UUID (+41 more)

### Community 415 - "runs.py"
Cohesion: 0.13
Nodes (48): _action_ref_by_step_index(), cancel_run_endpoint(), _compensation_step_to_response(), CompensationStepResponse, create_run_endpoint(), _EmptyBody, get_run_endpoint(), list_runs_endpoint() (+40 more)

### Community 416 - "test_ai_runtime_versioning_postgres.py"
Cohesion: 0.09
Nodes (44): activation_test_context(), cleanup_prompt_ids(), cleanup_tool_names(), _headers(), _insert_prompt_row(), _insert_tool_row(), fixture, parametrize (+36 more)

### Community 417 - "test_attention_planning_postgres.py"
Cohesion: 0.14
Nodes (48): _add_second_user_in_same_workspace(), _create_plan_with_one_block(), _headers(), _next_period(), planning_test_context(), fixture, TestClient, UUID (+40 more)

### Community 418 - "test_automation_approvals_postgres.py"
Cohesion: 0.12
Nodes (45): _action_step(), approval_test_context(), _cleanup_workspace(), _create_policy(), _fake_adapter(), _fake_policy(), _headers(), _HighImpactAdapter (+37 more)

### Community 419 - "accounts.py"
Cohesion: 0.10
Nodes (45): AccountCreateRequest, AccountResponse, create_account_endpoint(), _create_session_for(), create_workspace_endpoint(), get_me_endpoint(), get_workspace_endpoint(), list_workspaces_endpoint() (+37 more)

### Community 420 - "relationships.py"
Cohesion: 0.10
Nodes (45): create_relationship(), _entity_exists(), _entity_status(), _entity_version(), _fetch_relationship(), list_relationships(), _load_cached(), _lock_idempotency() (+37 more)

### Community 421 - "waiting.py"
Cohesion: 0.13
Nodes (44): cancel_waiting_link(), _counterparty_node_type(), create_waiting_link(), _decode_cursor(), _encode_cursor(), fulfil_waiting_link(), _get_row(), get_waiting_link() (+36 more)

### Community 422 - "decisions_incidents.py"
Cohesion: 0.16
Nodes (44): create_decision_endpoint(), create_incident_endpoint(), decide_decision_endpoint(), _decision_change_ids(), DecisionCreateRequest, DecisionDecideRequest, DecisionListResponse, DecisionResponse (+36 more)

### Community 423 - "entities.py"
Cohesion: 0.11
Nodes (40): create_organization(), create_person(), PersonOrganizationCreate, AuthDep, BaseModel, CsrfDep, IdempotencyHeader, post (+32 more)

### Community 424 - "test_automation_scheduler_postgres.py"
Cohesion: 0.06
Nodes (79): Phase 5 Automation domain package (Task 1: data layer only).…, FrameType, _handle_shutdown_signal(), main(), Run the Phase 5 durable automation worker's poll loop and the schedule- trigger…, _action_step(), adapters_test_context(), _chained_graph() (+71 more)

### Community 425 - "test_identity_invitations_postgres.py"
Cohesion: 0.17
Nodes (41): _cleanup_account(), _cleanup_new_member(), _cleanup_workspace(), client(), _headers(), _make_workspace(), _new_session(), owner_context() (+33 more)

### Community 426 - "create_identity"
Cohesion: 0.08
Nodes (44): add_membership(), create_identity(), _Executable, Any, datetime, Protocol, UUID, Shared test-fixture helper for creating a full Phase 8 identity (an `accounts`… (+36 more)

### Community 427 - "test_automation_runs_postgres.py"
Cohesion: 0.14
Nodes (37): _action_step(), _chained_graph(), _cleanup_workspace(), _EchoAdapter, _headers(), _Input, _Output, _publish_workflow() (+29 more)

### Community 428 - "test_engineering_team_suggestions_postgres.py"
Cohesion: 0.13
Nodes (40): _cleanup_workspace(), _headers(), _insert_connector_account(), _insert_pkos_team(), _insert_repository(), _insert_work_item(), fixture, TestClient (+32 more)

### Community 429 - "entity_operations.py"
Cohesion: 0.16
Nodes (39): _entity_retrieval_fields(), EntityMergeRequest, EntityOperationResponse, EntityOperationReverseRequest, EntityOperationSplitRequest, _has_post_merge_dependent_activity(), _load_cached(), _lock_entity() (+31 more)

### Community 430 - "test_mutation_brief_performance_postgres.py"
Cohesion: 0.09
Nodes (35): Phase1DatasetCounts, Connection, UUID, Deterministic, set-based PostgreSQL fixture generation for Phase 1 performance…, Documented row counts produced by :func:`seed_phase1_dataset`., Batch-insert the documented representative-scale Phase 1 dataset.…, seed_phase1_dataset(), dashboard_performance_dataset() (+27 more)

### Community 431 - "test_knowledge_entity_operations_postgres.py"
Cohesion: 0.26
Nodes (39): _create_confirmed_candidate(), _create_entity(), entity_operations_test_context(), _headers(), _merge(), fixture, TestClient, UUID (+31 more)

### Community 432 - "test_check_phase3_prohibited_signals.py"
Cohesion: 0.07
Nodes (26): AST, expr, Module, _docstring_nodes(), main(), _matches(), Path, Static gate enforcing ATTENTION-MODEL.md's excluded-inputs list. Phase 3 Task… (+18 more)

### Community 433 - "planning.py"
Cohesion: 0.11
Nodes (37): _blocks_overlap(), CapacityDayInput, _day_window(), DeadlineConstraintInput, _decode_cursor(), _diff_blocks(), _encode_cursor(), _fetch_candidates() (+29 more)

### Community 434 - "test_automation_kill_switches_postgres.py"
Cohesion: 0.13
Nodes (35): _action_step(), _chained_graph(), _cleanup_workspace(), _EchoAdapter, _headers(), _Input, kill_switch_test_context(), _make_registry() (+27 more)

### Community 435 - "test_observability.py"
Cohesion: 0.05
Nodes (87): _outbox_backlog_count(), queue_recommendation_transition(), Defer a recommendation-transition counter increment the same way…, record_request(), render_metrics(), _Iterator, pytestmark_postgres, pytestmark_postgres_domain (+79 more)

### Community 436 - "test_ai_runtime_meeting_prep_evaluation_postgres.py"
Cohesion: 0.11
Nodes (30): _adapter_with_responses(), _all_ids(), _flat_responses(), _headers(), http_client(), fixture, TestClient, UUID (+22 more)

### Community 437 - "test_automation_policy_postgres.py"
Cohesion: 0.17
Nodes (36): _cleanup_workspace(), _create_workflow_via_api(), _headers(), _insert_workflow_family(), _make_policy(), policy_test_context(), datetime, fixture (+28 more)

### Community 438 - "TaskCreate"
Cohesion: 0.08
Nodes (30): CommitmentCreate, model_validator, ConfirmAction, DeferAction, PinAction, BaseModel, datetime, field_validator (+22 more)

### Community 439 - "test_identity_accounts_postgres.py"
Cohesion: 0.17
Nodes (35): _hash_password(), _cleanup_account(), _cleanup_workspace(), client(), _headers(), _new_session(), datetime, fixture (+27 more)

### Community 440 - "test_gmail_threads_postgres.py"
Cohesion: 0.12
Nodes (37): encrypt_credential(), _fernet(), Cached per-process -- `Settings` itself is already process-cached…, `plaintext` is a raw OAuth token / personal access token string. Returns…, _pack_credential(), Phase 7 Personal Intelligence domain package (Task 1: domain/consent/ vault…, test_credential_encryption_round_trip(), _cleanup_workspace() (+29 more)

### Community 441 - "test_identity_membership_removal_postgres.py"
Cohesion: 0.14
Nodes (35): _Actor, _cleanup_workspace(), _create_incident(), _create_session(), _headers(), _make_actor(), membership_context(), _MembershipContext (+27 more)

### Community 442 - "test_personal_domains_postgres.py"
Cohesion: 0.21
Nodes (35): _cleanup_workspace(), _enable_habits(), _headers(), personal_test_context(), Any, fixture, TestClient, UUID (+27 more)

### Community 443 - "test_attention_email_awaiting_reply_postgres.py"
Cohesion: 0.20
Nodes (34): email_attention_test_context(), _headers(), datetime, fixture, TestClient, UUID, Phase 10 Task 3: the "awaiting reply" heuristic (`ecc.domains.attention.…, A `workspaces`/`users`/`sessions` row set (for the HTTP-level… (+26 more)

### Community 444 - "Delivery tasks"
Cohesion: 0.06
Nodes (33): Delivery tasks, Exit evidence, Phase 4 Implementation Status, Planning artifacts, Prerequisites, Sandbox constraint (carried forward from the design pass), Task 10 evidence -- second evaluated task type (`meeting.prep_summary`), Task 11 evidence -- post-launch audit fixes (+25 more)

### Community 445 - "_chained_graph"
Cohesion: 0.12
Nodes (26): _action_step(), _chained_graph(), Directly matches `docs/runbooks/PHASE-5-RECOVERY.md`'s own description: a run…, Regression test for a real bug found during this task's own self-review…, Wires each step's `on_success` to the next step's `step_id` (the last step's…, Found untested by the third whole-phase review, despite the fix itself landing…, `TEST-PLAN.md`'s scenario, verbatim: "the 11th run within an hour under a…, The window is genuinely trailing, not a lifetime cap: ten runs back-dated past… (+18 more)

### Community 446 - "test_knowledge_embeddings_postgres.py"
Cohesion: 0.16
Nodes (29): Test-only hook. Pass a fake EmbeddingProvider to make get_provider() return it…, set_provider_for_testing(), _create_entity(), embeddings_test_context(), FakeEmbeddingProvider, _headers(), fixture, MonkeyPatch (+21 more)

### Community 447 - "claims.py"
Cohesion: 0.20
Nodes (30): ClaimCreate, ClaimListResponse, ClaimResponse, create_claim(), _entity_retrieval_fields(), _entity_status(), _entity_version(), _evidence_state() (+22 more)

### Community 448 - "AttentionQueue.tsx"
Cohesion: 0.09
Nodes (22): AiExplainOutput, AiRunResponse, AiRunStatus, AiRunUsage, AttentionExplanation(), ERROR_COPY, errorCopy(), isStale() (+14 more)

### Community 449 - "test_collaboration_delegations_postgres.py"
Cohesion: 0.19
Nodes (31): _Actor, _cleanup_workspace(), _create_incident(), _create_session(), delegation_context(), _DelegationContext, _headers(), _make_actor() (+23 more)

### Community 450 - "test_phase1_acceptance.py"
Cohesion: 0.09
Nodes (27): check_daily_use_gate(), _document(), _parse_checklist(), _parse_daily_use_rows(), Any, MonkeyPatch, Path, require_head_sha_match must fail the check (not silently skip the staleness… (+19 more)

### Community 451 - "test_personal_insight_tools_postgres.py"
Cohesion: 0.16
Nodes (25): _active_grant_categories(), get_insight_sources_tool(), _is_domain_enabled(), Session, `personal.get_insight_sources` tool handler (Phase 7 Task 5 part 2): the…, The union of `granted_categories` across every currently-active grant for this…, _disable_domain(), _enable_domain() (+17 more)

### Community 452 - "move_block"
Cohesion: 0.24
Nodes (31): accept_plan(), create_plan(), get_plan(), _get_plan_for_update(), _load_cached(), _lock_idempotency(), move_block(), Plan (+23 more)

### Community 453 - "EchoInput"
Cohesion: 0.12
Nodes (12): DigestVisibilityProbeAdapter, EchoInput, EchoOutput, FailingAdapter, LeaseHandoverAdapter, BaseModel, The real property Decision 3 depends on, proven directly rather than inferred…, Adapter whose `execute()` always raises -- exercises the classified-failure… (+4 more)

### Community 454 - "assertNoSeriousAccessibilityViolations"
Cohesion: 0.11
Nodes (21): assertNoSeriousAccessibilityViolations(), describeViolation(), SERIOUS_IMPACTS, block, run(), run(), seedCommitment, run() (+13 more)

### Community 455 - "ScheduleWorkspace.tsx"
Cohesion: 0.11
Nodes (25): CalendarEvent, EntityList, EventDraft, EventStatus, Meeting, MeetingDraft, MeetingStatus, TimingDraft (+17 more)

### Community 456 - "score_candidate"
Cohesion: 0.20
Nodes (22): Pure, no-I/O candidate scorer (ENTITY-RESOLUTION-CONTRACT.md's "Candidate…, Typed configuration, not inline literals, per ENTITY-RESOLUTION-CONTRACT.md:…, ResolutionThresholds, score_candidate(), ScoreFactors, ScoreResult, _entity(), test_different_unicode_encodings_of_the_same_name_score_maximum_similarity() (+14 more)

### Community 457 - "gitlab_adapter.py"
Cohesion: 0.13
Nodes (15): _content_hash(), _default_resolve_host(), Any, datetime, Response, Real GitLab REST API connector adapter (Phase 6 Task 3 -- the second non-…, Same allow-list defense `datadog_adapter.py`'s `_upsert_dashboard` already…, GitLab's own `namespace` field -- the group or user this project belongs to --… (+7 more)

### Community 458 - "test_knowledge_entities_postgres.py"
Cohesion: 0.18
Nodes (26): _headers(), knowledge_test_context(), fixture, TestClient, UUID, `team` was added to `EntityKind` to unblock team-scoped views in later phases…, Phase 6's `RepositoriesPanel.tsx`/`WorkItemsPanel.tsx` team dropdown issues…, A second, fully independent workspace with one active entity, for proving… (+18 more)

### Community 459 - "capacity.py"
Cohesion: 0.14
Nodes (26): CapacityDay, CapacityProfile, CapacityProfilePut, _current_profile(), get_capacity_profile(), _load_cached(), _lock_idempotency(), put_capacity_profile() (+18 more)

### Community 460 - "entities_mutations.py"
Cohesion: 0.19
Nodes (29): EntityResponse, archive_entity(), EntityAction, EntityPatch, _get_row(), _load_cached(), _lock_idempotency(), Any (+21 more)

### Community 461 - "observability.py"
Cohesion: 0.09
Nodes (23): _Counter, _discard_lifecycle_events_on_rollback(), _flush_lifecycle_events(), _format_labels(), _Histogram, listens_for, Request, Response (+15 more)

### Community 462 - "ConnectorHealthPanel.tsx"
Cohesion: 0.11
Nodes (25): buildCredential(), ConnectorCard(), ConnectorHealthPanel(), CredentialFields, DATADOG_SITES, emptyCredentialFields(), errorMessage(), isCredentialComplete() (+17 more)

### Community 463 - "test_attention_risk_reviews_postgres.py"
Cohesion: 0.23
Nodes (28): _create_evidence(), _create_risk(), _headers(), datetime, fixture, TestClient, UUID, Migration 0024's documented design: a non-UUID ref (a URL, a document name) is… (+20 more)

### Community 464 - "test_platform_notifications_postgres.py"
Cohesion: 0.19
Nodes (28): _Actor, _cleanup_workspace(), _create_incident(), _create_session(), _headers(), _make_actor(), notification_context(), _NotificationContext (+20 more)

### Community 465 - "test_ai_runtime_personal_insight_evaluation_postgres.py"
Cohesion: 0.14
Nodes (17): Versioned, checked-in labelled dataset for `personal.generate_insight`'s…, _example_is_high_stakes(), _headers(), http_client(), fixture, TestClient, `personal.generate_insight`'s evaluation harness (Phase 7 Task 5 part 2) -- the…, `check_personal_insight_grounding`'s second, conditional check… (+9 more)

### Community 466 - "risk_reviews.py"
Cohesion: 0.14
Nodes (26): _evidence_state(), list_review_queue(), _load_cached(), _lock_idempotency(), AuthDep, BaseModel, CsrfDep, datetime (+18 more)

### Community 467 - "evidence.py"
Cohesion: 0.17
Nodes (26): delete_evidence(), _entity_retrieval_fields(), EvidenceDeleteRequest, EvidenceDeleteResponse, EvidenceItem, EvidenceListResponse, _load_cached(), _lock_idempotency() (+18 more)

### Community 468 - "MonkeyPatch"
Cohesion: 0.13
Nodes (27): _MutationRateLimiter, Fixed-window rate limiter keyed by an arbitrary string. Uses…, FastAPI, ModuleType, MonkeyPatch, Same regression as the two tests above, but against the *real* ``ecc.main.app``…, A client cannot evade the limit by sending a fresh, unvalidated session cookie…, `GET /api/v1/engineering/metrics` is the one documented exception to "reads are… (+19 more)

### Community 469 - "test_knowledge_relationships_postgres.py"
Cohesion: 0.27
Nodes (27): _create_evidence(), _headers(), fixture, TestClient, UUID, A second, fully independent workspace with two nodes and an active relationship…, The actual "team roster" query this filter pair exists for: a team entity's…, relationships_test_context() (+19 more)

### Community 470 - "get_ollama_adapter"
Cohesion: 0.22
Nodes (26): get_ollama_adapter(), FastAPI dependency provider, matching `get_session`'s own DI pattern --…, _cleanup_workspace(), _enable_and_grant_habits(), _headers(), _insert_insight_row(), _mock_adapter_citing(), personal_test_context() (+18 more)

### Community 471 - "check_promotion_floors"
Cohesion: 0.18
Nodes (21): check_promotion_floors(), `EVALUATION-CONTRACT.md`'s four floors, all required simultaneously (design doc…, _metrics(), `meeting.prep_summary`'s own declared floor is 35s today (`EVALUATION-…, 35.0s (phase H, raised from 25.0s -- four consistent live-model runs all landed…, An evaluation run for a task type not in the ceiling table (should not happen…, _run(), test_check_promotion_floors_fails_on_any_prohibited_fact() (+13 more)

### Community 472 - "database.py"
Cohesion: 0.12
Nodes (29): get_session(), listens_for, Session, _set_statement_timeout(), _decode_cursor(), _encode_cursor(), get_timeline(), _project() (+21 more)

### Community 473 - "gmail_threads.py"
Cohesion: 0.08
Nodes (51): The tool's target reference (an `attention_item_id`/`entity_id`) does not…, ToolNotFound, get_thread_content_tool(), Session, UUID, `email.get_thread_content` tool handler (Phase 10 Task 5): the deterministic…, _EmptyBody, forget_thread_endpoint() (+43 more)

### Community 474 - "test_ai_runtime_email_detect_action_evaluation_postgres.py"
Cohesion: 0.12
Nodes (22): Versioned, checked-in labelled dataset for `email.detect_action`'s evaluation…, _adapter_with_grounded_responses(), _headers(), http_client(), fixture, TestClient, `email.detect_action`'s evaluation harness (Phase 10 Task 5) -- the fourth task…, Builds a response for each call from that call's own rendered prompt… (+14 more)

### Community 475 - "test_knowledge_resolution_postgres.py"
Cohesion: 0.31
Nodes (26): _create_entity(), _create_open_candidate(), _headers(), fixture, TestClient, UUID, resolution_test_context(), _seed_foreign_workspace_entity() (+18 more)

### Community 476 - "validate_production_settings"
Cohesion: 0.14
Nodes (26): Fail fast when ``settings`` would be unsafe outside local development.…, validate_production_settings(), The unset-in-.env case: `frontend_url`'s pydantic default…, Build a ``Settings`` instance for a given field state directly. Uses…, _settings(), test_allows_staging_with_valid_settings(), test_allows_todays_development_defaults(), test_allows_valid_production_settings() (+18 more)

### Community 477 - "test_ai_runtime_tools_postgres.py"
Cohesion: 0.16
Nodes (25): A tool handler's successful, not-yet-schema-validated return value. `output` is…, ToolResult, get_item_tool(), Session, UUID, `attention.get_item` tool handler (design doc Decision 6): a thin, read-only…, get_entity_tool(), Session (+17 more)

### Community 478 - "notifications.py"
Cohesion: 0.16
Nodes (25): _account_id_for(), ActivityEventResponse, ActivityListResponse, _decode(), _fetch_audit_candidates(), list_notifications_endpoint(), list_shared_activity_endpoint(), mark_notification_read_endpoint() (+17 more)

### Community 479 - "test_knowledge_retrieval_postgres.py"
Cohesion: 0.30
Nodes (24): _create_alias(), _create_entity(), _headers(), fixture, TestClient, UUID, Settings.embeddings_enabled defaults to False (see config.py), which is exactly…, The one real end-to-end test of `retrieval.py`'s own second whole- phase review… (+16 more)

### Community 480 - "test_personal_finance_postgres.py"
Cohesion: 0.23
Nodes (24): _cleanup_workspace(), _create_account(), _enable(), finance_test_context(), _headers(), _mock_adapter_citing(), Any, fixture (+16 more)

### Community 481 - "test_personal_health_postgres.py"
Cohesion: 0.23
Nodes (24): _cleanup_workspace(), _create_vital_reading(), _enable(), _headers(), health_test_context(), _mock_adapter_citing(), Any, fixture (+16 more)

### Community 482 - "invitations.py"
Cohesion: 0.20
Nodes (26): Session, Shared by every mutation in this `identity` domain package that writes an audit…, _write_identity_audit_event(), accept_invitation_endpoint(), create_invitation_endpoint(), InvitationActionResponse, InvitationCreateRequest, InvitationCreateResponse (+18 more)

### Community 483 - "search"
Cohesion: 0.14
Nodes (21): record_search(), CursorPayload, _decode_cursor(), _normalize_query(), AuthDep, BaseModel, datetime, ge (+13 more)

### Community 484 - "RiskWorkspace.tsx"
Cohesion: 0.13
Nodes (20): createBody(), Draft, EditState, emptyDraft, errorMessage(), filters, fromRisk(), pad() (+12 more)

### Community 485 - "config.py"
Cohesion: 0.15
Nodes (18): ConfigurationError, RuntimeError, Raised when settings are unsafe for the declared deployment environment.…, Left unvalidated outside development, `frontend_url`'s permissive…, Fail closed outside development rather than let `ecc.domains.…, Same structural check as `_validate_connector_token_encryption_key`, for the…, _validate_connector_token_encryption_key(), _validate_cors_origins() (+10 more)

### Community 486 - "propose_plan"
Cohesion: 0.16
Nodes (22): CandidateItemInput, ConflictOutput, PlanProposal, propose_plan(), candidate(), deadline(), monday_local(), datetime (+14 more)

### Community 487 - "membership_removal.py"
Cohesion: 0.19
Nodes (22): _is_sole_active_owner(), list_members_endpoint(), _member_row(), MemberExportSnapshot, MemberListResponse, MemberRemovalResponse, MemberResponse, MemberRoleUpdateRequest (+14 more)

### Community 488 - "createFixtureApi"
Cohesion: 0.20
Nodes (21): archiveAction(), conflictBody(), createCollection(), createFixtureApi(), defaultAuditCorpus, defaultDashboardSections, defaultSearchCorpus, makeAiRuntimeApi() (+13 more)

### Community 489 - "_insert_attention_item"
Cohesion: 0.16
Nodes (21): _headers(), _insert_attention_item(), TestClient, `execute_run` above `store_idempotency`'s call site already committed the real…, Reusing an Idempotency-Key with a materially different payload is not a valid…, Two genuinely concurrent OS threads POSTing `/ai/runs` with the *same*…, ``entity_type`` defaults to ``'task'`` (every pre-existing call site relies on…, No live async execution exists in this activation (module docstring of… (+13 more)

### Community 490 - "test_knowledge_claims_postgres.py"
Cohesion: 0.33
Nodes (22): claims_test_context(), _create_evidence(), _headers(), fixture, TestClient, UUID, A second, fully independent workspace + entity + evidence row, for proving…, _seed_foreign_workspace_node_and_evidence() (+14 more)

### Community 491 - "test_personal_relationships_postgres.py"
Cohesion: 0.30
Nodes (22): _cleanup_workspace(), _create_contact(), _enable(), _headers(), Any, fixture, TestClient, UUID (+14 more)

### Community 492 - "_project"
Cohesion: 0.13
Nodes (20): _json_response(), _project(), Any, Response, Migration `0050_phase6_team_linkage.py`'s "hybrid: auto-suggest, human…, A self-managed GitLab instance old enough to predate `full_path` on the…, A real GitLab `Push Hook` webhook payload's embedded `project. namespace` is a…, A confirmed `team_entity_id` (set only through `POST .../repositories/… (+12 more)

### Community 493 - "test_phase1_evidence.py"
Cohesion: 0.16
Nodes (21): pytestmark_pg, _load_module_from(), _passing_kwargs(), ModuleType, MonkeyPatch, Path, Tests for scripts/phase1_evidence.py. The pure invariant-evaluation logic is…, Regression test: fixture_row_checksums emits 'empty' when a table has no seeded… (+13 more)

### Community 494 - "PHASE-010 — Gmail Connector"
Cohesion: 0.10
Nodes (20): Acceptance criteria, API changes, Approved decisions, Architecture impact, Changelog, Data changes, Deferred backlog, Exit criteria (+12 more)

### Community 495 - "test_ai_runtime_evaluation_live_ollama.py"
Cohesion: 0.13
Nodes (13): fixture, Phase 4 Task 5 Step 6: the live-Ollama evaluation floor check (design doc Test…, The real acceptance check design doc Decision 9 / `EVALUATION-CONTRACT.md`…, `meeting.prep_summary`'s equivalent of this file's `attention. explain_item`…, Migration `0032_phase4_second_model.py` registered a second real candidate,…, Reflection Engine (first slice, `runtime.py:_reflect_on_answer`, gated by…, `ollama_client.py:generate()` sets `temperature=0` and a fixed `seed=0` for…, run_context() (+5 more)

### Community 496 - "http_security.py"
Cohesion: 0.15
Nodes (19): _client_host(), _client_ip_from_forwarded_for(), _is_mutation_route(), mutation_rate_limit_middleware(), Exception, Request, Response, _rate_limit_key() (+11 more)

### Community 497 - "Documentation Governance Repair"
Cohesion: 0.10
Nodes (19): Acceptance criteria, Canonical phase registry, Changelog, Current-document reconciliation, Decision, Documentation Governance Repair, Documentation validator, Evidence policy (+11 more)

### Community 498 - "test_knowledge_retrieval_benchmark_postgres.py"
Cohesion: 0.16
Nodes (17): build_dataset(), LabelledDocument, LabelledQuery, Versioned labelled dataset for hybrid-retrieval semantic-recall evaluation, run…, benchmark_context(), _headers(), fixture, MonkeyPatch (+9 more)

### Community 499 - "_parse_credential"
Cohesion: 0.12
Nodes (14): _parse_credential(), Parses a stored GitLab credential into `(host, token)`. **A credential…, `token` is the *parsed* token half of the credential (`_parse_ credential`'s…, Two calls, unlike GitHub's one: `GET /personal_access_tokens/ self`…, Fails open (returns `"active"`) on a network error or any response other than a…, Best-effort self-revocation -- see module docstring for why this connector's…, Backward compatibility, not laxity: every `connector_accounts` row for provider…, test_parse_credential_reads_a_bare_token_as_a_legacy_gitlab_com_credential() (+6 more)

### Community 500 - "test_personal_grants_postgres.py"
Cohesion: 0.36
Nodes (19): _cleanup_workspace(), _create_grant(), _enable(), grants_test_context(), _headers(), Any, fixture, TestClient (+11 more)

### Community 501 - "test_production_security.py"
Cohesion: 0.16
Nodes (19): _build_test_app(), _free_tcp_port(), fixture, skipif, Production-hardening tests: settings validation and HTTP-layer protections. Two…, Sanity check: enabling CORS in the test app doesn't break the plain (non-short-…, If a deployer forgets -e VITE_API_BASE_URL at `docker run` time, the container…, restore_main_module() (+11 more)

### Community 502 - "github_adapter.py"
Cohesion: 0.08
Nodes (33): _content_hash(), _content_hash_change(), _content_hash_review(), _earliest_review_requested_at(), _list_changes_needing_reviews(), _list_synced_repositories(), Any, datetime (+25 more)

### Community 503 - "create_run"
Cohesion: 0.17
Nodes (17): cancel_run(), create_run(), get_run(), _persist_terminal(), AuthDep, CsrfDep, datetime, IdempotencyHeader (+9 more)

### Community 504 - "Phase 3 Human Attention Engine Design"
Cohesion: 0.11
Nodes (18): Approval decision gates (per `docs/phases/PHASE-REVIEW.md:128`), Architecture impact, Attention policy approach, Completion boundary for this planning pass, Coverage note: endpoints and conventions this document's first draft missed, Delivery strategy, Events and observability, Frontend approach (+10 more)

### Community 505 - "Phase 4 AI Runtime Design"
Cohesion: 0.11
Nodes (18): Approval decision gates (per `docs/phases/PHASE-REVIEW.md:135`), Architecture impact, Completion boundary for this planning pass, Decision 1: model runtime and first local model, Decision 2: model/provider registry shape and routing algorithm, Decision 3: prompt/tool versioning mechanism, Decision 4: structured output validation, Decision 5: budgets, timeouts, cancellation, circuit breakers (+10 more)

### Community 506 - "Phase 5 Automation Design"
Cohesion: 0.11
Nodes (18): Approval decision gates (per `docs/phases/PHASE-REVIEW.md:136`), Architecture impact, Completion boundary for this planning pass, Decision 10: what is deferred out of this first activation, Decision 1: durable execution technology -- PostgreSQL-backed, not Temporal, Decision 2: workflow definition and versioning mechanism, Decision 3: PostgreSQL worker/lease design (approval gate 1), Decision 4: simulation mechanism (+10 more)

### Community 507 - "test_evidence_postgres.py"
Cohesion: 0.33
Nodes (18): evidence_test_context(), _headers(), _insert_node_and_evidence(), datetime, fixture, TestClient, UUID, test_cross_workspace_evidence_resolves_as_missing_not_permission_denied() (+10 more)

### Community 508 - "test_knowledge_resolution_visibility_postgres.py"
Cohesion: 0.23
Nodes (18): _Actor, _create_candidate(), _create_entity(), _create_session(), _headers(), _make_actor(), Connection, fixture (+10 more)

### Community 509 - "Executive Command Center Roadmap"
Cohesion: 0.11
Nodes (18): Approval gates, Current status, Delivery principles, Delivery sequence, Executive Command Center Roadmap, Long-term goal, Phase 0 — Repository Foundation, Phase 10 — Gmail Connector (+10 more)

### Community 510 - "MeetingPrep.tsx"
Cohesion: 0.14
Nodes (14): errorMessage(), EVIDENCE_GAP_LABEL, formatInTimeZone(), MeetingPack, MeetingPackCommitment, MeetingPackDependency, MeetingPackEnrichment, MeetingPackEvidenceGap (+6 more)

### Community 511 - "Planner.tsx"
Cohesion: 0.17
Nodes (13): errorMessage(), pad(), Plan, PlanBlock, PlanConflict, PlanDiffEntry, PlanList, Planner() (+5 more)

### Community 512 - "WaitingView.tsx"
Cohesion: 0.16
Nodes (13): DIRECTIONS, Draft, emptyDraft, errorMessage(), SUBJECT_TYPES, link, validDraft, validateDraft() (+5 more)

### Community 513 - "test_personal_travel_postgres.py"
Cohesion: 0.37
Nodes (16): _cleanup_workspace(), _create_trip(), _enable(), _headers(), Any, datetime, fixture, TestClient (+8 more)

### Community 514 - "Executive Command Center — Phase Documentation"
Cohesion: 0.12
Nodes (16): Capability sequence and dependency policy, Executive Command Center — Phase Documentation, Governance, Phase 0 — Repository Foundation, Phase 10 — Gmail Connector, Phase 1 — Executive Dashboard MVP, Phase 2 — Knowledge Platform, Phase 3 — Human Attention Engine (+8 more)

### Community 515 - "Phase 2 Knowledge Platform Design"
Cohesion: 0.12
Nodes (15): Architecture impact, Completion boundary for this planning pass, Delivery strategy, Entity resolution approach, Events and observability, Frontend approach, Merge/split and reversibility, Non-goals (restating PHASE-002's "Out of scope" for implementation-time visibility) (+7 more)

### Community 516 - "test_phase1_performance_evidence.py"
Cohesion: 0.16
Nodes (12): _load_module(), ModuleType, MonkeyPatch, parametrize, Path, Tests for scripts/phase1_performance_evidence.py. Pure logic (report building,…, test_build_report_has_required_fields_for_each_status(), test_git_head_sha_returns_none_when_git_is_unavailable() (+4 more)

### Community 517 - "test_knowledge_retrieval_performance_postgres.py"
Cohesion: 0.21
Nodes (10): _FixedVectorProvider, _p95(), fixture, TestClient, UUID, Nearest-rank 95th percentile: the smallest value at or above 95% of samples., Deterministic, near-zero-cost stand-in for the real sentence- transformers…, retrieval_performance_context() (+2 more)

### Community 518 - "Phase 10 Gmail API Schemas"
Cohesion: 0.13
Nodes (14): Changelog, Consent revocation cascade (Task 7), Current OAuth endpoints, Current reused connector endpoints, Current thread endpoints (Task 6, list added Task 8), Delivery boundary, `GET /api/v1/personal/gmail/oauth/callback?code=...&state=...`, `GET /api/v1/personal/gmail/oauth/complete?code=...&state=...` (later addition) (+6 more)

### Community 519 - "test_automation_triggers_http_postgres.py"
Cohesion: 0.37
Nodes (14): _cleanup_workspace(), _insert_workflow_family(), fixture, TestClient, UUID, Phase 5 Automation Task 7: `GET /api/v1/automations/triggers`…, _seed_workspace(), test_list_triggers_empty_workflow_is_not_an_error() (+6 more)

### Community 520 - "test_dashboard_briefs_postgres.py"
Cohesion: 0.29
Nodes (14): dashboard_context(), _headers(), fixture, TestClient, UUID, Regression test: ``_build_sections``'s ``recently_changed`` query used to apply…, Regression test: the lazy-generate GET had no lock around its existence check,…, Round 4 (architecture/quality) review finding, Phase 10 Task 3:… (+6 more)

### Community 521 - "test_knowledge_entity_operations_performance_postgres.py"
Cohesion: 0.28
Nodes (14): _headers(), _measure_reverse_p95(), _merge_and_reverse_once(), _mint_session(), _p95(), fixture, TestClient, UUID (+6 more)

### Community 522 - "test_personal_learning_postgres.py"
Cohesion: 0.36
Nodes (14): _cleanup_workspace(), _enable(), _headers(), learning_test_context(), Any, fixture, TestClient, UUID (+6 more)

### Community 523 - "Phase 10 Implementation Status"
Cohesion: 0.14
Nodes (13): Phase 10 Implementation Status, Task 1 evidence, Task 2 evidence, Task 3 evidence, Task 4 evidence, Task 4 -- Loop 2 review evidence, Task 5 evidence, Task 5 -- Loop 2 review evidence (+5 more)

### Community 524 - "Planned file structure"
Cohesion: 0.14
Nodes (13): Completion checks, Global Constraints, Phase 2 Knowledge Platform Implementation Plan, Planned file structure, Task 0: Resolve Open decision 1 and move contracts to Approved for Implementation, Task 1: PKOS reconciliation migration and extended entity/relationship model, Task 2: Typed relationships and entity detail, Task 3: Timeline projection and deterministic rebuild (+5 more)

### Community 525 - "Planned file structure"
Cohesion: 0.14
Nodes (13): Completion checks, Global Constraints, Phase 3 Human Attention Engine Implementation Plan, Planned file structure, Task 0: Resolve Open decisions and move contracts to Approved for Implementation, Task 1: Versioned attention policy over the extended `attention_items`, Task 2: Waiting direction and dependency lifecycle, Task 3: Risk review queue and cadence (+5 more)

### Community 526 - "Team suggestions review page"
Cohesion: 0.14
Nodes (13): Authorization, Backend endpoints, Context, Data model, Error handling & concurrency, Frontend, `GET /api/v1/engineering/team-suggestions`, Out of scope (+5 more)

### Community 527 - "MorningBrief.tsx"
Cohesion: 0.21
Nodes (10): DashboardItem, fetchMorningBrief(), formatTime(), labelFor(), MorningBrief(), MorningBriefResponse, refreshMorningBrief(), Section() (+2 more)

### Community 528 - "test_automation_triggers_postgres.py"
Cohesion: 0.33
Nodes (13): _insert_workflow_family(), fixture, UUID, Phase 5 Automation Task 1: triggers (design doc Decision 7). `triggers.py` has…, test_create_event_trigger_requires_event_type_filter(), test_create_event_trigger_with_filter_succeeds(), test_create_manual_trigger(), test_create_schedule_trigger_requires_expression_and_timezone() (+5 more)

### Community 529 - "UUID"
Cohesion: 0.30
Nodes (17): JiraAddCommentAdapter, JiraAddCommentInput, _insert_work_item(), UUID, `work_item_id=uuid4()` here is a never-synced placeholder, not a real work-item…, A real, synced work item row must exist for `work_item_id` (its containment…, `external_id` (Jira's internal numeric issue id) and `source_url` (built from…, test_jira_add_comment_4xx_is_not_transient() (+9 more)

### Community 530 - "test_identity_person_organizations_postgres.py"
Cohesion: 0.38
Nodes (13): _headers(), identity_test_context(), fixture, TestClient, UUID, A fully independent workspace + user + session, for proving an identity-created…, _seed_second_session(), _teardown_second_session() (+5 more)

### Community 531 - "test_seed_phase1_acceptance.py"
Cohesion: 0.22
Nodes (13): _all_seeded_tables(), _load_module(), _pg_url(), Connection, fixture, ModuleType, Tests for scripts/seed_phase1_acceptance.py. These require a real PostgreSQL…, seeded_connection() (+5 more)

### Community 532 - "list_plans"
Cohesion: 0.17
Nodes (11): BlockMove, BlockRemove, list_plans(), PlanBlockResponse, PlanCreate, PlanList, alias, BaseModel (+3 more)

### Community 533 - "Phase 1 API Schemas"
Cohesion: 0.15
Nodes (13): Audit, Commitment endpoints, Common response models and status codes, Common rules, Dashboard, Meeting and calendar endpoints, Morning brief, Note endpoints (+5 more)

### Community 534 - "Phase 10 Gmail Data Model"
Cohesion: 0.15
Nodes (12): Changelog, `connector_accounts`, Current tables, Delivery boundary, `email_message_id_purge_log`, `email_messages`, `email_threads`, Ownership, retention, and deletion (+4 more)

### Community 535 - "Global Constraints"
Cohesion: 0.15
Nodes (12): Final verification (after Task 9), Global Constraints, Task 1: Migration — `team_suggestion_dismissed_at` columns, Task 2: GitHub adapter — dismiss-reset on changed suggestion, Task 3: GitLab adapter — dismiss-reset on changed suggestion, Task 4: Jira adapter — dismiss-reset on changed suggestion, Task 5: Backend — `GET /api/v1/engineering/team-suggestions` aggregation endpoint, Task 6: Backend — `POST /api/v1/engineering/team-suggestions/confirm` (+4 more)

### Community 536 - "check_result_evidence"
Cohesion: 0.31
Nodes (12): check_result_evidence(), _current_head_sha(), main(), Any, Path, Resolve `candidate`, returning it only if it stays within `root`. `--check-…, Validate a single recorded-result artifact against the…, Return the current git HEAD commit SHA for `root`, or None if it cannot be… (+4 more)

### Community 537 - "recommendation_queries.py"
Cohesion: 0.33
Nodes (9): _decode_cursor(), _encode_cursor(), list_recommendations(), AuthDep, datetime, SessionDep, UUID, LimitQuery (+1 more)

### Community 538 - "recommendation_targets.py"
Cohesion: 0.39
Nodes (11): _execute_create(), execute_target(), Any, Request, Session, UUID, Confirming an `operation="create"` recommendation calls the exact same…, _request_ids() (+3 more)

### Community 539 - "Planned file structure"
Cohesion: 0.17
Nodes (11): Completion checks, Global constraints, Phase 4 AI Runtime Implementation Plan, Planned file structure, Task 0: Resolve open decisions and move contracts to Approved for Implementation, Task 1: Model/provider registry and router, Task 2: Prompt/tool versioning and structured-output validation, Task 3: Budgets, timeouts, cancellation and circuit breakers (+3 more)

### Community 540 - "Architecture Constraints"
Cohesion: 0.18
Nodes (11): ARC-001, ARC-002, ARC-003, ARC-004, ARC-005, ARC-006, ARC-007, ARC-008 (+3 more)

### Community 541 - "Architecture Fitness Functions"
Cohesion: 0.18
Nodes (11): AFF-011, AFF-012, AFF-013, AFF-014, AFF-015, AFF-016, AFF-017, AFF-018 (+3 more)

### Community 542 - "Current controls (Tasks 1-2, 5-7)"
Cohesion: 0.18
Nodes (10): Changelog, Consent enforcement, Consent revocation cascade (Task 7), Current controls (Tasks 1-2, 5-7), Deferred controls (open, not scheduled), Encryption and minimization, On-demand thread reading and per-thread "forget" (Tasks 5-6), Phase 10 Gmail Privacy and Consent Contract (+2 more)

### Community 543 - "Current behavior (Tasks 2-3, 5-8)"
Cohesion: 0.18
Nodes (10): Backfill, Changelog, Consent and permissions, Current behavior (Tasks 2-3, 5-8), Idempotency and ordering, Incremental sync, Phase 10 Gmail Sync Contract, Planned behavior (+2 more)

### Community 544 - "Phase 8 Multi-user Workspaces Implementation Plan"
Cohesion: 0.18
Nodes (10): Phase 8 Multi-user Workspaces Implementation Plan, Task 1 — Account/membership/session framework (this activation), Task 2 — Invitations, Task 3 — Authorization engine, schema-wide visibility/owner migration, and `engineering` as the reference domain, Task 4 — Widen authorization to every remaining domain, Task 5 — Sharing, Task 6 — Delegation, Task 7 — Notifications and shared activity (+2 more)

### Community 545 - "File map"
Cohesion: 0.18
Nodes (10): Documentation Governance Repair Implementation Plan, File map, Global Constraints, Review and delivery checkpoint, Task 1: Canonical registry and deterministic renderer, Task 2: Structural documentation validator, Task 3: Reconcile governance, lifecycle metadata, and durable evidence, Task 4: Complete the Phase 10 contract set (+2 more)

### Community 546 - "GitLab Self-Managed Instance Support — Design"
Cohesion: 0.18
Nodes (10): Code changes, Decision: how the host travels through the system, GitLab Self-Managed Instance Support — Design, Migration, Problem, Rejected alternative: dedicated `host` column, Requirements (confirmed with repository owner), Scope (+2 more)

### Community 547 - "test_calendar_meetings_postgres.py"
Cohesion: 0.42
Nodes (10): calendar_test_context(), _headers(), fixture, TestClient, UUID, Regression test for update_meeting's UPDATE statement: a fixed 8-column ``CASE…, test_calendar_and_linked_meeting_lifecycle(), test_linked_meeting_rejects_cross_workspace_event() (+2 more)

### Community 548 - "test_knowledge_resolution_performance_postgres.py"
Cohesion: 0.31
Nodes (10): candidate_generation_performance_context(), _headers(), _p50(), _p95(), _p99(), fixture, TestClient, UUID (+2 more)

### Community 549 - "test_task_postgres.py"
Cohesion: 0.40
Nodes (10): _headers(), fixture, TestClient, UUID, Regression/coverage test for optimistic concurrency: update_task's `SELECT ...…, task_test_context(), test_concurrent_idempotent_create_returns_one_task(), test_concurrent_updates_with_same_expected_version_do_not_both_succeed() (+2 more)

### Community 550 - ".__call__"
Cohesion: 0.24
Nodes (8): ASGIApp, _declared_content_length(), MaxBodySizeMiddleware, Reject oversized request bodies without ever buffering them. Two layers of…, _send_body_too_large(), Receive, Scope, Send

### Community 551 - "PHASE-000-repository-foundation.md"
Cohesion: 0.05
Nodes (34): docker-compose postgres service (postgres:18.0), Audit, Communication, Contract rules, Domain API Contracts, Domain commands and queries, Executive Intelligence, Knowledge Platform (+26 more)

### Community 552 - "Phase 0 Backup and Restore"
Cohesion: 0.12
Nodes (14): Automation, Backup format, CI validation, Exit evidence, Failure handling, Objective, Phase 0 Backup and Restore, Recovery targets (+6 more)

### Community 553 - "Phase 6 Engineering Workspace Implementation Plan"
Cohesion: 0.20
Nodes (9): Phase 6 Engineering Workspace Implementation Plan, Task 1 — Connector framework and source projections (this activation), Task 2 — GitHub read sync, Task 3 — GitLab read sync, Task 4 — Jira work-item sync, Task 5 — Delivery and reliability metrics, Task 6 — Decisions, incidents and knowledge linking, Task 7 — Approved write actions (+1 more)

### Community 554 - "Phase 10 Gmail Connector Design"
Cohesion: 0.20
Nodes (9): Decision 1 (approval gate): connector mechanics and Protocol extension, Decision 2 (approval gate): privacy/consent model and encryption, Decision 3 (approval gate): OAuth scope, verification reality and rollout gating, Decision 4 (approval gate): recommendations extension and AI-tool safety rubric, Decision 5 (approval gate): attention and knowledge integration, Outcome, Phase 10 Gmail Connector Design, Scope for this activation (+1 more)

### Community 555 - "phase1_performance_evidence.py"
Cohesion: 0.36
Nodes (9): build_report(), _git_head_sha(), main(), _parse_args(), Any, Namespace, Path, Generate the Phase 1 performance-acceptance recorded-result evidence. The… (+1 more)

### Community 556 - "GitHubAddIssueCommentAdapter"
Cohesion: 0.25
Nodes (16): GitHubAddIssueCommentAdapter, GitHubAddIssueCommentInput, _insert_repository(), Proves this adapter is genuinely reached through Phase 5's own `worker.py`…, A real, synced repository row must exist for `repository_id` (contrary to…, A timeout on a POST is genuinely ambiguous -- the comment may have already been…, test_github_add_issue_comment_4xx_is_not_transient(), test_github_add_issue_comment_connection_failure_is_transient() (+8 more)

### Community 557 - "test_note_postgres.py"
Cohesion: 0.49
Nodes (9): _headers(), note_test_context(), fixture, TestClient, UUID, test_note_cursor_restore_guard_and_workspace_isolation(), test_note_lifecycle_autosave_search_and_redacted_audit(), test_note_meeting_reference_is_non_disclosing_until_meetings_exist() (+1 more)

### Community 558 - "._maybe_cool_down"
Cohesion: 0.22
Nodes (5): Must be called with `self._lock` held. Applies the lazy `open` -> `half_open`…, The breaker's current state, exactly the `HealthState` literal…, A successful call (or a successful half-open probe)., A failed call (or a failed half-open probe)., HealthState

### Community 559 - "MonkeyPatch"
Cohesion: 0.17
Nodes (16): _insert_email_thread_with_injected_message_body(), _insert_meeting_with_injected_participant_name(), MonkeyPatch, UUID, `email.detect_action`'s equivalent of the two injection tests above -- a real…, This function's only call site (`execute_run`) already gates on…, The injected instruction lives inside real, attacker-adjacent domain data (an…, `_insert_meeting_with_participant`'s exact shape, with the participant's… (+8 more)

### Community 560 - "0053_phase4_explain_item_prompt_v2.py"
Cohesion: 0.31
Nodes (8): _canonical_hash(), downgrade(), _prompt_versions_table(), Any, TableClause, Activate attention.explain_item.v2: a prompt-content change via a new versioned…, # NOTE: the revision id below is *not* this file's full basename, matching, upgrade()

### Community 561 - "UUID"
Cohesion: 0.14
Nodes (16): _cleanup_workspace(), fixture, UUID, The stronger version of the recovery claim: a run that already completed its…, Drives a run into `'needs_review'` the same way `test_no_policy_…, Regression test for the second half of the "a flag nothing ever consults is not…, Regression test for the lease-ownership hole an adversarial review of…, _run_parked_in_needs_review() (+8 more)

### Community 562 - "test_sync_backfill_writes_repositories_then_incremental_only_writes_newer"
Cohesion: 0.20
Nodes (15): _cleanup_workspace(), engineering_test_context(), _headers(), _insert_gitlab_connector_account(), fixture, MonkeyPatch, TestClient, UUID (+7 more)

### Community 563 - "ADR-0013 — Durable Workflow Execution"
Cohesion: 0.22
Nodes (8): ADR-0013 — Durable Workflow Execution, Alternatives considered, Consequences, Context, Decision, Negative, Positive, Risks

### Community 564 - "Architecture Constraints"
Cohesion: 0.22
Nodes (9): ARC-SEC-001, ARC-SEC-002, ARC-SEC-003, ARC-SEC-004, ARC-SEC-005, ARC-SEC-006, ARC-SEC-007, ARC-SEC-008 (+1 more)

### Community 565 - "Architecture Constraints"
Cohesion: 0.22
Nodes (9): ARC-OPS-001, ARC-OPS-002, ARC-OPS-003, ARC-OPS-004, ARC-OPS-005, ARC-OPS-006, ARC-OPS-007, ARC-OPS-008 (+1 more)

### Community 566 - "CandidateEntity"
Cohesion: 0.23
Nodes (11): CandidateEntity, Two open intervals (no recorded active_from/active_to on either side) default…, Only the non-sensitive attributes score_candidate is allowed to see, per the…, _temporal_compatibility(), build_dataset(), _entity(), LabelledPair, UUID (+3 more)

### Community 567 - "audit_queries.py"
Cohesion: 0.24
Nodes (13): AuditEventResponse, AuditListResponse, _decode(), list_audit_events(), AuthDep, BaseModel, datetime, SessionDep (+5 more)

### Community 568 - "Phase 7 Personal Intelligence Design"
Cohesion: 0.22
Nodes (8): Decision 1 (approval gate): per-domain schema/retention, Decision 2 (approval gate): privacy impact assessment, Decision 3 (approval gate): encryption fields, Decision 4 (approval gate): high-stakes safety rubric, Outcome, Phase 7 Personal Intelligence Design, Task 1 scope for this activation, Why this isn't a green field

### Community 569 - "Phase 8 Multi-user Workspaces Design"
Cohesion: 0.22
Nodes (8): Decision 1 (approval gate): identity migration, Decision 2 (approval gate): complete authorization matrix, Decision 3 (approval gate): invitation verification, Decision 4 (approval gate): revocation propagation SLO, Outcome, Phase 8 Multi-user Workspaces Design, Task 1 scope for this activation, Why this isn't a green field

### Community 570 - "0055_phase4_expl_item_prompt_v3.py"
Cohesion: 0.36
Nodes (7): _canonical_hash(), downgrade(), _prompt_versions_table(), Any, TableClause, Activate attention.explain_item.v3: a fourth prompt attempt for a newly…, upgrade()

### Community 571 - "0063_phase8_authz_visibility.py"
Cohesion: 0.46
Nodes (7): _add_owner_and_visibility(), _add_visibility(), downgrade(), _drop_owner_and_visibility(), _drop_visibility(), Phase 8 Task 3: schema-wide `owner_id`/`visibility` migration and the…, upgrade()

### Community 572 - "Domain Ownership"
Cohesion: 0.25
Nodes (8): Communication, Domain Ownership, Engineering, Executive Intelligence, Knowledge Platform, Personal OS, Planning, Platform

### Community 573 - "Architecture Constraints"
Cohesion: 0.25
Nodes (8): ARC-DATA-001, ARC-DATA-002, ARC-DATA-003, ARC-DATA-004, ARC-DATA-005, ARC-DATA-006, ARC-DATA-007, Architecture Constraints

### Community 574 - "Security Principles"
Cohesion: 0.25
Nodes (8): SEC-001, SEC-002, SEC-003, SEC-004, SEC-005, SEC-006, SEC-007, Security Principles

### Community 575 - "adapters.py"
Cohesion: 0.26
Nodes (10): AdapterAlreadyRegistered, AdapterCategoryInvalid, ValueError, Connector-independent action-adapter contract and in-process registry…, Raised by `AdapterRegistry.register` when `adapter_id` is already taken -- a…, Raised by `AdapterRegistry.register` when `high_impact_categories` contains a…, LocalCreateNoteAdapter, LocalSendTestNotificationAdapter (+2 more)

### Community 576 - "Phase 10 Gmail Connector Implementation Plan"
Cohesion: 0.20
Nodes (9): Phase 10 Gmail Connector Implementation Plan, Task 1 -- OAuth framework extension, Gmail connector skeleton, internal allowlist, Task 2 -- Backfill, incremental sync and entity linking, Task 3 -- Deterministic attention integration, Task 4 -- `recommendations` create-path extension, Task 5 -- AI-runtime action-detection tool, Task 6 -- On-demand thread reading and caching, Task 7 -- Consent revocation cascade (+1 more)

### Community 577 - "GitLab Self-Managed Instance Support Implementation Plan"
Cohesion: 0.25
Nodes (7): GitLab Self-Managed Instance Support Implementation Plan, Global Constraints, Task 1: Credential parsing and SSRF host guard (pure, no HTTP), Task 2: Wire the parsed host through `GitLabAdapter`, Task 3: `gitlab.add_note` write action host-awareness, Task 4: Documentation and frontend hint text, Task 5: Full verification pass

### Community 578 - "gmail-panel-states.mjs"
Cohesion: 0.36
Nodes (7): emailDomain, gmailConnector(), iso(), now, run(), seedEmailRecommendation, seedOtherRecommendation

### Community 579 - "_insert_meeting_with_participant"
Cohesion: 0.29
Nodes (10): _insert_meeting_with_participant(), _meeting_prep_response(), `meeting.prep_summary`'s own declared per-model-call timeout (40.0s,…, `TASK_PORTS["meeting.prep_summary"].reflection_prompt_id` is `None` -- the…, `meeting.prep_summary`'s own required input: a meeting with at least one…, test_execute_run_meeting_prep_summary_happy_path_persists_completed_run(), test_execute_run_meeting_prep_summary_never_triggers_reflection(), test_execute_run_meeting_prep_summary_passes_its_own_40s_timeout_to_the_adapter() (+2 more)

### Community 580 - "_deterministic_alias_match"
Cohesion: 0.29
Nodes (8): _alias_overlap(), _deterministic_alias_match(), _jaccard(), _name_similarity(), _neighbor_overlap(), _normalize(), ENTITY-RESOLUTION-CONTRACT.md's match hierarchy levels 2-4 collapsed into one…, _trigrams()

### Community 581 - "._request_with_rate_limit_retry"
Cohesion: 0.38
Nodes (4): _is_rate_limited(), Response, Mirrors `github_adapter.py`'s identically-named method -- one bounded wait, one…, Fetches one message's full body (`gmail.readonly`, `format= full`), encrypts…

### Community 582 - "0029_phase4_prompt_tool_versions.py"
Cohesion: 0.33
Nodes (5): _canonical_hash(), Any, Create prompt_versions and tool_definitions for Phase 4 AI Runtime Task 2.…, Mirrors ``ecc.domains.ai_runtime.prompts.compute_template_hash``/…, upgrade()

### Community 583 - "0033_phase4_reflection.py"
Cohesion: 0.33
Nodes (5): _canonical_hash(), Any, First-slice Reflection Engine for Phase 4 AI Runtime, attention.explain_item…, Mirrors `0029_phase4_prompt_tool_versions.py`'s `_canonical_hash` /…, upgrade()

### Community 584 - "0034_phase4_meeting_prep.py"
Cohesion: 0.33
Nodes (5): _canonical_hash(), Any, Wire Phase 3's meeting_prep.py "Optional enrichment" flag on (MEETING-PREP-…, Mirrors `0029_phase4_prompt_tool_versions.py`'s `_canonical_hash` /…, upgrade()

### Community 585 - "0035_phase4_meeting_eval.py"
Cohesion: 0.29
Nodes (4): _assign_deterministic_ids(), Any, Seed the `meeting.prep_summary` evaluation dataset. `meeting.prep_summary` is…, Verbatim copy of `tests/fixtures/phase4_evaluation_meeting_prep.py`'s identical…

### Community 586 - "0057_phase7_insight.py"
Cohesion: 0.33
Nodes (5): _canonical_hash(), Any, Phase 7 Task 5 part 2: register `personal.generate_insight`, the third AI task…, Mirrors `0034_phase4_meeting_prep.py`'s identical helper (in turn mirroring…, upgrade()

### Community 587 - "0072_phase10_email_detect_action.py"
Cohesion: 0.33
Nodes (5): _canonical_hash(), Any, Phase 10 Task 5: register `email.detect_action`, the fourth AI task type this…, Mirrors `0057_phase7_insight.py`'s identical helper (in turn mirroring…, upgrade()

### Community 588 - "Architecture Constraints"
Cohesion: 0.29
Nodes (7): ARC-HAE-001, ARC-HAE-002, ARC-HAE-003, ARC-HAE-004, ARC-HAE-005, ARC-HAE-006, Architecture Constraints

### Community 589 - "Design Goals"
Cohesion: 0.29
Nodes (7): Design Goals, HAE-001, HAE-002, HAE-003, HAE-004, HAE-005, HAE-006

### Community 590 - "Priority Signals"
Cohesion: 0.29
Nodes (7): Importance, Opportunity, Priority Signals, Relationship Impact, Risk, Strategic Value, Urgency

### Community 591 - "Architecture Constraints"
Cohesion: 0.29
Nodes (7): ARC-UX-001, ARC-UX-002, ARC-UX-003, ARC-UX-004, ARC-UX-005, ARC-UX-006, Architecture Constraints

### Community 592 - "Design Goals"
Cohesion: 0.29
Nodes (7): Design Goals, UX-001, UX-002, UX-003, UX-004, UX-005, UX-006

### Community 593 - "Durable Engineering Evidence Policy"
Cohesion: 0.29
Nodes (6): Accepted evidence, Changelog, Durable Engineering Evidence Policy, Gate ownership, Not durable evidence, Purpose

### Community 594 - "Phase 2 Deployment Runbook (Delta from Phase 1)"
Cohesion: 0.29
Nodes (6): Backup and restore, Deploy, Optional: enabling embeddings and hybrid retrieval (Task 7), Phase 2 Deployment Runbook (Delta from Phase 1), Rollback, What's new

### Community 595 - "seed_large_meeting_history"
Cohesion: 0.38
Nodes (6): Engine, datetime, UUID, Bulk-seeding helpers for Phase 3 Task 7's meeting-prep performance test. Unlike…, Seed enough timeline/commitment/note history behind one participant entity for…, seed_large_meeting_history()

### Community 596 - "multi-identity-collaboration-lifecycle.mjs"
Cohesion: 0.38
Nodes (6): createCollaborationStore(), ALICE, BOB, iso(), now, run()

### Community 597 - "check_ollama_models.py"
Cohesion: 0.43
Nodes (6): _database_url(), _installed_models(), main(), Verify the local Ollama server has every model this deployment's…, Returns None if the Ollama server is unreachable (distinct from an empty…, _required_models()

### Community 598 - "get_settings"
Cohesion: 0.12
Nodes (16): CsrfHeader, Request, SessionCookie, SessionDep, require_auth_context(), require_csrf(), get_settings(), bootstrap_page() (+8 more)

### Community 599 - "_GmailHistoryCursor"
Cohesion: 0.25
Nodes (5): _GmailHistoryCursor, Parses from, and serializes back to, the single opaque `str`…, `stuck_offset <= 0` (nothing of `stuck_record_id` has actually been processed…, A whole `history[]` record (`record_id`) just fully completed -- advances the…, This call is returning partway through `record_id` (or, when `record_id` is…

### Community 600 - "0036_phase4_meeting_prep_timeout.py"
Cohesion: 0.47
Nodes (5): downgrade(), TableClause, Raise meeting.prep_summary's per-model-call timeout from 20s to 25s. Phase 4…, _routing_policies_table(), upgrade()

### Community 601 - "0037_phase4_meeting_timeout2.py"
Cohesion: 0.47
Nodes (5): downgrade(), TableClause, Raise meeting.prep_summary's per-model-call timeout from 25s to 32s, and its…, _routing_policies_table(), upgrade()

### Community 602 - "0052_phase4_meeting_timeout3.py"
Cohesion: 0.47
Nodes (5): downgrade(), TableClause, Raise meeting.prep_summary's per-model-call timeout from 32s to 40s, its total…, _routing_policies_table(), upgrade()

### Community 603 - "0077_phase4_expl_item_timeout.py"
Cohesion: 0.47
Nodes (5): downgrade(), TableClause, Raise attention.explain_item's per-model-call timeout from 20s to 30s and its…, _routing_policies_table(), upgrade()

### Community 604 - "Phase 1 Implementation Status"
Cohesion: 0.25
Nodes (8): Capability status, Change policy, Contract traceability, Delivery sequence, Overall status, Phase 1 Implementation Status, Quality gates, Remaining Phase 1 exit work

### Community 605 - "Architectural Goals"
Cohesion: 0.33
Nodes (6): Architectural Goals, GOAL-001, GOAL-002, GOAL-003, GOAL-004, GOAL-005

### Community 606 - "Architectural Goals"
Cohesion: 0.33
Nodes (6): Architectural Goals, DP-001, DP-002, DP-003, DP-004, DP-005

### Community 607 - "Operational Philosophy"
Cohesion: 0.33
Nodes (6): Operational Philosophy, OPS-001, OPS-002, OPS-003, OPS-004, OPS-005

### Community 608 - "._reject_private_host"
Cohesion: 0.29
Nodes (6): _is_private_address(), Connect-time SSRF guard, called once from `authorize()` -- never from a sync…, `ipaddress.ip_address(...).is_private` does not cover `100.64.0.0/10` (RFC 6598…, test_is_private_address_allows_public_addresses(), test_is_private_address_flags_loopback_link_local_and_rfc1918(), test_is_private_address_flags_rfc6598_cgnat_range()

### Community 609 - "Team suggestions: create team inline"
Cohesion: 0.33
Nodes (5): Context, Frontend design, Scope, Team suggestions: create team inline, Testing

### Community 610 - "_reset_mutation_rate_limiters"
Cohesion: 0.40
Nodes (5): fixture, The mutation rate limiters in ecc.http_security are module-level singletons…, ecc.observability._outbox_backlog_count caches its result for…, _reset_mutation_rate_limiters(), _reset_outbox_backlog_cache()

### Community 611 - "_normalize_email"
Cohesion: 0.40
Nodes (3): _EmailPasswordField, _normalize_email(), field_validator

### Community 613 - "Runtime Philosophy"
Cohesion: 0.40
Nodes (5): RP-001, RP-002, RP-003, RP-004, Runtime Philosophy

### Community 614 - "State Management"
Cohesion: 0.40
Nodes (5): Cached Domain State, Live State, Session State, State Management, UI State

### Community 615 - "Phase Evolution"
Cohesion: 0.40
Nodes (5): Phase 0, Phase 1, Phase 2, Phase 3, Phase Evolution

### Community 616 - "Team suggestions: create team inline — Implementation Plan"
Cohesion: 0.40
Nodes (4): Global Constraints, Post-plan check, Task 1: Add create-and-confirm mutation and inline-create UI to `SuggestionRow`, Team suggestions: create team inline — Implementation Plan

### Community 617 - "GitLab suggested team name: full path, not immediate subgroup"
Cohesion: 0.40
Nodes (4): Change, Context, GitLab suggested team name: full path, not immediate subgroup, Scope

### Community 618 - "attention-queue.mjs"
Cohesion: 0.40
Nodes (4): deferredItem, item, run(), waitingLink

### Community 619 - "engineering-connector-states.mjs"
Cohesion: 0.70
Nodes (4): connector(), iso(), now, run()

### Community 620 - "knowledge-resolution.mjs"
Cohesion: 0.40
Nodes (4): run(), seedCandidate, sourceEntity, targetEntity

### Community 621 - "recommendation-terminals.mjs"
Cohesion: 0.40
Nodes (4): executed, failed, rejectedButFilteredOut, run()

### Community 622 - "gmail_revocation_context"
Cohesion: 0.14
Nodes (14): _cleanup_workspace(), gmail_revocation_context(), fixture, MockTransport, MonkeyPatch, Loop 2 round 4 review finding: `domains.py:_disable_domain` calls…, Loop 2 round 5 review finding: the round-4 concurrency test above only covers…, Blocks inside `disconnect()` until released -- mirrors `test_… (+6 more)

### Community 623 - "test_client_host_ignores_forwarded_for_when_trusted_proxy_count_is_zero"
Cohesion: 0.40
Nodes (5): _forwarded_request(), Request, Default posture: an unconfigured deployment must never trust a client-supplied…, test_client_host_ignores_forwarded_for_when_trusted_proxy_count_is_zero(), test_client_host_reads_forwarded_for_when_trusted_proxy_count_configured()

### Community 659 - "Deployment Strategy"
Cohesion: 0.50
Nodes (4): Deployment Strategy, Developer, Enterprise, Personal Production

### Community 660 - "attention-explanation.mjs"
Cohesion: 0.67
Nodes (3): buildRun(), item, run()

### Community 661 - "automation-lifecycle.mjs"
Cohesion: 0.67
Nodes (3): iso(), now, run()

### Community 662 - "conflict-audit-keyboard.mjs"
Cohesion: 0.50
Nodes (3): auditCorpus, run(), seedRisk

### Community 663 - "engineering-lifecycle.mjs"
Cohesion: 0.67
Nodes (3): iso(), now, run()

### Community 664 - "knowledge-entities.mjs"
Cohesion: 0.50
Nodes (3): run(), seedEntity, seedProject

### Community 665 - "personal-domain-lifecycle.mjs"
Cohesion: 0.67
Nodes (3): iso(), now, run()

### Community 667 - "Product KPI Contract"
Cohesion: 0.29
Nodes (7): Activation rule, AI and recommendation quality, Existing manual gates are authoritative, Performance, Product KPI Contract, Product outcomes, User experience

### Community 691 - "test_settings_is_actually_constructible_in_development_with_no_session_secret"
Cohesion: 0.67
Nodes (3): Path, Regression test for a real CI failure: pydantic-settings validates a field's…, test_settings_is_actually_constructible_in_development_with_no_session_secret()

### Community 693 - "Phase 5 Dogfood Validation Record"
Cohesion: 0.29
Nodes (7): Approved success thresholds, Closing the gate, Daily-use log, Judgment call: a 14-day window, split into two 7-day stages (reviewer should double-check), Phase 5 Dogfood Validation Record, Purpose, Status

### Community 718 - "Phase 6 Connector Recovery Runbook"
Cohesion: 0.29
Nodes (6): Detection and containment, Escalation, Evidence to retain, Partial sync and provider failure, Phase 6 Connector Recovery Runbook, Supported operator actions

### Community 719 - "test_github_add_issue_comment_rejects_connector_account_in_different_workspace"
Cohesion: 0.29
Nodes (7): _cleanup_workspace(), fixture, The adapter-level `..._rejects_connector_account_in_different_workspace` tests…, `repository_id=uuid4()` here is a never-synced placeholder, not a real…, test_github_add_issue_comment_rejects_connector_account_in_different_workspace(), test_load_credential_rejects_connector_account_in_different_workspace(), write_actions_test_context()

### Community 720 - "_publish_workflow"
Cohesion: 0.33
Nodes (6): _action_step(), _json_response(), _publish_workflow(), Any, Response, WorkflowVersion

### Community 721 - "phase4_evaluation_meeting_prep.py"
Cohesion: 0.40
Nodes (4): _assign_deterministic_ids(), Any, Versioned, checked-in labelled dataset for `meeting.prep_summary`'s evaluation…, Every row's `"id"` placeholder (`None` in the literal below) is filled in here,…

### Community 722 - "test_execute_run_repair_retry_provider_error_fails_run_gracefully"
Cohesion: 0.40
Nodes (5): _first_ok_then_failing_adapter(), Response, First `.generate()` call returns a schema-invalid 200 (triggering the bounded…, A transport-level failure on the *repair retry itself* (as opposed to the well-…, test_execute_run_repair_retry_provider_error_fails_run_gracefully()

### Community 723 - "test_preview_only_never_dispatches_even_after_an_approved_digest"
Cohesion: 0.40
Nodes (5): parametrize, Neither `preview_only` nor `per_run` lets a `bounded` step dispatch unattended…, The central guarantee, asserted on the adapter's own call counter -- not on a…, test_preview_only_and_per_run_modes_require_approval_for_bounded_step(), test_preview_only_never_dispatches_even_after_an_approved_digest()

## Knowledge Gaps
- **1655 isolated node(s):** `SERIOUS_IMPACTS`, `defaultDashboardSections`, `defaultSearchCorpus`, `defaultAuditCorpus`, `PERSONAL_CLASSIFICATION_BY_DOMAIN` (+1650 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **358 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `README (Executive Command Center)` connect `README (Executive Command Center)` to `Phase 1 Completion Design`, `Architecture Ch.6: Integration Platform`, `PHASE-000-repository-foundation.md`, `Phase 0 Backup and Restore`, `phases/README.md`, `Core entities`, `Dev Bootstrap Script`, `adr/README.md`, `RFC-000/RFC-003: Governance, Design Principles & Setup`, `RFC-002: Engineering Philosophy`, `main.py`, `ROADMAP.md`, `RFC-001: Product Definition`?**
  _High betweenness centrality (0.177) - this node is a cross-community bridge._
- **Why does `AuthContext` connect `AuthContext` to `recommendation_mutations.py`, `risks.py`, `Task API & Contract Tests`, `commitments.py`, `notes.py`, `test_ai_runtime_evaluation_postgres.py`, `Calendar Events API`, `Meeting Scheduling API`, `test_attention_capacity_postgres.py`, `attention.py`, `ActionAdapter`, `test_ai_runtime_runtime_postgres.py`, `test_gmail_revocation_postgres.py`, `dashboard_briefs.py`, `auth.py`, `prompts.py`, `test_automation_triggers_postgres.py`, `list_plans`, `ConnectorAccountContext`, `gmail_adapter.py`, `test_engineering_authz_postgres.py`, `scheduler.py`, `automation/policy.py`, `recommendation_targets.py`, `kill_switches.py`, `runs.py`, `accounts.py`, `relationships.py`, `waiting.py`, `decisions_incidents.py`, `_get`, `entities.py`, `test_automation_scheduler_postgres.py`, `test_automation_runs_postgres.py`, `delegations.py`, `entity_operations.py`, `worker.py`, `OllamaAdapter`, `planning.py`, `test_automation_kill_switches_postgres.py`, `meeting_prep.py`, `test_ai_runtime_meeting_prep_evaluation_postgres.py`, `test_automation_worker_postgres.py`, `TaskCreate`, `CandidateEntity`, `execute_run`, `_chained_graph`, `claims.py`, `record_idempotency_conflict`, `move_block`, `test_personal_insight_tools_postgres.py`, `EchoInput`, `_publish_workflow`, `score_candidate`, `capacity.py`, `entities_mutations.py`, `test_ai_runtime_personal_insight_evaluation_postgres.py`, `risk_reviews.py`, `AdapterRegistry`, `evidence.py`, `test_preview_only_never_dispatches_even_after_an_approved_digest`, `get_settings`, `gmail_threads.py`, `test_ai_runtime_email_detect_action_evaluation_postgres.py`, `test_ai_runtime_tools_postgres.py`, `_normalize_email`, `propose_plan`, `gmail_revocation_context`, `test_ai_runtime_evaluation_live_ollama.py`, `create_run`, `domains.py`?**
  _High betweenness centrality (0.096) - this node is a cross-community bridge._
- **Why does `create_identity()` connect `create_identity` to `Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]`, `test_personal_travel_postgres.py`, `test_knowledge_retrieval_performance_postgres.py`, `test_automation_triggers_http_postgres.py`, `test_dashboard_briefs_postgres.py`, `test_knowledge_entity_operations_performance_postgres.py`, `test_personal_learning_postgres.py`, `attention.py`, `test_automation_triggers_postgres.py`, `UUID`, `test_identity_person_organizations_postgres.py`, `Recommendation Postgres Integration Tests`, `DatadogAdapter`, `test_calendar_meetings_postgres.py`, `test_knowledge_resolution_performance_postgres.py`, `test_task_postgres.py`, `GmailAdapter`, `test_engineering_gitlab_sync_postgres.py`, `test_note_postgres.py`, `test_gmail_connector_sync_postgres.py`, `UUID`, `test_automation_worker_postgres.py`, `test_sync_backfill_writes_repositories_then_incremental_only_writes_newer`, `test_engineering_write_actions_postgres.py`, `execute_run`, `JiraAdapter`, `test_automation_compensation_postgres.py`, `test_engineering_metrics_postgres.py`, `test_engineering_connectors_postgres.py`, `test_automation_workflows_postgres.py`, `gmail_revocation_context`, `test_github_add_issue_comment_rejects_connector_account_in_different_workspace`, `test_engineering_decisions_incidents_postgres.py`, `test_engineering_query_endpoints_postgres.py`, `test_ai_runtime_evaluation_postgres.py`, `test_ai_runtime_routing_postgres.py`, `test_ai_runtime_runtime_postgres.py`, `test_attention_capacity_postgres.py`, `test_gmail_revocation_postgres.py`, `TransientAdapterError`, `test_engineering_authz_postgres.py`, `test_automation_simulate_postgres.py`, `test_gmail_action_detection_sync_postgres.py`, `test_engineering_github_sync_postgres.py`, `test_attention_meeting_prep_postgres.py`, `test_ai_runtime_versioning_postgres.py`, `test_attention_planning_postgres.py`, `test_automation_approvals_postgres.py`, `test_automation_scheduler_postgres.py`, `test_identity_invitations_postgres.py`, `test_automation_runs_postgres.py`, `test_engineering_team_suggestions_postgres.py`, `test_mutation_brief_performance_postgres.py`, `test_knowledge_entity_operations_postgres.py`, `test_automation_kill_switches_postgres.py`, `test_observability.py`, `test_ai_runtime_meeting_prep_evaluation_postgres.py`, `test_automation_policy_postgres.py`, `test_identity_accounts_postgres.py`, `test_gmail_threads_postgres.py`, `test_identity_membership_removal_postgres.py`, `test_personal_domains_postgres.py`, `test_attention_email_awaiting_reply_postgres.py`, `test_knowledge_embeddings_postgres.py`, `test_collaboration_delegations_postgres.py`, `test_personal_insight_tools_postgres.py`, `test_knowledge_entities_postgres.py`, `test_attention_risk_reviews_postgres.py`, `test_platform_notifications_postgres.py`, `test_ai_runtime_personal_insight_evaluation_postgres.py`, `test_knowledge_relationships_postgres.py`, `get_ollama_adapter`, `database.py`, `gmail_threads.py`, `test_ai_runtime_email_detect_action_evaluation_postgres.py`, `test_knowledge_resolution_postgres.py`, `test_ai_runtime_tools_postgres.py`, `test_knowledge_retrieval_postgres.py`, `test_personal_finance_postgres.py`, `test_personal_health_postgres.py`, `config.py`, `test_knowledge_claims_postgres.py`, `test_personal_relationships_postgres.py`, `test_ai_runtime_evaluation_live_ollama.py`, `test_knowledge_retrieval_benchmark_postgres.py`, `test_personal_grants_postgres.py`, `test_evidence_postgres.py`, `test_knowledge_resolution_visibility_postgres.py`?**
  _High betweenness centrality (0.041) - this node is a cross-community bridge._
- **Are the 364 inferred relationships involving `AuthContext` (e.g. with `EmailDetectActionEvaluationExample` and `EmailDetectActionMessageExample`) actually correct?**
  _`AuthContext` has 364 INFERRED edges - model-reasoned connections that need verification._
- **Are the 49 inferred relationships involving `ConnectorAccountContext` (e.g. with `ConnectorAccount` and `ConnectorAccountListResponse`) actually correct?**
  _`ConnectorAccountContext` has 49 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `GmailAdapter` (e.g. with `OllamaAdapter` and `AdapterAuthorizationError`) actually correct?**
  _`GmailAdapter` has 13 INFERRED edges - model-reasoned connections that need verification._
- **Are the 73 inferred relationships involving `OllamaAdapter` (e.g. with `EmailDetectActionEvaluationExample` and `EmailDetectActionMessageExample`) actually correct?**
  _`OllamaAdapter` has 73 INFERRED edges - model-reasoned connections that need verification._