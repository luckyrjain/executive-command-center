# Composition Philosophy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each workspace declare a `canvas` or `cards` composition so single-panel pages drop their card chrome onto a white open canvas, while side-by-side and dashboard pages keep cards.

**Architecture:** A required `composition` field on every entry in `frontend/src/navigation/workspaces.ts`, read by `AppShell` in `App.tsx` and written onto a new `div.app-root` as `data-composition`. Three CSS rules in `styles.css` key off that attribute. No component files change. Before/after screenshots prove the `cards` pages are pixel-identical.

**Tech Stack:** React 19, react-router-dom 7, Vitest + Testing Library (jsdom), plain CSS custom properties, Playwright (existing e2e harness and `frontend/e2e/visual-snapshots.mjs`).

**Spec:** `docs/superpowers/specs/2026-09-19-composition-philosophy-design.md`

## Global Constraints

- **`frontend/src/navigation/workspaces.ts`, `frontend/src/App.tsx`, `frontend/src/styles.css`, their tests, and `DESIGN.md` only.** No feature component (`frontend/src/features/**`, `frontend/src/dashboard/**`) may change.
- **The split is fixed (approved).** `canvas` (9): recommendations, notes, planner, meeting-prep, search-audit, automation, engineering, personal, collaboration (path `/team`). `cards` (6): today, attention, work, schedule, risks, knowledge.
- **`composition` is a required field**, not optional with a default. An unmatched path resolves to `'cards'` in `compositionForPath` only (the not-found route keeps today's look).
- **The canvas surface is white:** `background: var(--color-surface-panel)`, never `--color-surface-page`.
- **Tokens only:** no raw hex/rgb color and no raw `font-size` in `styles.css` (`pnpm check:tokens` enforces it). Use `--color-surface-panel`, `--color-border-subtle`, `--space-6`.
- **`cards` routes must be pixel-identical** before and after (maximum per-channel screenshot delta 0). This is a hard gate in Task 4.
- **Untouched by this work:** `.dashboard-card`, `.connector-card`, `.simulation-panel`, `.brief-panel`, list rows, `div.workspace-switcher`'s own styling, every design token, `--shadow-panel`'s value.
- **Out of scope:** new tokens, any list-row change, a mobile-specific composition, icons/action hierarchy, the Today rebuild.
- **Commit trailer:** every commit message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- **Work only in the worktree** `/Users/luckyjain/Projects/ecc-worktree-composition` (branch `composition-philosophy`), commands from its `frontend/` directory unless a step says otherwise. Confirm `git branch --show-current` prints `composition-philosophy` before every commit. Stage explicit paths only (never `git add -A` or `.`). Do not push and do not open a PR. Another Claude session uses `/Users/luckyjain/Projects/executive-command-center` on a different branch: never work there.
- **Snapshot root** (session scratchpad, outside the repo): `/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/composition-snaps`, referred to below as `$SNAPS`.
- The shell is zsh: never `echo` a bare string of `=` characters.

## File Structure

- Modify `frontend/src/navigation/workspaces.ts`: the `Composition` type, the required `composition` field, `compositionForPath` (Task 2).
- Modify `frontend/src/navigation/workspaces.test.ts`: pin the split and the helper (Task 2).
- Modify `frontend/src/App.tsx`: `div.app-root` wrapper carrying `data-composition`, `WorkspaceSwitcher` moved into `AppShell` (Task 2).
- Modify `frontend/src/App.test.tsx`: attribute and structure tests (Task 2).
- Modify `frontend/src/styles.css`: the canvas rules (Task 3).
- Modify `DESIGN.md`: the Composition section and related edits (Task 5).

---

### Task 1: Capture the "before" baseline

**Files:**
- No source changes. Output goes to `$SNAPS/before` (outside the repo).

**Interfaces:**
- Produces (used by Task 4): `$SNAPS/before` and `$SNAPS/before2`, 17 PNGs each, captured from the tree before any change to `frontend/src`.

- [ ] **Step 1: Confirm nothing under `frontend/src` has changed yet**

Run from the worktree root: `git diff 53d0ae8 HEAD --stat -- frontend | cat`
Expected: no output (the branch so far only adds the spec and plan docs).

- [ ] **Step 2: Build and capture twice**

Run from `frontend/`:

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/composition-snaps
VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build
pnpm visual:snapshots capture "$SNAPS/before"
pnpm visual:snapshots capture "$SNAPS/before2"
ls "$SNAPS/before" | wc -l
```

Expected: build succeeds; each capture prints `captured 17 screenshots to ...`; the count is `17`. If port 4173 is busy, report it rather than killing unknown processes.

- [ ] **Step 3: Confirm the capture is deterministic where it must be**

Run: `pnpm visual:snapshots compare "$SNAPS/before" "$SNAPS/before2"`
Expected: 17 rows, every `maxDelta` at most 1. The rows for `desktop-today`, `desktop-attention`, `desktop-work`, `desktop-risks`, `desktop-knowledge`, `desktop-schedule`, `desktop-swatch` and `mobile-today` must be exactly `0` (Task 4 depends on that). A known render-noise of 1 on `desktop-engineering` is acceptable. If any of those eight rows is non-zero, the baseline is unusable: stop and report BLOCKED with the rows.

- [ ] **Step 4: Report**

No commit. The final message contains the 17-row compare table from Step 3.

---

### Task 2: Add the `composition` setting and the `data-composition` attribute

**Files:**
- Modify: `frontend/src/navigation/workspaces.ts`
- Modify: `frontend/src/navigation/workspaces.test.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces (used by Tasks 3 to 5): `export type Composition = 'canvas' | 'cards'`; `WorkspaceEntry.composition: Composition` (required); `export function compositionForPath(pathname: string): Composition`; a DOM element `div.app-root` with attribute `data-composition="canvas" | "cards"` whose first child is the `WorkspaceSwitcher` output and whose second child is `div.app-frame`.

- [ ] **Step 1: Write the failing tests for `workspaces.ts`**

In `frontend/src/navigation/workspaces.test.ts`, change the import line to:

```ts
import { compositionForPath, pathForView, viewForPath, WORKSPACES } from './workspaces'
```

and add these two cases inside the existing `describe('workspaces', ...)` block, after the last existing `it`:

```ts
  it('gives every workspace a composition and pins the canvas/cards split', () => {
    const canvas = WORKSPACES.filter((w) => w.composition === 'canvas').map((w) => w.view)
    const cards = WORKSPACES.filter((w) => w.composition === 'cards').map((w) => w.view)
    expect(canvas).toEqual([
      'recommendations', 'notes', 'planner', 'meeting-prep', 'search-audit',
      'automation', 'engineering', 'personal', 'collaboration',
    ])
    expect(cards).toEqual(['today', 'attention', 'work', 'schedule', 'risks', 'knowledge'])
    expect(canvas.length + cards.length).toBe(WORKSPACES.length)
  })

  it('resolves a path to its composition, and falls back to cards for an unknown path', () => {
    expect(compositionForPath('/notes')).toBe('canvas')
    expect(compositionForPath('/team')).toBe('canvas')
    expect(compositionForPath('/today')).toBe('cards')
    expect(compositionForPath('/schedule')).toBe('cards')
    expect(compositionForPath('/does-not-exist')).toBe('cards')
  })
```

- [ ] **Step 2: Write the failing tests for `App.tsx`**

In `frontend/src/App.test.tsx`, append this block after the existing `describe('App routing', ...)` block:

```tsx
describe('App composition', () => {
  it.each([
    ['/notes', 'canvas'],
    ['/engineering', 'canvas'],
    ['/today', 'cards'],
    ['/risks', 'cards'],
    ['/does-not-exist', 'cards'],
  ] as const)('%s renders data-composition="%s" on .app-root', (path, expected) => {
    const { container } = renderAppAt(path)
    expect(container.querySelector('.app-root')?.getAttribute('data-composition')).toBe(expected)
  })

  it('keeps WorkspaceSwitcher first and .app-frame second inside .app-root', () => {
    const { container } = renderAppAt('/notes')
    const root = container.querySelector('.app-root')
    expect(root).not.toBeNull()
    // The switcher renders its loading state on first paint, so it is present here.
    expect(root?.children[0]?.classList.contains('workspace-switcher')).toBe(true)
    expect(root?.children[1]?.classList.contains('app-frame')).toBe(true)
  })
})
```

- [ ] **Step 3: Run the tests and confirm they fail for the expected reason**

Run: `pnpm test -- --run src/navigation/workspaces.test.ts src/App.test.tsx`
Expected: FAIL. The `workspaces.test.ts` cases fail with `compositionForPath is not a function` (and the split test sees `undefined` compositions); the new `App` cases fail because `.app-root` is `null`. The pre-existing tests still pass. Record this output as RED evidence.

- [ ] **Step 4: Implement `workspaces.ts`**

In `frontend/src/navigation/workspaces.ts`:

1. Directly under the `WorkspaceGroupKey` type, add:

```ts
/** How a workspace's panels are drawn. `cards`: panels keep their card chrome
 * (side-by-side panels, or a dashboard of independent cards). `canvas`: panels
 * drop the chrome and the page sits on a white open canvas (content that
 * stacks vertically). See DESIGN.md's Composition section. */
export type Composition = 'canvas' | 'cards'
```

2. In the `WorkspaceEntry` type, add a required field directly after `group: WorkspaceGroupKey | null`:

```ts
  composition: Composition
```

3. Replace the whole `export const WORKSPACES: ReadonlyArray<WorkspaceEntry> = [ ... ]` array (keep the comment block above it exactly as it is) with:

```ts
export const WORKSPACES: ReadonlyArray<WorkspaceEntry> = [
  { view: 'today', label: 'Today', path: '/today', group: null, composition: 'cards' },
  {
    view: 'attention',
    label: 'Attention',
    path: '/attention',
    group: null,
    composition: 'cards',
    badgeCountLabel: (n) => `${n} ${n === 1 ? 'item' : 'items'} needing attention`,
  },
  {
    view: 'recommendations',
    label: 'Recommendations',
    path: '/recommendations',
    group: null,
    composition: 'canvas',
    badgeCountLabel: (n) => `${n} open ${n === 1 ? 'recommendation' : 'recommendations'}`,
  },
  {
    view: 'work',
    label: 'Work',
    path: '/work',
    group: 'work',
    composition: 'cards',
    badgeCountLabel: (n) => `${n} open ${n === 1 ? 'task' : 'tasks'}`,
  },
  { view: 'notes', label: 'Notes', path: '/notes', group: 'work', composition: 'canvas' },
  { view: 'schedule', label: 'Schedule', path: '/schedule', group: 'work', composition: 'cards' },
  { view: 'planner', label: 'Planner', path: '/planner', group: 'work', composition: 'canvas' },
  { view: 'meeting-prep', label: 'Meeting prep', path: '/meeting-prep', group: 'work', composition: 'canvas' },
  {
    view: 'risks',
    label: 'Risks',
    path: '/risks',
    group: 'risk-knowledge',
    composition: 'cards',
    badgeCountLabel: (n) => `${n} ${n === 1 ? 'risk' : 'risks'} due for review`,
  },
  {
    view: 'knowledge',
    label: 'Knowledge',
    path: '/knowledge',
    group: 'risk-knowledge',
    composition: 'cards',
    badgeCountLabel: (n) => `${n} resolution ${n === 1 ? 'candidate' : 'candidates'}`,
  },
  { view: 'search-audit', label: 'Search & audit', path: '/search-audit', group: 'risk-knowledge', composition: 'canvas' },
  {
    view: 'automation',
    label: 'Automation',
    path: '/automation',
    group: 'systems',
    composition: 'canvas',
    badgeCountLabel: (n) => `${n} pending ${n === 1 ? 'approval' : 'approvals'}`,
  },
  { view: 'engineering', label: 'Engineering', path: '/engineering', group: 'systems', composition: 'canvas' },
  { view: 'personal', label: 'Personal', path: '/personal', group: 'account', composition: 'canvas' },
  // "Team", not "Collaboration" -- matches WorkspaceNavigation.tsx's
  // existing visible label. Path is /team for the same reason (a URL a
  // user would actually type/bookmark should match what they read).
  { view: 'collaboration', label: 'Team', path: '/team', group: 'account', composition: 'canvas' },
]
```

4. At the end of the file, after `pathForView`, add:

```ts
/** Unmatched paths (the not-found route) fall back to `cards`, so an
 * unknown URL keeps the app's original look. */
export function compositionForPath(pathname: string): Composition {
  return WORKSPACES.find((entry) => entry.path === pathname)?.composition ?? 'cards'
}
```

- [ ] **Step 5: Implement `App.tsx`**

1. Change the import `import { viewForPath } from './navigation/workspaces'` to `import { compositionForPath, viewForPath } from './navigation/workspaces'`.

2. Replace the `AppShell` doc comment and function (from `/** Everything below WorkspaceSwitcher ...` through the closing `}` of `AppShell`) with the following. The `<Routes>` block and everything inside `#workspace-panel` are unchanged; only the wrapping and indentation change:

```tsx
/** The whole app body, split out from App() because computing the ARIA
 * labelling tab id and the composition below needs the current route, and
 * useLocation() only works inside <BrowserRouter>, which App() itself
 * renders (same pattern MobileWorkspaceNav.tsx already uses). */
function AppShell({ noteDraftRecovery }: AppShellProps) {
  const location = useLocation()
  // Mirrors MobileWorkspaceNav.tsx's own fallback: an unmatched route (the
  // "*" catch-all below) has no workspace view, so default to 'today' rather
  // than leaving the tabpanel unlabelled.
  const currentWorkspaceView = viewForPath(location.pathname) ?? 'today'

  return (
    // `.app-root` carries the route's composition so the canvas surface can
    // cover the switcher band above the sidebar as well as the content area.
    <div className="app-root" data-composition={compositionForPath(location.pathname)}>
      {/* Mounted globally, above the sidebar -- which company workspace an
          account is viewing applies to every route, not just one; see
          WorkspaceSwitcher.tsx's own docstring. Framed via its own
          `.workspace-switcher` CSS rule (styles.css) rather than by nesting
          it inside `.app-shell`/`.app-frame` -- it's a global org-switcher,
          not part of either nav or the sidebar+content row. */}
      <WorkspaceSwitcher />
      <div className="app-frame">
        <SidebarNavigation />
        <MobileWorkspaceNav />
        <main id="workspace-main" className="app-shell">
          {/* This id/role/aria-labelledby trio is what WorkspaceNavigation.tsx's
              mobile pill tabs (aria-controls="workspace-panel") actually point
              at -- keeping it on an inner div rather than <main> itself lets
              <main> stay the page's one landmark while this div carries the
              tab/tabpanel contract WorkspaceNavigation.test.tsx already
              exercises against a synthetic harness with this same shape. */}
          <div
            id="workspace-panel"
            role="tabpanel"
            aria-labelledby={`workspace-tab-${currentWorkspaceView}`}
          >
            <Routes>
              <Route path="/" element={<Navigate to="/today" replace />} />
              <Route path="/today" element={<TodayPage />} />
              <Route
                path="/attention"
                element={<div className="work-grid"><AttentionQueue /><WaitingView /></div>}
              />
              <Route
                path="/work"
                element={<div className="work-grid"><TaskWorkspace /><CommitmentWorkspace /></div>}
              />
              <Route path="/notes" element={<NoteWorkspace recoveryStore={noteDraftRecovery} />} />
              <Route path="/schedule" element={<ScheduleWorkspace />} />
              <Route path="/planner" element={<Planner />} />
              <Route path="/meeting-prep" element={<MeetingPrep />} />
              <Route
                path="/risks"
                element={<div className="work-grid"><RiskWorkspace /><RiskReviewQueue /></div>}
              />
              <Route
                path="/knowledge"
                element={<div className="work-grid"><EntityExplorer /><ResolutionInbox /><MergeReview /></div>}
              />
              <Route path="/recommendations" element={<RecommendationPanel />} />
              <Route path="/search-audit" element={<SearchAuditPanel />} />
              <Route path="/automation" element={<AutomationWorkspace />} />
              <Route path="/engineering" element={<EngineeringWorkspace />} />
              <Route path="/personal" element={<PersonalWorkspace />} />
              <Route path="/team" element={<CollaborationWorkspace />} />
              <Route
                path="*"
                element={<p role="alert">Page not found. <a href="/today">Go to Today</a>.</p>}
              />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  )
}
```

3. Replace the `App` function's `return (...)` so it no longer mounts the switcher:

```tsx
export default function App() {
  const [noteDraftRecovery] = useState(() => createNoteDraftRecoveryStore({ namespace: crypto.randomUUID() }))

  return (
    <BrowserRouter>
      <AppShell noteDraftRecovery={noteDraftRecovery} />
    </BrowserRouter>
  )
}
```

- [ ] **Step 6: Run the focused tests and confirm they pass (GREEN)**

Run: `pnpm test -- --run src/navigation/workspaces.test.ts src/App.test.tsx`
Expected: PASS: 7 tests in `workspaces.test.ts` (5 existing + 2 new) and 15 + 6 = 21 in `App.test.tsx`. Record this as GREEN evidence.

- [ ] **Step 7: Run the full checks**

Run: `pnpm typecheck && pnpm test -- --run && pnpm check:tokens && pnpm build`
Expected: all pass (the total unit count is 571 + 2 + 6 = 579 tests).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/navigation/workspaces.ts frontend/src/navigation/workspaces.test.ts frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "$(cat <<'EOF'
feat(navigation): add a per-workspace composition setting

Every workspace now declares canvas or cards (required, so a new one cannot
skip the choice). AppShell writes it onto a new div.app-root as
data-composition, and WorkspaceSwitcher moves inside that wrapper so a later
canvas surface can cover the top band. No styling changes yet.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add the canvas CSS

**Files:**
- Modify: `frontend/src/styles.css` (insert one block directly after the existing `.recommendation-panel, .explore-panel, .work-panel { ... }` rule)

**Interfaces:**
- Consumes: `div.app-root[data-composition]` from Task 2.

- [ ] **Step 1: Locate the insertion point**

Run: `grep -n '^\.work-panel {' src/styles.css; grep -n '^\.recommendation-panel,' src/styles.css`
Expected: the shared rule starts with `.recommendation-panel,` / `.explore-panel,` / `.work-panel {` (at about line 284) and ends with `box-shadow: var(--shadow-panel);` then `}`. Insert the new block on the line after that rule's closing `}` (with one blank line between).

- [ ] **Step 2: Insert the canvas rules**

```css

/* Composition (DESIGN.md, Composition). `cards` is the default look above.
 * Under `canvas`, a page's content stacks vertically, so its panels drop the
 * card chrome and the whole app body (including the switcher band) sits on
 * the white panel surface. White, not the grey page: every chip, recessed
 * pill and row divider was designed to sit on the white panel. Attribute plus
 * class specificity beats the mobile radius overrides for these selectors. */
.app-root[data-composition="canvas"] {
  background: var(--color-surface-panel);
}

.app-root[data-composition="canvas"] :is(.work-panel, .recommendation-panel, .explore-panel) {
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  padding: 0;
}

/* A panel directly followed by another (Gmail consent, a selected workflow
 * version, a run detail) reads as a stacked block, separated by a hairline
 * instead of a second box. */
.app-root[data-composition="canvas"]
  :is(.work-panel, .recommendation-panel, .explore-panel)
  + :is(.work-panel, .recommendation-panel, .explore-panel) {
  border-top: 1px solid var(--color-border-subtle);
  padding-top: var(--space-6);
}
```

- [ ] **Step 3: Static checks**

Run: `pnpm check:tokens && pnpm typecheck && pnpm test -- --run && VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build`
Expected: all pass. `check:tokens` must stay OK (no raw colors or font sizes were added).

- [ ] **Step 4: Look at one canvas page and one cards page**

Run:

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/composition-snaps
pnpm visual:snapshots capture "$SNAPS/task3"
```

Then open `$SNAPS/task3/desktop-notes.png` and `$SNAPS/task3/desktop-today.png` with the Read tool. Expected: Notes has no card box (no border or shadow), the content area is white with the heading and form aligned to the page gutter, and the sidebar is still tinted. Today looks exactly as before: grey page with white cards. If Notes still shows a box, or Today changed, the selector or the attribute is wrong: fix it before committing.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/styles.css
git commit -m "$(cat <<'EOF'
feat(styles): draw single-panel workspaces as an open white canvas

Under data-composition="canvas" the panel classes drop their border, radius,
fill, shadow and padding, the app body sits on the white panel surface, and a
stacked second panel gets a subtle hairline instead of a second box. Cards
pages are untouched.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Verify: screenshots, stacked case, suites

**Files:**
- No source changes expected. Evidence only. One temporary file `frontend/tmp-stacked.mjs` is created and must be deleted before finishing.

**Interfaces:**
- Consumes: `$SNAPS/before` (Task 1), the CSS from Task 3.
- Produces (used by Task 5): a hard pass/fail on "cards routes are pixel-identical", which the Provenance sentence in Task 5 relies on.

- [ ] **Step 1: Build and capture the "after" set, then compare**

Run from `frontend/`:

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/composition-snaps
VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build
pnpm visual:snapshots capture "$SNAPS/after"
pnpm visual:snapshots compare "$SNAPS/before" "$SNAPS/after"
```

**Hard gate:** the rows `desktop-today`, `desktop-attention`, `desktop-work`, `desktop-risks`, `desktop-knowledge`, `desktop-schedule`, `desktop-swatch` and `mobile-today` must all show `maxDelta` exactly `0` and `differing%` `0.00%`. If any is non-zero, the canvas CSS is leaking into a cards page (or the wrapper changed layout): find the cause (compare that image before and after, then inspect the CSS and the wrapper), report it precisely, and do not proceed. A one-line obvious fix (for example a selector that is too broad) may be made in `frontend/src/styles.css` and committed as `fix(styles): ...` with the trailer and an explicit path; anything larger is reported as BLOCKED.

**Expected on the nine canvas routes** (`desktop-recommendations`, `-notes`, `-planner`, `-meeting-prep`, `-search-audit`, `-automation`, `-engineering`, `-personal`, `-team`): large deltas, because the card chrome is gone and the background is white. Also note any height change and any width change (`desktop-engineering` was already 1420px wide at baseline: a pre-existing horizontal overflow, not a regression).

- [ ] **Step 2: Look at every canvas page**

Open each of the nine `$SNAPS/after/desktop-<route>.png` images listed above with the Read tool, and for each say in one line whether it looks right: no leftover box or shadow, content aligned to the page gutter, white content area, the sidebar still tinted, nothing clipped, overlapping or unstyled. Also open `$SNAPS/after/mobile-today.png` (a cards route) and confirm it is unchanged in appearance.

- [ ] **Step 3: Capture the stacked-block case and a mobile canvas page**

The default fixtures never render a second stacked panel, so use a one-off script. Create `frontend/tmp-stacked.mjs` (do not commit it):

```js
import path from 'node:path'

import { chromium } from 'playwright'

import { createFixtureApi } from './e2e/fixtures.mjs'
import { startPreviewServer } from './e2e/server.mjs'

const OUT = process.argv[2]
const SECOND = `<section class="work-panel"><h2>Second block</h2><p>A detail panel stacked below the first, as Gmail consent, a selected workflow version or a run detail would render it.</p></section>`

const server = await startPreviewServer()
const browser = await chromium.launch()
try {
  for (const [name, viewport, route, stack] of [
    ['stacked-desktop-personal', { width: 1280, height: 1000 }, '/personal', true],
    ['canvas-mobile-notes', { width: 390, height: 844 }, '/notes', false],
  ]) {
    const context = await browser.newContext({ viewport, reducedMotion: 'reduce' })
    const page = await context.newPage()
    await createFixtureApi(page)
    await page.goto(`${server.baseURL}${route}`)
    await page.waitForLoadState('networkidle')
    if (stack) {
      await page.evaluate((html) => {
        const first = document.querySelector('#workspace-panel .work-panel')
        if (!first) throw new Error('no .work-panel found to stack after')
        first.insertAdjacentHTML('afterend', html)
      }, SECOND)
    }
    await page.screenshot({ path: path.join(OUT, `${name}.png`), fullPage: true })
    await context.close()
  }
} finally {
  await browser.close()
  server.stop()
}
console.log('done')
```

Run: `mkdir -p "$SNAPS/stacked" && node tmp-stacked.mjs "$SNAPS/stacked"`, then open `$SNAPS/stacked/stacked-desktop-personal.png` and `$SNAPS/stacked/canvas-mobile-notes.png`. Expected: the second block sits below the first with a thin hairline above it and no box around either; on mobile the content aligns to the narrow gutter with no card chrome. Then delete the script: `rm tmp-stacked.mjs`, and confirm `git status --porcelain` is empty.

- [ ] **Step 4: Run every suite**

Run: `pnpm typecheck && pnpm test -- --run && pnpm check:tokens && pnpm test:e2e`
Expected: typecheck clean, all unit tests pass (579), the token check OK, and the e2e run finishes with every scenario passing (26, each with its axe accessibility scan). If e2e fails on a browser-install error rather than a scenario, run `pnpm exec playwright install chromium` once and retry; if port 4173 is busy, report rather than kill unknown processes. A scenario failure is a finding: report the scenario and the assertion verbatim.

- [ ] **Step 5: Report**

The final message contains: the complete 17-row compare table; the eight cards-route rows called out with their exact deltas and a clear PASS or FAIL for the hard gate; a one-line verdict per canvas page from Step 2; a description of the two Step 3 images; and the pass/fail of each suite with counts. No commit unless a fix was made in Step 1.

---

### Task 5: Update DESIGN.md

**Files:**
- Modify: `DESIGN.md`

**Interfaces:**
- Consumes: Tasks 2 to 4 (the setting, the CSS, and the verified result that cards routes were pixel-identical).

- [ ] **Step 1: Add the Composition section after Layout primitives**

Insert this section immediately before the line `## Morning Brief` (the Layout primitives section ends right above it):

```markdown
## Composition: canvas and cards

Every workspace declares one of two compositions in `frontend/src/navigation/workspaces.ts` (`composition: 'canvas' | 'cards'`, required on every entry, so a new workspace cannot skip the choice). `AppShell` (`App.tsx`) writes it onto `div.app-root` as `data-composition`, and the CSS keys off that attribute.

- **`cards`** — the panel is a card: white fill, 1px border, `--shadow-panel`, `--radius-panel`, padding. Use it when the page's content sits **side by side or in a grid** (a two-up `.work-grid`, Schedule's four panels) or is a **dashboard of independent cards** (Today). The card edge is what separates adjacent headings there: Attention, Work and Risks each render two h1s side by side.
- **`canvas`** — the panel has no card chrome (no border, radius, fill, shadow or padding) and the page's content area is `--color-surface-panel` (white) instead of the grey page. Use it when the page's content **stacks vertically**, one panel per view or per tab. This is most workspaces: recommendations, notes, planner, meeting-prep, search-audit, automation, engineering, personal and team.

**Why the canvas is white, not the grey page.** Every interior component (the recessed chips and pills, the `--color-border-faint` row dividers, inputs) was designed to sit on the white panel. On the grey page (`--color-surface-page`, `#f3f5f7`), `--color-surface-recessed` chips (`#eef1f5`) and the dividers nearly vanish. On white they read exactly as they did inside the card. The consequence is that the content area flips between white (canvas pages) and grey (cards pages) when navigating between the two kinds; the sidebar stays tinted in both.

**Stacked blocks.** Under `canvas`, a panel directly followed by another (Personal's Gmail consent, Automation's selected workflow version, a run's detail) gets a `--color-border-subtle` hairline on top and `--space-6` padding rather than a second box.

**Choosing for a new workspace.** Decide by layout, not by taste: if two or more panels can be visible side by side, or the page is a grid of independent cards, use `cards`; if everything stacks in one column, use `canvas`. A workspace that sometimes shows a second stacked panel when a detail opens is still `canvas` — the setting is per page, so the chrome does not change when a detail panel appears. An unmatched route (the not-found page) falls back to `cards`.

Not affected by the setting: `.dashboard-card`, `.connector-card`, `.simulation-panel`, `.brief-panel`, list rows, and every token. Cards nested inside a canvas page keep their own boundaries.
```

- [ ] **Step 2: Update the Contents line**

In the `**Contents:**` line (line 9), insert a new entry labelled `Composition` between the existing Layout primitives entry and the Morning Brief entry, separated by ` · ` like its neighbors, using the same markdown link format the neighboring entries use. Its anchor is `composition-canvas-and-cards` (the GitHub slug of the new `## Composition: canvas and cards` heading). Do not touch any other entry.

- [ ] **Step 3: Update the `.work-panel` row in Layout primitives**

Replace the table row
`| `.work-panel` | Standard card: `border-radius: var(--radius-panel)`, `--shadow-panel` elevation, `padding: clamp(24px, 4vw, 42px)` |`
with
`| `.work-panel` | Standard card under `cards` composition: `border-radius: var(--radius-panel)`, `--shadow-panel` elevation, `padding: clamp(24px, 4vw, 42px)`. Under `canvas` composition it loses its border, radius, fill, shadow and padding (Composition below) |`

- [ ] **Step 4: Update the Hierarchy rules bullet**

Replace the sentence
`**Don't nest bordered panels inside bordered panels.** Nothing in the app currently does this; a new feature that needs a sub-grouping inside a `.work-panel` should reach for a heading + spacing, not another card.`
with the same sentence followed by ` Whether the page itself is drawn as a card at all is a separate, page-level decision; see Composition.` (appended before the bullet ends).

- [ ] **Step 5: Update the Anti-patterns line**

In the Anti-patterns "Also avoid, per Building a new page above:" list, replace `a card around every section; a card inside a card;` with `a card around every section (a page whose content stacks vertically should be `canvas`, see Composition); a card inside a card;`.

- [ ] **Step 6: Add the Provenance entry**

Append this paragraph at the very end of `DESIGN.md`:

```markdown
Composition philosophy (sub-project 3 of the "Calm Executive Workspace" direction) landed 2026-09-19. Two of its three original items turned out to be already satisfied and needed no work: every list already used hairline-divided rows rather than bordered cards (only `.connector-card` is a bordered per-item card, in one file), and card criteria already existed in Hierarchy rules. The real gap was panel versus open canvas: `.work-panel` appears 49 times across 41 files and most pages were one large card on a grey page, the "a card around every section" anti-pattern this document already names. Fixed with a required per-workspace `composition` setting (9 `canvas`, 6 `cards`) rather than a per-panel modifier class (about 30 component edits, easy to forget) or an automatic `:only-child` selector (three pages flip between one and two panels, so the chrome would change when a detail panel opened). The canvas is white rather than the grey page because the recessed chips and row dividers vanish on grey (prototyped both). Verified with before/after full-page screenshots of every workspace route: the six `cards` routes, the token swatch and mobile Today were pixel-identical (maximum per-channel delta 0), the nine `canvas` routes were inspected by eye, and the stacked-block hairline was captured with a one-off injected second panel because the default fixtures never render one. The unit and e2e suites including axe scans, `tsc`, the token check and a production build all pass. See `docs/superpowers/specs/2026-09-19-composition-philosophy-design.md` and `docs/superpowers/plans/2026-09-19-composition-philosophy.md`.
```

- [ ] **Step 7: Check and commit**

Run from the worktree root: `python3 scripts/check_docs.py; echo "docs exit: $?"` (must print `docs exit: 0`), then from `frontend/`: `pnpm check:tokens`. The two `docs/superpowers/...` paths above are plain backtick text, not markdown links: keep them that way.

```bash
git add DESIGN.md
git commit -m "$(cat <<'EOF'
docs: document the canvas and cards composition rule

Adds the Composition section (the rule, the white canvas surface, stacked
blocks, how a new workspace chooses), updates the work-panel row, hierarchy
rule and anti-pattern line to point at it, and adds the provenance entry,
including that two of the sub-project's three original items were already
satisfied.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
