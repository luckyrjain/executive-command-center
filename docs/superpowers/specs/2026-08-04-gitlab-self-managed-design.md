---
id: GITLAB-SELF-MANAGED-DESIGN
title: GitLab Self-Managed Instance Support — Design
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on:
  - PHASE-006
  - docs/phases/phase-006/CONNECTOR-CONTRACT.md
---

# GitLab Self-Managed Instance Support — Design

**Status of this document:** planning artifact only. It does not by itself authorize implementation — per this repository's own governance (`docs/phases/README.md`), a phase's contracts must move from Draft to Approved before code lands. This document proposes updating `docs/phases/phase-006/CONNECTOR-CONTRACT.md` (already Approved for Implementation) rather than opening a new phase, since this is an extension of an existing, shipped connector, not a new domain.

## Problem

`gitlab_adapter.GitLabAdapter` (Phase 6 Task 3) hardcodes `https://gitlab.com` as the only reachable GitLab host (`GITLAB_API_BASE_URL`/`_GITLAB_WEB_BASE_URL` module constants, `gitlab_adapter.py:119-120`). A workspace whose GitLab lives on a private, self-managed instance (e.g. `gitlab-ee.mpokket.org`) cannot connect it today.

## Requirements (confirmed with repository owner)

- A workspace must be able to connect **both** gitlab.com and one or more self-managed instances at once — not a replacement, an addition.
- Connections are already per-user, not workspace-shared: `connector_accounts.owner_id`/`visibility` (Phase 8 Task 3, migration `0063_phase8_authz_visibility.py`) already scopes this correctly, and any active `member` (not just `owner`/`admin`) already holds the `write` action needed to create one (`ecc/platform/authz.py:120-122`). **No change needed here** — confirming existing behavior already satisfies "per user."
- Target instance for this activation: `gitlab-ee.mpokket.org`, confirmed via live TLS check to use a publicly-trusted Amazon/ACM-issued certificate (`issuer=C=US, O=Amazon, CN=Amazon RSA 2048 M04`) — **not self-signed**. Custom CA trust configuration is explicitly out of scope for this activation; the system CA bundle `httpx` already trusts is sufficient.

## Decision: how the host travels through the system

`ConnectorAdapter.authorize(credential: str)` (`connectors.py`) is a shared `Protocol` every adapter (GitHub, GitLab, Jira, Datadog, sandbox) implements with an identical signature. Adding a second `host` parameter would change that Protocol for every adapter to serve one connector's need — against this codebase's own precedent of narrow, adapter-local solutions (`jira_adapter.py`'s own module docstring explains it takes exactly this approach for Jira's own multi-tenancy problem).

**Chosen approach: mirror `jira_adapter.py`'s existing `site|email|api_token`-encoded credential pattern.** GitLab's credential becomes `host|token` (e.g. `gitlab-ee.mpokket.org|glpat-xxxxxxxxxxxx`). No `ConnectorAdapter` Protocol change, no `connector_accounts` schema migration — `credential` is already an opaque, encrypted, provider-defined string (`encrypted_credentials`, migration `0044_phase6_connector_platform.py`).

**Discoverability without a new column:** `ConnectorAuthorization.external_account_id` becomes `f"{host}:{gitlab_user_id}"` instead of a bare user ID. This is already returned verbatim in `ConnectorAccountResponse.external_account_id` (`connector_accounts.py:385`) — so `GET /engineering/connectors` already shows which instance a connection points to, with zero new response fields. It also closes the collision risk for free: the existing `UniqueConstraint(workspace_id, provider, external_account_id)` (migration `0044`) now naturally distinguishes gitlab.com's user ID `42` from `gitlab-ee.mpokket.org`'s user ID `42` — two different composite strings, same constraint, no migration.

### Rejected alternative: dedicated `host` column

Considered and rejected. Would give a first-class, directly-queryable field, but changes a schema shared by four other providers for one connector's need, and duplicates what `external_account_id` already carries once host-prefixed. The one real cost of the chosen approach — host isn't independently filterable in SQL without string-parsing `external_account_id` — isn't a requirement here (no filtering-by-host feature was asked for) and can be revisited if one is.

## Code changes

**`gitlab_adapter.py`:**
- Add `_parse_credential(credential: str) -> tuple[str, str]` returning `(host, token)`, mirroring `jira_adapter.py:161`'s `_parse_credential` shape. Validate `host` against a generic hostname pattern (RFC 1035 label rules — GitLab self-managed hosts are arbitrary customer domains, unlike Jira's `*.atlassian.net`-locked `_JIRA_SITE_PATTERN`); reject anything containing a scheme, path, or whitespace. The adapter always builds `https://{host}/...` itself — the credential never supplies a scheme.
- Remove the two module-level constants as fixed values. Every method that currently reads `GITLAB_API_BASE_URL`/`_GITLAB_WEB_BASE_URL` (`authorize`, `backfill`, `incremental_sync`, `refresh_permissions`, `disconnect`, `_safe_source_url`) derives `api_base_url`/`web_base_url` from the parsed host instead — each of these already receives the credential (directly, or via `ConnectorAccountContext.credential`, already decrypted per-call, identical to how `jira_adapter.py:410/524/540` already re-parse their own credential per method).
- `_safe_source_url` takes `web_base_url` as a parameter instead of closing over the module constant.
- `authorize()` builds `external_account_id=f"{host}:{body['id']}"`.

**`connectors.py` / `connector_accounts.py`:** no change. `ConnectorCreateRequest.credential`'s docstring gains a one-line example for GitLab's `host|token` shape, matching however Jira's format is already documented there (or added alongside it if it isn't yet).

**`docs/phases/phase-006/CONNECTOR-CONTRACT.md`:** add a "Task N status" section (this activation) documenting the `host|token` credential format and the host-prefixed `external_account_id`, in the same style as the existing Jira/Datadog sections.

**Frontend (`ConnectorHealthPanel.tsx`):** the GitLab credential input gets a placeholder/helper text showing the `host|token` format. No other change — the connector list already renders `external_account_id`, which now carries the host.

## Migration

None. Reuses `credential`/`encrypted_credentials` (opaque) and `external_account_id` (already free text) exactly as they exist today.

## Security

The host is a member-supplied value the backend makes outbound server-side HTTP calls to — the same shape Jira's `site` field already has today (accepted, precedented risk, not new). Enforced: the adapter always constructs the URL itself with a fixed `https://` scheme; the credential can never inject a scheme, port override, or path. No SSRF allowlist is added — matching Jira's own current posture exactly, disclosed here rather than silently assumed. Broader SSRF hardening across all host-accepting connectors, if ever needed, is a separate, larger cross-cutting task, not scoped to this one.

## Scope

**In:** repository sync against a self-managed GitLab host, using a Personal Access Token, over standard (system-CA-trusted) TLS.

**Out (explicitly, not silently dropped):**
- Custom/self-signed CA trust config — not needed per the confirmed cert check; revisit if a future instance needs it.
- Work items/changes/reviews for GitLab — already out of scope for GitLab entirely (repositories-only, pre-existing limitation, unrelated to this change).
- OAuth app flow — PAT only, matching every other connector's current scope.
- Any UI beyond the existing credential-input placeholder text.

## Testing

Extend `tests/test_engineering_gitlab_sync_postgres.py`:
- A self-managed-host `authorize()`/`backfill` case parallel to the existing gitlab.com case (fake HTTP responses keyed by host, not a live network call — matching this suite's existing fake-adapter-response convention).
- Malformed-host credential rejection (scheme included, whitespace, empty host).
- Cross-host non-collision: two connector accounts with the identical numeric GitLab user ID but different hosts both connect successfully in the same workspace.
