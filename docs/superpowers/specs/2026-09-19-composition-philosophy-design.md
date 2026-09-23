# Composition Philosophy: Design

**Sub-project 3 of the "Calm Executive Workspace" redesign.** Foundation tokens (1) and Navigation (2) are merged. Action hierarchy and icons (4) and the Today page rebuild (5) are separate, later sub-projects and are not in scope here.

## Why this exists, and what changed from the original brief

The original brief for this sub-project was "panel-vs-open-canvas rule, card criteria, list/row pattern replacing bordered list-cards." Checked against the app before designing, two of those three parts are already true:

- **The list/row pattern already exists.** Every list (`.item-list`, `.work-list`, `.audit-list`, `.recommendation-list`) already uses hairline-divided rows, not bordered cards. The only bordered per-item card is `.connector-card`, used in one file (`ConnectorHealthPanel.tsx`).
- **Card criteria are already written down.** DESIGN.md's "Hierarchy rules" already say to reach for whitespace before a new border and not to nest bordered panels.

The real, unaddressed problem is **panel versus open canvas**:

- `.work-panel` is used 49 times across 41 files, and DESIGN.md itself states that most pages collapse to a single `.work-panel`.
- So most pages are one large white card (1px border, `--shadow-panel`, up to 42px padding) floating on a grey page. That is the "a card around every section" anti-pattern DESIGN.md's own Anti-patterns section names.
- The sidebar shell from sub-project 2 made this more visible: the chrome is now calm and the content sits in a heavy box.

## Scope decisions (from brainstorming)

- **The rule.** Content that sits side by side or in a grid, or a dashboard of independent cards, keeps cards, because the card edge is what separates adjacent headings. Content that stacks vertically reads as open canvas, with spacing and a hairline between blocks.
- **Mechanism: a route-level setting.** Each workspace declares `composition: 'canvas' | 'cards'`. Not a per-panel modifier class (about 30 TSX edits, easy to forget on new panels), and not an automatic `:only-child` selector (three pages flip between one and two panels, so the chrome would visibly change when a detail panel opened).
- **The split (approved):**
  - `canvas` (9): recommendations, notes, planner, meeting-prep, search-audit, engineering, personal, team, automation.
  - `cards` (6): today, attention, work, risks, knowledge, schedule.
  - Judgment calls: automation and personal are `canvas` even though a second block sometimes stacks below (Gmail consent, a selected workflow version, a run detail). Stacked, not side by side, so spacing and a hairline suffice.
- **The canvas surface is white, not grey.** Prototyped both. On the grey page background the recessed chips and pills (`--color-surface-recessed`, `#eef1f5`) and the row dividers nearly vanish, because every interior component was designed to sit on the white panel. On a white canvas they read exactly as they do today. The cost is that the content area changes between white (canvas pages) and grey (cards pages) when navigating between the two kinds.
- **Unaffected either way:** the dark `.brief-panel` (Today), `.dashboard-card`, `.connector-card`, `.simulation-panel` (its dashed striped look is deliberately alarming per UX-STATES.md), list rows, and every token.

## Structure the design rests on (verified against the code)

Every page sits in `main#workspace-main.app-shell > div#workspace-panel[role=tabpanel] > <Routes>`. `WorkspaceSwitcher` is mounted in `App()` above `AppShell`, outside that chain.

- **One panel per view:** notes, planner, meeting-prep, recommendations, search-audit (its tab list is inside the panel).
- **One panel per tab, heading and tab list outside the panel:** engineering (10 tabs), personal, team, automation. Exceptions that stack a second block: Personal's Gmail tab (`.work-panel` plus a `.recommendation-panel` when consent is active), Automation's Workflows tab (a second `.work-panel` when a version is selected) and Runs tab (a run-detail `.work-panel`).
- **Side by side or grid:** attention (2 panels), work (2), risks (2), knowledge (3, wrapping), schedule (4 in two grids plus conditional edit panels), today (1 panel, the dark brief, and a 5-card grid). Attention, work and risks each render two h1s, so the card boundary is the only separator between them.
- **No `.work-panel` is nested inside another card-like container.** `.dashboard-card` (Planner, MeetingPrep, AttentionQueue), `.connector-card` and `.simulation-panel` do nest inside a `.work-panel` in places; those keep their own boundaries.

## Design

### 1. Plumbing (`frontend/src/navigation/workspaces.ts`, `frontend/src/App.tsx`)

- Add `export type Composition = 'canvas' | 'cards'`, and a **required** `composition: Composition` field on `WorkspaceEntry`, set on all 15 entries per the split above. Required, so a new workspace cannot ship without choosing.
- Add `export function compositionForPath(pathname: string): Composition`, which returns the matching entry's `composition`, and `'cards'` when no workspace matches (the not-found route keeps today's look). It reuses the same path matching `viewForPath` uses.
- In `AppShell`, wrap the switcher and the frame:
  `<div className="app-root" data-composition={compositionForPath(location.pathname)}> <WorkspaceSwitcher /> <div className="app-frame">…</div> </div>`.
  `WorkspaceSwitcher` moves from `App()` into `AppShell` (still inside `BrowserRouter`) so the top band shares the surface. This is the one structural change; its styling (`div.workspace-switcher`) is untouched.

### 2. CSS (`frontend/src/styles.css`)

```css
.app-root[data-composition="canvas"] { background: var(--color-surface-panel); }

.app-root[data-composition="canvas"] :is(.work-panel, .recommendation-panel, .explore-panel) {
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
  padding: 0;
}

.app-root[data-composition="canvas"]
  :is(.work-panel, .recommendation-panel, .explore-panel)
  + :is(.work-panel, .recommendation-panel, .explore-panel) {
  border-top: 1px solid var(--color-border-subtle);
  padding-top: var(--space-6);
}
```

- `margin-top` and each panel's inner heading stay, so content aligns to the page gutter and the vertical rhythm is unchanged.
- The rules use attribute plus class specificity, so they beat the mobile radius override for the same selectors regardless of source order.
- `--shadow-panel` remains in use by `cards` pages.
- `.app-root` is not otherwise styled (no min-height, no layout). The sidebar already provides `min-height: 100vh`.

### 3. Tests

- `navigation/workspaces.test.ts`: every `WORKSPACES` entry has a composition, and the exact split is pinned (the 9 canvas paths and the 6 cards paths).
- `App.test.tsx`: rendering `/notes` yields an `.app-root` with `data-composition="canvas"`; `/today` yields `cards`; an unmatched path yields `cards`.

### 4. Documentation (`DESIGN.md`)

- New "Composition" section: the rule and its criterion, the canvas surface (white) and what it removes, the required `composition` field and how a new workspace chooses, and the stacked-block hairline.
- Update the `.work-panel` row in Layout primitives ("Standard card") to say cards under `cards`, open canvas under `canvas`.
- Update Hierarchy rules' "don't nest bordered panels" line to point at the Composition section; add the "card around every section" anti-pattern's resolution.
- Provenance entry for this sub-project, including that the original list/row and card-criteria items were found already satisfied.

## Verification

- **Baseline first:** full-page screenshots from unmodified `main` (`pnpm visual:snapshots capture`), before any change.
- **Cards routes must be pixel-identical:** Today, Attention, Work, Risks, Knowledge, Schedule and mobile Today must show a maximum per-channel delta of 0. This guards against the change leaking into cards pages.
- **Canvas routes:** recommendations, notes, planner, meeting-prep, search-audit, engineering, personal, team and automation are inspected by eye, since they are meant to change. The stacked case (Gmail consent, a selected workflow version, a run detail) is not rendered by the default fixtures, so capture it with a one-off, uncommitted Playwright script that appends a second `.work-panel` section directly after the first on `/personal` (a canvas route) and screenshots the result, so the hairline is seen at least once.
- **Suites:** unit tests, typecheck, `check:tokens`, build, and the full e2e suite with axe. Contrast is unaffected because canvas text sits on the same white the panels use today.

## Out of scope

- Any new token; `.dashboard-card`, `.connector-card`, `.simulation-panel` and `.brief-panel` restyles; list-row changes; a mobile-specific composition; icons and action hierarchy (sub-project 4); rebuilding Today (sub-project 5).
- Making `cards` pages calmer. This sub-project only decides where cards belong.
