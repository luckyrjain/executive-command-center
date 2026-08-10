# Team suggestions: create team inline

## Context

`docs/superpowers/specs/2026-08-06-team-suggestions-review-page-design.md`
shipped the grouped review page (`TeamSuggestionsPanel.tsx`) for
migration `0050_phase6_team_linkage.py`'s "auto-suggest, human confirms"
team link. That spec explicitly scoped out team creation: Confirm only
works against a `pkos_nodes` team that already exists, picked from a
dropdown (`GET /api/v1/knowledge/entities?kind=team`). If no team
exists yet for a suggested name, Confirm is a dead end -- a user has to
leave the page, create the team elsewhere (Knowledge -> Entities), then
come back and pick it from the now-refreshed dropdown.

The original intent (from the review-page brainstorm) was to be able to
add the team *from the suggestion itself*, in one action. This spec
covers that gap. No backend changes -- both endpoints this needs
already exist: `POST /api/v1/knowledge/entities` (generic entity
create, used elsewhere by `EntityExplorer.tsx`) and the existing
`POST /api/v1/engineering/team-suggestions/confirm`.

## Scope

- `frontend/src/features/engineering/TeamSuggestionsPanel.tsx` only.
- Per suggestion row (`SuggestionRow`), add a second path to the same
  outcome the existing dropdown+Confirm path reaches: link every
  unconfirmed, undismissed row in the group to a team entity. The new
  path creates that entity first instead of requiring it to pre-exist.
- The existing dropdown+Confirm path is unchanged and stays available
  (e.g. for confirming against a team that already exists for an
  unrelated reason).
- Out of scope: any change to `POST .../team-suggestions/confirm`,
  `POST /api/v1/knowledge/entities`, or the per-row `TeamAssignment`
  UI in `RepositoriesPanel.tsx`.

## Frontend design

**New state per `SuggestionRow`:** `newTeamName`, a text input seeded
from `group.suggested_team_name` (via `useState(group.suggested_team_name)`),
editable before creating.

**New mutation, `createAndConfirmMutation`:**
1. `POST /api/v1/knowledge/entities` with `{ kind: 'team', canonical_name: newTeamName.trim() }`
   via `apiRequest<KnowledgeEntity>`, mirroring `EntityExplorer.tsx`'s
   existing create call.
2. On success, call the existing `confirmSuggestion(group.suggested_team_name, entity.id)`
   helper already defined in this file -- reuses the same request
   shape and endpoint the dropdown path uses, just with a freshly
   created id instead of a selected one.
3. `onSuccess` of the combined flow: reuse the existing `invalidate()`
   helper (`team-suggestions`, `repositories`, `work-items` query
   keys) and additionally invalidate `['knowledge', 'entities', 'team']`
   so the new team appears in every other row's dropdown without a
   reload.
4. `lastResult` state updates the same way the existing confirm/dismiss
   mutations do, so the "N of M applied, skipped: insufficient
   permission" status line works unchanged for this path too.

**Error handling:** if step 1 (create) fails, nothing else runs --
same as any other failed mutation in this file, error surfaces via the
existing `role="alert"` pattern. If step 1 succeeds but step 2
(confirm) fails, the created team entity is *not* rolled back: it now
exists and appears in the dropdown (via the entities-query
invalidation triggered even on partial failure), so the user can retry
via either path. This matches how entity creation already works
elsewhere in this codebase -- creation is never speculative or
transactional across an unrelated follow-up call.

**No new validation:** no client- or server-side check for a
case-insensitive name collision against an existing team. Matches
`EntityExplorer.tsx`'s existing create-entity behavior; duplicate teams
are reconciled the same way any duplicate entity is today, through the
existing resolution-candidates merge workflow, not prevented at
creation.

**Layout, per row** (two independent ways to reach the same Confirm
outcome):

```
[Select a team… ▾]              [Confirm]           [Dismiss]
[team name, editable, prefilled] [Create & confirm]
```

Both controls stay enabled/disabled independently based on their own
mutation's `isPending` plus the row's overall `busy` flag (extended to
include `createAndConfirmMutation.isPending`). The "Create & confirm"
button is disabled when the trimmed name is empty, mirroring the
existing dropdown Confirm button's disabled-when-empty rule.

## Testing

- Frontend component test (existing test file for this panel, if one
  exists -- otherwise add one): create-and-confirm happy path (mocked
  `apiRequest` create + confirm calls, assert both fire in order and
  the group disappears/updates after invalidation); create succeeds
  but confirm fails (assert team entity remains selectable, error
  surfaces); empty/whitespace-only name keeps the button disabled.
- No backend test changes needed -- no backend code changes in this
  spec.
