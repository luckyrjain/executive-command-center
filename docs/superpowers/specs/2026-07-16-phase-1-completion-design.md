# Phase 1 Completion Design

**Status:** Approved design  
**Branch:** `feature/phase-1-production-hardening`  
**Scope:** Complete every remaining PHASE-001 product, verification, security, recovery, and operational requirement without changing the frozen Phase 1 API contracts.

## Outcome

Phase 1 is complete when the Executive Command Center supports the full local authenticated workflow from the browser, every normative automated gate is executable and green, production defaults fail safely, recovery is demonstrated with populated data, and project status documents match the evidence.

The one-week daily-use validation remains a human-duration gate. This work provides a dated evidence template and procedure but does not mark that gate complete before seven days of recorded use.

## Delivery strategy

Work proceeds in independently verifiable vertical slices. Each behavior change starts with a failing focused test, adds the smallest implementation needed, and finishes with focused and regression checks. Shared infrastructure is introduced only where multiple completed slices need the same behavior.

The sequence is:

1. Shared frontend API, mutation, conflict, offline, and navigation foundation.
2. Task and commitment create/edit/lifecycle workflows.
3. Note create/autosave/search/archive/restore workflows.
4. Calendar event and linked/standalone meeting workflows.
5. Risk create/edit/lifecycle workflows.
6. Recommendation, Morning Brief, Search, and Audit acceptance completion.
7. Accessibility and offline/degraded acceptance.
8. Production configuration, HTTP security, limits, logging, and metrics.
9. Populated backup/restore and recovery evidence.
10. CI security and container-image gates.
11. Deployment, rollback, daily-use evidence, and synchronized status documents.

## Frontend architecture

### Application structure

The existing React application remains a single Phase 1 frontend. It gains workspace navigation for:

- Today and Morning Brief
- Work: tasks and commitments
- Notes
- Schedule: calendar events and meetings
- Risks
- Recommendations
- Search
- Audit

Navigation uses semantic landmarks, keyboard-operable tabs or links, stable focus behavior, and a small-screen layout. No new frontend state framework is introduced. TanStack Query remains responsible for server state; local form state stays inside the owning feature component.

### Shared API boundary

A single typed API client owns:

- the configured API base URL;
- cookie credentials;
- CSRF headers for mutations;
- a unique idempotency key for each user-initiated mutation attempt;
- JSON request and response handling;
- normalized API error envelopes;
- network/offline classification;
- `409 VERSION_CONFLICT` metadata.

Feature components do not duplicate fetch, header, or error-envelope logic. They invalidate only affected query keys after successful mutations.

### Conflict and recovery behavior

An optimistic-concurrency conflict preserves unsaved user input, fetches the current server representation, and presents a comparison-safe retry choice. The application never silently overwrites a newer version. Network failures keep the current view usable, announce the failure, and expose an explicit retry. Cached server data may remain visible while marked stale; unsupported mutations are disabled while offline.

## Entity workflows

### Tasks

The browser supports create, edit, complete, cancel, archive, and restore. Forms enforce mutually exclusive `due_date` and `due_at`, expose manual priority, and do not accept owner, actor, or workspace fields.

### Commitments

The browser supports create, edit, confirm, fulfil, cancel, archive, and restore. Direction and permitted counterparty references are explicit. Actions are offered only for valid lifecycle states.

### Notes

The browser supports create, autosave, search, archive, and restore. Autosave is debounced, reports saving/saved/error state accessibly, uses the latest version, and never loses locally edited content after a failed request or conflict.

### Calendar events and meetings

The browser supports local calendar-event CRUD plus archive and restore. It supports linked and standalone meetings. Linked meeting timing is read-only and projected from its authoritative calendar event; rescheduling occurs through the event. Standalone meeting fields map directly to the frozen API schema.

### Risks

The browser supports create, edit, contracted lifecycle transitions, archive, and restore. Probability, impact, mitigation, trigger, and review-date validation follow the existing schema.

### Recommendations and briefs

Recommendations expose explanations, confidence, evidence state, proposed action, publish preview, confirmation preview, reject, defer, pin, and conflict-safe execution. Only pending-confirmation recommendations can execute. Morning Brief refresh is exercised as a real mutation and exposes stale, refreshing, AI-disabled, and failure states.

## Accessibility and UX states

Automated browser tests combine semantic assertions with an accessibility engine. Core flows must have no serious or critical automated violations. Tests explicitly cover:

- main and navigation landmarks;
- document title and heading hierarchy;
- named form fields and controls;
- keyboard-only navigation and operation;
- visible focus indicators;
- status and alert announcements;
- loading, empty, stale, degraded, offline, validation, conflict, and recoverable-error states;
- all four evidence states: available, missing, permission denied, and deleted.

Automated checks supplement rather than claim complete proof of WCAG 2.2 AA. A manual checklist records contrast, zoom/reflow, screen-reader labels, and keyboard review.

## Browser acceptance

Playwright exercises the normative Phase 1 scenarios against deterministic API fixtures where UI behavior is the target and against a running PostgreSQL-backed backend where transaction or integration behavior is the target. The suite covers:

1. Task create, edit, complete, cancel, archive, restore, and conflict recovery.
2. Commitment create, confirm, fulfil, cancel, archive, and restore.
3. Note create, autosave, search, archive, and restore.
4. Calendar event plus linked and standalone meeting creation and authoritative rescheduling.
5. Calendar-event search and result opening.
6. Dashboard and Morning Brief operation with AI disabled.
7. Recommendation generation, publication, confirmation, and execution.
8. Recommendation rejection, deferral, and pinning.
9. Prevention of execution from proposed, rejected, expired, and superseded states.
10. Version-conflict recovery, audit inspection, and a keyboard-only core workflow.

The existing smoke script is split into focused scenario modules with shared server lifecycle and fixture helpers so failures identify the broken workflow.

## Backend production hardening

### Configuration

Production startup rejects placeholder or short session secrets, permissive origins, insecure cookie settings, development bootstrap options, and missing environment classification. Development defaults remain convenient only when the environment is explicitly development.

### HTTP protections

Backend responses include a defined security-header policy. Request body limits reject oversized payloads before domain handling. Rate limits are bounded per authenticated session and route class, with stricter mutation limits. Health and readiness endpoints remain available without creating an amplification path.

Frontend production serving adds matching headers, including content-type protection, frame restrictions, referrer policy, permissions policy, and a content security policy compatible with the built assets.

### Observability

Structured request logs include request ID, correlation ID, route template, method, status, duration, and workspace identifier when authenticated. Sensitive bodies, cookies, CSRF values, tokens, note content, and evidence payloads are excluded.

Metrics cover request count, latency, errors, database failures, lifecycle outcomes, search duration and result count, ranking input and duration, brief generation and staleness, recommendation transitions, idempotency conflicts, audit/outbox failures, and outbox backlog. Metrics use bounded labels and contain no user content.

## Backup and recovery

The acceptance workflow creates representative data across every Phase 1 table before backup. Verification restores into an isolated PostgreSQL 18 database and checks:

- archive SHA-256;
- Alembic head;
- per-table row counts;
- representative record checksums;
- composite workspace constraints;
- append-only audit protection;
- lifecycle restoration fields;
- PKOS mapped columns;
- search index/query readiness;
- application readiness;
- completion within the 600-second development RTO.

The recovery command emits a timestamped machine-readable and human-readable evidence report. Backup retention and RPO are explicit in the runbook.

## CI and security

CI continues frozen backend and frontend installs and adds:

- high and critical dependency enforcement for Python and JavaScript;
- filesystem secret and vulnerability scanning;
- SBOM generation;
- scans of the built backend and frontend container images;
- the expanded Playwright and accessibility suites;
- representative-data performance smoke tests;
- populated backup/restore verification;
- acceptance evidence validation that checks results, not only file existence.

Required checks run on pull requests. Main-branch CI also runs the final acceptance checks needed to prove the merged head.

## Performance evidence

The documented dataset remains authoritative: 10,000 tasks, commitments, risks, and events; 50,000 notes; and 100,000 audit rows. Automated evidence records dashboard p95 below 2 seconds, search p95 below 500 ms locally and 800 ms in CI, ranking of 10,000 eligible entities below 500 ms, task and commitment mutation p95 below 300 ms, brief generation p95 below 2 seconds, and no query above the approved statement timeout.

Performance fixtures may be generated deterministically to avoid committing large datasets.

## Operations and evidence

Runbooks define environment ownership, deployment, migration limitations, rollback, restore, post-deployment smoke checks, and evidence retention. Rollback uses application image rollback plus forward-fix migrations unless a tested Alembic downgrade is explicitly available.

The daily-use template records seven consecutive dates, operator, start and completion time, failures, recovery action, and outcome. Phase 1 status remains in progress until this record and all automated evidence are complete.

## Documentation consistency

The roadmap, Phase 1 implementation status, final acceptance record, release gate, setup guide, and primary Phase 1 specification must agree. Status changes are evidence-driven:

- `Implemented` means code exists with focused proof.
- `Verified` means required local or CI evidence is recorded.
- `Complete` means every exit criterion, including daily-use validation, is satisfied.

No document may claim broader coverage than the referenced test performs.

## Error handling and safety

- User input is preserved across recoverable errors.
- Mutations use version and idempotency contracts.
- Cross-workspace resources remain indistinguishable from missing resources.
- Client rendering never injects server-provided HTML.
- Sensitive data is absent from logs, metrics, audit payloads, and test artifacts.
- Backup and deployment scripts fail closed and avoid destructive defaults.
- No migration or recovery test targets a non-test database without an explicit guard.

## Verification hierarchy

Each slice requires:

1. A focused test observed failing for the missing behavior.
2. The focused test passing after the smallest implementation.
3. Related component or backend regression tests.
4. Type, lint, and build checks for the affected package.
5. Browser proof for user-visible workflows.
6. Full Phase 1 acceptance and security gates before status closure.

Snyk Code scanning is required for newly generated first-party code when the configured Snyk tool is available. Findings introduced by the change are fixed and rescanned before handoff. If the tool is unavailable, CI security evidence and that limitation are recorded explicitly.

## Non-goals

- External calendar, email, or messaging connectors.
- Semantic/vector search.
- Autonomous or external recommendation execution.
- Multi-user collaboration.
- Cloud-specific deployment implementation.
- Phase 2 or later domain behavior.
- Redesigning frozen Phase 1 backend contracts without an approved, versioned contract change.

## Completion boundary

Implementation is ready for change review only when all automated Phase 1 gates pass and every release-gate checkbox backed by automation or operational evidence is updated accurately. Final Phase 1 closure additionally requires the completed seven-day daily-use record and explicit human approval.
