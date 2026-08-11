# GitLab suggested team name: full path, not immediate subgroup

## Context

`gitlab_adapter.py::_suggested_team_name` reads `project.namespace.name`
from GitLab's REST payload -- the project's *immediate* parent
group/subgroup, not the top-level org group. A repo at
`disbursement/neo/disbursement-core-service` gets `suggested_team_name
= "Neo"`, never `"disbursement"`, so the top-level group never appears
as a pending suggestion on the team-suggestions review page even though
every repo under it is present (just bucketed one level too deep).

## Change

REST sync (`namespace` is a dict): use `namespace.get("full_path")` when
present (e.g. `"disbursement/neo"` -- GitLab's default `GET /projects`
response includes `full_path` on the namespace object already, no extra
API call). Fall back to `namespace.get("name")` if `full_path` is
absent.

Webhook sync (`namespace` is a bare string): unchanged. The webhook
payload has no parent info at all -- stays immediate-name-only, same
limitation as today. Update the function's docstring to explain why
REST and webhook now diverge in what they capture.

Side effect: `full_path` is the deterministic lowercase path-slug form,
unlike `name` which mirrors GitLab's mutable, inconsistently-cased
display name -- this also collapses the pre-existing `"Legacy"` /
`"legacy"` suggestion split for REST-synced rows.

Existing DB rows keep their current `suggested_team_name` value until
the next sync touches that repo -- `suggested_team_name` is
sync-refreshed on every `ON CONFLICT ... DO UPDATE`
(`_upsert_repository`), and the existing dismissal-reset `CASE` clause
already clears `team_suggestion_dismissed_at` when the incoming value
differs from the stored one, so no migration or manual step is needed.

## Scope

- `backend/ecc/domains/engineering/gitlab_adapter.py`,
  `_suggested_team_name` only.
- `tests/test_engineering_gitlab_sync_postgres.py`: update existing
  `namespace={...}` REST fixtures to include `full_path`, update
  assertions from the old `name`-only values to the new full-path
  values, add one new test for the fallback-to-`name`-when-`full_path`-
  absent case.
- No schema change, no other adapter (`github_adapter.py`,
  `jira_adapter.py`) touched -- GitHub has no nested-group concept and
  Jira projects are flat, so this ambiguity doesn't apply to either.
- Frontend (`TeamSuggestionsPanel.tsx`) unchanged -- it already renders
  whatever string `suggested_team_name` is.
