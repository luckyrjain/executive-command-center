# Graph Report - .  (2026-07-16)

## Corpus Check
- 222 files · ~94,480 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1360 nodes · 2798 edges · 147 communities (80 shown, 67 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 273 edges (avg confidence: 0.78)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Recommendation Lifecycle API
- Risk Tracking API
- Task API & Contracts
- Phase 2 Knowledge & Phase 4 AI Runtime Docs
- Commitment Tracking API
- Frontend Dashboard & Panels
- Note-Taking API
- Foundation Specs (RFCs, ADRs, Setup)
- Architecture Chapters & Domain Model
- Frontend Package Dependencies
- Calendar Events API
- Meeting Scheduling API
- Phase Roadmap & Data Models
- Phase 3 Attention Engine Specs
- Attention Scoring API
- Dev Bootstrap & Type-Safety Tooling
- Morning Brief / Dashboard API
- Auth, Config & Workspace Isolation
- Phase 6 Connector Contracts
- Frontend TypeScript Config
- Search API
- Phase 7 Personal Domain Privacy
- AI Coding Standards (STD-001)
- Frontend Work Action Center
- Health Checks & Response Middleware
- Phase 5 Automation Data Model
- Constitution Articles (SPEC-000)
- Audit Log Query API
- Phase 6 Engineering Data Model
- Audit Logging Infrastructure
- Phase 7 Insight Contract Guardrails
- Recommendation Postgres Integration Tests
- Phase 8 Delegation Contract
- Dev Bootstrap Script
- Phase 9 Compliance Contract
- Phase 0 Security Baseline
- In-Process Event Bus
- Phase 5 Approval Policy
- Phase 8 API Schemas
- Phase 9 Tenancy API Schemas
- Dev Bootstrap Backend Route
- Phase 4-7 Implementation Status Slices
- Phase 5/7 UX Required States
- Phase 5 Execution Contract & Durability
- Phase 8/9 UX States & Templates
- Commitment Postgres Integration Tests
- Note Postgres Integration Tests
- Phase 8 Permission Contract & Tests
- Root Package Manifest
- Dashboard Brief Postgres Tests
- Risk/Attention Postgres Tests
- Task Postgres Integration Tests
- Phase 9 Tenancy Test Plan
- Constitutional Governance Templates
- Calendar/Meeting Postgres Tests
- Search Audit Postgres Tests
- Frontend E2E Test Runner
- Search Performance Postgres Tests
- Phase 8/9 Implementation Status
- Cross-RFC Principle: Local-First
- Cross-RFC Principle: Human Authority
- Cross-RFC Principle: Explainability
- Cross-RFC Principle: Permanent Memory
- Frontend Entrypoint & Workspace Config
- Calendar Domain Init
- Communication Domain Init
- Governance Domain Init
- Platform Domain Init
- Backend App Init
- Document Control & Product Definition
- Spec-Code Sync & PR Template
- Cross-RFC Principle: Attention as Product
- AI-Specific Coding Rules
- Frontend Vite Config
- Backup Script
- Scripts Init
- Restore Script
- Verify-Restore Script
- Docker Compose Frontend Service
- Stop-and-Ask Protocol
- AI Contributions Policy
- RFC-000 Documentation Fitness Functions
- GOV-001 Specification Authoritative
- GOV-002 Decisions Documented
- GOV-003 Traceability
- GOV-004 Documentation Updates
- GOV-005 Version Control
- RFC-000 Specification Drift
- RFC-000 Specification Freeze
- RFC-000 Stop-and-Ask Rule
- RFC-001 CTO Persona
- RFC-001 Director of Engineering Persona
- RFC-001 Founder Persona
- RFC-001 Functional Requirements
- RFC-001 Non-Functional Requirements
- Principle: Progressive Automation
- RFC-001 Product Maturity Model
- RFC-001 VP Engineering Persona
- EP-002 Simplicity Wins
- EP-003 Delete Before Add
- EP-004 AI Is a Junior Engineer
- EP-009 Modular Architecture
- EP-010 Evolution Over Perfection
- RFC-002 Technical Debt Philosophy
- RFC-003 Design Heuristics
- DP-005 Progressive Disclosure
- DP-006 Calm Interfaces
- DP-007 Relationships Over Documents
- DP-008 Context Before Content
- DP-010 AI Should Feel Invisible
- DP-011 Executive Dashboard First
- DP-012 Reduce Decisions
- DP-013 Search Is a Backup
- DP-014 Time Is a First-Class Entity
- DP-015 Design for Years
- DP-016 Small Surfaces
- DP-018 Consistency Over Novelty
- DP-019 Defaults Matter
- DP-020 Every Screen Must Earn Its Place
- RFC-004 Architectural Rules
- RFC-005 Authentication Baseline
- RFC-005 Phase 0 Architecture Baseline
- Roadmap Delivery Sequence
- CI: Backend Job
- CI: Containers Job
- CI: Frontend Job
- CI: Security Job
- CI: Phase 1 Acceptance Contract Job
- CI: Accessibility Smoke Job
- CI: Backup/Restore Job
- Root Repo Package

## God Nodes (most connected - your core abstractions)
1. `AuthContext` - 99 edges
2. `STD-001 Repository Standards` - 24 edges
3. `get_settings()` - 23 edges
4. `RFC-004: System Architecture (chapter index)` - 23 edges
5. `_lifecycle()` - 22 edges
6. `_transition()` - 22 edges
7. `update_note()` - 22 edges
8. `_lifecycle_task()` - 21 edges
9. `_lifecycle()` - 20 edges
10. `_mutate_commitment()` - 19 edges

## Surprising Connections (you probably didn't know these)
- `Executive Command Center README` --semantically_similar_to--> `Golden Rule: if it is not documented in the current phase, it does not get implemented`  [INFERRED] [semantically similar]
  README.md → docs/00-document-control.md
- `test_note_body_bounds_are_enforced_by_schema()` --calls--> `NoteCreate`  [INFERRED]
  tests/test_note_contract.py → backend/ecc/domains/knowledge/notes.py
- `PR18 Bootstrap Dev Test Session Report` --semantically_similar_to--> `PR7 Mypy Type-Check Report`  [INFERRED] [semantically similar]
  pr18-bootstrap-report.txt → pr7-mypy.txt
- `test_commitment_create_defaults()` --calls--> `CommitmentCreate`  [INFERRED]
  tests/test_commitment_contract.py → backend/ecc/domains/communication/commitments.py
- `test_detected_commitment_requires_evidence()` --calls--> `CommitmentCreate`  [INFERRED]
  tests/test_commitment_contract.py → backend/ecc/domains/communication/commitments.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Foundation Governance Set: RFC-000 through RFC-005 explicitly form the completed foundational governance specification** — docs_rfc_000_rfc_000, docs_rfc_001_rfc_001, docs_rfc_002_rfc_002, docs_rfc_003_rfc_003, docs_rfc_004_rfc_004, docs_rfc_005_rfc_005 [EXTRACTED 1.00]
- **Phase 0 Accepted Architecture Decisions: five ADRs grouped together as the accepted Phase 0 decision record** — docs_adr_adr_0001_repository_layout_repository_layout, docs_adr_adr_0002_local_first_architecture_local_first_architecture, docs_adr_adr_0003_knowledge_platform_pkos_pkos, docs_adr_adr_0004_ai_runtime_ai_runtime, docs_adr_adr_0005_event_bus_event_bus [INFERRED 0.85]
- **Local-First Principle repeated across product, engineering, design and architecture specification layers without direct cross-citation** — docs_rfc_001_principle_1_local_first, docs_rfc_002_ep_005_local_first, docs_rfc_003_dp_002_local_first, docs_adr_adr_0002_local_first_architecture_local_first_architecture [INFERRED 0.85]
- **RFC-004 System Architecture Chapters** — docs_architecture_chapter_01_vision_chapter1, docs_architecture_chapter_02a_core_platform_chapter2a, docs_architecture_chapter_02b_runtime_chapter2b, docs_architecture_chapter_03_ai_runtime_chapter3, docs_architecture_chapter_04_knowledge_platform_chapter4, docs_architecture_chapter_05_attention_engine_chapter5, docs_architecture_chapter_06_integration_platform_chapter6, docs_architecture_chapter_07_frontend_chapter7, docs_architecture_chapter_08_data_platform_chapter8, docs_architecture_chapter_09_security_chapter9, docs_architecture_chapter_10_operations_chapter10 [EXTRACTED 1.00]
- **Phase 0 Accepted ADR Decisions** — docs_adr_readme_adr_index, docs_adr_adr_0006_storage_strategy_storage_strategy_decision, docs_adr_adr_0007_model_router_model_router_decision, docs_adr_adr_0008_authentication_authentication_workspace_identity_decision, docs_adr_adr_0009_synchronization_connector_synchronization_decision, docs_adr_adr_0010_deployment_strategy_deployment_strategy_decision [EXTRACTED 1.00]
- **PKOS Data & API Governance System** — docs_domain_pkos_schema_pkos_schema, docs_domain_domain_model_domain_model, docs_domain_event_catalog_event_catalog, docs_domain_api_contracts_api_contracts, docs_architecture_chapter_08_data_platform_pkos [INFERRED 0.85]
- **Non-surveillance / work-not-people ranking invariant** — docs_phases_phase_review_non_surveillance_principle, docs_phases_phase_003_human_attention_engine_human_attention_engine, docs_phases_phase_006_engineering_workspace_engineering_workspace, docs_phases_phase_008_multi_user_multi_user [INFERRED 0.85]
- **Phase 1 recommendation lifecycle contract set** — docs_phases_phase_001_data_model_recommendation_lifecycle, docs_phases_phase_001_data_model_data_model, docs_phases_phase_001_api_schemas_api_schemas, docs_phases_phase_001_audit_contract_audit_contract, docs_phases_phase_001_test_plan_test_plan [EXTRACTED 1.00]
- **Phase 0-9 roadmap dependency chain** — docs_phases_phase_000_repository_foundation_repository_foundation, docs_phases_phase_001_executive_dashboard_mvp_executive_dashboard_mvp, docs_phases_phase_002_knowledge_platform_knowledge_platform, docs_phases_phase_003_human_attention_engine_human_attention_engine, docs_phases_phase_004_ai_runtime_ai_runtime, docs_phases_phase_005_automation_automation, docs_phases_phase_006_engineering_workspace_engineering_workspace, docs_phases_phase_007_personal_intelligence_personal_intelligence, docs_phases_phase_008_multi_user_multi_user, docs_phases_phase_009_enterprise_enterprise [EXTRACTED 1.00]
- **Phase 2 Core Provenance-Linked Knowledge Records** — docs_phases_phase_002_data_model_knowledge_entities, docs_phases_phase_002_data_model_entity_aliases, docs_phases_phase_002_data_model_knowledge_claims, docs_phases_phase_002_data_model_relationships, docs_phases_phase_002_data_model_source_refs [INFERRED 0.85]
- **Phase 3 Deterministic Planning Pipeline** — docs_phases_phase_003_data_model_capacity_profiles, docs_phases_phase_003_data_model_planning_constraints, docs_phases_phase_003_data_model_plans, docs_phases_phase_003_data_model_plan_blocks, docs_phases_phase_003_planning_contract_deterministic_planning_order [INFERRED 0.85]
- **Phase 4 AI Model and Policy Governance Gate** — docs_phases_phase_004_data_model_routing_policies, docs_phases_phase_004_data_model_evaluation_runs, docs_phases_phase_004_evaluation_contract_promotion_blocking [INFERRED 0.85]
- **Durable Execution Contract Mechanism** — docs_phases_phase_005_execution_contract_durable_checkpointing, docs_phases_phase_005_execution_contract_action_digest_idempotency, docs_phases_phase_005_execution_contract_bounded_retry, docs_phases_phase_005_execution_contract_needs_review_state, docs_phases_phase_005_execution_contract_compensation [EXTRACTED 1.00]
- **No-Ranking Ethics Guardrail Across Contract, UX and Test Plan** — docs_phases_phase_006_delivery_intelligence_contract_no_engineer_scoring, docs_phases_phase_006_ux_states_no_rankings_rationale, docs_phases_phase_006_test_plan_ethics_checks [INFERRED 0.85]
- **Personal Insight Safety Constraint Set** — docs_phases_phase_007_insight_contract_no_diagnosis_prescription, docs_phases_phase_007_insight_contract_correlation_causation_guard, docs_phases_phase_007_api_schemas_no_diagnostic_language, docs_phases_phase_007_ux_states_calm_language_rationale [INFERRED 0.85]
- **Constitutional Document Governance Hierarchy** — docs_specifications_spec_000_doc, docs_standards_std_001_doc, docs_templates_adr_template_doc, docs_templates_rfc_template_doc, docs_templates_phase_template_doc [EXTRACTED 0.95]
- **Phase 8 Multi-user Authorization Design** — docs_phases_phase_008_permission_contract_authorization_evaluation, docs_phases_phase_008_permission_contract_deny_override_rule, docs_phases_phase_008_data_model_visibility_model, docs_phases_phase_008_test_plan_adversarial_tests [INFERRED 0.85]
- **Phase 9 Enterprise Tenant Isolation Design** — docs_phases_phase_009_tenancy_contract_tenant_scoping, docs_phases_phase_009_tenancy_contract_cross_tenant_404, docs_phases_phase_009_data_model_tenant_id_boundary, docs_phases_phase_009_test_plan_tenant_isolation_tests [INFERRED 0.85]
- **CI Verification Reports for executive-command-center PRs** — pr18_bootstrap_report_test_report, pr7_mypy_report, concept_dev_bootstrap_feature, concept_mypy_type_safety_gate [INFERRED 0.75]

## Communities (147 total, 67 thin omitted)

### Community 0 - "Recommendation Lifecycle API"
Cohesion: 0.08
Nodes (62): Any, datetime, Request, Session, UUID, record_event(), record_feedback(), ConfirmAction (+54 more)

### Community 1 - "Risk Tracking API"
Cohesion: 0.10
Nodes (52): alias, _archive_action(), archive_risk(), _get_row(), _load_cached(), _lock_idempotency(), Any, AuthDep (+44 more)

### Community 2 - "Task API & Contracts"
Cohesion: 0.12
Nodes (49): archive_task(), cancel_task(), complete_task(), create_task(), _decode_cursor(), _encode_cursor(), get_task(), _get_task_row() (+41 more)

### Community 3 - "Phase 2 Knowledge & Phase 4 AI Runtime Docs"
Cohesion: 0.05
Nodes (55): Evidence State Model, Phase 1 Feature Flags, Phase 1 UX States, Recommendation Publication Lifecycle, EvidenceRef Representation, KnowledgeEntity Representation, MatchExplanation Representation, Phase 2 Knowledge Platform API (+47 more)

### Community 4 - "Commitment Tracking API"
Cohesion: 0.14
Nodes (49): archive_commitment(), cancel_commitment(), _check_version(), CommitmentAction, CommitmentCreate, CommitmentLinks, CommitmentListResponse, CommitmentPatch (+41 more)

### Community 5 - "Frontend Dashboard & Panels"
Cohesion: 0.07
Nodes (40): api(), App(), DashboardItem, DashboardResponse, ErrorEnvelope, fetchDashboard(), fetchMorningBrief(), formatTime() (+32 more)

### Community 6 - "Note-Taking API"
Cohesion: 0.20
Nodes (40): AuthContext, archive_note(), _body_checksum(), _check_version(), create_note(), _decode_cursor(), _encode_cursor(), get_note() (+32 more)

### Community 7 - "Foundation Specs (RFCs, ADRs, Setup)"
Cohesion: 0.09
Nodes (39): docker-compose backend service (read-only, no-new-privileges, health-checked), docker-compose.yml (Postgres, backend, frontend local stack), docker-compose postgres service (postgres:18.0), Golden Rule: if it is not documented in the current phase, it does not get implemented, ADR-0001 Repository Layout: monorepo with docs/backend/frontend/packages/tests/scripts/infrastructure/.github; rejected multi-repo (fragments context) and framework-first folders (weakens domain ownership), ADR-0002 Local-First Architecture: core workflows, storage, search, scheduling and default AI path operate locally; cloud is an optional adapter, never mandatory; rejected SaaS-first and offline-cache-with-cloud-authority for conflicting with product principles / subordinating local operation, ADR-0003 PKOS (Personal Knowledge Operating System): canonical knowledge subsystem storing normalized entities, typed relationships, provenance, temporal validity; source artifacts remain immutable evidence, derived summaries/embeddings are replaceable projections; rejected vector-DB-as-primary-memory (no authoritative relationships) and notes-only model (no cross-domain reasoning), ADR-0004 AI Runtime: all model calls pass through a single AI Runtime (Model Router, prompt registry, structured-output validation, tool permission checks, evaluation hooks, audit logging); AI outputs are proposals until validated; rejected direct model calls per feature for fragmenting policy/evaluation/observability (+31 more)

### Community 8 - "Architecture Chapters & Domain Model"
Cohesion: 0.11
Nodes (40): ADR-0006: Storage Strategy, ADR-0007: Model Router, ADR-0008: Authentication and Workspace Identity, ADR-0009: Connector Synchronization, ADR-0010: Deployment Strategy, ADR Index, Chapter 1: Architectural Vision & System Context, RFC-004: System Architecture (+32 more)

### Community 9 - "Frontend Package Dependencies"
Cohesion: 0.05
Nodes (39): dependencies, react, react-dom, react-router, @tanstack/react-query, zustand, devDependencies, playwright (+31 more)

### Community 10 - "Calendar Events API"
Cohesion: 0.18
Nodes (36): archive_calendar_event(), CalendarEventAction, CalendarEventCreate, CalendarEventListResponse, CalendarEventPatch, CalendarEventResponse, create_calendar_event(), _decode_cursor() (+28 more)

### Community 11 - "Meeting Scheduling API"
Cohesion: 0.18
Nodes (36): archive_meeting(), _calendar_event(), create_meeting(), _decode_cursor(), _encode_cursor(), get_meeting(), _get_row(), _lifecycle() (+28 more)

### Community 12 - "Phase Roadmap & Data Models"
Cohesion: 0.17
Nodes (38): Modular Monolith Architecture, PKOS (Personal Knowledge Operating System) schema/repository, PHASE-000 Repository Foundation, Workspace Isolation Invariant, Phase 1 API Schemas, Phase 1 Audit Contract, Phase 1 Consistency Review Closure, Attention Items projection (+30 more)

### Community 13 - "Phase 3 Attention Engine Specs"
Cohesion: 0.09
Nodes (35): Attention Result Representation, Meeting Preparation API, Phase 3 Human Attention API, Planning API Surface, Deterministic Attention Score Formula, Excluded Attention Inputs, Human Attention Model, Attention Overrides (Pin/Dismiss/Defer/Restore) (+27 more)

### Community 14 - "Attention Scoring API"
Cohesion: 0.21
Nodes (28): AttentionAction, AttentionItem, AttentionList, defer_attention(), dismiss_attention(), _due_points(), _factor(), list_attention() (+20 more)

### Community 15 - "Dev Bootstrap & Type-Safety Tooling"
Cohesion: 0.10
Nodes (25): Local Dev Bootstrap Environment Guard, Mypy Static Type-Check CI Gate, ModuleType, Path, PR18 Bootstrap Dev Test Session Report, PR7 Mypy Type-Check Report, _get(), main() (+17 more)

### Community 16 - "Morning Brief / Dashboard API"
Cohesion: 0.20
Nodes (25): _bounds(), _brief_staleness(), _build_sections(), dashboard_today(), DashboardResponse, _entity_ref(), _generate(), get_morning_brief() (+17 more)

### Community 17 - "Auth, Config & Workspace Isolation"
Cohesion: 0.12
Nodes (14): SessionDep, require_auth_context(), require_csrf(), get_settings(), Settings, get_session(), Session, _decode_cursor() (+6 more)

### Community 18 - "Phase 6 Connector Contracts"
Cohesion: 0.10
Nodes (24): /engineering/connectors Endpoints, Phase 6 Engineering Workspace API, /engineering/metrics Endpoint, /engineering/overview and Query Endpoints, Connector Creation Never Returns Token Values, Provider Deletion, Access Loss, Rename and Disconnect Are Distinct States, Engineering Connector Contract, Least-Privilege Tokens With Encrypted Secret Storage (+16 more)

### Community 19 - "Frontend TypeScript Config"
Cohesion: 0.09
Nodes (22): compilerOptions, allowJs, allowSyntheticDefaultImports, esModuleInterop, forceConsistentCasingInFileNames, isolatedModules, jsx, lib (+14 more)

### Community 20 - "Search API"
Cohesion: 0.15
Nodes (20): CursorPayload, _decode_cursor(), _normalize_query(), AuthDep, BaseModel, datetime, Query, SessionDep (+12 more)

### Community 21 - "Phase 7 Personal Domain Privacy"
Cohesion: 0.12
Nodes (21): APIs Enforce Consent and Field Policy Server-Side, Phase 7 Personal Intelligence API, /personal/domains Endpoints, /personal/insights Endpoints, check_ins, deletion_jobs, Phase 7 Personal Intelligence Data Model, domain_consents (+13 more)

### Community 22 - "AI Coding Standards (STD-001)"
Cohesion: 0.11
Nodes (19): AI Coding Standards, CI Architecture Enforcement, Configuration Hierarchy (Default->Env->Workspace->User), Definition of Done, Dependency Direction Rule (Application->Domain->Infrastructure), STD-001 Repository Standards, Backend Domain Structure, Forbidden Practices (+11 more)

### Community 23 - "Frontend Work Action Center"
Cohesion: 0.19
Nodes (16): actionBody(), actionErrorMessage(), Commitment, csrfToken(), dueLabel(), EntityAction, EntityKind, ErrorEnvelope (+8 more)

### Community 24 - "Health Checks & Response Middleware"
Cohesion: 0.14
Nodes (8): _error_payload(), JSONResponse, Request, Response, ready(), _request_uuid(), response_contract_middleware(), test_note_body_bounds_are_enforced_by_schema()

### Community 25 - "Phase 5 Automation Data Model"
Cohesion: 0.16
Nodes (17): /automations/approvals Endpoints, Phase 5 Automation API Schemas, /automations/policies Endpoints, /automations/runs Endpoints, Workflow Simulation (Predicted Steps Without Executing), /automations/workflows Endpoints, approval_requests, automation_policies (+9 more)

### Community 26 - "Constitution Articles (SPEC-000)"
Cohesion: 0.12
Nodes (16): Constitutional Amendment Process, Article I: Human Judgment Is Sovereign, Article II: Human Attention Is Primary Optimization Target, Article III: Knowledge Is The Product, Article IV: Local First By Default, Article VI: Replace Technologies, Preserve Architecture, Article VII: AI Is Infrastructure, Article VIII: Explainability Is Mandatory (+8 more)

### Community 27 - "Audit Log Query API"
Cohesion: 0.25
Nodes (13): AuditEventResponse, AuditListResponse, _decode(), list_audit_events(), AuthDep, BaseModel, datetime, SessionDep (+5 more)

### Community 28 - "Phase 6 Engineering Data Model"
Cohesion: 0.16
Nodes (14): changes, deployments, Phase 6 Engineering Workspace Data Model, engineering_decisions, engineering_work_items, incidents, Raw Provider Payload Retention Minimized, People Link to Phase 2 Entities (+6 more)

### Community 29 - "Audit Logging Infrastructure"
Cohesion: 0.28
Nodes (10): _correlation_id(), Request, Response, UUID, _record_rejected_task_mutation(), rejected_mutation_audit_middleware(), _task_id_from_path(), configure_logging() (+2 more)

### Community 30 - "Phase 7 Insight Contract Guardrails"
Cohesion: 0.17
Nodes (13): Connector Payloads Untrusted, Cannot Issue Runtime Instructions, Health/Finance Suggestions Never Use Diagnostic or Guaranteed-Return Language, cross_domain_grants, Sensitive Data Never Leaves Device Unless Explicitly Allowed, Feedback Cannot Silently Turn Correlation Into Causation, Cross-Domain Insight Requires Active Grant Covering Every Source, Personal Insight Contract, Insights Must Be Evidence-Backed, Proportionate, Non-Manipulative (+5 more)

### Community 31 - "Recommendation Postgres Integration Tests"
Cohesion: 0.54
Nodes (12): _generate(), _headers(), datetime, TestClient, UUID, recommendation_context(), _task(), test_expiry_emits_audit_and_outbox() (+4 more)

### Community 32 - "Phase 8 Delegation Contract"
Cohesion: 0.17
Nodes (12): Phase 8 Core Records, Append-only Delegation History, Phase 8 Data Model, Membership Removal Cannot Orphan Records, Resource Grants, Resource Visibility Model (private/shared_explicitly/workspace), Accountability Transfers Only on Acceptance, Delegation Contract (+4 more)

### Community 33 - "Dev Bootstrap Script"
Cohesion: 0.35
Nodes (10): Cursor, _allow_remote_database(), _create_identity(), _database_url(), _existing_identity(), main(), datetime, UUID (+2 more)

### Community 34 - "Phase 9 Compliance Contract"
Cohesion: 0.20
Nodes (11): Audit Event Properties (append-only, redacted, exportable), Control-to-Evidence Mapping, Enterprise Compliance Contract, Exceptions Expire and Require Risk Acceptance, Legal Hold Prevents Deletion, Not Access Control, Audit Export Manifest/Hash/Signature State, Phase 9 Core Records, Phase 9 Enterprise Data Model (+3 more)

### Community 35 - "Phase 0 Security Baseline"
Cohesion: 0.18
Nodes (11): Application Controls, Container Controls, Database Controls, Phase 0 Security Baseline, Phase 0 Exit Evidence Requirements, Identity and Session Controls, Required Security Tests, Secrets Management (+3 more)

### Community 36 - "In-Process Event Bus"
Cohesion: 0.22
Nodes (5): NonDurableInProcessEventBus, Test and development adapter for synchronous in-process dispatch.      This adap, EventEnvelope, BaseModel, EventHandler

### Community 37 - "Phase 5 Approval Policy"
Cohesion: 0.24
Nodes (10): Approval Modes (preview_only / per_run / bounded_recurring), Automation Approval Policy, Least-Privilege, Explicit, Time-Bound, Revocable Authority, Material Changes Invalidate Approval, Mandatory Per-Run Approval for High-Risk Action Classes, secret_references, Phase 5 Test Plan, Preview-Only Dogfood With Explicit Exit Review (+2 more)

### Community 38 - "Phase 8 API Schemas"
Cohesion: 0.20
Nodes (10): 404 Privacy Masking for Unauthorized Access, Delegation Endpoints, Phase 8 API Schemas, Effective Permissions Exposure, Invitation Endpoints, Server-validated Session Context, Shared Activity Endpoint, Sharing Grants Endpoints (+2 more)

### Community 39 - "Phase 9 Tenancy API Schemas"
Cohesion: 0.20
Nodes (10): Break-glass Endpoints, Phase 9 Enterprise API, Identity Provider Endpoints, Enterprise Policy Endpoints, Policy Simulation Before Publication, SCIM Idempotent Provisioning, Step-up Authentication for High-impact Actions, Just-in-time Administrative Support Access (+2 more)

### Community 40 - "Dev Bootstrap Backend Route"
Cohesion: 0.31
Nodes (8): bootstrap_page(), BootstrapExchange, exchange_bootstrap_code(), BaseModel, JSONResponse, SessionDep, _require_development(), HTMLResponse

### Community 41 - "Phase 4-7 Implementation Status Slices"
Cohesion: 0.25
Nodes (9): AI-Enhanced Surface Required States, Failure Never Blocks Deterministic Core Flow, Phase 4 AI UX States, Phase 5 Implementation Status, Phase 5 Implementation Slices, Phase 6 Implementation Status, Phase 6 Implementation Slices, Phase 7 Implementation Status (+1 more)

### Community 42 - "Phase 5/7 UX Required States"
Cohesion: 0.22
Nodes (9): WCAG 2.2 AA Accessibility Standard, Workflow Run States Enum, needs_review Unknown External Outcome State, Phase 5 Automation UX States, Automation Required UX States, Automation Primary Surfaces (Builder/Simulation/Policy/Approval/History), Calm, Non-Judgmental Language; No Shame, Addiction Loops, or False Urgency, Phase 7 Personal Intelligence UX States (+1 more)

### Community 43 - "Phase 5 Execution Contract & Durability"
Cohesion: 0.25
Nodes (9): Approval Responses Require Current Version and Exact Action Digest, compensation_steps, Stable Action Digest and Idempotency Key, Bounded Exponential Backoff Only for Classified Transient Failures, Compensation Executes Only Declared, Approved Steps, Durable Execution Contract, Worker Persists State Before/After Each Side Effect, Cursor Persists Only After Durable Projection (+1 more)

### Community 44 - "Phase 8/9 UX States & Templates"
Cohesion: 0.25
Nodes (9): Phase 8 UX States, Phase 8 Required UX States, Phase 8 Multi-user Surfaces, WCAG 2.2 AA Compliance, Phase 9 Admin Surfaces, Phase 9 Enterprise UX States, Phase 9 Required UX States, WCAG 2.2 AA Compliance (+1 more)

### Community 45 - "Commitment Postgres Integration Tests"
Cohesion: 0.58
Nodes (8): commitment_test_context(), _headers(), TestClient, UUID, test_commitment_lifecycle_is_transactional_and_workspace_scoped(), test_commitment_list_uses_signed_cursor_pagination(), test_cross_workspace_references_are_not_disclosed(), test_restore_requires_archived_state()

### Community 46 - "Note Postgres Integration Tests"
Cohesion: 0.58
Nodes (8): _headers(), note_test_context(), TestClient, UUID, test_note_cursor_restore_guard_and_workspace_isolation(), test_note_lifecycle_autosave_search_and_redacted_audit(), test_note_meeting_reference_is_non_disclosing_until_meetings_exist(), test_note_patch_rejects_null_required_fields()

### Community 47 - "Phase 8 Permission Contract & Tests"
Cohesion: 0.25
Nodes (8): Background Job Re-check Before Side Effects, Deny and Privacy Override Role Grants, Multi-user Permission Contract, Checks at Service and Query Boundaries, Adversarial Tests (IDOR, Privilege Escalation, Confused Deputy), Role/Resource/Action Authorization Matrix, Multi-identity Browser Acceptance Tests, Phase 8 Test Plan

### Community 48 - "Root Package Manifest"
Cohesion: 0.25
Nodes (7): name, packageManager, private, scripts, build, dev, test

### Community 49 - "Dashboard Brief Postgres Tests"
Cohesion: 0.57
Nodes (7): dashboard_context(), _headers(), TestClient, UUID, test_dashboard_and_persisted_brief_lifecycle(), test_dashboard_empty_state_and_budget(), test_dashboard_review_regressions()

### Community 50 - "Risk/Attention Postgres Tests"
Cohesion: 0.57
Nodes (7): _headers(), TestClient, UUID, risk_test_context(), test_closed_risk_cannot_reopen(), test_risk_is_hidden_across_workspaces(), test_risk_lifecycle_and_attention_controls()

### Community 51 - "Task Postgres Integration Tests"
Cohesion: 0.57
Nodes (7): _headers(), TestClient, UUID, task_test_context(), test_concurrent_idempotent_create_returns_one_task(), test_task_lifecycle_is_transactional_and_workspace_scoped(), test_terminal_transitions_and_workspace_timezone_are_enforced()

### Community 52 - "Phase 9 Tenancy Test Plan"
Cohesion: 0.29
Nodes (7): Tenant ID Uniqueness/Reference Boundary, Tenant-scoped Storage, Cache, Jobs, AI Context, Disaster Recovery Exercise (RPO/RTO), Phase 9 Test Plan, Independent Penetration/Security Review, OIDC/SAML Interoperability Validation, Tenant Isolation Tests Across Layers

### Community 53 - "Constitutional Governance Templates"
Cohesion: 0.33
Nodes (7): Article V: Specification Before Code, Constitutional Document Hierarchy, Feature Development Workflow (RFC->Spec->ADR->Impl->Tests->Docs->Review->Merge), Stop-and-Ask Protocol, ADR Template, RFC Template, Specification Change Request Template

### Community 54 - "Calendar/Meeting Postgres Tests"
Cohesion: 0.62
Nodes (6): calendar_test_context(), _headers(), TestClient, UUID, test_calendar_and_linked_meeting_lifecycle(), test_linked_meeting_rejects_cross_workspace_event()

### Community 55 - "Search Audit Postgres Tests"
Cohesion: 0.67
Nodes (5): TestClient, UUID, search_audit_context(), test_audit_filters_pagination_redaction_and_isolation(), test_search_ranking_sanitization_pagination_and_isolation()

### Community 56 - "Frontend E2E Test Runner"
Cohesion: 0.50
Nodes (4): main(), preview, sections, waitForServer()

### Community 57 - "Search Performance Postgres Tests"
Cohesion: 0.70
Nodes (4): TestClient, UUID, search_performance_context(), test_search_10000_entity_ci_budget()

### Community 58 - "Phase 8/9 Implementation Status"
Cohesion: 0.67
Nodes (4): Phase 8 Implementation Status, Dependency on Phase 7 Exit, Phase 9 Implementation Status, Dependency on Phase 8 Exit

### Community 68 - "Cross-RFC Principle: Local-First"
Cohesion: 0.67
Nodes (3): Product Principle 1 - Local First: user data belongs to user; system MUST function locally, cloud is optional and never mandatory, EP-005 Local First: local execution is default for privacy, ownership, latency, resilience, cost; cloud SHALL NOT be mandatory for core workflows, DP-002 Local First: local execution is default architecture for privacy, speed, ownership, offline capability, lower cost; cloud extends but never owns the product

### Community 69 - "Cross-RFC Principle: Human Authority"
Cohesion: 0.67
Nodes (3): Product Principle 2 - Human Decision Making: AI assists, humans decide; system MUST recommend and explain, MUST NOT silently make executive decisions, EP-007 Human Authority: AI recommends, humans decide; AI SHALL NEVER silently send email, delete data, modify data, execute workflows or approve requests, DP-009 Human In Control: AI recommends, humans decide; all state-changing actions (send email, delete, schedule, delegate, update tasks) require explicit confirmation

### Community 70 - "Cross-RFC Principle: Explainability"
Cohesion: 0.67
Nodes (3): Product Principle 3 - Explainability: every recommendation must answer why, based on what, which evidence, what confidence, EP-006 Explainability Over Intelligence: an explainable recommendation beats an opaque one; unexplainable recommendations SHOULD NOT be shown, DP-003 Explain Everything: every recommendation should answer why, what evidence, how confident, what happens if ignored

### Community 71 - "Cross-RFC Principle: Permanent Memory"
Cohesion: 0.67
Nodes (3): Product Principle 4 - Long-Term Memory: executives forget, software should not; memory is permanent, summaries are disposable, EP-008 Permanent Memory: information should never be intentionally discarded; system prefers preservation over deletion; summaries regenerate, memory persists, DP-004 Memory Is Permanent: ECC continuously builds richer understanding of people, projects, orgs, commitments, meetings, decisions; deleting context is exceptional

### Community 72 - "Frontend Entrypoint & Workspace Config"
Cohesion: 0.67
Nodes (3): Executive Command Center Frontend Entry (index.html), frontend/src/main.tsx (module entry, referenced), pnpm-workspace.yaml (monorepo package config)

## Ambiguous Edges - Review These
- `Prohibited In Phase 0: floating versions, unpinned images, Neo4j, Qdrant, Redis, Kafka, NATS, Temporal, Kubernetes, cloud-only deps, JWT browser sessions, no-auth dev mode, LangChain, LangGraph, CrewAI, AutoGen, MongoDB, Firebase, Django, Flask, Express, Electron` → `Model Router: centralized component through which all LLM/model calls must pass (provider routing, local/cloud selection, auditable versions)`  [AMBIGUOUS]
  docs/RFC-005.md · relation: conceptually_related_to
- `Phase 4 AI UX States` → `Phase 5 Implementation Status`  [AMBIGUOUS]
  docs/phases/phase-005/IMPLEMENTATION-STATUS.md · relation: references
- `Phase 5 Implementation Status` → `Phase 6 Implementation Status`  [AMBIGUOUS]
  docs/phases/phase-006/IMPLEMENTATION-STATUS.md · relation: references
- `Phase 6 Implementation Status` → `Phase 7 Implementation Status`  [AMBIGUOUS]
  docs/phases/phase-007/IMPLEMENTATION-STATUS.md · relation: references
- `Workflow Run States Enum` → `needs_review Unknown External Outcome State`  [AMBIGUOUS]
  docs/phases/phase-005/EXECUTION-CONTRACT.md · relation: conceptually_related_to

## Knowledge Gaps
- **210 isolated node(s):** `preview`, `sections`, `name`, `private`, `version` (+205 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **67 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What is the exact relationship between `Prohibited In Phase 0: floating versions, unpinned images, Neo4j, Qdrant, Redis, Kafka, NATS, Temporal, Kubernetes, cloud-only deps, JWT browser sessions, no-auth dev mode, LangChain, LangGraph, CrewAI, AutoGen, MongoDB, Firebase, Django, Flask, Express, Electron` and `Model Router: centralized component through which all LLM/model calls must pass (provider routing, local/cloud selection, auditable versions)`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **What is the exact relationship between `Phase 4 AI UX States` and `Phase 5 Implementation Status`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Phase 5 Implementation Status` and `Phase 6 Implementation Status`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Phase 6 Implementation Status` and `Phase 7 Implementation Status`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Workflow Run States Enum` and `needs_review Unknown External Outcome State`?**
  _Edge tagged AMBIGUOUS (relation: conceptually_related_to) - confidence is low._
- **Why does `AuthContext` connect `Note-Taking API` to `Recommendation Lifecycle API`, `Risk Tracking API`, `Task API & Contracts`, `Commitment Tracking API`, `Calendar Events API`, `Meeting Scheduling API`, `Attention Scoring API`, `Auth, Config & Workspace Isolation`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._
- **Why does `_transition()` connect `Recommendation Lifecycle API` to `Note-Taking API`?**
  _High betweenness centrality (0.013) - this node is a cross-community bridge._