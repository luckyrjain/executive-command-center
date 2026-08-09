---
id: PHASE-010
title: Gmail Connector
status: Approved for Implementation
version: 0.4.4
owner: Lucky Jain
depends_on:
  - PHASE-001
  - PHASE-002
  - PHASE-003
  - PHASE-004
  - PHASE-006
  - PHASE-007
  - RFC-001
  - RFC-004
  - RFC-005
  - STD-001
contracts:
  - phase-010/DATA-MODEL.md
  - phase-010/API-SCHEMAS.md
  - phase-010/SYNC-CONTRACT.md
  - phase-010/PRIVACY-CONSENT-CONTRACT.md
  - phase-010/UX-STATES.md
  - phase-010/TEST-PLAN.md
---

# PHASE-010 — Gmail Connector

## Approved decisions

Status moved from Draft after resolving `docs/superpowers/specs/2026-08-04-phase-10-gmail-connector-design.md`'s five decisions (connector mechanics/Protocol extension; privacy/consent model; OAuth scope, verification reality and rollout gating; `recommendations` create-path extension and AI-tool safety rubric; attention/knowledge integration), reached through direct discussion with the repository owner. In summary: the `ConnectorAdapter` Protocol gains its first OAuth2-authorization-code-grant shape (`get_authorization_url`/`handle_oauth_callback`) and its first re-invocable windowed `backfill`, composing Phase 6's connector mechanics with Phase 7's consent/encryption/deletion framework for the first time; a new `email` `personal_domains` entry at the `high_stakes` tier owns dedicated `email_threads`/`email_messages` tables, encrypted with Phase 7's existing personal-data key while the OAuth token itself uses Phase 6's existing connector-token key; shipping is restricted to an application-enforced internal-user allowlist, which is the actual mechanism keeping this phase outside Google's CASA security-assessment requirement (verified directly against Google's own OAuth verification docs, not assumed); `recommendations`' `execute_target()` gains a `"create"` operation reusing the existing per-resource `Create` models, giving the long-unused `source="ai"` field its first real populator; and email-derived attention items reuse Phase 3's existing deterministic ingestion path, not a new AI-runtime task type. Implementation plan: `docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`. Delivery status: Tasks 1-6 (OAuth2 connector framework, internal allowlist, 30-day backfill, incremental history sync, deduplication, participant entity linking, deterministic awaiting-reply attention integration, the governed recommendations create path, the `email.detect_action` AI-runtime action-detection tool, and on-demand human-facing thread reading plus a per-thread "forget this" deletion control) are complete. Tasks 7-8 remain open; see `docs/phases/phase-010/IMPLEMENTATION-STATUS.md`.

**Disclosed, not yet resolved, per the design doc's own "not yet decided" note:** incremental-sync transport (polling assumed, Cloud Pub/Sub push not yet committed to) and whether `httpx` alone (vs. Google's official client library) suffices are both defaults carried into the implementation plan, not explicit repository-owner sign-off in the same form as the five decisions above.

## Objective

Give users visibility into Gmail correspondence that implies outstanding work, and let them confirm AI-derived tasks/commitments straight from it, without weakening the local-first, consent-driven privacy posture Phase 7 established for sensitive personal data.

## User value

Users see which emails imply a reply, a task or a commitment without manually re-entering them, and confirm every AI-derived proposal before it becomes a real record — governed by the same explicit consent, encryption and deletion guarantees their other personal data already has.

## In scope

- Gmail OAuth2 connection, restricted to an explicit internal-user allowlist.
- `gmail.metadata` synced broadly (headers/labels/thread structure); `gmail.readonly` (full body) used both for on-demand thread reads and for the new AI-runtime tool's proactive action-detection pass over newly synced mail.
- Initial 30-day backfill per connected account, expandable later on an ad-hoc, user-triggered, consent-reverified basis.
- A new `email` personal domain (Phase 7 shape: `domain_consents`-gated, Fernet-encrypted, exportable/deletable), with dedicated `email_threads`/`email_messages` tables rather than the generic `domain_records` shape.
- Deterministic "awaiting reply" attention-item generation from synced metadata, feeding `attention_items` through the same ingestion path tasks/commitments/risks already use.
- A new Phase 4 AI-runtime tool that reads a synced email's content and proposes a task/commitment/risk, grounded against the email as `pkos_evidence`, always requiring human confirmation before any record is created.
- `recommendations`' `execute_target()` extended with a `"create"` operation (today it only updates existing rows), reusing the existing `TaskCreate`/`CommitmentCreate`/`RiskCreate` request models as the create-recommendation's proposed-fields payload.
- `ConnectorAdapter` Protocol extended: `authorize()` split into `get_authorization_url()`/`handle_oauth_callback()` for true 3-legged OAuth2 (no existing adapter needed this — all are PAT-based); `backfill()` gains an optional bounded window parameter.
- Sender/recipient resolution into `pkos_nodes` via Phase 2's existing entity resolution.
- Revoking the `email` domain's consent disconnects the OAuth connector and purges all synced content via Phase 7's existing deletion-job pipeline, in one action.
- A `GmailPanel` inside the existing `PersonalWorkspace` shell; `RecommendationPanel` extended to render and confirm create-type proposals.

## Out of scope

Any Gmail write action (compose, reply, send, archive, label-modify) this phase. Public/general-availability rollout — deferred behind Google's CASA third-party security assessment, which the internal-user allowlist exists specifically to avoid triggering. Google Calendar or any other Google Workspace product. `cross_domain_grants` sharing of the `email` domain into other personal domains. Non-internal users. Any personal-domain connector beyond Gmail.

**Not yet decided, carried forward from this phase's own design brainstorm rather than silently assumed:** incremental sync transport (Gmail History API + Cloud Pub/Sub push notifications vs. simple polling) defaults to polling for this activation, pending confirmation; whether to use Google's official `google-api-python-client` SDK vs. calling the Gmail/OAuth REST APIs directly via the already-approved `httpx` (default: `httpx`, matching every existing connector's own no-new-SDK precedent) is likewise a default, not an explicit sign-off.

## Functional requirements

- Gmail sync of any kind requires an active `domain_consents` row for the `email` domain; revoking it halts sync, disconnects the OAuth grant and deletes synced content.
- Only an account on the internal allowlist (config-driven) may initiate the Gmail OAuth flow at all. As built: `authorize()` is never called for `gmail` at all, and `get_authorization_url()`'s own fixed signature carries no account/email argument to check -- the caller-side allowlist check lives in `gmail_oauth.py`'s router (pre-redirect, against the caller's own account email) and again, authoritatively, inside `handle_oauth_callback` (post-exchange, against the actual Google account).
- Every synced message body is Fernet-encrypted at rest using the existing `ECC_PERSONAL_DATA_ENCRYPTION_KEY` (Phase 7); the Gmail OAuth access/refresh tokens themselves use the existing `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY` (Phase 6) — two already-approved keys used for their already-established purposes, no new key.
- Every AI-proposed task/commitment/risk requires explicit human confirmation via the extended `recommendations` flow before any row is created; nothing is written automatically.
- Every AI-proposed action must cite the synced email it was derived from as `pkos_evidence`, schema-enforced (a proposal with no grounded evidence fails validation, mirroring `attention.explain_item`'s existing fail-closed shape).
- An expand-backfill request re-verifies `email` domain consent is still active at call time, not merely at original connect time.

## Non-functional requirements

- Backfill and incremental sync are quota-aware and resumable, reusing `sync_runs`/`sync_cursors` and the existing GitHub adapter's rate-limit-retry pattern.
- The new AI-runtime tool meets the same evaluation-floor discipline as `attention.explain_item`/`meeting.prep_summary`/`personal.generate_insight`: schema validity rate, grounding rate, prohibited/hallucinated-fact count, latency — each with a real adversarial fixture set (positive "this implies a task" and negative "no action needed" examples) before any prompt version is promoted.

## Architecture impact

`ConnectorAdapter` Protocol gains its first OAuth2-authorization-code-grant implementation and its first windowed backfill. `recommendations`' `execute_target()` gains its first row-creation path (previously update-only). A new `personal_domains` entry composes Phase 6's connector mechanics with Phase 7's consent/encryption framework for the first time — no prior phase needed both.

## Data changes

New migration(s): `email_threads`/`email_messages` tables (Fernet-encrypted body fields, plaintext structural fields); `personal_domains` seed row for `email`; `connector_accounts.provider` CHECK constraint gains `gmail`; `recommendations` schema changes supporting a nullable `target_id` + `"create"` operation with a `proposed_fields` payload; new `pkos_evidence` rows for synced email content referenced by AI-proposed recommendations.

## API changes

New Gmail OAuth initiate/callback endpoints. Existing `connector_accounts`/`sync_runs` endpoints reused as-is. Existing `personal/domains`/`domain_consents` surface reused for the `email` domain. `recommendations` endpoints extended to accept and render create-type proposals.

## Frontend changes

New `GmailPanel` inside `PersonalWorkspace`, alongside `DomainsPanel`/`RecordsPanel`/`InsightsPanel`/`GrantsPanel`/`ExportDeletePanel`. `RecommendationPanel` extended to render a create-type recommendation (no existing `target_id` to fetch) and confirm it into a real task/commitment/risk.

## Security and privacy

Internal-user allowlist is the load-bearing mechanism keeping this phase outside Google's CASA security-assessment requirement (verified against Google's own OAuth verification docs: `gmail.metadata` and `gmail.readonly` are both restricted-tier scopes regardless of usage pattern — narrow scope usage does not itself avoid the audit, staying within Google's OAuth test-user cap does). Email content is classified and encrypted at the same tier Phase 7 uses for `health`/`finance`. Consent revocation is the single action that both disconnects the connector and purges all synced content — no separate, weaker "just disconnect" path exists in this phase's scope.

## Observability

`sync_runs` already provides observable sync history per account. Consent grant/revoke and deletion-job execution emit audit events per Phase 7's existing pattern.

## Test strategy

Cross-workspace isolation; consent-revocation purges connector and content together; encrypted fields never returned in a list/summary view; OAuth flow (authorization-url generation, callback/code-exchange, refresh); internal-allowlist enforcement (rejected for a non-allowlisted account); expand-backfill re-verifies live consent; `recommendations`' new create-path (schema validation, versioned confirm, audit event); the new AI-runtime tool's evaluation-floor adversarial fixtures.

## Acceptance criteria

- A connected internal-allowlisted account backfills its last 30 days of Gmail metadata and produces at least one deterministic "awaiting reply" attention item where the fixture data warrants it.
- An email whose content clearly implies a task produces a grounded, evidence-linked create-type recommendation that becomes a real `tasks` row only after explicit confirmation.
- Revoking `email` domain consent disconnects the OAuth grant and leaves zero readable synced content behind.
- A non-allowlisted account cannot initiate the Gmail OAuth flow.
- The new AI-runtime tool clears its own evaluation floors before its prompt version is promoted.

## Exit criteria

Real dynamic verification against a real test Gmail account (not solely mocked `httpx` transport); evaluation floors met; zero Critical/High findings on independent review; backup/restore exercised against the new encrypted tables.

## Rollback plan

Revoking the `email` domain's consent purges all synced content and disconnects the OAuth grant in one action — there is no partial/soft-disconnect state to reason about. No migration in this phase is destructive to any existing table.

## Deferred backlog

Gmail write actions (compose/reply/send/archive/label-modify); public/general-availability rollout (blocked on Google's CASA assessment); push-notification-based incremental sync; Google Calendar or any other Google Workspace product; `cross_domain_grants` sharing of the `email` domain; non-internal users.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 0.4.4 | 2026-08-06 | Reconciled delivery through Task 6 after the on-demand thread read/forget merge | Lucky Jain |
| 0.4.3 | 2026-08-06 | Reconciled delivery through Task 5 after the email.detect_action merge | Lucky Jain |
| 0.4.2 | 2026-08-06 | Reconciled delivery through Task 4 after the recommendations create-path merge | Lucky Jain |
| 0.4.1 | 2026-08-06 | Reconciled delivery through Task 3 after the attention-integration merge | Lucky Jain |
| 0.4.0 | 2026-08-06 | Added six governed contracts and reconciled delivery through Task 2 | Lucky Jain |
| 0.3.0 | 2026-08-04 | Approved the Phase 10 implementation scope and decisions | Lucky Jain |
