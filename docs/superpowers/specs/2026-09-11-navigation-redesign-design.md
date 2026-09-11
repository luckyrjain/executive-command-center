# Navigation redesign — sidebar + real routing

**Status:** approved design, spec written; implementation not yet planned or built.
**Sub-project 2 of the "Calm Executive Workspace" redesign** (sub-project 1, Foundation tokens, has not been built yet — this sub-project was pulled forward at the user's explicit request, since navigation is the single biggest visible change). Composition philosophy (panel-vs-open-canvas, card criteria, list/row pattern), action hierarchy + icons, and the Today page rebuild are separate, later sub-projects. None of that is in scope here, except where this sub-project's own components (the sidebar itself) need a visual treatment now.

## Why this exists

A second external design-system review pushed further than incremental fixes: not "polish the existing pills and cards" but a genuinely calmer, denser, more hierarchical visual language — "Linear's calmness + Stripe's information rigor + an executive command center's prioritization." The review's single highest-priority item was the top-level navigation: 15 workspaces rendered as a wrapped pill row (`WorkspaceNavigation.tsx`), which reads as chrome competing with content rather than a quiet frame around it.

This is the first sub-project of that larger direction, chosen to go first because it's the most visible change and because several later sub-projects (Today's rebuild, composition philosophy) are easier to design against a settled navigation shell than before one exists.

Decomposition (this document covers only #2 — reordered ahead of #1 at the user's request):

1. Foundation tokens — typography scale, surface/elevation hierarchy, motion timings. Not yet built.
2. **Navigation — sidebar + real routing (this spec).**
3. Composition philosophy — panel-vs-open-canvas rule, card criteria, list/row pattern replacing bordered list-cards.
4. Action hierarchy + icons — icon system (first real one), refined button variants.
5. Today page rebuild.
6. Remaining smaller items (wizard stepper visual, form field pairing, inspector pattern) — folded into whichever later sub-project touches that surface, or standalone.

## Scope decisions (from brainstorming)

Recorded here because each was a real fork, not an assumption:

- **Real URL routing is in scope**, not just a visual reskin. `react-router-dom` is a new dependency; each workspace gets a real path.
- **The IA stays flat**: the same 15 top-level workspaces, none of today's in-workspace tabs get promoted to top-level routes. Only the sidebar's visual/structural presentation and the routing layer change.
- **Grouping**: 5 domain-based sections (below), not a flat list — the user's own reference mockup showed grouped sections, and confirmed this scheme specifically over a flat list or an alternate taxonomy.
- **Badge counts are in scope**, for exactly 6 of the 15 workspaces (below) — the ones with one natural, meaningful single number. Confirmed after checking the backend has no existing `total`/count field anywhere; 2 are free (reuse an already-unbounded endpoint), 4 need a small new count endpoint each.
- **Mobile is explicitly out of scope** for this sub-project beyond a stopgap (below) — a real mobile nav redesign is an agreed fast-follow, not bundled in here.
- **Icons are not part of this sub-project** — sidebar items are plain text. Icons are sub-project 4's job; introducing them here would pre-empt that sub-project's own icon-vocabulary decision.

## Architecture: real routing

Add `react-router-dom` (latest stable, React 19-compatible). `App.tsx` currently holds `currentView` in a bare `useState<WorkspaceView>` and renders one giant ternary keyed off it (see the file as it stands today — every workspace component is a branch of that ternary, several wrapped in a shared `.work-grid` alongside a sibling component, e.g. `work` renders `<TaskWorkspace />` + `<CommitmentWorkspace />`).

This becomes:

- `<BrowserRouter>` wraps the app.
- A `<Routes>` block replaces the ternary, one `<Route path="..." element={...} />` per workspace, preserving each existing multi-component grouping exactly as-is (e.g. the `/risks` route still renders `<RiskWorkspace /><RiskReviewQueue />` inside `.work-grid`).
- `WorkspaceView` (currently a union type gating the ternary) becomes redundant as a piece of React state — routing owns "which workspace is active" — but the type itself may still be useful for the `WORKSPACES`/sidebar-items config array; confirmed at implementation time, not a spec-level decision.
- `<WorkspaceSwitcher />` stays mounted globally above the routed content, unchanged (it's org-switching, not workspace navigation — see its own docstring, already covers why it's global).
- Route paths, one per current `WorkspaceView` value, unchanged from today's kebab-case naming except `collaboration` → `/team` (matches the visible label "Team" a user would actually type/bookmark, rather than the internal `WorkspaceView` value):

  `/today` `/attention` `/work` `/notes` `/schedule` `/planner` `/meeting-prep` `/risks` `/knowledge` `/recommendations` `/search-audit` `/automation` `/engineering` `/personal` `/team`

- Root path `/` redirects to `/today`.
- Unknown paths: a plain not-found state (not designed in detail here — a one-line "Page not found, go to Today" is enough; this isn't a product surface worth more investment).

**Nav semantics, settled for real this time.** The last review raised this and it was correctly declined then — the app had no router, so ARIA tabs was the *correct* pattern, not a misuse of it (see current `DESIGN.md`'s Navigation section). Now that real routes exist, the premise that review needed is finally true: the sidebar is genuine route navigation. It's `<nav aria-label="Workspaces">` containing real `<NavLink>` elements, `aria-current="page"` on the active one, native browser Tab/Enter/Cmd+Click/right-click-open-in-new-tab semantics for free. `WorkspaceNavigation.tsx`'s custom roving-tabindex implementation (`nextWorkspaceIndex`, `moveWorkspaceFocus`, the `Home`/`End`/`ArrowLeft`/`ArrowRight` keydown handler) is deleted, not migrated — links don't need it.

In-workspace `.tab-list` sub-navigation (used identically in `PersonalWorkspace.tsx`, `CollaborationWorkspace.tsx`, `AutomationWorkspace.tsx`, `EngineeringWorkspace.tsx`) is untouched. That's still panel-switching within one already-loaded route, which is exactly what ARIA tabs is for — this sub-project doesn't touch it.

## The sidebar component

New `frontend/src/navigation/SidebarNavigation.tsx` replaces `WorkspaceNavigation.tsx`.

**Structure.** Fixed width (~224px desktop), persistent, positioned left of the content canvas. Five sections, in this order:

1. *(unlabeled top group)* — Today, Attention, Recommendations
2. **WORK** — Work, Notes, Schedule, Planner, Meeting prep
3. **RISK & KNOWLEDGE** — Risks, Knowledge, Search & audit
4. **SYSTEMS** — Automation, Engineering
5. **ACCOUNT** — Personal, Team

Each item: a `<NavLink to="...">`, visible label text (same labels as today — "Team" for `/team`, "Search & audit" for `/search-audit`, etc.), optional badge (below).

**Visual states** (exact tokens are an implementation-time detail against whatever the Foundation sub-project eventually formalizes, but the states themselves are fixed here):

| State | Treatment |
|---|---|
| Default | Transparent background, secondary text color |
| Hover | Subtle neutral background tint |
| Selected (`aria-current="page"`) | Light accent-tinted background, a small solid accent-colored bar on the item's left edge, primary ink text color, medium font weight |

Sidebar's own background sits one step recessed from the content canvas — a new, slightly-darker-than-canvas surface token (not today's approach of filling the selected item with solid `--color-ink`/`--color-accent`; the accent becomes a signal — the left bar plus a tint — not a block).

Section headers: small, uppercase, muted eyebrow-style text (matches the existing eyebrow role already documented in `DESIGN.md`'s Typography table) — except the unlabeled first group, which gets no header at all.

**Badge counts.** Exactly 6 items get one: Attention, Work, Risks, Knowledge, Automation, Recommendations. Rendered as a small numeral, right-aligned in the item row. The other 9 items never render a badge slot at all (not a badge showing "0" — no slot).

| Workspace | What it counts | Backend cost |
|---|---|---|
| Risks | Items in the existing due/overdue review queue (`GET /api/v1/risks/review-queue`) | Free — endpoint already returns this unbounded, `items.length` |
| Automation | Pending approvals (`GET /api/v1/automations/approvals?status=pending`) | Free — endpoint already returns this unbounded, `items.length` |
| Attention | Needs-action attention-queue items | New: a `COUNT(*)`-shaped endpoint mirroring `list_attention`'s existing filter |
| Work | Open tasks | New: a `COUNT(*)`-shaped endpoint mirroring `list_tasks`'s existing filter |
| Knowledge | Open resolution candidates | New: a `COUNT(*)`-shaped endpoint mirroring the existing `status=open` filter |
| Recommendations | Pending recommendations | New: a `COUNT(*)`-shaped endpoint mirroring `list_recommendations`'s existing filter |

The 4 new endpoints are additive (`GET /api/v1/{domain}/count`-shaped, or an existing list endpoint's own new query param — exact shape is an implementation-time API-design decision, not fixed here), each a thin wrapper around a `WHERE` clause that already exists in that domain's `list_*` handler. No new business logic, no new joins.

Frontend: the sidebar issues its own react-query fetches for these 6 counts (same staleness/refetch-on-focus behavior as every other query in this app — no polling, no websocket, nothing new invented for this).

## Mobile stopgap

Below the sidebar's breakpoint (existing `800px`/`520px` breakpoints in `styles.css`, exact cutoff decided at implementation time), render today's existing pill row verbatim — `WorkspaceNavigation.tsx`'s current markup, ARIA-tabs behavior, and CSS stay exactly as they are today, just conditionally shown only at narrow widths instead of always. Desktop gets the new sidebar. This is intentionally a stopgap, not a design: the real mobile nav (drawer/sheet) is an agreed fast-follow sub-project once the desktop shell is settled, not bundled into this one. Concretely: `WorkspaceNavigation.tsx` is not deleted — it's kept, narrowed to a mobile-only render path, and its own roving-tabindex keyboard behavior is preserved unchanged in that path (deleted only from the desktop `SidebarNavigation.tsx`, per the settled-semantics note above — the mobile fallback keeps working exactly as it does today until it's replaced for real).

## Testing impact

- New unit tests for `SidebarNavigation.tsx` (rendering, section grouping, selected-state via route, badge rendering) and for each new count endpoint (backend).
- `WorkspaceNavigation.test.tsx` (existing) stays, covering the mobile-fallback path — assertions about its ARIA-tabs/roving-tabindex behavior remain valid since that component is unchanged, just narrower in scope.
- e2e: every scenario that currently reaches a workspace via `page.getByRole('tab', {name: '...'}).click()` on the top-level nav switches to `page.goto('/{path}')` directly — arguably a simplification (a real URL to navigate to, instead of a synthetic in-page click), and removes the need for most scenarios to even touch the sidebar at all just to get into a workspace. `frontend/e2e/scenarios/conflict-audit-keyboard.mjs`'s fixed `ArrowRight`-count assertions against the old tablist are rewritten or removed (the roving-tabindex behavior they test no longer exists in the desktop path). Every scenario's own *in-workspace* tab interactions (e.g. `search-audit`'s "Audit history" tab) are unaffected — those keep using `getByRole('tab', ...)` exactly as today. Full enumeration of which scenarios need which specific change is implementation-plan work, not spec work — the shape of the change (top-level tab-click → `page.goto`) is fixed here, the file-by-file list isn't.

## Documentation impact

`DESIGN.md`'s Navigation section is rewritten, not appended to — the current section's "two ARIA-tab-based systems" framing and the roving-tabindex mechanics documentation are superseded for the top-level nav (kept, accurately, for the in-workspace `.tab-list` pattern and for the mobile-fallback description). The dedicated note this session already added about why tabs were correct without a router stays, updated to record that a router now exists and this is why the nav changed.

## Branch / rollout

New branch off `main` — not the currently-open `design-system-review-fixes` PR, which is a distinct, smaller, already-scoped piece of work. This lands as its own PR.

## Out of scope (explicitly, for this sub-project)

- Icons in the sidebar (sub-project 4).
- Real mobile drawer/sheet redesign (agreed fast-follow, separate from this spec).
- Any visual token changes beyond what the sidebar itself needs (surface/elevation formalization is Foundation, sub-project 1 — not yet built; the sidebar's recessed-background token is a narrow, sidebar-scoped addition, not a preview of that whole system).
- Promoting any in-workspace tab to a top-level route.
- Collapsible/resizable sidebar — fixed width only.
