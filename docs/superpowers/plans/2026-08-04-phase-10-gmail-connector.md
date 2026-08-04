# Phase 10 Gmail Connector Implementation Plan

Companion to `docs/superpowers/specs/2026-08-04-phase-10-gmail-connector-design.md` (the decisions) -- this document is the task sequence and per-task scope, mirroring the shape of `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`'s own task breakdown.

**Real external dependency, named up front rather than discovered mid-task:** a Gmail OAuth2 app registration in Google Cloud Console (a project, an OAuth consent screen configured for restricted scopes, a `client_id`/`client_secret` pair, and a registered redirect URI) is required before Task 1's OAuth flow can be exercised against the real Google API. This is not something this session can provision -- it requires the repository owner's own Google account and Google Cloud access. Every task below is fully buildable and unit-testable against a mocked `httpx` transport (the identical `transport: httpx.BaseTransport | None` injection pattern `github_adapter.py` already uses) without those credentials; only this phase's own "real dynamic verification" exit criterion (`PHASE-010-gmail-connector.md`) needs them to actually exist.

## Task 1 -- OAuth framework extension, Gmail connector skeleton, internal allowlist

- `ConnectorAdapter` Protocol extension (`ecc.domains.engineering.connectors`): `authorize(credential: str)` unchanged for every existing adapter; new `get_authorization_url(state: str) -> str` and `handle_oauth_callback(code: str, state: str) -> ConnectorAuthorization` methods, additive to the Protocol, exercised by `GmailAdapter` only this task.
- `backfill()` gains an optional `since: datetime | None` parameter across the Protocol -- existing adapters accept and ignore it (full backfill regardless, matching their current one-shot behavior); `GmailAdapter` is the first to act on it.
- `ecc.domains.personal.gmail_adapter.GmailAdapter`: `get_authorization_url`/`handle_oauth_callback` against Google's real OAuth2 token endpoint via `httpx` (no new RFC-005 dependency, per the design doc's default); `refresh_permissions`/`disconnect` real, `backfill`/`incremental_sync` stubbed (Task 2's own scope).
- Migration: `connector_accounts.provider` CHECK constraint gains `gmail`; `email_threads`/`email_messages` tables (Fernet-encrypted body fields via the existing `ECC_PERSONAL_DATA_ENCRYPTION_KEY`, plaintext structural fields -- thread/message id, sender/recipient, subject, label, timestamps); `personal_domains` seed row for `domain_key='email'`, `classification='high_stakes'`.
- Internal-allowlist enforcement: a config-driven check (env var or settings field, no new table) inside `get_authorization_url()`, rejecting a non-allowlisted account before any redirect is generated.
- `POST /api/v1/personal/gmail/oauth/start`, `GET /api/v1/personal/gmail/oauth/callback` -- the OAuth initiate/callback endpoints; standard `connector_accounts` CRUD reused as-is for everything after the token exchange completes.
- Tests: allowlist rejects a non-listed account before any Google call; authorization-URL generation; callback/code-exchange against a mocked token-endpoint response; refresh-token renewal; encrypted-field-never-returned-in-list-view; workspace isolation.

## Task 2 -- Backfill, incremental sync and entity linking

- `GmailAdapter.backfill`: last-30-days initial sync via `gmail.metadata`, real Gmail API pagination, writing to `email_threads`/`email_messages`. Re-invocable with an explicit wider/narrower `since` window (the "expand later, ad hoc" requirement) -- each re-invocation re-verifies the `email` domain's `domain_consents` row is still active at call time, not merely at original connect time.
- `GmailAdapter.incremental_sync`: polling-based (Gmail History API `historyId` cursor via `sync_cursors`), not push-notification-based this task -- Cloud Pub/Sub remains the disclosed, not-yet-decided item from the design doc.
- Rate-limit handling reuses `github_adapter.py`'s `_request_with_rate_limit_retry` pattern verbatim (still-rate-limited retry degrades to `partial`, never raises).
- Sender/recipient -> `pkos_nodes` resolution via Phase 2's existing entity-resolution path (no new resolution mechanism).
- Tests: initial backfill window correctness; expand-backfill re-verifies consent (a revoked-mid-window consent halts the call); incremental cursor resumability; rate-limit degrade-to-partial; sender/recipient resolution linking.

## Task 3 -- Deterministic attention integration

- "Awaiting reply" heuristic (last message in a thread is inbound, no outbound reply since, sender resolves to a known `pkos_nodes` contact) feeding `attention_items` via the exact ingestion pattern tasks/commitments/risks already use -- no new AI-runtime task type.
- `attention.explain_item` extended to handle a Gmail-sourced item's resource type.
- Tests: heuristic fixtures (inbound-unanswered creates an item; outbound-replied does not; unresolved sender does not); `explain_item` coverage for an email-sourced item.

## Task 4 -- `recommendations` create-path extension

- `execute_target()` (`ecc.domains.governance.recommendation_targets`) gains an `operation="create"` branch per `target_type` (`task`/`commitment`/`risk`), added to the existing per-type operation whitelist. The branch calls the same internal creation function `POST /api/v1/tasks`/`POST /api/v1/commitments`/`POST /api/v1/risks` already use -- no second insert path.
- `RecommendationCreate`: `target_id` becomes `UUID | None` (`None` valid only when `operation="create"`); `proposed_fields` reuses the existing `TaskCreate`/`CommitmentCreate`/`RiskCreate` Pydantic models rather than a new schema shape.
- Frontend: `RecommendationPanel.tsx` extended to render a create-type recommendation (no existing `target_id` to fetch details from) and confirm it into a real new row.
- Tests: create-path schema validation (`target_id` must be null for `operation="create"` and non-null otherwise); versioned confirm; audit event on create; frontend rendering/confirm of a not-yet-existing target.

## Task 5 -- AI-runtime action-detection tool

- New Phase 4 tool (`email.detect_action` or similarly named), prompt, and Pydantic output schema (`has_action: bool`, `target_type: Literal["task","commitment","risk"] | None`, `operation: Literal["create"]`, the relevant `*Create` model's fields, `rationale`, `confidence`) -- fail-closed on a missing required field, matching every other Phase 4 tool.
- Grounding check against the source email's own content, reusing `PersonalInsightOutput`'s established grounding-check pattern; the source email is registered as `pkos_evidence` and cited as the recommendation's evidence.
- Evaluation dataset: positive examples (clear task/commitment language) and negative examples (newsletters, FYI-only mail, already-resolved threads, `has_action: false` as a required-to-clear-the-floor outcome) -- evaluation floors (schema validity rate, grounding rate, prohibited/hallucinated-fact count, latency) match `attention.explain_item`/`meeting.prep_summary`/`personal.generate_insight`'s existing bar exactly.
- Wired into the sync pipeline: on newly synced mail, a proactive `gmail.readonly` fetch feeds this tool; a `has_action: true` result creates a `source="ai"` recommendation via Task 4's new create-path -- never writes a task/commitment/risk directly.
- Tests: schema validation; grounding-check rejection of an ungrounded claim; evaluation floors met against the adversarial dataset; sync-pipeline wiring produces a pending (unconfirmed) recommendation, never an immediate write.

## Task 6 -- On-demand thread reading and caching

- `gmail.readonly` fetch for a specific thread on explicit user open (in addition to Task 5's proactive sync-time fetch); fetched body cached as a normal Phase-7-governed encrypted `email_messages` record, matching Task 5's own fetch-and-store path rather than a second one.
- Per-thread "forget this" action extending Phase 7's existing deletion granularity (deletes the cached body/message content for that thread only, not the whole `email` domain).
- Tests: on-demand fetch-and-cache round-trip; "forget this" removes only the targeted thread's cached content.

## Task 7 -- Consent revocation cascade

- Revoking the `email` domain's `domain_consents` row calls `GmailAdapter.disconnect()` (revokes the OAuth grant) and runs Phase 7's existing deletion-job pipeline against `email_threads`/`email_messages` -- one action, both effects, no partial "disconnected but data remains" state reachable through this path.
- Tests: revocation leaves zero readable synced content and a revoked OAuth grant; no code path disconnects without also purging, or purges without also disconnecting.

## Task 8 -- Executive UX and browser acceptance

- `GmailPanel` inside the existing `PersonalWorkspace` shell (connect/disconnect, allowlist-denied state, sync status, thread list, expand-backfill control, pending-recommendation review reusing the extended `RecommendationPanel`) -- wired into `App.tsx`/`WorkspaceNavigation.tsx` alongside `DomainsPanel`/`RecordsPanel`/`InsightsPanel`/`GrantsPanel`/`ExportDeletePanel`.
- Component tests for the new panel; e2e fixture/scenario registration matching Phase 6 Task 8's/Phase 7 Task 8's own precedent (a real, non-mocked-at-the-component-level browser-acceptance pass with accessibility checks).
