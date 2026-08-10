# Team suggestions: create team inline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user create a new team entity directly from a `TeamSuggestionsPanel` row and have it confirmed against that group in one action, instead of requiring the team to already exist in the dropdown.

**Architecture:** Pure frontend change, one file plus its test file. Adds a second mutation (`createAndConfirmMutation`) to the existing `SuggestionRow` component that chains `POST /api/v1/knowledge/entities` (create, `kind: 'team'`) into the existing `confirmSuggestion` helper's `POST /api/v1/engineering/team-suggestions/confirm` call. No backend changes — both endpoints already exist and are already used elsewhere in this file/codebase.

**Tech Stack:** React, TanStack Query (`useMutation`), Vitest + Testing Library (`fireEvent`, `waitFor`), existing `apiRequest` client wrapper.

## Global Constraints

- No backend changes — spec (`docs/superpowers/specs/2026-08-10-team-suggestions-inline-create-design.md`) scopes this to `TeamSuggestionsPanel.tsx` only.
- No new client- or server-side duplicate-name validation — matches `EntityExplorer.tsx`'s existing create-entity behavior (spec, "No new validation").
- On create-success/confirm-failure, do not roll back the created team entity — it must remain usable via the existing dropdown (spec, "Error handling").
- New team-name input starts pre-filled with `group.suggested_team_name`, remains editable (spec, "Frontend design").
- "Create & confirm" button disabled when the trimmed name is empty (spec, "Layout, per row").

---

### Task 1: Add create-and-confirm mutation and inline-create UI to `SuggestionRow`

**Files:**
- Modify: `frontend/src/features/engineering/TeamSuggestionsPanel.tsx`
- Test: `frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx`

**Interfaces:**
- Consumes: `apiRequest<T>(path, options)` from `../../api/client` (already imported in this file); `KnowledgeEntity` type from `../knowledge/types` (not yet imported in this file — add the import); existing `confirmSuggestion(suggestedTeamName: string, teamEntityId: string): Promise<TeamSuggestionActionResponse>` helper (already defined in this file, unchanged).
- Produces: no new exports — `SuggestionRow` and `TeamSuggestionsPanel` remain the file's only export (`TeamSuggestionsPanel` default export unchanged).

This is one task because the mutation, the input, the button, and their tests are one deliverable — a reviewer can't meaningfully approve "the mutation" without "the button that triggers it."

- [ ] **Step 1: Write the failing test for the happy path (create, then confirm, group disappears)**

Add to `frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx`, inside the existing `describe('TeamSuggestionsPanel', ...)` block, after the `'confirming posts the suggested name and chosen team, then removes the group'` test:

```tsx
  it('creating a team from the suggestion posts create then confirm, then removes the group', async () => {
    stubFetch({ groups: [group()], teams: [] })
    renderPanel()
    await screen.findByText('acme')

    fireEvent.click(screen.getByRole('button', { name: 'Create & confirm' }))

    await waitFor(() => expect(screen.queryByText('acme')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const createCall = calls.find(([url, init]) => init?.method === 'POST' && String(url).includes('/knowledge/entities'))
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({ kind: 'team', canonical_name: 'acme' })

    const confirmCall = calls.find(([url, init]) => init?.method === 'POST' && String(url).includes('/team-suggestions/confirm'))
    expect(JSON.parse(String(confirmCall?.[1]?.body))).toEqual({ suggested_team_name: 'acme', team_entity_id: 'new-team-1' })
  })
```

This requires `stubFetch` to know how to answer a `POST /knowledge/entities` call with a created entity. Update the `stubFetch` helper in the same test file — replace its current unconditional `GET` handling of `/knowledge/entities` with a method check, since it now needs to handle both `GET` (list) and `POST` (create):

```tsx
function stubFetch({
  groups,
  teams = [],
}: {
  groups: TeamSuggestionGroup[]
  teams?: { id: string; canonical_name: string }[]
}) {
  // Stateful, not a fixed response -- proves confirm/dismiss remove the
  // group from the next refetch, mirroring RepositoriesPanel.test.tsx's
  // own identical pattern for the same reason.
  const state = groups.map((g) => ({ ...g }))
  let nextTeamId = 1
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/knowledge/entities')) {
        if (init?.method === 'POST') {
          const body = JSON.parse(String(init.body)) as { kind: string; canonical_name: string }
          return response({ id: `new-team-${nextTeamId++}`, kind: body.kind, canonical_name: body.canonical_name })
        }
        return response({ items: teams })
      }
      if (init?.method === 'POST' && url.includes('/team-suggestions/confirm')) {
        const body = JSON.parse(String(init.body)) as { suggested_team_name: string }
        const index = state.findIndex((g) => g.suggested_team_name === body.suggested_team_name)
        const removed = index >= 0 ? state.splice(index, 1)[0] : undefined
        const count = removed ? removed.repository_count + removed.work_item_count : 0
        return response({
          updated: Array.from({ length: count }, (_, i) => `id-${i}`),
          skipped_unauthorized: [],
        })
      }
      if (init?.method === 'POST' && url.includes('/team-suggestions/dismiss')) {
        const body = JSON.parse(String(init.body)) as { suggested_team_name: string }
        const index = state.findIndex((g) => g.suggested_team_name === body.suggested_team_name)
        if (index >= 0) state.splice(index, 1)
        return response({ updated: [], skipped_unauthorized: [] })
      }
      return response({ items: state })
    }),
  )
}
```

(Note: `nextTeamId` starts at 1, so the first created team in any test is `new-team-1` — matches the assertion above. The dismiss branch's response shape — `{ updated, skipped_unauthorized }` — is unchanged from the original `stubFetch`; only the `/knowledge/entities` branch gained the `POST` case.)

- [ ] **Step 2: Run the new test to verify it fails**

Run: `pnpm --filter @ecc/frontend vitest run src/features/engineering/TeamSuggestionsPanel.test.tsx`
Expected: FAIL — no button with accessible name `'Create & confirm'` exists yet (`TestingLibraryElementError: Unable to find an accessible element with the role "button" and name "Create & confirm"`).

- [ ] **Step 3: Write the failing test for disabled-when-empty**

Add to the same `describe` block, after the `'disables Confirm until a team is picked'` test:

```tsx
  it('disables Create & confirm when the team name is empty', async () => {
    stubFetch({ groups: [group()], teams: [] })
    renderPanel()
    await screen.findByText('acme')
    const createButton = screen.getByRole('button', { name: 'Create & confirm' })
    expect(createButton.hasAttribute('disabled')).toBe(false)

    fireEvent.change(screen.getByLabelText('New team name for acme'), { target: { value: '   ' } })
    expect(createButton.hasAttribute('disabled')).toBe(true)

    fireEvent.change(screen.getByLabelText('New team name for acme'), { target: { value: 'Acme Platform' } })
    expect(createButton.hasAttribute('disabled')).toBe(false)
  })
```

- [ ] **Step 4: Write the failing test for create-succeeds-confirm-fails**

Add to the same `describe` block, after the new happy-path test from Step 1:

```tsx
  it('keeps the created team usable when confirm fails after create succeeds', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) {
          if (init?.method === 'POST') {
            const body = JSON.parse(String(init.body)) as { kind: string; canonical_name: string }
            return response({ id: 'new-team-1', kind: body.kind, canonical_name: body.canonical_name })
          }
          return response({ items: [{ id: 'new-team-1', canonical_name: 'acme' }] })
        }
        if (init?.method === 'POST' && url.includes('/team-suggestions/confirm')) {
          return Promise.reject(new TypeError('fetch failed'))
        }
        return response({ items: [group()] })
      }),
    )
    renderPanel()
    await screen.findByText('acme')

    fireEvent.click(screen.getByRole('button', { name: 'Create & confirm' }))

    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.getByText('acme')).toBeTruthy()
    expect(screen.getByRole('option', { name: 'acme' })).toBeTruthy()
  })
```

- [ ] **Step 5: Run all three new tests to verify they fail**

Run: `pnpm --filter @ecc/frontend vitest run src/features/engineering/TeamSuggestionsPanel.test.tsx`
Expected: all three new tests FAIL (button/label not found), pre-existing tests still PASS.

- [ ] **Step 6: Implement the create-and-confirm mutation and UI**

In `frontend/src/features/engineering/TeamSuggestionsPanel.tsx`:

Add the import for `KnowledgeEntity` alongside the existing `EntityList` import:

```tsx
import type { EntityList, KnowledgeEntity } from '../knowledge/types'
```

Add a `createTeamEntity` helper function next to the existing `listTeams`/`confirmSuggestion`/`dismissSuggestion` functions:

```tsx
function createTeamEntity(canonicalName: string): Promise<KnowledgeEntity> {
  return apiRequest('/api/v1/knowledge/entities', { method: 'POST', body: { kind: 'team', canonical_name: canonicalName } })
}
```

In `SuggestionRow`, add new state next to the existing `teamEntityId`/`lastResult` state:

```tsx
  const [newTeamName, setNewTeamName] = useState(group.suggested_team_name)
```

Add the new mutation next to the existing `confirmMutation`/`dismissMutation`:

```tsx
  const createAndConfirmMutation = useMutation({
    mutationFn: async () => {
      const entity = await createTeamEntity(newTeamName.trim())
      return confirmSuggestion(group.suggested_team_name, entity.id)
    },
    onSuccess: (result) => {
      setLastResult(result)
      invalidate()
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entities', 'team'] })
    },
  })
```

Update `busy` to include the new mutation:

```tsx
  const busy = confirmMutation.isPending || dismissMutation.isPending || createAndConfirmMutation.isPending
```

Add the new input/button after the existing `<div className="work-actions">...</div>` block's closing tag, as a sibling within the row (i.e. a second `<div className="work-actions">` block), and add its error rendering alongside the existing two:

```tsx
      <div className="work-actions">
        <label>
          {`New team name for ${group.suggested_team_name}`}
          <input
            aria-label={`New team name for ${group.suggested_team_name}`}
            type="text"
            value={newTeamName}
            disabled={busy}
            onChange={(event) => setNewTeamName(event.target.value)}
          />
        </label>
        <button
          type="button"
          disabled={!newTeamName.trim() || busy}
          onClick={() => createAndConfirmMutation.mutate()}
        >
          Create & confirm
        </button>
      </div>
```

and, alongside the existing `confirmMutation.isError`/`dismissMutation.isError` alert lines:

```tsx
      {createAndConfirmMutation.isError ? <span role="alert" className="inline-status error-panel">{createAndConfirmMutation.error.message}</span> : null}
```

The full `SuggestionRow` return block, for reference (existing lines unchanged except where noted, new lines marked):

```tsx
  return (
    <li>
      <div>
        <strong>{group.suggested_team_name}</strong>
        <small>{`${group.repository_count} repositories · ${group.work_item_count} work items (${total} total)`}</small>
      </div>
      <ul>
        {group.sample_items.map((item) => (
          <li key={`${item.resource_type}-${item.id}`}>{`${item.name} (${item.resource_type})`}</li>
        ))}
      </ul>
      <div className="work-actions">
        <label>
          {`Assign team for ${group.suggested_team_name}`}
          <select
            aria-label={`Assign team for ${group.suggested_team_name}`}
            value={teamEntityId}
            disabled={busy}
            onChange={(event) => setTeamEntityId(event.target.value)}
          >
            <option value="">Select a team…</option>
            {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
        </label>
        <button type="button" disabled={!teamEntityId || busy} onClick={() => confirmMutation.mutate()}>
          Confirm
        </button>
        <button type="button" disabled={busy} onClick={() => dismissMutation.mutate()}>
          Dismiss
        </button>
      </div>
      {/* NEW */}
      <div className="work-actions">
        <label>
          {`New team name for ${group.suggested_team_name}`}
          <input
            aria-label={`New team name for ${group.suggested_team_name}`}
            type="text"
            value={newTeamName}
            disabled={busy}
            onChange={(event) => setNewTeamName(event.target.value)}
          />
        </label>
        <button
          type="button"
          disabled={!newTeamName.trim() || busy}
          onClick={() => createAndConfirmMutation.mutate()}
        >
          Create & confirm
        </button>
      </div>
      {/* END NEW */}
      {confirmMutation.isError ? <span role="alert" className="inline-status error-panel">{confirmMutation.error.message}</span> : null}
      {dismissMutation.isError ? <span role="alert" className="inline-status error-panel">{dismissMutation.error.message}</span> : null}
      {createAndConfirmMutation.isError ? <span role="alert" className="inline-status error-panel">{createAndConfirmMutation.error.message}</span> : null}
      {lastResult && lastResult.skipped_unauthorized.length > 0 ? (
        <p role="status">
          {`Applied to ${lastResult.updated.length} of ${lastResult.updated.length + lastResult.skipped_unauthorized.length} — ${lastResult.skipped_unauthorized.length} skipped: insufficient permission.`}
        </p>
      ) : null}
    </li>
  )
```

- [ ] **Step 7: Run the full test file to verify all tests pass**

Run: `pnpm --filter @ecc/frontend vitest run src/features/engineering/TeamSuggestionsPanel.test.tsx`
Expected: PASS — all pre-existing tests plus the three new ones from Steps 1, 3, 4.

- [ ] **Step 8: Type-check and lint the frontend**

Run: `pnpm --filter @ecc/frontend typecheck && pnpm --filter @ecc/frontend lint`
Expected: no errors in either command. If lint flags the `&` in the button label (e.g. an a11y/react rule about raw ampersands), replace `Create & confirm` with `Create &amp; confirm` is unnecessary in JSX text (JSX does not require entity-escaping of `&`) — if lint still objects, use `{'Create & confirm'}` as a JSX expression instead of raw text; keep the visible label text identical (`Create & confirm`) either way so the Step 1/3/4 test queries keep matching.

- [ ] **Step 9: Manually verify in the running app**

Backend and frontend are already running (`127.0.0.1:8000`, `127.0.0.1:5173` per the earlier session). In a browser: navigate to Engineering workspace → "Team suggestions" tab. If a pending suggestion group exists, confirm the new "New team name for <name>" input is pre-filled with the suggested name and the "Create & confirm" button is present and enabled; type a name and click it; confirm the group disappears from the list (same behavior as the existing Confirm button) and that a new team now appears in another row's "Assign team" dropdown (if another suggestion group exists) without a manual page reload.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/features/engineering/TeamSuggestionsPanel.tsx frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx
git commit -m "feat(frontend): create team inline from a team suggestion

Confirm previously required a team to already exist in the dropdown.
Add a create-and-confirm path per suggestion row so a team can be
created from the suggestion itself, matching the original review-page
intent (docs/superpowers/specs/2026-08-10-team-suggestions-inline-
create-design.md).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Post-plan check

- Spec's "Frontend design" section: new state, mutation, layout, error handling, no-new-validation — all covered by Task 1.
- Spec's "Testing" section: happy path, create-succeeds-confirm-fails, empty-name-disables-button — all covered by Steps 1/3/4.
- No backend task — spec has none.
