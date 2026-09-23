# Action Hierarchy and Icons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the sidebar its first icons and bring the app's existing `.btn-primary`/`.btn-quiet` button-hierarchy rule up to date with the rows that already need it, per a full survey of all 231 buttons in the app.

**Architecture:** 15 inline SVG components (no new dependency) wired onto `WorkspaceEntry` and rendered by `SidebarNavigation`. Separately, a mechanical, file-by-file sweep that adds `className="btn-primary"` to the one dominant forward action in each row the survey identified, and `className="btn-quiet"` to the single row that matches its narrow definition. The two halves don't share code and are independently testable.

**Tech Stack:** React 19 function components, inline SVG (stroke icons, `currentColor`), Vitest + Testing Library, existing `.btn-primary`/`.btn-destructive`/`.btn-quiet` CSS (unchanged — already defined in `styles.css`).

**Spec:** `docs/superpowers/specs/2026-09-23-action-hierarchy-design.md`

## Global Constraints

- **No new npm dependency.** Icons are hand-written inline SVG, not a package.
- **No new CSS.** `.btn-primary`, `.btn-destructive`, `.btn-quiet` already exist in `frontend/src/styles.css` (lines ~207-210, ~593-594) from earlier sub-projects; this plan only adds two small rules for icon layout (`.sidebar-nav-link-content`, `.sidebar-nav-icon`).
- **No new `.btn-destructive` assignment anywhere.** The `ApprovalInbox`/`DelegationsPanel` "Reject" inconsistency found during the survey is documented in DESIGN.md (Task 6), not fixed.
- **Icons are `aria-hidden="true"`.** The link's own visible text stays the sole accessible name.
- **Icon color is `currentColor`, no new color token.** It inherits whatever the surrounding link's text color already is.
- **Button edits are additive only:** add `className="btn-primary"` (or `"btn-quiet"`) to an existing `<button>` element. No other prop, handler, or JSX structure changes. Any button not explicitly listed in Tasks 3-5 is left untouched, including every row the spec's survey placed in "peer/no hierarchy" or "lone submit" categories.
- **Commit trailer:** every commit message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- **Work only in the worktree** `/Users/luckyjain/Projects/ecc-worktree-action-hierarchy` (branch `action-hierarchy`), commands from its `frontend/` directory unless a step says otherwise. Confirm `git branch --show-current` prints `action-hierarchy` before every commit. Stage explicit paths only (never `git add -A` or `.`). Do not push and do not open a PR. Another Claude session may be using `/Users/luckyjain/Projects/executive-command-center` on a different branch: never work there.
- **Snapshot root** (session scratchpad, outside the repo): `/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/action-hierarchy-snaps`, referred to below as `$SNAPS`.
- The shell is zsh: never `echo` a bare string of `=` characters.

## File Structure

- Create `frontend/src/navigation/icons.tsx`: 15 exported SVG icon components (Task 1).
- Create `frontend/src/navigation/icons.test.tsx`: renders each icon, asserts an `<svg>` with the right attributes (Task 1).
- Modify `frontend/src/navigation/workspaces.ts`, `frontend/src/navigation/workspaces.test.ts`: `icon` field on every entry (Task 2).
- Modify `frontend/src/navigation/SidebarNavigation.tsx`, `frontend/src/navigation/SidebarNavigation.test.tsx`: render the icon (Task 2).
- Modify `frontend/src/styles.css`: two new small rules (Task 2).
- Modify 5 files under `frontend/src/features/{schedule,risks,automation,personal,engineering}` and their tests: wizard forward-action buttons (Task 3).
- Modify 6 files under `frontend/src/features/{knowledge,attention,engineering,automation}` and their tests: confirm/cancel pairs (Task 4).
- Modify 3 files under `frontend/src/features/{knowledge,tasks,collaboration}` and their tests: multi-button dominant actions and the first `.btn-quiet` (Task 5).
- Modify `DESIGN.md` (Task 6).

---

### Task 1: Icon components

**Files:**
- Create: `frontend/src/navigation/icons.tsx`
- Create: `frontend/src/navigation/icons.test.tsx`

**Interfaces:**
- Produces (used by Task 2): 15 named exports, each `(props: SVGProps<SVGSVGElement>) => JSX.Element`: `TodayIcon`, `AttentionIcon`, `RecommendationsIcon`, `WorkIcon`, `NotesIcon`, `ScheduleIcon`, `PlannerIcon`, `MeetingPrepIcon`, `RisksIcon`, `KnowledgeIcon`, `SearchAuditIcon`, `AutomationIcon`, `EngineeringIcon`, `PersonalIcon`, `TeamIcon`. Every component forwards `...props` onto its `<svg>` so a caller can pass `aria-hidden` and `className`.

- [ ] **Step 1: Write the icon file**

Create `frontend/src/navigation/icons.tsx`:

```tsx
import type { SVGProps } from 'react'

/** Sidebar workspace icons: 15 hand-simplified stroke glyphs in the style of
 * Lucide (MIT), inlined rather than pulled in as a dependency -- this app has
 * added exactly one runtime dependency (react-router-dom, for real routing)
 * since its start, and 15 static glyphs don't justify a second. Every icon
 * shares the same contract: 24x24 viewBox, 2px round-capped stroke,
 * `currentColor` (so it inherits the caller's text color -- no new color
 * token), `fill="none"` except the one alert-dot exception noted below. Props
 * forward onto the <svg> so the caller sets `aria-hidden`/`className`; see
 * DESIGN.md's Composition -- Buttons: action hierarchy section update
 * (Task 6 of this plan) for the icon-vocabulary note, and
 * `docs/superpowers/specs/2026-09-23-action-hierarchy-design.md` for the
 * glyph-to-workspace mapping this file implements. */

const base = {
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

export function TodayIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2" x2="12" y2="4" />
      <line x1="12" y1="20" x2="12" y2="22" />
      <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
      <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
      <line x1="2" y1="12" x2="4" y2="12" />
      <line x1="20" y1="12" x2="22" y2="12" />
      <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
      <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
    </svg>
  )
}

export function AttentionIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M6 8a6 6 0 0 1 12 0c0 5 2 7 2 7H4s2-2 2-7" />
      <path d="M10.5 19a1.5 1.5 0 0 0 3 0" />
    </svg>
  )
}

export function RecommendationsIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8L12 2z" />
      <path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z" />
    </svg>
  )
}

export function WorkIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <rect x="3" y="8" width="18" height="12" rx="2" />
      <path d="M8 8V6a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
      <line x1="3" y1="13" x2="21" y2="13" />
    </svg>
  )
}

export function NotesIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M6 2h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z" />
      <path d="M15 2v5h5" />
      <line x1="8" y1="13" x2="16" y2="13" />
      <line x1="8" y1="17" x2="16" y2="17" />
    </svg>
  )
}

export function ScheduleIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <line x1="3" y1="10" x2="21" y2="10" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="16" y1="2" x2="16" y2="6" />
    </svg>
  )
}

export function PlannerIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <polyline points="3 6 4.5 7.5 8 4" />
      <line x1="12" y1="6" x2="21" y2="6" />
      <polyline points="3 13 4.5 14.5 8 11" />
      <line x1="12" y1="13" x2="21" y2="13" />
      <polyline points="3 20 4.5 21.5 8 18" />
      <line x1="12" y1="20" x2="21" y2="20" />
    </svg>
  )
}

export function MeetingPrepIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="9" cy="8" r="3" />
      <circle cx="17" cy="9" r="2.5" />
      <path d="M3 20v-1a6 6 0 0 1 12 0v1" />
      <path d="M16 14a4 4 0 0 1 4 4v2" />
    </svg>
  )
}

export function RisksIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <circle cx="12" cy="16.5" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  )
}

export function KnowledgeIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M12 6c-2-1.5-5-2-8-1v13c3-1 6-.5 8 1 2-1.5 5-2 8-1V5c-3-1-6-.5-8 1z" />
      <line x1="12" y1="6" x2="12" y2="19" />
    </svg>
  )
}

export function SearchAuditIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="11" cy="11" r="7" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  )
}

export function AutomationIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <polygon points="13 2 4 14 11 14 10 22 20 10 13 10 13 2" />
    </svg>
  )
}

export function EngineeringIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L2 19v3h3l7.3-7.3a4 4 0 0 0 5.4-5.4l-2.8 2.8-2-2 2.8-2.8z" />
    </svg>
  )
}

export function PersonalIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21v-1a8 8 0 0 1 16 0v1" />
    </svg>
  )
}

export function TeamIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg {...base} {...props}>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2 20v-1a6 6 0 0 1 11 0v1" />
      <circle cx="17" cy="9" r="2.8" />
      <path d="M15 14a5 5 0 0 1 5 5v1" />
    </svg>
  )
}
```

- [ ] **Step 2: Write the test**

Create `frontend/src/navigation/icons.test.tsx`:

```tsx
// @vitest-environment jsdom

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import {
  AttentionIcon, AutomationIcon, EngineeringIcon, KnowledgeIcon, MeetingPrepIcon,
  NotesIcon, PersonalIcon, PlannerIcon, RecommendationsIcon, RisksIcon,
  ScheduleIcon, SearchAuditIcon, TeamIcon, TodayIcon, WorkIcon,
} from './icons'

afterEach(() => cleanup())

const ICONS = {
  TodayIcon, AttentionIcon, RecommendationsIcon, WorkIcon, NotesIcon, ScheduleIcon,
  PlannerIcon, MeetingPrepIcon, RisksIcon, KnowledgeIcon, SearchAuditIcon,
  AutomationIcon, EngineeringIcon, PersonalIcon, TeamIcon,
}

describe('sidebar workspace icons', () => {
  it('has exactly 15 icons, one per workspace', () => {
    expect(Object.keys(ICONS)).toHaveLength(15)
  })

  it.each(Object.entries(ICONS))('%s renders a 24x24 stroke svg and forwards props', (_name, Icon) => {
    const { container } = render(<Icon aria-hidden="true" className="sidebar-nav-icon" data-testid="icon" />)
    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg?.getAttribute('viewBox')).toBe('0 0 24 24')
    expect(svg?.getAttribute('aria-hidden')).toBe('true')
    expect(svg?.getAttribute('class')).toBe('sidebar-nav-icon')
    expect(svg?.getAttribute('stroke')).toBe('currentColor')
  })
})
```

- [ ] **Step 3: Run the test**

Run: `pnpm vitest run src/navigation/icons.test.tsx`
Expected: PASS, 16 tests (1 count check + 15 `it.each` cases).

- [ ] **Step 4: Visual check**

Build and capture a close-up of all 15 icons rendered in the sidebar is not possible yet (Task 2 wires them in) — instead, render each icon standalone via a small throwaway script to eyeball them for gross SVG errors (a self-intersecting path, an off-canvas coordinate). Create `frontend/tmp-icon-check.mjs` (do not commit it):

```js
import path from 'node:path'
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { startPreviewServer } from './e2e/server.mjs'

const OUT = process.argv[2]
await mkdir(OUT, { recursive: true })
// A tiny standalone HTML harness that imports nothing from the app -- just
// confirms the 15 <svg> blocks above render as visible, on-canvas strokes.
const html = `<!doctype html><html><body style="background:#fff;display:flex;gap:12px;padding:24px;">
${['sun','bell','sparkles','briefcase','file','calendar','checks','meeting','shield','book','search','zap','wrench','user','team']
  .map((n) => `<div style="width:32px;height:32px;color:#18212f" id="${n}"></div>`).join('')}
</body></html>`
const server = await startPreviewServer()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 600, height: 100 } })
const page = await context.newPage()
await page.setContent(html)
// Paste each icon's inner <svg>...</svg> markup (from icons.tsx) into its div by hand for this one-off check, or skip this script and instead just re-read icons.tsx by eye for any coordinate outside 0-24 -- either is acceptable for this step.
await context.close()
await browser.close()
server.stop()
```

This throwaway harness is optional scaffolding, not a requirement — the real, required check is: read every icon's `<svg>` block in `icons.tsx` and confirm every numeric coordinate is between 0 and 24 (a coordinate outside that range draws outside the canvas and is a bug). Do this by eye; if anything looks wrong, fix the coordinates directly in `icons.tsx` before committing. Delete `frontend/tmp-icon-check.mjs` if created; it must not be committed.

- [ ] **Step 5: Full checks**

Run: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build`
Expected: all pass. `check:tokens` is unaffected (no color or font-size literals in this file). Unit total: existing count + 16.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/navigation/icons.tsx frontend/src/navigation/icons.test.tsx
git commit -m "$(cat <<'EOF'
feat(navigation): add 15 inline sidebar workspace icons

Hand-simplified stroke glyphs in the style of Lucide (MIT), inlined rather
than adding an icon package as a dependency. currentColor stroke, no new
color token; not yet wired into the sidebar (next commit).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Wire icons into the sidebar

**Files:**
- Modify: `frontend/src/navigation/workspaces.ts`
- Modify: `frontend/src/navigation/workspaces.test.ts`
- Modify: `frontend/src/navigation/SidebarNavigation.tsx`
- Modify: `frontend/src/navigation/SidebarNavigation.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: the 15 icon components from Task 1.
- Produces: `WorkspaceEntry.icon: ComponentType<SVGProps<SVGSVGElement>>` (required); each `<li>` in the sidebar renders `<span className="sidebar-nav-link-content"><Icon aria-hidden="true" className="sidebar-nav-icon" /><span>{label}</span></span>` before the badge.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/navigation/workspaces.test.ts`, change the import line to add `icon` checking; append this case inside `describe('workspaces', ...)`, after the last existing `it`:

```ts
  it('gives every workspace a distinct icon component', () => {
    const icons = WORKSPACES.map((w) => w.icon)
    expect(icons).toHaveLength(15)
    expect(icons.every((Icon) => typeof Icon === 'function')).toBe(true)
    expect(new Set(icons).size).toBe(15)
  })
```

In `frontend/src/navigation/SidebarNavigation.test.tsx`, append this case inside `describe('SidebarNavigation', ...)`, after the last existing `it`:

```tsx
  it('renders a decorative, aria-hidden icon before every link label', () => {
    renderAt('/today')
    const link = screen.getByRole('link', { name: 'Notes' })
    const icon = link.querySelector('svg')
    expect(icon).not.toBeNull()
    expect(icon?.getAttribute('aria-hidden')).toBe('true')
    // The icon must not be part of the accessible name -- getByRole above
    // already proves this (it matched on the visible text "Notes" alone),
    // but assert it explicitly too: the svg carries no text content.
    expect(icon?.textContent).toBe('')
  })
```

- [ ] **Step 2: Run to confirm failure**

Run: `pnpm vitest run src/navigation/workspaces.test.ts src/navigation/SidebarNavigation.test.tsx`
Expected: FAIL. `workspaces.test.ts`'s new case fails because `icon` is `undefined` on every entry; `SidebarNavigation.test.tsx`'s new case fails because no `<svg>` exists inside the link yet.

- [ ] **Step 3: Add the `icon` field to `workspaces.ts`**

Add this import line at the top of `frontend/src/navigation/workspaces.ts`, directly after the existing `import type { WorkspaceView } from '../api/types'` line:

```ts
import type { ComponentType, SVGProps } from 'react'

import {
  AttentionIcon, AutomationIcon, EngineeringIcon, KnowledgeIcon, MeetingPrepIcon,
  NotesIcon, PersonalIcon, PlannerIcon, RecommendationsIcon, RisksIcon,
  ScheduleIcon, SearchAuditIcon, TeamIcon, TodayIcon, WorkIcon,
} from './icons'
```

In the `WorkspaceEntry` type, add a required field directly after `composition: Composition`:

```ts
  icon: ComponentType<SVGProps<SVGSVGElement>>
```

In the `WORKSPACES` array, add `icon: <X>Icon,` to every one of the 15 entries, directly after each entry's `composition:` field. The array becomes (only the changed lines shown per entry — every other field on each entry is unchanged):

```ts
export const WORKSPACES: ReadonlyArray<WorkspaceEntry> = [
  { view: 'today', label: 'Today', path: '/today', group: null, composition: 'cards', icon: TodayIcon },
  {
    view: 'attention',
    label: 'Attention',
    path: '/attention',
    group: null,
    composition: 'cards',
    icon: AttentionIcon,
    badgeCountLabel: (n) => `${n} ${n === 1 ? 'item' : 'items'} needing attention`,
  },
  {
    view: 'recommendations',
    label: 'Recommendations',
    path: '/recommendations',
    group: null,
    composition: 'canvas',
    icon: RecommendationsIcon,
    badgeCountLabel: (n) => `${n} open ${n === 1 ? 'recommendation' : 'recommendations'}`,
  },
  {
    view: 'work',
    label: 'Work',
    path: '/work',
    group: 'work',
    composition: 'cards',
    icon: WorkIcon,
    badgeCountLabel: (n) => `${n} open ${n === 1 ? 'task' : 'tasks'}`,
  },
  { view: 'notes', label: 'Notes', path: '/notes', group: 'work', composition: 'canvas', icon: NotesIcon },
  { view: 'schedule', label: 'Schedule', path: '/schedule', group: 'work', composition: 'cards', icon: ScheduleIcon },
  { view: 'planner', label: 'Planner', path: '/planner', group: 'work', composition: 'canvas', icon: PlannerIcon },
  { view: 'meeting-prep', label: 'Meeting prep', path: '/meeting-prep', group: 'work', composition: 'canvas', icon: MeetingPrepIcon },
  {
    view: 'risks',
    label: 'Risks',
    path: '/risks',
    group: 'risk-knowledge',
    composition: 'cards',
    icon: RisksIcon,
    badgeCountLabel: (n) => `${n} ${n === 1 ? 'risk' : 'risks'} due for review`,
  },
  {
    view: 'knowledge',
    label: 'Knowledge',
    path: '/knowledge',
    group: 'risk-knowledge',
    composition: 'cards',
    icon: KnowledgeIcon,
    badgeCountLabel: (n) => `${n} resolution ${n === 1 ? 'candidate' : 'candidates'}`,
  },
  { view: 'search-audit', label: 'Search & audit', path: '/search-audit', group: 'risk-knowledge', composition: 'canvas', icon: SearchAuditIcon },
  {
    view: 'automation',
    label: 'Automation',
    path: '/automation',
    group: 'systems',
    composition: 'canvas',
    icon: AutomationIcon,
    badgeCountLabel: (n) => `${n} pending ${n === 1 ? 'approval' : 'approvals'}`,
  },
  { view: 'engineering', label: 'Engineering', path: '/engineering', group: 'systems', composition: 'canvas', icon: EngineeringIcon },
  { view: 'personal', label: 'Personal', path: '/personal', group: 'account', composition: 'canvas', icon: PersonalIcon },
  // "Team", not "Collaboration" -- matches WorkspaceNavigation.tsx's
  // existing visible label. Path is /team for the same reason (a URL a
  // user would actually type/bookmark should match what they read).
  { view: 'collaboration', label: 'Team', path: '/team', group: 'account', composition: 'canvas', icon: TeamIcon },
]
```

- [ ] **Step 4: Update `SidebarNavigation.tsx`**

Replace the `<li>` block:

```tsx
              <li key={entry.view}>
                <NavLink to={entry.path} end>
                  <span>{entry.label}</span>
                  {counts[entry.view] ? (
                    <span className="sidebar-nav-badge" aria-label={entry.badgeCountLabel?.(counts[entry.view]!) ?? `${counts[entry.view]} items`}>
                      {counts[entry.view]}
                    </span>
                  ) : null}
                </NavLink>
              </li>
```

with:

```tsx
              <li key={entry.view}>
                <NavLink to={entry.path} end>
                  <span className="sidebar-nav-link-content">
                    <entry.icon aria-hidden="true" className="sidebar-nav-icon" />
                    <span>{entry.label}</span>
                  </span>
                  {counts[entry.view] ? (
                    <span className="sidebar-nav-badge" aria-label={entry.badgeCountLabel?.(counts[entry.view]!) ?? `${counts[entry.view]} items`}>
                      {counts[entry.view]}
                    </span>
                  ) : null}
                </NavLink>
              </li>
```

- [ ] **Step 5: Add the two CSS rules**

In `frontend/src/styles.css`, find the `.sidebar-nav a { ... }` rule and insert these two rules directly after its closing `}`:

```css
.sidebar-nav-link-content { display: flex; align-items: center; gap: var(--space-2); }
.sidebar-nav-icon { width: 18px; height: 18px; flex-shrink: 0; }
```

- [ ] **Step 6: Run the tests, confirm GREEN**

Run: `pnpm vitest run src/navigation/workspaces.test.ts src/navigation/SidebarNavigation.test.tsx`
Expected: PASS. `workspaces.test.ts`: 9 existing + 1 new = 10. `SidebarNavigation.test.tsx`: 4 existing + 1 new = 5.

- [ ] **Step 7: Full checks and a visual look**

Run: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build`
Expected: all pass.

Then capture and look at the sidebar:

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/action-hierarchy-snaps
pnpm visual:snapshots capture "$SNAPS/task2"
```

Open `$SNAPS/task2/desktop-today.png` with the Read tool. Expected: all 15 sidebar links now show a small icon before their label, aligned with the existing selected/unselected text color (the selected item's icon should read as ink/weight-650-toned, matching its label). If any icon renders broken (a visibly wrong or missing shape), go back and fix its coordinates in `icons.tsx` (from Task 1, not yet committed further — amend is not needed since Task 1's commit already landed; make the fix as part of this task's commit) before proceeding.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/navigation/workspaces.ts frontend/src/navigation/workspaces.test.ts frontend/src/navigation/SidebarNavigation.tsx frontend/src/navigation/SidebarNavigation.test.tsx frontend/src/styles.css
git commit -m "$(cat <<'EOF'
feat(navigation): render the new workspace icons in the sidebar

WorkspaceEntry gains a required icon field (mirrors composition's own
required-field discipline -- a new workspace can't ship without choosing
one). Icons sit before the label, aria-hidden, inheriting the link's own
text color via currentColor -- no new CSS color rule.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Wizard forward-action buttons

**Files:**
- Modify: `frontend/src/features/schedule/ScheduleWorkspace.tsx`, `frontend/src/features/schedule/ScheduleWorkspace.test.tsx`
- Modify: `frontend/src/features/risks/RiskWorkspace.tsx`, `frontend/src/features/risks/RiskWorkspace.test.tsx`
- Modify: `frontend/src/features/automation/PolicyPanel.tsx`, `frontend/src/features/automation/PolicyPanel.test.tsx`
- Modify: `frontend/src/features/automation/WorkflowList.tsx`, `frontend/src/features/automation/WorkflowList.test.tsx`
- Modify: `frontend/src/features/personal/GmailPanel.tsx`, `frontend/src/features/personal/GmailPanel.test.tsx`

**Interfaces:**
- None — each edit is additive (`className="btn-primary"` on an existing button), no new exports or props.

This task is 17 near-identical edits across 5 files: add `className="btn-primary"` to the Continue/Create/Connect button in every wizard step row that has one, per the spec's shape-1 rule (the single unambiguous forward action in the row). `ConnectorHealthPanel.tsx`'s 3 existing `.btn-primary` uses are the precedent and are NOT touched (already correct).

- [ ] **Step 1: `ScheduleWorkspace.tsx` — 6 edits**

Edit 1, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateEventNext}>Continue</button></div>
          </div>
        ) : createEventStep === 'details' ? (
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" className="btn-primary" onClick={goCreateEventNext}>Continue</button></div>
          </div>
        ) : createEventStep === 'details' ? (
```

Edit 2, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateEventBack}>Back</button><button type="button" onClick={goCreateEventNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createEventStepIndex + 1} of {CREATE_EVENT_STEPS.length} · Review</p>
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateEventBack}>Back</button><button type="button" className="btn-primary" onClick={goCreateEventNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createEventStepIndex + 1} of {CREATE_EVENT_STEPS.length} · Review</p>
```

Edit 3, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateEventBack}>Back</button><button type="submit" aria-busy={pending} disabled={pending}>Create event</button></div>
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateEventBack}>Back</button><button type="submit" className="btn-primary" aria-busy={pending} disabled={pending}>Create event</button></div>
```

Edit 4, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateMeetingNext}>Continue</button></div>
          </div>
        ) : createMeetingStep === 'notes' ? (
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" className="btn-primary" onClick={goCreateMeetingNext}>Continue</button></div>
          </div>
        ) : createMeetingStep === 'notes' ? (
```

Edit 5, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateMeetingBack}>Back</button><button type="button" onClick={goCreateMeetingNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createMeetingStepIndex + 1} of {CREATE_MEETING_STEPS.length} · Review</p>
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateMeetingBack}>Back</button><button type="button" className="btn-primary" onClick={goCreateMeetingNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createMeetingStepIndex + 1} of {CREATE_MEETING_STEPS.length} · Review</p>
```

Edit 6, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateMeetingBack}>Back</button><button type="submit" aria-busy={pending} disabled={pending}>{createMeeting.calendarEventId ? 'Create linked meeting' : 'Create standalone meeting'}</button></div>
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateMeetingBack}>Back</button><button type="submit" className="btn-primary" aria-busy={pending} disabled={pending}>{createMeeting.calendarEventId ? 'Create linked meeting' : 'Create standalone meeting'}</button></div>
```

In `frontend/src/features/schedule/ScheduleWorkspace.test.tsx`, add these assertions (create a new `it` if none of these buttons is already targeted by name in an existing test; otherwise add the `.className` check next to the existing interaction that already clicks/finds that button):

```tsx
  it('marks the forward action in each wizard step and review submit as btn-primary', () => {
    // Adjust the render/step-navigation calls below to match this file's
    // existing helper functions for reaching each step (see the file's other
    // tests for the pattern) -- render the workspace, open "Create event",
    // and check each step in turn.
    // Basics step:
    expect(screen.getByRole('button', { name: 'Continue' }).className).toBe('btn-primary')
    // ... navigate to Details step ...
    // expect(screen.getByRole('button', { name: 'Continue' }).className).toBe('btn-primary')
    // ... navigate to Review step ...
    // expect(screen.getByRole('button', { name: 'Create event' }).className).toBe('btn-primary')
  })
```

Because this file's exact step-navigation test helpers are internal to the existing test suite (not part of this plan's survey), the implementer must read `ScheduleWorkspace.test.tsx`'s existing tests for how they open the create-event and create-meeting wizards and drive them step by step, then add one `.className` assertion per button at the point each step is already reached in an existing or new test — six assertions total (3 for create-event: Continue/Continue/Create event; 3 for create-meeting: Continue/Continue/Create-linked-or-standalone-meeting). Do not weaken this to fewer than 6 assertions.

- [ ] **Step 2: `RiskWorkspace.tsx` — 3 edits**

Edit 1, find:
```tsx
          <div className="work-actions">
            <button type="button" onClick={goCreateNext}>Continue</button>
          </div>
        </div>
      ) : createStep === 'plan' ? (
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button>
          </div>
        </div>
      ) : createStep === 'plan' ? (
```

Edit 2, find:
```tsx
          <div className="work-actions">
            <button type="button" onClick={goCreateBack}>Back</button>
            <button type="button" onClick={goCreateNext}>Continue</button>
          </div>
        </div>
      ) : (
        <div className="wizard-review">
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="button" onClick={goCreateBack}>Back</button>
            <button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button>
          </div>
        </div>
      ) : (
        <div className="wizard-review">
```

Edit 3, find:
```tsx
          <div className="work-actions">
            <button type="button" onClick={goCreateBack}>Back</button>
            <button type="submit" aria-busy={pending} disabled={pending}>Create risk</button>
          </div>
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="button" onClick={goCreateBack}>Back</button>
            <button type="submit" className="btn-primary" aria-busy={pending} disabled={pending}>Create risk</button>
          </div>
```

In `frontend/src/features/risks/RiskWorkspace.test.tsx`, following the same pattern as Task 3 Step 1 (read the file's existing wizard-navigation tests, add one `.className` assertion per button at each step already reached): 3 assertions — Continue (details step), Continue (plan step), "Create risk" (review step).

- [ ] **Step 3: `PolicyPanel.tsx` — 3 edits**

Edit 1, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateNext}>Continue</button></div>
          </div>
        ) : createStep === 'limits' ? (
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button></div>
          </div>
        ) : createStep === 'limits' ? (
```

Edit 2, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateBack}>Back</button><button type="button" onClick={goCreateNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Review</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>Review and create</h4>
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateBack}>Back</button><button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Review</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>Review and create</h4>
```

Edit 3, find:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateBack}>Back</button><button type="submit" aria-busy={createMutation.isPending} disabled={pending}>{createMutation.isPending ? 'Creating…' : 'Create policy'}</button></div>
```
Replace with:
```tsx
            <div className="work-actions"><button type="button" onClick={goCreateBack}>Back</button><button type="submit" className="btn-primary" aria-busy={createMutation.isPending} disabled={pending}>{createMutation.isPending ? 'Creating…' : 'Create policy'}</button></div>
```

In `frontend/src/features/automation/PolicyPanel.test.tsx`, same pattern: 3 assertions — Continue x2, "Create policy" (the review-step submit's accessible name is the rendered text at the moment of assertion; if the test reaches this step before the mutation is pending, assert `{ name: 'Create policy' }`).

- [ ] **Step 4: `WorkflowList.tsx` — 3 edits**

Edit 1, find (the basics-step Continue — locate the exact surrounding text in the file, since the survey's line 242-244 falls inside a longer basics-step block not fully quoted above; search for this unique fragment):
```tsx
            <div className="work-actions">
              <button type="button" onClick={goCreateNext}>Continue</button>
            </div>
          </div>
        ) : createStep === 'build' ? (
```
Replace with:
```tsx
            <div className="work-actions">
              <button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button>
            </div>
          </div>
        ) : createStep === 'build' ? (
```

Edit 2, find:
```tsx
            <div className="work-actions">
              <button type="button" onClick={goCreateBack}>Back</button>
              <button type="button" onClick={goCreateNext}>Continue</button>
            </div>
          </div>
        ) : (
          <div className="wizard-review">
```
Replace with:
```tsx
            <div className="work-actions">
              <button type="button" onClick={goCreateBack}>Back</button>
              <button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button>
            </div>
          </div>
        ) : (
          <div className="wizard-review">
```

Edit 3, find:
```tsx
            <div className="work-actions">
              <button type="button" onClick={goCreateBack}>Back</button>
              <button type="submit" aria-busy={pending} disabled={pending}>{pending ? 'Creating…' : 'Create draft'}</button>
            </div>
```
Replace with:
```tsx
            <div className="work-actions">
              <button type="button" onClick={goCreateBack}>Back</button>
              <button type="submit" className="btn-primary" aria-busy={pending} disabled={pending}>{pending ? 'Creating…' : 'Create draft'}</button>
            </div>
```

In `frontend/src/features/automation/WorkflowList.test.tsx`, same pattern: 3 assertions.

- [ ] **Step 5: `GmailPanel.tsx` — 2 edits**

Edit 1, find:
```tsx
          <div className="work-actions">
            <button type="button" onClick={() => setConnectStep(2)}>Continue</button>
          </div>
        </div>
      ) : (
        <div>
          <p className="eyebrow">Step 2 of 2 · Sign in</p>
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="button" className="btn-primary" onClick={() => setConnectStep(2)}>Continue</button>
          </div>
        </div>
      ) : (
        <div>
          <p className="eyebrow">Step 2 of 2 · Sign in</p>
```

Edit 2, find:
```tsx
          <div className="work-actions">
            <button type="button" onClick={() => setConnectStep(1)}>Back</button>
            <button type="button" aria-busy={oauthStartMutation.isPending} disabled={oauthStartMutation.isPending} onClick={() => oauthStartMutation.mutate()}>
              {oauthStartMutation.isPending ? 'Redirecting to Google…' : 'Connect Gmail'}
            </button>
          </div>
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="button" onClick={() => setConnectStep(1)}>Back</button>
            <button type="button" className="btn-primary" aria-busy={oauthStartMutation.isPending} disabled={oauthStartMutation.isPending} onClick={() => oauthStartMutation.mutate()}>
              {oauthStartMutation.isPending ? 'Redirecting to Google…' : 'Connect Gmail'}
            </button>
          </div>
```

In `frontend/src/features/personal/GmailPanel.test.tsx`, same pattern: 2 assertions — "Continue" (step 1) and "Connect Gmail" (step 2).

- [ ] **Step 6: Run the 5 test files**

Run: `pnpm vitest run src/features/schedule/ScheduleWorkspace.test.tsx src/features/risks/RiskWorkspace.test.tsx src/features/automation/PolicyPanel.test.tsx src/features/automation/WorkflowList.test.tsx src/features/personal/GmailPanel.test.tsx`
Expected: PASS, all `.className` assertions green (17 total across the 5 files, folded into however many `it` blocks each file's existing structure needed).

- [ ] **Step 7: Full checks**

Run: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/features/schedule/ScheduleWorkspace.tsx frontend/src/features/schedule/ScheduleWorkspace.test.tsx frontend/src/features/risks/RiskWorkspace.tsx frontend/src/features/risks/RiskWorkspace.test.tsx frontend/src/features/automation/PolicyPanel.tsx frontend/src/features/automation/PolicyPanel.test.tsx frontend/src/features/automation/WorkflowList.tsx frontend/src/features/automation/WorkflowList.test.tsx frontend/src/features/personal/GmailPanel.tsx frontend/src/features/personal/GmailPanel.test.tsx
git commit -m "$(cat <<'EOF'
feat(ui): mark the forward action in every wizard step as btn-primary

Applies the existing "one dominant forward action per row" rule (DESIGN.md,
Buttons: action hierarchy) to the Continue/Connect/Create buttons in
ScheduleWorkspace's two wizards, RiskWorkspace, PolicyPanel, WorkflowList,
and GmailPanel's connect flow -- the same Back+primary-forward shape
ConnectorHealthPanel already used, generalized to the rows that were still
unstyled. 17 buttons across 5 files; every Back/Cancel sibling stays
default.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Confirm/cancel and confirm/reject pairs

**Files:**
- Modify: `frontend/src/features/knowledge/EntityDetail.tsx`, `frontend/src/features/knowledge/EntityDetail.test.tsx`
- Modify: `frontend/src/features/attention/RiskReviewQueue.tsx`, `frontend/src/features/attention/RiskReviewQueue.test.tsx`
- Modify: `frontend/src/features/attention/Planner.tsx`, `frontend/src/features/attention/Planner.test.tsx`
- Modify: `frontend/src/features/attention/AttentionExplanation.tsx`, `frontend/src/features/attention/AttentionExplanation.test.tsx`
- Modify: `frontend/src/features/engineering/TeamSuggestionsPanel.tsx`, `frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx`
- Modify: `frontend/src/features/attention/WaitingView.tsx`, `frontend/src/features/attention/WaitingView.test.tsx`
- Modify: `frontend/src/features/automation/ApprovalInbox.tsx`, `frontend/src/features/automation/ApprovalInbox.test.tsx`

**Interfaces:** none — additive `className="btn-primary"` only, same as Task 3.

- [ ] **Step 1: `EntityDetail.tsx` — 1 edit**

Find:
```tsx
                      <button type="submit" disabled={correctClaimMutation.isPending} aria-busy={correctClaimMutation.isPending}>Save correction</button>
                      <button type="button" onClick={() => setCorrectingClaimId(null)}>Cancel</button>
```
Replace with:
```tsx
                      <button type="submit" className="btn-primary" disabled={correctClaimMutation.isPending} aria-busy={correctClaimMutation.isPending}>Save correction</button>
                      <button type="button" onClick={() => setCorrectingClaimId(null)}>Cancel</button>
```

In `EntityDetail.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Save correction' }).className).toBe('btn-primary')` at the point an existing test already opens the correction form (or add a new minimal test that opens it and asserts this).

- [ ] **Step 2: `RiskReviewQueue.tsx` — 1 edit**

Find:
```tsx
          <button type="submit" disabled={pending} aria-busy={pending}>Save review</button>
          <button type="button" disabled={pending} aria-busy={pending} onClick={() => setReviewing(null)}>Discard</button>
```
Replace with:
```tsx
          <button type="submit" className="btn-primary" disabled={pending} aria-busy={pending}>Save review</button>
          <button type="button" disabled={pending} aria-busy={pending} onClick={() => setReviewing(null)}>Discard</button>
```

In `RiskReviewQueue.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Save review' }).className).toBe('btn-primary')` at the point an existing test opens the review form (the existing test at line ~63 already clicks "Save review" — add the assertion just before that click).

- [ ] **Step 3: `Planner.tsx` — 2 edits**

Edit 1, find:
```tsx
          <div className="work-actions">
            <button type="button" disabled={pending} aria-busy={pending} onClick={() => { acceptMutation.mutate(pendingDiff); setPendingDiff(null) }}>Accept new plan</button>
            <button type="button" disabled={pending} aria-busy={pending} onClick={() => setPendingDiff(null)}>Keep reviewing</button>
          </div>
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="button" className="btn-primary" disabled={pending} aria-busy={pending} onClick={() => { acceptMutation.mutate(pendingDiff); setPendingDiff(null) }}>Accept new plan</button>
            <button type="button" disabled={pending} aria-busy={pending} onClick={() => setPendingDiff(null)}>Keep reviewing</button>
          </div>
```

Edit 2, find:
```tsx
                          <button
                            type="button"
                            disabled={pending}
                            aria-busy={pending}
                            onClick={() => { moveBlockMutation.mutate({ plan, block, startsAt: editingBlock.startsAt, endsAt: editingBlock.endsAt }); setEditingBlock(null) }}
                          >
                            Save new time
                          </button>
                          <button type="button" disabled={pending} aria-busy={pending} onClick={() => setEditingBlock(null)}>Cancel</button>
```
Replace with:
```tsx
                          <button
                            type="button"
                            className="btn-primary"
                            disabled={pending}
                            aria-busy={pending}
                            onClick={() => { moveBlockMutation.mutate({ plan, block, startsAt: editingBlock.startsAt, endsAt: editingBlock.endsAt }); setEditingBlock(null) }}
                          >
                            Save new time
                          </button>
                          <button type="button" disabled={pending} aria-busy={pending} onClick={() => setEditingBlock(null)}>Cancel</button>
```

In `Planner.test.tsx`, add two assertions at the points an existing test reaches the replan-diff panel and the block-edit panel: `expect(screen.getByRole('button', { name: 'Accept new plan' }).className).toBe('btn-primary')` and `expect(screen.getByRole('button', { name: 'Save new time' }).className).toBe('btn-primary')`.

- [ ] **Step 4: `AttentionExplanation.tsx` — 1 edit**

Find:
```tsx
      <div className="work-actions" role="group" aria-label="AI explanation actions">
        <button type="button" onClick={requestExplanation}>Regenerate</button>
        <button type="button" aria-label="Discard AI explanation" onClick={discard}>Discard</button>
      </div>
```
Replace with:
```tsx
      <div className="work-actions" role="group" aria-label="AI explanation actions">
        <button type="button" className="btn-primary" onClick={requestExplanation}>Regenerate</button>
        <button type="button" aria-label="Discard AI explanation" onClick={discard}>Discard</button>
      </div>
```

In `AttentionExplanation.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Regenerate' }).className).toBe('btn-primary')` at the point an existing test reaches this state.

- [ ] **Step 5: `TeamSuggestionsPanel.tsx` — 1 edit**

Find:
```tsx
      <div className="work-actions">
        <button type="button" aria-busy={confirmMutation.isPending} disabled={!teamEntityId || busy} onClick={() => confirmMutation.mutate()}>
          Confirm
        </button>
        <button type="button" aria-busy={dismissMutation.isPending} disabled={busy} onClick={() => dismissMutation.mutate()}>
          Dismiss
        </button>
      </div>
```
Replace with:
```tsx
      <div className="work-actions">
        <button type="button" className="btn-primary" aria-busy={confirmMutation.isPending} disabled={!teamEntityId || busy} onClick={() => confirmMutation.mutate()}>
          Confirm
        </button>
        <button type="button" aria-busy={dismissMutation.isPending} disabled={busy} onClick={() => dismissMutation.mutate()}>
          Dismiss
        </button>
      </div>
```

In `TeamSuggestionsPanel.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Confirm' }).className).toBe('btn-primary')`.

- [ ] **Step 6: `WaitingView.tsx` — 1 edit**

Find:
```tsx
              <button type="button" disabled={pending} aria-busy={pending} aria-label={`Fulfil waiting item ${link.id}`} onClick={() => terminalMutation.mutate({ link, action: 'fulfil' })}>Fulfil</button>
              <button type="button" disabled={pending} aria-busy={pending} aria-label={`Cancel waiting item ${link.id}`} onClick={() => terminalMutation.mutate({ link, action: 'cancel' })}>Cancel</button>
```
Replace with:
```tsx
              <button type="button" className="btn-primary" disabled={pending} aria-busy={pending} aria-label={`Fulfil waiting item ${link.id}`} onClick={() => terminalMutation.mutate({ link, action: 'fulfil' })}>Fulfil</button>
              <button type="button" disabled={pending} aria-busy={pending} aria-label={`Cancel waiting item ${link.id}`} onClick={() => terminalMutation.mutate({ link, action: 'cancel' })}>Cancel</button>
```

In `WaitingView.test.tsx`, add: `expect(screen.getByRole('button', { name: /^Fulfil waiting item/ }).className).toBe('btn-primary')`.

- [ ] **Step 7: `ApprovalInbox.tsx` — 1 edit**

Find:
```tsx
          <div className="work-actions">
            <button type="submit" aria-busy={pending && decideMutation.variables?.decision === 'approve'} disabled={expired || pending}>{pending && decideMutation.variables?.decision === 'approve' ? 'Approving…' : 'Approve'}</button>
            <button type="button" aria-busy={pending && decideMutation.variables?.decision === 'reject'} disabled={expired || pending} onClick={() => decideMutation.mutate({ decision: 'reject' })}>{pending && decideMutation.variables?.decision === 'reject' ? 'Rejecting…' : 'Reject'}</button>
          </div>
```
Replace with:
```tsx
          <div className="work-actions">
            <button type="submit" className="btn-primary" aria-busy={pending && decideMutation.variables?.decision === 'approve'} disabled={expired || pending}>{pending && decideMutation.variables?.decision === 'approve' ? 'Approving…' : 'Approve'}</button>
            <button type="button" aria-busy={pending && decideMutation.variables?.decision === 'reject'} disabled={expired || pending} onClick={() => decideMutation.mutate({ decision: 'reject' })}>{pending && decideMutation.variables?.decision === 'reject' ? 'Rejecting…' : 'Reject'}</button>
          </div>
```

In `ApprovalInbox.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Approve' }).className).toBe('btn-primary')`. Leave "Reject" unstyled — per the spec, this sub-project does not add new `.btn-destructive` assignments, even though `DelegationsPanel.tsx`'s analogous "Reject" already has one (that inconsistency is documented in Task 6, not fixed here).

- [ ] **Step 8: Run the 7 test files**

Run: `pnpm vitest run src/features/knowledge/EntityDetail.test.tsx src/features/attention/RiskReviewQueue.test.tsx src/features/attention/Planner.test.tsx src/features/attention/AttentionExplanation.test.tsx src/features/engineering/TeamSuggestionsPanel.test.tsx src/features/attention/WaitingView.test.tsx src/features/automation/ApprovalInbox.test.tsx`
Expected: PASS, all 8 `.className` assertions green.

- [ ] **Step 9: Full checks**

Run: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/features/knowledge/EntityDetail.tsx frontend/src/features/knowledge/EntityDetail.test.tsx frontend/src/features/attention/RiskReviewQueue.tsx frontend/src/features/attention/RiskReviewQueue.test.tsx frontend/src/features/attention/Planner.tsx frontend/src/features/attention/Planner.test.tsx frontend/src/features/attention/AttentionExplanation.tsx frontend/src/features/attention/AttentionExplanation.test.tsx frontend/src/features/engineering/TeamSuggestionsPanel.tsx frontend/src/features/engineering/TeamSuggestionsPanel.test.tsx frontend/src/features/attention/WaitingView.tsx frontend/src/features/attention/WaitingView.test.tsx frontend/src/features/automation/ApprovalInbox.tsx frontend/src/features/automation/ApprovalInbox.test.tsx
git commit -m "$(cat <<'EOF'
feat(ui): mark the forward action in 7 confirm/cancel and confirm/reject rows

Save correction, Save review, Accept new plan, Save new time, Regenerate,
Confirm, Fulfil, and Approve each get btn-primary beside their unstyled
Cancel/Discard/Keep-reviewing/Dismiss/Reject sibling, per the same rule
Task 3 applied to wizards. ApprovalInbox's Reject stays unstyled -- no new
btn-destructive assignment in this sub-project (see DESIGN.md's Known
follow-ups after Task 6).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Multi-button dominant actions and the first `.btn-quiet`

**Files:**
- Modify: `frontend/src/features/knowledge/ResolutionInbox.tsx`, `frontend/src/features/knowledge/ResolutionInbox.test.tsx`
- Modify: `frontend/src/features/tasks/TaskWorkspace.tsx`, `frontend/src/features/tasks/TaskWorkspace.test.tsx`
- Modify: `frontend/src/features/collaboration/MembersPanel.tsx`, `frontend/src/features/collaboration/MembersPanel.test.tsx`

**Interfaces:** none — additive classNames only.

- [ ] **Step 1: `ResolutionInbox.tsx` — 1 edit**

Find:
```tsx
      <div className="work-actions" role="group" aria-label={`Actions for candidate ${candidate.id}`}>
        <button
          type="button"
          disabled={decisionMutation.isPending || !reason.trim()}
          onClick={() => decisionMutation.mutate('confirm')}
        >
          Confirm match
        </button>
```
Replace with:
```tsx
      <div className="work-actions" role="group" aria-label={`Actions for candidate ${candidate.id}`}>
        <button
          type="button"
          className="btn-primary"
          disabled={decisionMutation.isPending || !reason.trim()}
          onClick={() => decisionMutation.mutate('confirm')}
        >
          Confirm match
        </button>
```

(The "Reject" and "Defer" buttons directly below are unchanged — same rationale as `RecommendationPanel.tsx`'s existing precedent, where only the single dominant forward action gets `.btn-primary` and its other siblings stay default.)

In `ResolutionInbox.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Confirm match' }).className).toBe('btn-primary')`.

- [ ] **Step 2: `TaskWorkspace.tsx` — 1 edit**

Find:
```tsx
              {!archived && !terminal ? <><button type="button" disabled={rowBusy} aria-busy={rowBusy} aria-label={`Edit ${task.title}`} onClick={() => setEdit({ task, ...taskDraft(task), latestVersion: task.version, conflict: false, reloadFailed: false })}>Edit</button><button type="button" disabled={rowBusy} aria-busy={rowBusy} aria-label={`Complete ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'complete' })}>Complete</button><button type="button" className="btn-destructive" disabled={rowBusy} aria-busy={rowBusy} aria-label={`Cancel ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'cancel' })}>Cancel</button></> : null}
```
Replace with:
```tsx
              {!archived && !terminal ? <><button type="button" disabled={rowBusy} aria-busy={rowBusy} aria-label={`Edit ${task.title}`} onClick={() => setEdit({ task, ...taskDraft(task), latestVersion: task.version, conflict: false, reloadFailed: false })}>Edit</button><button type="button" className="btn-primary" disabled={rowBusy} aria-busy={rowBusy} aria-label={`Complete ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'complete' })}>Complete</button><button type="button" className="btn-destructive" disabled={rowBusy} aria-busy={rowBusy} aria-label={`Cancel ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'cancel' })}>Cancel</button></> : null}
```

("Edit" and "Cancel" are unchanged: Edit is a peer mode-switch, not a competing forward action — the same reasoning that lets `RecommendationPanel.tsx`'s existing "Pin" sibling stay default beside its own `.btn-primary` button. "Cancel" keeps its existing `.btn-destructive`.)

In `TaskWorkspace.test.tsx`, add: `expect(screen.getByRole('button', { name: /^Complete /  }).className).toBe('btn-primary')` (adjust the exact accessible-name pattern to match `aria-label={\`Complete ${task.title}\`}` for whatever task title the existing test fixture uses).

- [ ] **Step 3: `MembersPanel.tsx` — 1 edit (the first `.btn-quiet` consumer)**

Find:
```tsx
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(false)
                removeMutation.reset()
              }}
            >
              Cancel
            </button>
```
Replace with:
```tsx
            <button
              type="button"
              className="btn-quiet"
              onClick={() => {
                setConfirmRemove(false)
                removeMutation.reset()
              }}
            >
              Cancel
            </button>
```

In `MembersPanel.test.tsx`, add: `expect(screen.getByRole('button', { name: 'Cancel' }).className).toBe('btn-quiet')` at the point an existing test already opens the confirm-removal panel (the file's existing test at line ~134 already clicks "Remove" to reach this state — add the assertion right after).

- [ ] **Step 4: Run the 3 test files**

Run: `pnpm vitest run src/features/knowledge/ResolutionInbox.test.tsx src/features/tasks/TaskWorkspace.test.tsx src/features/collaboration/MembersPanel.test.tsx`
Expected: PASS, 3 assertions green.

- [ ] **Step 5: Full checks**

Run: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/features/knowledge/ResolutionInbox.tsx frontend/src/features/knowledge/ResolutionInbox.test.tsx frontend/src/features/tasks/TaskWorkspace.tsx frontend/src/features/tasks/TaskWorkspace.test.tsx frontend/src/features/collaboration/MembersPanel.tsx frontend/src/features/collaboration/MembersPanel.test.tsx
git commit -m "$(cat <<'EOF'
feat(ui): mark two multi-button dominant actions primary, give btn-quiet its first consumer

Confirm match (ResolutionInbox) and Complete (TaskWorkspace) get btn-primary
in their 3-4-button rows, the same shape RecommendationPanel's existing
Confirm-and-execute already established. MembersPanel's confirm-removal
Cancel becomes btn-quiet -- the one exact match the full-app survey found
for a dismiss action beside a btn-destructive confirm in an elevated
sub-panel; the class has had zero consumers since Visual Foundation v2.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Update DESIGN.md

**Files:**
- Modify: `DESIGN.md`

**Interfaces:**
- Consumes: the icon system (Task 2) and the button-hierarchy sweep (Tasks 3-5).

- [ ] **Step 1: Find and confirm every anchor**

Run from the worktree root: `grep -n "Not yet consumed by any component\|no icon library (zero icons anywhere" DESIGN.md`
Expected: two hits, one in the Buttons section, one in "Not yet part of the system." If either does not match exactly, STOP and report which one before editing.

- [ ] **Step 2: Update the Buttons section's `.btn-quiet` sentence**

Find (inside the `## Buttons: action hierarchy` paragraph):
```
Not yet consumed by any component — the redesign's later sub-projects (dashboard, navigation) are expected to be its first real usage.
```
Replace with:
```
Its first real consumer: `MembersPanel.tsx`'s "Cancel" beside "Confirm removal" — a dismiss action inside an elevated confirm sub-panel, next to a `.btn-destructive` button, where even the default bordered pill read as too heavy. A full app-wide button-hierarchy audit (sub-project 4, `docs/superpowers/specs/2026-09-23-action-hierarchy-design.md`) found this to be the only row that exact shape describes; reach for `.btn-quiet` again only when the same shape genuinely recurs, not as a general-purpose fourth tier.
```

Find (the sentence right after the existing `.btn-primary` explanation, still in the same paragraph):
```
Everything else is the default (secondary) button.
```
Leave this sentence exactly as is — do not edit it.

Append this new sentence to the end of the same paragraph (after the existing final sentence about the link-action variant):
```
That same audit brought `.btn-primary` up to date with the rule this paragraph already stated: every wizard's Continue/Connect/Create step, and every row with one unambiguous forward action beside a back/cancel/discard/reject/defer/dismiss sibling, now carries it — about 20 rows across 12 files, generalizing the shape `ConnectorHealthPanel.tsx`'s wizard and `RecommendationPanel.tsx`'s "Confirm and execute" already established. Left deliberately unstyled: rows with no real hierarchy (peer toggles, equal navigation links, Edit+Archive row actions), and lone terminal-submit buttons with no sibling and no wizard context — nothing in either shape is louder for `.btn-primary` to differentiate from.
```

- [ ] **Step 3: Update "Not yet part of the system"**

Find:
```
no icon library (zero icons anywhere in the app today — text labels do this work; picking a library is a real decision to make once a surface actually needs one, not before)
```
Replace with:
```
no general-purpose icon library — the sidebar has 15 hand-simplified inline SVG icons (`frontend/src/navigation/icons.tsx`, no new dependency), scoped to that one surface; picking a package for arbitrary future icon needs is still a real decision to make only once a surface actually needs one
```

- [ ] **Step 4: Add a Composition section — Navigation cross-reference (icons)**

In the `## Navigation` section, find the sentence in the "**Top-level workspace nav**" paragraph that ends `...(Composition above).` (added by the previous sub-project, describing the pinned sidebar). Append this new sentence directly after it, in the same paragraph:
```
Each link now also carries a small `aria-hidden` icon before its label (`frontend/src/navigation/icons.tsx`), colored via `currentColor` so it follows the same selected/unselected treatment as the label text with no new CSS color rule.
```

- [ ] **Step 5: Add a Known follow-ups bullet**

Find the `## Known follow-ups (deferred, not forgotten)` heading. Add this new bullet at the very end of that section, immediately before the next `##` heading:
```
- **`.btn-destructive` classification is inconsistent between structurally identical rows.** The action-hierarchy audit (sub-project 4) found `DelegationsPanel.tsx`'s "Reject" (beside "Accept") styled `.btn-destructive`, while `ApprovalInbox.tsx`'s "Reject" (beside "Approve," a comparably high-stakes decision) is not. Deliberately not resolved by that sub-project — deciding what counts as "irreversible or high-risk" per the Buttons section's own definition is a separate judgment call from assigning primary/quiet emphasis, and re-litigating every `.btn-destructive` use app-wide was out of that sub-project's scope. Resolve with its own small audit when picked up.
```

- [ ] **Step 6: Add the Provenance entry**

Append this paragraph at the very end of the file:
```
Action hierarchy and icons (sub-project 4 of the "Calm Executive Workspace" direction) landed 2026-09-23. Unlike sub-project 3, both of this sub-project's named halves turned out to have real, unaddressed work: the sidebar's 15 workspace links were still plain text (icons were explicitly deferred here by the navigation redesign), and a full survey of all 231 buttons in the app found `.btn-primary` used in only 2 files despite the Buttons section's own stated rule, and `.btn-quiet` with zero consumers anywhere. Fixed with 15 inline SVG icons (no new dependency, `currentColor`, `aria-hidden`) and a mechanical sweep applying `.btn-primary` to about 20 rows' single dominant forward action and `.btn-quiet` to its one exact-match row (`MembersPanel.tsx`). No new `.btn-destructive` assignment was made; a real inconsistency the survey found there is recorded above instead of fixed, since deciding what counts as destructive is a separate call from assigning emphasis. See `docs/superpowers/specs/2026-09-23-action-hierarchy-design.md` and `docs/superpowers/plans/2026-09-23-action-hierarchy.md`.
```

- [ ] **Step 7: Check and commit**

Run from the worktree root: `python3 scripts/check_docs.py; echo "docs exit: $?"` (must print `docs exit: 0`). The two `docs/superpowers/...` paths above are plain backtick text, not markdown links: keep them that way. Then from `frontend/`: `pnpm check:tokens` (must stay OK — no color/size literals were touched).

```bash
git add DESIGN.md
git commit -m "$(cat <<'EOF'
docs: document the sidebar icons and the button-hierarchy audit

Gives btn-quiet its first real consumer in prose, records the ~20-row
btn-primary sweep, updates the icon-library follow-up to reflect the new
sidebar icons, adds a Known follow-up for the Reject/btn-destructive
inconsistency the audit found but did not fix, and adds the sub-project 4
provenance entry.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Verify

**Files:**
- No source changes expected. Evidence only.

**Interfaces:**
- Consumes: every prior task.

- [ ] **Step 1: Full suite**

Run from `frontend/`: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build && pnpm test:e2e`
Expected: typecheck clean; `check:tokens` OK; build succeeds; every e2e scenario passes with its axe scan (a changed accessible name or a broken focus order on any edited button would show up here). Unit suite green, at a count of at least 602 (the pre-plan baseline of 584, plus Task 1's 16 icon tests, plus Task 2's 2 new cases) — the 28 `.className` assertions from Tasks 3-5 add to that floor exactly once per assertion that became its own new `it` block, and not at all for one folded into an existing test's body (both are correct per those tasks' own instructions, so there is no single exact target count to check against — only that floor of 602, and that every assertion those tasks listed is present and passing somewhere in the suite).

- [ ] **Step 2: Screenshots**

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/action-hierarchy-snaps
pnpm visual:snapshots capture "$SNAPS/final"
```

Open `$SNAPS/final/desktop-today.png` with the Read tool and confirm all 15 sidebar icons render cleanly at both selected and unselected states. Then open `$SNAPS/final/desktop-team.png` (or whichever captured page most easily exercises `MembersPanel.tsx`'s remove flow) — if the default e2e fixtures don't put the confirm-removal panel into a capturable state, this is acceptable to skip; the e2e scenario in Step 1 already exercises that interaction under axe.

- [ ] **Step 3: Report**

Final message: the full-suite pass/fail with counts, confirmation that all 15 icons render without visible defects, and a one-line note on the `MembersPanel.tsx` `.btn-quiet` check (screenshot or e2e-covered). No commit — this task only verifies.
