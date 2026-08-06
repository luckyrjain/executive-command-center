# Team suggestions review page

## Context

Migration `0050_phase6_team_linkage.py` gave `repositories` and
`engineering_work_items` a "hybrid: auto-suggest, human confirms" team
link: every adapter sync (GitHub, GitLab, Jira) writes a best-effort
`suggested_team_name` (the repo's owning org/group/namespace, or the
Jira project name) on every row, and a human confirms the real link via
`POST .../repositories/{id}/team` or `POST .../work-items/{id}/team`,
which sets `team_entity_id` against a real `pkos_nodes` row of
`kind='team'`.

That confirm UI exists today (`RepositoriesPanel.tsx`'s `TeamAssignment`
component) but only inline, one row at a time, buried in the full
repository list. There is no page that surfaces "here is everything
still waiting on a team decision," and no way to act on several rows
sharing the same suggested name in one step. This spec adds that page.

User-inference (resolving GitLab/GitHub commit authors or members to
`pkos_nodes` people) is an explicitly separate, larger effort — no
contributor/member sync exists in either adapter today — and is out of
scope here.

## Scope

- Both `repositories` and `engineering_work_items` (GitHub, GitLab, and
  Jira all write `suggested_team_name` already).
- Grouped review: one row per distinct `suggested_team_name`, not per
  item — a workspace with 12 repos all under the same GitLab group
  should confirm them in one action, not 12.
- Bulk confirm and bulk dismiss, both via new backend endpoints (not a
  client-side loop over the existing per-item endpoint) — a bulk
  operation needs one transaction, not N independent ones that can
  partially fail.
- The existing per-row `TeamAssignment` UI in `RepositoriesPanel.tsx` is
  unaffected and stays as-is.

## Data model

New migration, following `0050`'s own precedent exactly. Two new
nullable columns, one per table:

- `repositories.team_suggestion_dismissed_at` (`TIMESTAMPTZ`, nullable)
- `engineering_work_items.team_suggestion_dismissed_at` (`TIMESTAMPTZ`,
  nullable)

No new table, no new FK. Reuses `team_entity_id`'s existing validation
(`_validate_team_entity`) and the existing `team_assignment_version` /
`team_assignment_updated_by` audit columns unchanged.

**Sync-silent, with one reset rule.** Like `team_entity_id`, no
adapter's `ON CONFLICT ... DO UPDATE` ever *sets*
`team_suggestion_dismissed_at`. It only ever *clears* it, and only when
the incoming `suggested_team_name` differs from the row's current
stored value — a dismissal is a judgment about that specific suggested
name; if the repo moves namespace/project (or the Jira issue moves
project), the old dismissal must not silently suppress a brand-new
suggestion. Each adapter's existing upsert SQL (`github_adapter.py`,
`gitlab_adapter.py`, `jira_adapter.py`) gains a `CASE` comparing
`EXCLUDED.suggested_team_name` against the existing row's value:
matches → keep `team_suggestion_dismissed_at` as-is; differs → `NULL`.

## Backend endpoints

### `GET /api/v1/engineering/team-suggestions`

Aggregates both tables, `GROUP BY suggested_team_name`, filtered to
`team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL AND
suggested_team_name IS NOT NULL`. Sorted by total count descending.

```
{
  "items": [
    {
      "suggested_team_name": "Platform",
      "repository_count": 4,
      "work_item_count": 2,
      "sample_items": [
        {"id": "...", "resource_type": "repository", "name": "..."},
        ...  # capped, e.g. first 5 across both types combined
      ]
    },
    ...
  ]
}
```

### `POST /api/v1/engineering/team-suggestions/confirm`

Body: `{ "suggested_team_name": str, "team_entity_id": UUID }`. Requires
`Idempotency-Key`, using the exact same `_lock_idempotency` /
`_load_cached` / `_store_idempotency` helpers the existing single-item
team endpoints already call — a replayed request returns the cached
`{updated, skipped_unauthorized}` response rather than re-running the
update.

1. Validate `team_entity_id` via `_validate_team_entity`.
2. In one transaction, for each of `repositories` and
   `engineering_work_items`: `SELECT id FROM <table> WHERE workspace_id
   = :ws AND suggested_team_name = :name AND team_entity_id IS NULL AND
   team_suggestion_dismissed_at IS NULL FOR UPDATE` to get the candidate
   row ids and lock them.
3. For each candidate id, call `authz.authorize(..., action="write")`
   (an application-level check, not expressible in the `SELECT`/`UPDATE`
   itself) — authorized ids proceed to `UPDATE ... SET team_entity_id =
   :id, team_assignment_version = team_assignment_version + 1,
   team_assignment_updated_by = :actor WHERE id = :row_id`; unauthorized
   ids are added to `skipped_unauthorized` and left untouched.
4. For each updated row, call the existing
   `_write_team_assignment_side_effects` — a bulk-confirmed row's audit
   trail is indistinguishable from one confirmed individually; no new
   event shape.
5. Response: `{ "updated": [...ids], "skipped_unauthorized": [...ids] }`,
   cached under the request's `Idempotency-Key`.

No `expected_version`. This isn't editing one known row with a version
the client already has — it's "confirm whatever is still unconfirmed
under this name right now." The `WHERE team_entity_id IS NULL AND
team_suggestion_dismissed_at IS NULL` clause makes a replay or a
concurrent call naturally match zero rows the second time; there is no
window for a double-assignment.

### `POST /api/v1/engineering/team-suggestions/dismiss`

Body: `{ "suggested_team_name": str }`. Same idempotent, per-row-authz,
`Idempotency-Key`-guarded shape, setting
`team_suggestion_dismissed_at = now()` instead of `team_entity_id`.

### Authorization

Both bulk endpoints authorize **per matched row**
(`authz.authorize(..., action="write")` in the update loop, mirroring
how the existing single-item endpoints authorize their one row) —
skipping rows the actor can't write rather than failing the whole
batch. A bulk endpoint must never grant more access than confirming
each row individually already would.

## Frontend

New tab in the Engineering workspace's nested tablist
(`EngineeringWorkspace.tsx`, alongside Repositories / Work Items /
etc.): **"Team suggestions"**, backed by a new
`TeamSuggestionsPanel.tsx`.

One row per distinct `suggested_team_name`:

- Suggested name, repository/work-item counts, expandable preview of
  `sample_items`.
- Team picker — reuses the same `listTeams()` query
  `RepositoriesPanel.tsx`'s `TeamAssignment` already uses — plus a
  **Confirm** button calling the bulk-confirm endpoint.
- A **Dismiss** button calling the bulk-dismiss endpoint.

On success, the group is removed from the list (React Query cache
invalidation on both the suggestions query and the repositories/
work-items list queries, since their `suggested_team_name` /
`team_entity_id` values just changed). Empty state: "No pending team
suggestions." A partial-authorization response ("Assigned 4 of 5 — 1
skipped: insufficient permission") is surfaced inline rather than
treated as an error.

The existing per-row `TeamAssignment` UI in `RepositoriesPanel.tsx` is
unchanged — still useful when looking at one specific repository.

## Error handling & concurrency

- Bulk confirm/dismiss re-checks `team_entity_id IS NULL AND
  team_suggestion_dismissed_at IS NULL` inside the `UPDATE ... WHERE`
  itself, not as a separate pre-check — a row someone else already
  confirmed between page load and click is silently excluded, never
  double-assigned.
- `_validate_team_entity` failure (team deleted/deactivated since page
  load) → 400, identical to the existing single-item endpoint's
  behavior.
- The adapter-side dismiss-reset rule only ever *clears*
  `team_suggestion_dismissed_at` — a sync can never dismiss something a
  human hasn't already dismissed.

## Testing

- Backend: new `tests/test_engineering_team_suggestions_postgres.py` —
  aggregation grouping/filtering correctness (both resource types,
  excludes confirmed/dismissed/null-suggestion rows), bulk confirm
  (multi-row, cross-table, partial-authorization skip, idempotent
  replay producing zero further updates), bulk dismiss, and the
  adapter-side dismiss-reset-on-changed-suggestion behavior (one test
  per adapter: GitHub, GitLab, Jira).
- Frontend: `TeamSuggestionsPanel.test.tsx` — renders groups, confirm
  flow, dismiss flow, partial-authorization display, empty state —
  mirroring `RepositoriesPanel.test.tsx`'s existing conventions.
- No new perf-budget test: the aggregation query groups by distinct
  suggested names, not per-row, so it stays small regardless of
  workspace repo/work-item count.

## Out of scope

- User/contributor inference (resolving commit authors, MR assignees,
  or project members to `pkos_nodes` people). No contributor sync
  exists in any adapter today; this is separate, larger follow-up work
  with its own design.
- Normalizing/fuzzy-matching suggested names (e.g. case-insensitive or
  near-duplicate grouping). Exact string match only for this pass — can
  be revisited if real-world suggestion noise justifies it.
