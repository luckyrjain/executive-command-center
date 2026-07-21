# Graph Report - .  (2026-07-16)

## Corpus Check
- 237 files · ~107,070 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1385 nodes · 2471 edges · 388 communities (60 shown, 328 thin omitted)
- Extraction: 94% EXTRACTED · 6% INFERRED · 0% AMBIGUOUS · INFERRED: 144 edges (avg confidence: 0.73)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]
- Recommendation Lifecycle API
- Phase 1 Release Gate & Acceptance Docs
- Risk Tracking API
- Task API & Contract Tests
- Commitment API & Contract Tests
- Frontend Package Dependencies
- Note-Taking API & Contract Tests
- Phase Roadmap & Implementation Status
- Calendar Events API
- Meeting Scheduling API
- Dev Bootstrap & Settings
- Attention Scoring API
- Domain Model, Event Catalog & Phase 0 Foundation Docs
- Dev Bootstrap & Phase 1 Acceptance Tooling
- ADR Index (ADR-0001 to ADR-0010)
- Morning Brief & Dashboard API
- Frontend Autosave Controller & Note Workspace
- Phase Roadmap (000-009), Templates & CONTRIBUTING
- Frontend Task Workspace
- Frontend Commitment Workspace
- Frontend Dashboard & Panel Components
- RFC-002: Engineering Philosophy
- SPEC-000: Constitutional Articles
- Recommendation Postgres Integration Tests
- Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]
- Frontend API Types & Workspace Navigation
- Frontend Dashboard & Panel Components
- Dev Bootstrap Script
- Document Control & Phase 5 Automation Data Model
- RFC-001: Product Definition
- Frontend API Client
- Architecture Ch.2b: Runtime
- Architecture Ch.3/8: AI Runtime & Data Platform
- Architecture Ch.2a: Core Platform
- Architecture Ch.5: Attention Engine
- Architecture Ch.6: Integration Platform
- bus.py & NonDurableInProcessEventBus Group
- Architecture Ch.1: Vision
- Architecture Ch.4: Knowledge Platform
- Architecture Ch.9: Security
- Architecture Ch.10: Operations
- RFC-005: Technology Registry
- Document Control & Phase 5 Automation Data Model
- Architecture Ch.7: Frontend
- RFC-000/RFC-003: Governance, Design Principles & Setup
- STD-001: Repository Standards
- Frontend Dashboard & Panel Components
- PHASE-001: Data Model Tables
- PHASE-001: Test Plan
- Docs Phases Phase 002 Test Plan
- RFC-000/RFC-003: Governance, Design Principles & Setup
- RFC-000/RFC-003: Governance, Design Principles & Setup
- Frontend E2e Run
- Docs Phases Phase 001 Audit Contract
- Docs Phases Phase 001 Morning Brief Contract
- Docs Phases Phase 001 Priority Model
- PHASE-001: Search Contract
- PHASE-001: UX States
- Docs Phases Phase 002 Api Schemas
- Docs Phases Phase 002 Data Model
- Docs Phases Phase 002 Entity Resolution Contract
- Docs Phases Phase 002 Implementation Status
- Docs Phases Phase 002 Retrieval Contract
- Docs Phases Phase 002 Ux States
- Docs Phases Phase 003 Api Schemas
- Docs Phases Phase 003 Attention Model
- Docs Phases Phase 003 Data Model
- Docs Phases Phase 003 Implementation Status
- Docs Phases Phase 003 Meeting Prep Contract
- Docs Phases Phase 003 Planning Contract
- Docs Phases Phase 003 Test Plan
- Docs Phases Phase 003 Ux States
- Docs Phases Phase 005 Implementation Status
- Phase Roadmap & Implementation Status
- Phase Roadmap & Implementation Status
- Frontend TypeScript Config
- package.json & Package Name Group
- Backend Ecc Domains Calendar Init
- Backend Ecc Domains Communication Init
- Backend Ecc Domains Governance Init
- Backend Ecc Domains Platform Init
- Backend Ecc Init
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
- Docs Phases Phase 001 Final Acceptance
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
- CI Backend Job: ruff, mypy, alembic upgrade, pytest, pip-audit against Postgres 18 service
- CI Containers Job: docker build backend/frontend images
- CI Frontend Job: typecheck, vitest, build, playwright e2e
- CI Security Job: gitleaks secret scan, SBOM (anchore/syft), trivy critical vuln scan
- Acceptance-Contract Job: runs scripts/check_phase1_acceptance.py
- Accessibility-Smoke Job: playwright e2e accessibility checks
- Backup-Restore Job: migrate, backup.sh, verify_restore.sh, phase1 acceptance pytest
- Scripts Init

## God Nodes (most connected - your core abstractions)
1. `AuthContext` - 99 edges
2. `README (Executive Command Center)` - 41 edges
3. `get_settings()` - 23 edges
4. `_lifecycle()` - 22 edges
5. `update_note()` - 22 edges
6. `_transition()` - 21 edges
7. `_lifecycle_task()` - 21 edges
8. `_lifecycle()` - 20 edges
9. `_mutate_commitment()` - 19 edges
10. `confirm_recommendation()` - 19 edges

## Surprising Connections (you probably didn't know these)
- `PR18 Bootstrap Dev Test Session Report` --semantically_similar_to--> `PR7 Mypy Type-Check Report`  [INFERRED] [semantically similar]
  pr18-bootstrap-report.txt → pr7-mypy.txt
- `test_note_body_bounds_are_enforced_by_schema()` --calls--> `NoteCreate`  [EXTRACTED]
  tests/test_note_contract.py → backend/ecc/domains/knowledge/notes.py
- `Task 12: Operations, Status Synchronization, and Full Proof` --references--> `README (Executive Command Center)`  [INFERRED]
  docs/superpowers/plans/2026-07-16-phase-1-completion.md → README.md
- `test_commitment_create_defaults()` --calls--> `CommitmentCreate`  [EXTRACTED]
  tests/test_commitment_contract.py → backend/ecc/domains/communication/commitments.py
- `test_detected_commitment_requires_evidence()` --calls--> `CommitmentCreate`  [EXTRACTED]
  tests/test_commitment_contract.py → backend/ecc/domains/communication/commitments.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Evidence-Driven Phase 1 Closure Gate** — docs_runbooks_phase_1_release_gate_exit_criteria, docs_superpowers_plans_2026_07_16_phase_1_completion_task_12_operations_status_synchronization_and_full_proof, docs_superpowers_specs_2026_07_16_phase_1_completion_design_completion_boundary, docs_superpowers_specs_2026_07_16_phase_1_completion_design_documentation_consistency [INFERRED 0.85]
- **Seven-Day Human-Duration Daily-Use Gate** — docs_superpowers_plans_2026_07_16_phase_1_completion_global_constraints, docs_superpowers_specs_2026_07_16_phase_1_completion_design_outcome, docs_runbooks_phase_1_daily_use, docs_superpowers_plans_2026_07_16_phase_1_completion_task_12_operations_status_synchronization_and_full_proof [INFERRED 0.85]
- **Production Hardening and Security Gate Pipeline** — docs_superpowers_plans_2026_07_16_phase_1_completion_task_7_production_configuration_and_http_protections, docs_superpowers_plans_2026_07_16_phase_1_completion_task_8_structured_observability_and_phase_1_metrics, docs_superpowers_plans_2026_07_16_phase_1_completion_task_11_dependency_filesystem_and_container_security_gates, docs_superpowers_specs_2026_07_16_phase_1_completion_design_backend_production_hardening, docs_superpowers_specs_2026_07_16_phase_1_completion_design_ci_and_security [INFERRED 0.85]
- **Phase 5 run lifecycle state machine** — docs_phases_phase_005_data_model_run_states, docs_phases_phase_005_data_model_workflow_runs, docs_phases_phase_005_data_model_approval_requests, docs_phases_phase_005_data_model_compensation_steps [INFERRED 0.85]
- **README governance onboarding reading order** — readme, docs_00_document_control, docs_setup, docs_specifications_spec_000_doc [EXTRACTED 1.00]
- **CI Verification Reports for executive-command-center PRs** — pr18_bootstrap_report_test_report, pr7_mypy_report, concept_dev_bootstrap_feature, concept_mypy_type_safety_gate [INFERRED 0.75]

## Communities (388 total, 328 thin omitted)

### Community 0 - "Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]"
Cohesion: 0.06
Nodes (59): Settings, _error_payload(), JSONResponse, ready(), _request_uuid(), response_contract_middleware(), calendar_test_context(), _headers() (+51 more)

### Community 1 - "Recommendation Lifecycle API"
Cohesion: 0.09
Nodes (58): datetime, Session, UUID, record_event(), record_feedback(), ConfirmAction, DeferAction, PinAction (+50 more)

### Community 2 - "Phase 1 Release Gate & Acceptance Docs"
Cohesion: 0.05
Nodes (57): Phase 1 API Schemas, PHASE-001 Executive Dashboard MVP, FINAL-ACCEPTANCE.md, PHASE-1-DAILY-USE.md (planned), PHASE-1-DEPLOYMENT.md (planned), Phase 1 Production Release Gate, Accessibility and UX Checks, Application Correctness Checks (+49 more)

### Community 3 - "Risk Tracking API"
Cohesion: 0.12
Nodes (46): _archive_action(), archive_risk(), _get_row(), _load_cached(), _lock_idempotency(), AuthDep, BaseModel, CsrfDep (+38 more)

### Community 4 - "Task API & Contract Tests"
Cohesion: 0.16
Nodes (46): archive_task(), cancel_task(), complete_task(), create_task(), _decode_cursor(), _encode_cursor(), get_task(), _get_task_row() (+38 more)

### Community 5 - "Commitment API & Contract Tests"
Cohesion: 0.18
Nodes (45): archive_commitment(), cancel_commitment(), _check_version(), CommitmentAction, CommitmentCreate, CommitmentLinks, CommitmentListResponse, CommitmentPatch (+37 more)

### Community 6 - "Frontend Package Dependencies"
Cohesion: 0.05
Nodes (41): dependencies, react, react-dom, react-router, @tanstack/react-query, zustand, devDependencies, jsdom (+33 more)

### Community 7 - "Note-Taking API & Contract Tests"
Cohesion: 0.21
Nodes (39): AuthContext, archive_note(), _body_checksum(), _check_version(), create_note(), _decode_cursor(), _encode_cursor(), get_note() (+31 more)

### Community 8 - "Phase Roadmap & Implementation Status"
Cohesion: 0.05
Nodes (13): Phase 4 AI UX States, Phase 5 Automation API Schemas, Phase 5 Automation UX States, Phase 6 Engineering Workspace API, Phase 6 Engineering Workspace Data Model, Phase 6 Engineering UX States, Phase 7 Personal Intelligence API, Phase 7 Personal Intelligence Data Model (+5 more)

### Community 9 - "Calendar Events API"
Cohesion: 0.19
Nodes (34): archive_calendar_event(), CalendarEventAction, CalendarEventCreate, CalendarEventListResponse, CalendarEventPatch, CalendarEventResponse, create_calendar_event(), _decode_cursor() (+26 more)

### Community 10 - "Meeting Scheduling API"
Cohesion: 0.20
Nodes (35): archive_meeting(), _calendar_event(), create_meeting(), _decode_cursor(), _encode_cursor(), get_meeting(), _get_row(), _lifecycle() (+27 more)

### Community 11 - "Dev Bootstrap & Settings"
Cohesion: 0.12
Nodes (28): require_auth_context(), require_csrf(), get_settings(), bootstrap_page(), BootstrapExchange, exchange_bootstrap_code(), _require_development(), AuditEventResponse (+20 more)

### Community 12 - "Attention Scoring API"
Cohesion: 0.22
Nodes (27): AttentionAction, AttentionItem, AttentionList, defer_attention(), dismiss_attention(), _due_points(), _factor(), list_attention() (+19 more)

### Community 13 - "Domain Model, Event Catalog & Phase 0 Foundation Docs"
Cohesion: 0.08
Nodes (21): Architecture Decision Records (docs/adr), Domain API Contracts, Domain commands and queries, Canonical Domain Model, Core entities, Domain Event Catalog, PKOS Schema, Phase 0 Backup and Restore (+13 more)

### Community 14 - "Dev Bootstrap & Phase 1 Acceptance Tooling"
Cohesion: 0.11
Nodes (23): Local Dev Bootstrap Environment Guard, Mypy Static Type-Check CI Gate, Path, PR18 Bootstrap Dev Test Session Report, PR7 Mypy Type-Check Report, _get(), main(), Any (+15 more)

### Community 15 - "ADR Index (ADR-0001 to ADR-0010)"
Cohesion: 0.08
Nodes (15): ADR-0001 — Repository Layout, ADR-0002 — Local-First Architecture, ADR-0004 — AI Runtime, ADR-0005 — Event Bus, ADR-0006 — Storage Strategy, ADR-0007 — Model Router, ADR-0008: Authentication and Workspace Identity, Decision (+7 more)

### Community 16 - "Morning Brief & Dashboard API"
Cohesion: 0.22
Nodes (24): _bounds(), _brief_staleness(), _build_sections(), dashboard_today(), DashboardResponse, _entity_ref(), _generate(), get_morning_brief() (+16 more)

### Community 17 - "Frontend Autosave Controller & Note Workspace"
Cohesion: 0.11
Nodes (17): AutosaveController, AutosaveOptions, AutosaveState, AutosaveStatus, createAutosaveController(), Action, displayTitle(), emptyDraft (+9 more)

### Community 18 - "Phase Roadmap (000-009), Templates & CONTRIBUTING"
Cohesion: 0.09
Nodes (12): Phase 1 Implementation Status, PHASE-002 Knowledge Platform, PHASE-003 Human Attention Engine, PHASE-004 AI Runtime, PHASE-005 Automation, PHASE-006 Engineering Workspace, PHASE-007 Personal Intelligence, PHASE-008 Multi-user Workspaces (+4 more)

### Community 19 - "Frontend Task Workspace"
Cohesion: 0.15
Nodes (14): Action, duePayload(), EditState, emptyDraft, errorMessage(), filters, listTasks(), pad() (+6 more)

### Community 20 - "Frontend Commitment Workspace"
Cohesion: 0.16
Nodes (13): Action, Commitment, CommitmentList, CommitmentWorkspace(), Draft, duePayload(), EditState, emptyDraft (+5 more)

### Community 21 - "Frontend Dashboard & Panel Components"
Cohesion: 0.20
Nodes (14): api(), App(), DashboardItem, DashboardResponse, ErrorEnvelope, fetchDashboard(), fetchMorningBrief(), formatTime() (+6 more)

### Community 22 - "RFC-002: Engineering Philosophy"
Cohesion: 0.14
Nodes (13): AI Engineering Philosophy, Architectural Fitness Functions, Engineering Principles, EP-001, EP-002, EP-003, EP-004, EP-005 (+5 more)

### Community 23 - "SPEC-000: Constitutional Articles"
Cohesion: 0.14
Nodes (14): Article I, Article II, Article III, Article IV, Article IX, Article V, Article VI, Article VII (+6 more)

### Community 24 - "Recommendation Postgres Integration Tests"
Cohesion: 0.54
Nodes (12): _generate(), _headers(), datetime, TestClient, UUID, recommendation_context(), _task(), test_expiry_emits_audit_and_outbox() (+4 more)

### Community 25 - "Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]"
Cohesion: 0.30
Nodes (9): _correlation_id(), Request, Response, UUID, _record_rejected_task_mutation(), rejected_mutation_audit_middleware(), _task_id_from_path(), configure_logging() (+1 more)

### Community 26 - "Frontend API Types & Workspace Navigation"
Cohesion: 0.27
Nodes (8): ApiErrorEnvelope, ApiRequestOptions, WorkspaceView, moveWorkspaceFocus(), nextWorkspaceIndex(), WorkspaceNavigation(), WorkspaceNavigationProps, WORKSPACES

### Community 27 - "Frontend Dashboard & Panel Components"
Cohesion: 0.38
Nodes (10): actionPayload(), actionSummary(), confidenceLabel(), csrfToken(), fetchRecommendations(), mutateRecommendation(), Recommendation, recommendationErrorMessage() (+2 more)

### Community 28 - "Dev Bootstrap Script"
Cohesion: 0.35
Nodes (10): Cursor, _allow_remote_database(), _create_identity(), _database_url(), _existing_identity(), main(), datetime, UUID (+2 more)

### Community 29 - "Document Control & Phase 5 Automation Data Model"
Cohesion: 0.24
Nodes (11): Phase 5 Automation Data Model, approval_requests, automation_policies, compensation_steps, notifications, Run states enum, secret_references, triggers (+3 more)

### Community 30 - "RFC-001: Product Definition"
Cohesion: 0.18
Nodes (10): Functional Requirements, Jobs To Be Done, Non-Functional Requirements, Primary Persona, Product Evolution, Product Maturity Model, Product Principles, Secondary Persona (+2 more)

### Community 31 - "Frontend API Client"
Cohesion: 0.39
Nodes (6): ApiError, apiRequest(), cookieValue(), currentState(), requestHeaders(), SAFE_METHODS

### Community 32 - "Architecture Ch.2b: Runtime"
Cohesion: 0.29
Nodes (7): Architecture Fitness Functions, Caching Strategy, Domain Ownership, Error Handling, Internal Communication, Platform, Runtime Philosophy

### Community 33 - "Architecture Ch.3/8: AI Runtime & Data Platform"
Cohesion: 0.25
Nodes (5): AI Runtime Goals, Runtime Constraints, Architectural Goals, Architecture Constraints, RFC-004 — System Architecture

### Community 34 - "Architecture Ch.2a: Core Platform"
Cohesion: 0.33
Nodes (5): Architectural Goals, Architecture Constraints, Event Types, Executive Dashboard, Responsibility

### Community 35 - "Architecture Ch.5: Attention Engine"
Cohesion: 0.33
Nodes (5): Architecture Constraints, Design Goals, Failure Modes, Priority Signals, Waiting Engine

### Community 36 - "Architecture Ch.6: Integration Platform"
Cohesion: 0.33
Nodes (5): Architectural Goals, Architecture Constraints, Failure Modes, Supported Connector Types, Synchronization Model

### Community 37 - "bus.py & NonDurableInProcessEventBus Group"
Cohesion: 0.50
Nodes (3): NonDurableInProcessEventBus, Test and development adapter for synchronous in-process dispatch.      This adap, EventEnvelope

### Community 38 - "Architecture Ch.1: Vision"
Cohesion: 0.40
Nodes (4): Architectural Goals, Architectural Principles, Architecture Fitness Functions, RFC-004: System Architecture

### Community 39 - "Architecture Ch.4: Knowledge Platform"
Cohesion: 0.40
Nodes (4): Architecture Constraints, Core Principles, Primary Entity Types, Search Strategy

### Community 40 - "Architecture Ch.9: Security"
Cohesion: 0.40
Nodes (4): Architecture Constraints, Encryption, Identity Model, Security Principles

### Community 41 - "Architecture Ch.10: Operations"
Cohesion: 0.40
Nodes (4): Architecture Constraints, Deployment Strategy, Operational Philosophy, Phase Evolution

### Community 42 - "RFC-005: Technology Registry"
Cohesion: 0.50
Nodes (3): docker-compose postgres service (postgres:18.0), RFC-005: Approved Technology Registry, CI Workflow (ci.yml)

### Community 43 - "Document Control & Phase 5 Automation Data Model"
Cohesion: 0.50
Nodes (3): 00 - Document Control, Golden Rule, workflow_definitions / workflow_versions

### Community 44 - "Architecture Ch.7: Frontend"
Cohesion: 0.50
Nodes (3): Architecture Constraints, Design Goals, State Management

### Community 45 - "RFC-000/RFC-003: Governance, Design Principles & Setup"
Cohesion: 0.50
Nodes (3): Changelog, Document Types, Guiding Principles

### Community 46 - "STD-001: Repository Standards"
Cohesion: 0.67
Nodes (4): Changelog, STD-001 Repository Standards, Functions, Naming Standards

### Community 47 - "Frontend Dashboard & Panel Components"
Cohesion: 0.83
Nodes (3): formatDate(), request(), SearchAuditPanel()

## Knowledge Gaps
- **416 isolated node(s):** `compilerOptions`, `scripts`, `Guiding Principles`, `Document Types`, `Changelog` (+411 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **328 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `README (Executive Command Center)` connect `Domain Model, Event Catalog & Phase 0 Foundation Docs` to `Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]`, `Architecture Ch.3/8: AI Runtime & Data Platform`, `Phase 1 Release Gate & Acceptance Docs`, `Phase Roadmap & Implementation Status`, `RFC-005: Technology Registry`, `Document Control & Phase 5 Automation Data Model`, `RFC-000/RFC-003: Governance, Design Principles & Setup`, `STD-001: Repository Standards`, `ADR Index (ADR-0001 to ADR-0010)`, `Phase Roadmap (000-009), Templates & CONTRIBUTING`, `RFC-000/RFC-003: Governance, Design Principles & Setup`, `RFC-000/RFC-003: Governance, Design Principles & Setup`, `RFC-002: Engineering Philosophy`, `SPEC-000: Constitutional Articles`, `Dev Bootstrap Script`, `RFC-001: Product Definition`?**
  _High betweenness centrality (0.240) - this node is a cross-community bridge._
- **Why does `AuthContext` connect `Note-Taking API & Contract Tests` to `Recommendation Lifecycle API`, `Risk Tracking API`, `Task API & Contract Tests`, `Commitment API & Contract Tests`, `Calendar Events API`, `Meeting Scheduling API`, `Dev Bootstrap & Settings`, `Attention Scoring API`?**
  _High betweenness centrality (0.047) - this node is a cross-community bridge._
- **Why does `Task 12: Operations, Status Synchronization, and Full Proof` connect `Phase 1 Release Gate & Acceptance Docs` to `Phase Roadmap (000-009), Templates & CONTRIBUTING`, `Domain Model, Event Catalog & Phase 0 Foundation Docs`?**
  _High betweenness centrality (0.022) - this node is a cross-community bridge._
- **Are the 36 inferred relationships involving `AuthContext` (e.g. with `CalendarEventAction` and `CalendarEventCreate`) actually correct?**
  _`AuthContext` has 36 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `get_settings()` (e.g. with `require_csrf()` and `exchange_bootstrap_code()`) actually correct?**
  _`get_settings()` has 21 INFERRED edges - model-reasoned connections that need verification._
- **What connects `compilerOptions`, `scripts`, `Guiding Principles` to the rest of the system?**
  _416 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Core Infra (audit/config/db/logging) + Postgres Integration Tests [mixed cluster]` be split into smaller, more focused modules?**
  _Cohesion score 0.05837837837837838 - nodes in this community are weakly interconnected._