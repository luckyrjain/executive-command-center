# Navigation Redesign: Sidebar + Real Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the wrapped 15-pill top-level workspace nav with a grouped, persistent left sidebar backed by real URL routing (`react-router-dom`), with badge counts on 6 workspaces and a mobile stopgap that reuses today's pill nav unchanged.

**Architecture:** `react-router-dom`'s `<BrowserRouter>`/`<Routes>` replaces `App.tsx`'s `currentView` `useState` + giant ternary. A new `frontend/src/navigation/workspaces.ts` module is the single source of truth for the 15 workspaces' view/label/path/group. `SidebarNavigation.tsx` (desktop) renders real `<NavLink>`s grouped into 5 sections plus badge counts from a new `useWorkspaceBadgeCounts` hook. `WorkspaceNavigation.tsx` (today's pill nav) is kept completely unchanged and wrapped by a thin new `MobileWorkspaceNav.tsx` that adapts router state to its existing `currentView`/`onNavigate` props — CSS shows exactly one of the two navs per viewport width.

**Tech Stack:** React 19, `react-router-dom` 7.18.3 (new dependency), TanStack Query (existing), Vitest + Testing Library (existing), Playwright e2e (existing).

**Spec:** [docs/superpowers/specs/2026-09-11-navigation-redesign-design.md](../specs/2026-09-11-navigation-redesign-design.md)

**Depends on:** [docs/superpowers/plans/2026-09-11-nav-badge-count-endpoints.md](2026-09-11-nav-badge-count-endpoints.md) (separate worktree/branch `feat/nav-badge-count-endpoints`, off `main`). The 4 new backend count endpoints don't need to be merged before this plan's frontend tasks are implemented and unit-tested (unit tests mock `fetch`, matching this codebase's existing convention — see `RiskWorkspace.test.tsx` etc.), but they must be merged before this branch's own e2e suite (Task 7) can pass against a real backend, since e2e hits the real API.

## Global Constraints

- No icons anywhere in this plan's own new components — sidebar items are plain text (icons are a later, separate sub-project per the spec's explicit scope boundary).
- `WorkspaceNavigation.tsx` and `WorkspaceNavigation.test.tsx` are not modified in this plan — they're reused verbatim as the mobile fallback.
- Every new route path is exactly: `/today` `/attention` `/work` `/notes` `/schedule` `/planner` `/meeting-prep` `/risks` `/knowledge` `/recommendations` `/search-audit` `/automation` `/engineering` `/personal` `/team` (per the spec — `collaboration` → `/team`, everything else matches the existing `WorkspaceView` value).
- Badge counts only for: Attention, Work, Risks, Knowledge, Automation, Recommendations. No badge slot renders for the other 9 workspaces.
- Sidebar is fixed-width, not collapsible/resizable (explicit spec constraint).
- Mobile behavior is desktop-first CSS breakpoint swap only — no new mobile-specific component logic beyond the thin `MobileWorkspaceNav` wrapper.

---

### Task 1: Add react-router-dom and the shared workspaces config module

**Files:**
- Modify: `frontend/package.json` (add dependency)
- Create: `frontend/src/navigation/workspaces.ts`
- Test: `frontend/src/navigation/workspaces.test.ts`

**Interfaces:**
- Produces: `WORKSPACES: ReadonlyArray<WorkspaceEntry>`, `WORKSPACE_GROUP_LABELS: Record<WorkspaceGroupKey, string>`, `viewForPath(pathname: string): WorkspaceView | null`, `pathForView(view: WorkspaceView): string` — all consumed by Tasks 3, 4, 6.

- [ ] **Step 1: Add the dependency**

Run: `cd frontend && pnpm add react-router-dom@7.18.3`

- [ ] **Step 2: Write the failing test**

Create `frontend/src/navigation/workspaces.test.ts`:

```ts
import { describe, expect, it } from 'vitest'

import { pathForView, viewForPath, WORKSPACES } from './workspaces'

describe('workspaces', () => {
  it('has exactly 15 entries, each with a unique view and a unique path', () => {
    expect(WORKSPACES).toHaveLength(15)
    expect(new Set(WORKSPACES.map((w) => w.view)).size).toBe(15)
    expect(new Set(WORKSPACES.map((w) => w.path)).size).toBe(15)
  })

  it('maps collaboration to /team, not /collaboration', () => {
    const team = WORKSPACES.find((w) => w.view === 'collaboration')
    expect(team?.path).toBe('/team')
    expect(team?.label).toBe('Team')
  })

  it('resolves a path back to its view, and back again', () => {
    expect(viewForPath('/risks')).toBe('risks')
    expect(pathForView('risks')).toBe('/risks')
    expect(viewForPath('/team')).toBe('collaboration')
    expect(pathForView('collaboration')).toBe('/team')
  })

  it('returns null for an unknown path', () => {
    expect(viewForPath('/does-not-exist')).toBeNull()
  })

  it('groups Today/Attention/Recommendations under no header, and groups the rest into 4 named sections', () => {
    const ungrouped = WORKSPACES.filter((w) => w.group === null).map((w) => w.view)
    expect(ungrouped).toEqual(['today', 'attention', 'recommendations'])

    const work = WORKSPACES.filter((w) => w.group === 'work').map((w) => w.view)
    expect(work).toEqual(['work', 'notes', 'schedule', 'planner', 'meeting-prep'])

    const riskKnowledge = WORKSPACES.filter((w) => w.group === 'risk-knowledge').map((w) => w.view)
    expect(riskKnowledge).toEqual(['risks', 'knowledge', 'search-audit'])

    const systems = WORKSPACES.filter((w) => w.group === 'systems').map((w) => w.view)
    expect(systems).toEqual(['automation', 'engineering'])

    const account = WORKSPACES.filter((w) => w.group === 'account').map((w) => w.view)
    expect(account).toEqual(['personal', 'collaboration'])
  })
})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd frontend && pnpm test -- --run workspaces.test.ts`
Expected: FAIL — `Cannot find module './workspaces'`.

- [ ] **Step 4: Write minimal implementation**

Create `frontend/src/navigation/workspaces.ts`:

```ts
import type { WorkspaceView } from '../api/types'

export type WorkspaceGroupKey = 'work' | 'risk-knowledge' | 'systems' | 'account'

export type WorkspaceEntry = {
  view: WorkspaceView
  label: string
  path: string
  group: WorkspaceGroupKey | null
}

export const WORKSPACE_GROUP_LABELS: Record<WorkspaceGroupKey, string> = {
  work: 'Work',
  'risk-knowledge': 'Risk & knowledge',
  systems: 'Systems',
  account: 'Account',
}

// Order here is the sidebar's own render order -- unlike the old
// WorkspaceNavigation.tsx array, position no longer feeds any fixed
// e2e ArrowRight-count assertion (that whole mechanism is deleted along
// with the roving-tabindex nav it drove), so a workspace can be added
// anywhere in its natural group without the old ordering constraint.
export const WORKSPACES: ReadonlyArray<WorkspaceEntry> = [
  { view: 'today', label: 'Today', path: '/today', group: null },
  { view: 'attention', label: 'Attention', path: '/attention', group: null },
  { view: 'recommendations', label: 'Recommendations', path: '/recommendations', group: null },
  { view: 'work', label: 'Work', path: '/work', group: 'work' },
  { view: 'notes', label: 'Notes', path: '/notes', group: 'work' },
  { view: 'schedule', label: 'Schedule', path: '/schedule', group: 'work' },
  { view: 'planner', label: 'Planner', path: '/planner', group: 'work' },
  { view: 'meeting-prep', label: 'Meeting prep', path: '/meeting-prep', group: 'work' },
  { view: 'risks', label: 'Risks', path: '/risks', group: 'risk-knowledge' },
  { view: 'knowledge', label: 'Knowledge', path: '/knowledge', group: 'risk-knowledge' },
  { view: 'search-audit', label: 'Search & audit', path: '/search-audit', group: 'risk-knowledge' },
  { view: 'automation', label: 'Automation', path: '/automation', group: 'systems' },
  { view: 'engineering', label: 'Engineering', path: '/engineering', group: 'systems' },
  { view: 'personal', label: 'Personal', path: '/personal', group: 'account' },
  // "Team", not "Collaboration" -- matches WorkspaceNavigation.tsx's
  // existing visible label. Path is /team for the same reason (a URL a
  // user would actually type/bookmark should match what they read).
  { view: 'collaboration', label: 'Team', path: '/team', group: 'account' },
]

export function viewForPath(pathname: string): WorkspaceView | null {
  return WORKSPACES.find((entry) => entry.path === pathname)?.view ?? null
}

export function pathForView(view: WorkspaceView): string {
  return WORKSPACES.find((entry) => entry.view === view)?.path ?? '/today'
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && pnpm test -- --run workspaces.test.ts`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/navigation/workspaces.ts frontend/src/navigation/workspaces.test.ts
git commit -m "feat(navigation): add react-router-dom and the shared workspaces config module"
```

---

### Task 2: Badge-count hook

**Files:**
- Create: `frontend/src/navigation/useWorkspaceBadgeCounts.ts`
- Test: `frontend/src/navigation/useWorkspaceBadgeCounts.test.tsx`

**Interfaces:**
- Consumes: `apiRequest<T>(path: string): Promise<T>` from `../api/client` (existing).
- Produces: `useWorkspaceBadgeCounts(): Partial<Record<WorkspaceView, number>>`, consumed by Task 3's `SidebarNavigation.tsx`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/navigation/useWorkspaceBadgeCounts.test.tsx`:

```tsx
// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceBadgeCounts } from './useWorkspaceBadgeCounts'

afterEach(() => vi.unstubAllGlobals())

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

describe('useWorkspaceBadgeCounts', () => {
  it('reads count-shaped responses directly and items.length off list-shaped ones', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/attention/count')) return response({ count: 4 })
      if (url.includes('/api/v1/tasks/count')) return response({ count: 7 })
      if (url.includes('/api/v1/risks/review-queue')) return response({ items: [{}, {}] })
      if (url.includes('/api/v1/knowledge/resolution/candidates/count')) return response({ count: 0 })
      if (url.includes('/api/v1/automations/approvals')) return response({ approvals: [{}, {}, {}] })
      if (url.includes('/api/v1/recommendations/count')) return response({ count: 1 })
      return response({ error: { code: 'NOT_FOUND', message: 'no fixture route' } })
    }))

    const { result } = renderHook(() => useWorkspaceBadgeCounts(), { wrapper })

    await waitFor(() => {
      expect(result.current).toEqual({
        attention: 4,
        work: 7,
        risks: 2,
        automation: 3,
        recommendations: 1,
        // knowledge omitted: a badge of 0 renders no slot at all (spec:
        // "never a badge showing 0 -- no slot"), so a 0 count is dropped
        // from the returned map rather than included as 0.
      })
    })
  })

  it('omits a workspace entirely while its count is still loading or failed', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('', { status: 500 }))))
    const { result } = renderHook(() => useWorkspaceBadgeCounts(), { wrapper })
    expect(result.current).toEqual({})
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && pnpm test -- --run useWorkspaceBadgeCounts.test.tsx`
Expected: FAIL — `Cannot find module './useWorkspaceBadgeCounts'`.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/navigation/useWorkspaceBadgeCounts.ts`:

```ts
import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../api/client'
import type { WorkspaceView } from '../api/types'

type CountResponse = { count: number }
type ItemsResponse = { items: unknown[] }
type ApprovalsResponse = { approvals: unknown[] }

function useCount(key: string, path: string) {
  return useQuery({
    queryKey: ['nav-badge', key],
    queryFn: () => apiRequest<CountResponse>(path),
    select: (data) => data.count,
    retry: 1,
  })
}

function useReviewQueueCount() {
  return useQuery({
    queryKey: ['nav-badge', 'risks'],
    queryFn: () => apiRequest<ItemsResponse>('/api/v1/risks/review-queue'),
    select: (data) => data.items.length,
    retry: 1,
  })
}

function usePendingApprovalsCount() {
  return useQuery({
    queryKey: ['nav-badge', 'automation'],
    queryFn: () => apiRequest<ApprovalsResponse>('/api/v1/automations/approvals?status=pending'),
    select: (data) => data.approvals.length,
    retry: 1,
  })
}

/** Badge counts for the 6 sidebar workspaces with a natural single number
 * (spec: "Badge counts" section). A workspace with an undefined or zero
 * count is simply absent from the returned map -- the sidebar renders no
 * badge slot at all for it, never a badge reading "0". */
export function useWorkspaceBadgeCounts(): Partial<Record<WorkspaceView, number>> {
  const attention = useCount('attention', '/api/v1/attention/count')
  const work = useCount('work', '/api/v1/tasks/count')
  const risks = useReviewQueueCount()
  const knowledge = useCount('knowledge', '/api/v1/knowledge/resolution/candidates/count')
  const automation = usePendingApprovalsCount()
  const recommendations = useCount('recommendations', '/api/v1/recommendations/count')

  const counts: Partial<Record<WorkspaceView, number>> = {}
  if (attention.data) counts.attention = attention.data
  if (work.data) counts.work = work.data
  if (risks.data) counts.risks = risks.data
  if (knowledge.data) counts.knowledge = knowledge.data
  if (automation.data) counts.automation = automation.data
  if (recommendations.data) counts.recommendations = recommendations.data
  return counts
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && pnpm test -- --run useWorkspaceBadgeCounts.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/navigation/useWorkspaceBadgeCounts.ts frontend/src/navigation/useWorkspaceBadgeCounts.test.tsx
git commit -m "feat(navigation): add useWorkspaceBadgeCounts hook for the sidebar's 6 badge counts"
```

---

### Task 3: SidebarNavigation component + CSS

**Files:**
- Create: `frontend/src/navigation/SidebarNavigation.tsx`
- Test: `frontend/src/navigation/SidebarNavigation.test.tsx`
- Modify: `frontend/src/styles.css` (append new rules, described below)

**Interfaces:**
- Consumes: `WORKSPACES`, `WORKSPACE_GROUP_LABELS` from `./workspaces` (Task 1); `useWorkspaceBadgeCounts` from `./useWorkspaceBadgeCounts` (Task 2); `NavLink` from `react-router-dom`.
- Produces: `export default function SidebarNavigation(): JSX.Element`, consumed by Task 6's `App.tsx`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/navigation/SidebarNavigation.test.tsx`:

```tsx
// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SidebarNavigation from './SidebarNavigation'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('', { status: 500 }))))
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <SidebarNavigation />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SidebarNavigation', () => {
  it('renders a nav landmark with all 15 workspace links, each a real link with a real href', () => {
    renderAt('/today')
    const nav = screen.getByRole('navigation', { name: 'Workspaces' })
    const links = nav.querySelectorAll('a')
    expect(links).toHaveLength(15)
    expect(screen.getByRole('link', { name: 'Risks' }).getAttribute('href')).toBe('/risks')
    expect(screen.getByRole('link', { name: 'Team' }).getAttribute('href')).toBe('/team')
  })

  it('marks exactly the current route as aria-current="page"', () => {
    renderAt('/risks')
    expect(screen.getByRole('link', { name: 'Risks' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('link', { name: 'Today' }).getAttribute('aria-current')).toBeNull()
  })

  it('renders the 4 named group headers, in order, with the correct workspaces under each', () => {
    renderAt('/today')
    const headers = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headers).toEqual(['Work', 'Risk & knowledge', 'Systems', 'Account'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && pnpm test -- --run SidebarNavigation.test.tsx`
Expected: FAIL — `Cannot find module './SidebarNavigation'`.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/navigation/SidebarNavigation.tsx`:

```tsx
import { NavLink } from 'react-router-dom'

import { useWorkspaceBadgeCounts } from './useWorkspaceBadgeCounts'
import { WORKSPACE_GROUP_LABELS, WORKSPACES, type WorkspaceGroupKey } from './workspaces'

const GROUP_ORDER: ReadonlyArray<WorkspaceGroupKey | null> = [null, 'work', 'risk-knowledge', 'systems', 'account']

export default function SidebarNavigation() {
  const counts = useWorkspaceBadgeCounts()

  return (
    <nav className="sidebar-nav" aria-label="Workspaces">
      {GROUP_ORDER.map((group) => (
        <div className="sidebar-nav-group" key={group ?? 'top'}>
          {group ? <h2 className="sidebar-nav-group-label">{WORKSPACE_GROUP_LABELS[group]}</h2> : null}
          <ul>
            {WORKSPACES.filter((entry) => entry.group === group).map((entry) => (
              <li key={entry.view}>
                <NavLink to={entry.path} end>
                  <span>{entry.label}</span>
                  {counts[entry.view] ? <span className="sidebar-nav-badge">{counts[entry.view]}</span> : null}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  )
}
```

`react-router-dom`'s `NavLink` sets `aria-current="page"` automatically on the matching link — no manual wiring needed.

Append to `frontend/src/styles.css`, right after the existing `.workspace-nav`/`.tab-list` block (near line 392):

```css
.app-frame { display: flex; align-items: flex-start; gap: 0; }
.app-shell { flex: 1 1 auto; min-width: 0; }

.sidebar-nav {
  flex: 0 0 224px;
  background: var(--color-surface-recessed);
  border-right: 1px solid var(--color-border-hairline);
  padding: var(--space-6) var(--space-4);
  min-height: 100vh;
}
.sidebar-nav-group { margin-top: var(--space-6); }
.sidebar-nav-group:first-child { margin-top: 0; }
.sidebar-nav-group-label {
  margin: 0 0 var(--space-2);
  padding: 0 var(--space-3);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--color-text-tertiary);
}
.sidebar-nav ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; }
.sidebar-nav a {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  border-radius: var(--radius-control);
  padding: 8px 12px;
  color: var(--color-text-secondary);
  text-decoration: none;
  font-weight: 500;
  border-left: 3px solid transparent;
  transition: background var(--motion-fast) var(--ease-standard);
}
.sidebar-nav a:hover { background: var(--color-white); }
.sidebar-nav a[aria-current="page"] {
  background: color-mix(in srgb, var(--color-accent) 12%, var(--color-white));
  border-left-color: var(--color-accent);
  color: var(--color-ink);
  font-weight: 650;
}
.sidebar-nav a:focus-visible { outline: 3px solid var(--focus-ring); outline-offset: -3px; }
.sidebar-nav-badge {
  border-radius: 999px;
  background: var(--color-surface-recessed);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 700;
  padding: 1px 7px;
}
.sidebar-nav a[aria-current="page"] .sidebar-nav-badge { background: var(--color-white); }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && pnpm test -- --run SidebarNavigation.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the design-token check**

Run: `cd frontend && pnpm check:tokens`
Expected: PASS. `color-mix(in srgb, var(--color-accent) 12%, var(--color-white))` uses `var(--color-accent)`/`var(--color-white)` tokens, not a raw color — the check only flags raw hex/rgb literals outside `:root`, which this isn't.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/navigation/SidebarNavigation.tsx frontend/src/navigation/SidebarNavigation.test.tsx frontend/src/styles.css
git commit -m "feat(navigation): add SidebarNavigation component and its CSS"
```

---

### Task 4: Mobile stopgap wrapper

**Files:**
- Create: `frontend/src/navigation/MobileWorkspaceNav.tsx`
- Test: `frontend/src/navigation/MobileWorkspaceNav.test.tsx`
- Modify: `frontend/src/styles.css` (append breakpoint rules)

**Interfaces:**
- Consumes: `WorkspaceNavigation` (default export, unchanged) from `./WorkspaceNavigation`; `viewForPath`/`pathForView` from `./workspaces` (Task 1); `useLocation`/`useNavigate` from `react-router-dom`.
- Produces: `export default function MobileWorkspaceNav(): JSX.Element`, consumed by Task 6's `App.tsx`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/navigation/MobileWorkspaceNav.test.tsx`:

```tsx
// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup } from '@testing-library/react'

import MobileWorkspaceNav from './MobileWorkspaceNav'

afterEach(cleanup)

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<MobileWorkspaceNav />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('MobileWorkspaceNav', () => {
  it('selects the tab matching the current route', () => {
    renderAt('/risks')
    expect(screen.getByRole('tab', { name: 'Risks' }).getAttribute('aria-selected')).toBe('true')
  })

  it('defaults to Today for an unknown or root path', () => {
    renderAt('/')
    expect(screen.getByRole('tab', { name: 'Today' }).getAttribute('aria-selected')).toBe('true')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && pnpm test -- --run MobileWorkspaceNav.test.tsx`
Expected: FAIL — `Cannot find module './MobileWorkspaceNav'`.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/navigation/MobileWorkspaceNav.tsx`:

```tsx
import { useLocation, useNavigate } from 'react-router-dom'

import type { WorkspaceView } from '../api/types'
import { pathForView, viewForPath } from './workspaces'
import WorkspaceNavigation from './WorkspaceNavigation'

/** The mobile stopgap: WorkspaceNavigation.tsx (today's pill nav, unchanged)
 * stays exactly as it is, just adapted to real routing via this thin
 * wrapper, and shown only below the sidebar's breakpoint (styles.css). A
 * real mobile nav redesign is a separate, later fast-follow -- see the
 * spec's "Mobile stopgap" section for why this isn't more than that. */
export default function MobileWorkspaceNav() {
  const location = useLocation()
  const navigate = useNavigate()
  const currentView = viewForPath(location.pathname) ?? 'today'

  function handleNavigate(view: WorkspaceView) {
    navigate(pathForView(view))
  }

  return (
    <div className="mobile-workspace-nav">
      <WorkspaceNavigation currentView={currentView} onNavigate={handleNavigate} />
    </div>
  )
}
```

Append to `frontend/src/styles.css`'s existing `@media (max-width: 800px)` block (near line 457):

```css
@media (max-width: 800px) {
  .sidebar-nav { display: none; }
  .mobile-workspace-nav { display: block; }
  .app-frame { display: block; }
}
```

And, outside any media query (so it's the default, desktop-first state — near the `.sidebar-nav` rules added in Task 3):

```css
.mobile-workspace-nav { display: none; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && pnpm test -- --run MobileWorkspaceNav.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/navigation/MobileWorkspaceNav.tsx frontend/src/navigation/MobileWorkspaceNav.test.tsx frontend/src/styles.css
git commit -m "feat(navigation): add MobileWorkspaceNav stopgap wrapper for the sub-800px breakpoint"
```

---

### Task 5: Extract TodayPage from App.tsx

**Files:**
- Create: `frontend/src/dashboard/TodayPage.tsx`
- Test: `frontend/src/dashboard/TodayPage.test.tsx`

**Interfaces:**
- Consumes: `apiRequest` from `../api/client`; `Section`, `type DashboardItem` from `./Sections` (existing); `MorningBrief` (existing default export from `./MorningBrief`).
- Produces: `export default function TodayPage(): JSX.Element`, consumed by Task 6's `App.tsx`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/dashboard/TodayPage.test.tsx`:

```tsx
// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TodayPage from './TodayPage'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><TodayPage /></QueryClientProvider>)
}

describe('TodayPage', () => {
  it('shows the loading state, then the dashboard heading once data resolves', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      date: '2026-09-11', timezone: 'Asia/Kolkata', generated_at: '2026-09-11T00:00:00Z', stale: false,
      sections: { top_priorities: [], today_schedule: [], overdue_commitments: [], risks: [], waiting_on: [], recently_changed: [] },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))))

    renderPage()
    expect(screen.getByRole('status')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Today' })).toBeTruthy())
  })

  it('surfaces a dashboard fetch failure as an alert', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      error: { code: 'INTERNAL', message: 'boom' },
    }), { status: 500, headers: { 'Content-Type': 'application/json' } }))))

    renderPage()
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && pnpm test -- --run TodayPage.test.tsx`
Expected: FAIL — `Cannot find module './TodayPage'`.

- [ ] **Step 3: Write minimal implementation**

Create `frontend/src/dashboard/TodayPage.tsx` — this is `App.tsx`'s existing Today-dashboard branch (the final `else` arm of its ternary today), moved verbatim into its own component with its own `dashboard` query:

```tsx
import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../api/client'
import MorningBrief from './MorningBrief'
import { Section, type DashboardItem } from './Sections'

type DashboardResponse = {
  date: string
  timezone: string
  generated_at: string
  stale: boolean
  sections: Record<string, DashboardItem[]>
}

function fetchDashboard(): Promise<DashboardResponse> {
  return apiRequest('/api/v1/dashboard/today')
}

export default function TodayPage() {
  const dashboard = useQuery({
    queryKey: ['dashboard', 'today'],
    queryFn: fetchDashboard,
    refetchInterval: 60_000,
    retry: 1,
  })

  const sections = dashboard.data?.sections

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">EXECUTIVE COMMAND CENTER</p>
          <h1>Today</h1>
          <p className="subtitle">
            {dashboard.data?.date ?? 'Your schedule, priorities, commitments and risks'}
            {dashboard.data?.timezone ? ` · ${dashboard.data.timezone}` : ''}
          </p>
        </div>
        <button type="button" onClick={() => dashboard.refetch()} disabled={dashboard.isFetching} aria-busy={dashboard.isFetching}>
          {dashboard.isFetching ? 'Refreshing…' : 'Refresh dashboard'}
        </button>
      </header>

      {dashboard.isLoading ? <div className="status-panel" role="status">Loading today’s command center…</div> : null}
      {dashboard.isError ? (
        <div className="status-panel error-panel" role="alert">
          <strong>{dashboard.error.message}</strong>
          <span>Check your session and backend connection, then retry.</span>
        </div>
      ) : null}
      {dashboard.data?.stale ? <div className="status-panel degraded-panel" role="status">Dashboard data may be stale.</div> : null}

      {sections ? (
        <Section title="Top priorities" items={sections.top_priorities} emptyMessage="No ranked priorities need attention." variant="panel" />
      ) : null}

      <MorningBrief />

      {sections ? (
        <div className="dashboard-grid">
          <Section title="Schedule" items={sections.today_schedule} emptyMessage="No meetings scheduled for today." />
          <Section title="Overdue commitments" items={sections.overdue_commitments} emptyMessage="No overdue commitments." />
          <Section title="Open risks" items={sections.risks} emptyMessage="No active risks." />
          <Section title="Waiting on" items={sections.waiting_on} emptyMessage="Nothing is currently blocked on others." />
          <Section title="Recent changes" items={sections.recently_changed} emptyMessage="No recent changes." />
        </div>
      ) : null}
    </>
  )
}
```

`aria-busy={dashboard.isFetching}` on the refresh button is added here (it didn't have it in `App.tsx` before) — matches this session's already-established `aria-busy` loading contract (DESIGN.md's Interaction states table) for a button whose `disabled` reason is purely "a fetch is in flight."

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && pnpm test -- --run TodayPage.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/dashboard/TodayPage.tsx frontend/src/dashboard/TodayPage.test.tsx
git commit -m "refactor(dashboard): extract TodayPage from App.tsx's inline dashboard branch"
```

---

### Task 6: Restructure App.tsx into BrowserRouter + Routes

**Files:**
- Modify: `frontend/src/App.tsx` (full rewrite)

**Interfaces:**
- Consumes: `SidebarNavigation` (Task 3), `MobileWorkspaceNav` (Task 4), `TodayPage` (Task 5), every existing feature component `App.tsx` already imports.

- [ ] **Step 1: Rewrite App.tsx**

Replace the entire contents of `frontend/src/App.tsx` with:

```tsx
import { useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'

import TodayPage from './dashboard/TodayPage'
import RecommendationPanel from './features/governance/RecommendationPanel'
import SearchAuditPanel from './features/search-audit/SearchAuditPanel'
import CommitmentWorkspace from './features/commitments/CommitmentWorkspace'
import NoteWorkspace from './features/notes/NoteWorkspace'
import { createNoteDraftRecoveryStore } from './features/notes/draftRecovery'
import TaskWorkspace from './features/tasks/TaskWorkspace'
import ScheduleWorkspace from './features/schedule/ScheduleWorkspace'
import RiskWorkspace from './features/risks/RiskWorkspace'
import EntityExplorer from './features/knowledge/EntityExplorer'
import ResolutionInbox from './features/knowledge/ResolutionInbox'
import MergeReview from './features/knowledge/MergeReview'
import AttentionQueue from './features/attention/AttentionQueue'
import WaitingView from './features/attention/WaitingView'
import RiskReviewQueue from './features/attention/RiskReviewQueue'
import Planner from './features/attention/Planner'
import MeetingPrep from './features/attention/MeetingPrep'
import AutomationWorkspace from './features/automation/AutomationWorkspace'
import EngineeringWorkspace from './features/engineering/EngineeringWorkspace'
import PersonalWorkspace from './features/personal/PersonalWorkspace'
import CollaborationWorkspace from './features/collaboration/CollaborationWorkspace'
import WorkspaceSwitcher from './features/collaboration/WorkspaceSwitcher'
import MobileWorkspaceNav from './navigation/MobileWorkspaceNav'
import SidebarNavigation from './navigation/SidebarNavigation'

export default function App() {
  const [noteDraftRecovery] = useState(() => createNoteDraftRecoveryStore({ namespace: crypto.randomUUID() }))

  return (
    <BrowserRouter>
      {/* Mounted globally, above the sidebar -- which company workspace an
          account is viewing applies to every route, not just one; see
          WorkspaceSwitcher.tsx's own docstring. */}
      <WorkspaceSwitcher />
      <div className="app-frame">
        <SidebarNavigation />
        <MobileWorkspaceNav />
        <main id="workspace-panel" className="app-shell">
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
        </main>
      </div>
    </BrowserRouter>
  )
}
```

Every workspace's rendered content (which components, in what grouping/wrapper) is unchanged from the file's prior ternary — this is a structural rewrite of how the active workspace is chosen (routing vs. `useState`), not a change to what any individual workspace renders.

- [ ] **Step 2: Run the full unit test suite**

Run: `cd frontend && pnpm test -- --run`
Expected: PASS, no regressions (this task doesn't add new App-level tests — `App.tsx` itself has never had a dedicated test file in this codebase; its composition is exercised by e2e, which Task 7 updates).

- [ ] **Step 3: Typecheck and build**

Run: `cd frontend && pnpm typecheck && pnpm build`
Expected: both clean.

- [ ] **Step 4: Manually verify in a real browser**

Run: `cd frontend && pnpm dev`, open the app, confirm: the sidebar renders grouped with 5 sections, clicking a link navigates and updates the URL, the browser back button works, `/risks` loaded directly (paste the URL) renders the Risks workspace, and resizing below 800px swaps to the pill nav.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat(navigation): restructure App.tsx onto react-router-dom routes"
```

---

### Task 7: Migrate e2e scenarios off the old top-level tablist

**Files:**
- Modify: 21 files under `frontend/e2e/scenarios/` (mechanical `page.goto` change, see table)
- Modify: `frontend/e2e/scenarios/conflict-audit-keyboard.mjs`, `frontend/e2e/scenarios/knowledge-keyboard.mjs`, `frontend/e2e/scenarios/automation-approvals-keyboard.mjs` (real rewrite of their top-nav section, see below)

**The mechanical transformation** (applies to the 21 files in the table): every one of these scenarios starts with `await page.goto(baseURL)` followed shortly by `await page.getByRole('tab', { name: '<Label>' }).click()` to reach its workspace. Replace both lines with a single `await page.goto(\`${baseURL}<path>\`)`, using the path from the table (same mapping as `frontend/src/navigation/workspaces.ts`, Task 1). Everything in each file *after* that initial navigation is unrelated to the top-level nav and stays unchanged.

Two fully worked examples:

`frontend/e2e/scenarios/tasks.mjs` — before:
```js
  await page.goto(baseURL)
  ...
  await page.getByRole('tab', { name: 'Work' }).click()
```
after:
```js
  await page.goto(`${baseURL}/work`)
  ...
```
(delete the `.click()` line entirely, keep whatever came between it and the `goto` if anything did — check each file individually, since the gap between `goto` and the tab click is not identical across files).

`frontend/e2e/scenarios/risks-empty-state.mjs` — before:
```js
  await page.goto(baseURL)
  ...
  await page.getByRole('tab', { name: 'Risks' }).click()
```
after:
```js
  await page.goto(`${baseURL}/risks`)
  ...
```

**Full file → path table** for the remaining 19 mechanical files:

| File | Path |
|---|---|
| `attention-explanation.mjs` | `/attention` |
| `attention-meeting-prep.mjs` | `/meeting-prep` |
| `attention-planning.mjs` | `/planner` |
| `attention-queue.mjs` | `/attention` |
| `automation-lifecycle.mjs` | `/automation` |
| `commitments.mjs` | `/work` |
| `engineering-connector-states.mjs` | `/engineering` |
| `engineering-lifecycle.mjs` | `/engineering` |
| `gmail-panel-states.mjs` | `/personal` |
| `knowledge-entities.mjs` | `/knowledge` |
| `knowledge-resolution.mjs` | `/knowledge` |
| `members-panel-text-rendering.mjs` | `/team` |
| `multi-identity-collaboration-lifecycle.mjs` | `/team` |
| `notes.mjs` | `/notes` |
| `personal-domain-lifecycle.mjs` | `/personal` |
| `recommendation-decisions.mjs` | `/recommendations` |
| `recommendation-execution.mjs` | `/recommendations` |
| `recommendation-terminals.mjs` | `/recommendations` |
| `schedule.mjs` | `/schedule` |
| `search-calendar.mjs` | `/search-audit` |

- [ ] **Step 1: Apply the mechanical change to all 21 files** (the 2 worked examples above plus the 19 in the table).

- [ ] **Step 2: Rewrite the 3 keyboard-navigation scenarios' top-nav sections**

These 3 files specifically tested the old roving-tabindex widget's keyboard accessibility. That widget no longer exists on desktop (real `<NavLink>`s have native Tab/Enter support for free), so each file's top-nav section is rewritten to navigate directly and verify the new nav's accessibility properties instead — everything *after* the top-nav section (in-page tablist interactions, forms, etc.) is unchanged.

In `frontend/e2e/scenarios/conflict-audit-keyboard.mjs`, replace:
```js
  await page.goto(baseURL)
  assert.equal(await page.title(), 'Executive Command Center')

  // Landmarks: one main region and a named navigation region for the
  // workspace tablist.
  await page.getByRole('main').waitFor()
  await page.getByRole('navigation', { name: 'Workspace' }).waitFor()

  // The persistent workspace tablist lives outside every other scenario's
  // `include:` scan (it's a sibling of #workspace-panel/#search-panel, never
  // inside them), so it otherwise has no automated a11y regression coverage.
  await assertNoSeriousAccessibilityViolations(page, { include: 'nav[aria-label="Workspace"]' })

  // `.focus()` seeds initial focus into the tablist the way a user who has
  // just Tabbed in from the browser chrome would land on it; every
  // subsequent step below drives the UI with keyboard presses only.
  const todayTab = page.getByRole('tab', { name: 'Today' })
  await todayTab.focus()
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Today')

  // Visible focus: the focused tab must carry a real focus outline, not just
  // programmatic focus.
  const outline = await todayTab.evaluate((el) => getComputedStyle(el).outlineStyle)
  assert.notEqual(outline, 'none')

  // Roving tabindex: today(0) -> attention(1) -> work(2) -> notes(3) ->
  // schedule(4) -> planner(5) -> meeting-prep(6) -> risks(7).
  for (let step = 0; step < 7; step += 1) await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Risks')
  assert.equal(await page.getByRole('tab', { name: 'Risks' }).getAttribute('aria-selected'), 'true')
```
with:
```js
  await page.goto(`${baseURL}/risks`)
  assert.equal(await page.title(), 'Executive Command Center')

  // Landmarks: one main region and a named navigation region for the sidebar.
  await page.getByRole('main').waitFor()
  const risksLink = page.getByRole('link', { name: 'Risks' })
  await risksLink.waitFor()

  // The persistent sidebar lives outside every other scenario's `include:`
  // scan, so it otherwise has no automated a11y regression coverage.
  await assertNoSeriousAccessibilityViolations(page, { include: 'nav[aria-label="Workspaces"]' })

  // Visible focus: a keyboard user must be able to reach and see focus on
  // the active sidebar link. Seeding focus here (rather than a bare
  // assertion) also gives the next real Tab press in this file a known
  // starting point, same discipline as automation-approvals-keyboard.mjs's
  // own tabTo() helper.
  await risksLink.focus()
  const outline = await risksLink.evaluate((el) => getComputedStyle(el).outlineStyle)
  assert.notEqual(outline, 'none')
  assert.equal(await risksLink.getAttribute('aria-current'), 'page')
```

In `frontend/e2e/scenarios/knowledge-keyboard.mjs`, replace:
```js
  await page.goto(baseURL)
  await page.getByRole('main').waitFor()

  const todayTab = page.getByRole('tab', { name: 'Today' })
  await todayTab.focus()
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Today')

  // Roving tabindex: today(0) -> attention(1) -> work(2) -> notes(3) ->
  // schedule(4) -> planner(5) -> meeting-prep(6) -> risks(7) -> knowledge(8).
  for (let step = 0; step < 8; step += 1) await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Knowledge')
  assert.equal(await page.getByRole('tab', { name: 'Knowledge' }).getAttribute('aria-selected'), 'true')
```
with:
```js
  await page.goto(`${baseURL}/knowledge`)
  await page.getByRole('main').waitFor()

  const knowledgeLink = page.getByRole('link', { name: 'Knowledge' })
  await knowledgeLink.waitFor()
  assert.equal(await knowledgeLink.getAttribute('aria-current'), 'page')
```
(the code immediately following this in the file focuses `entityButton` directly and doesn't depend on the sidebar link retaining focus, so no `.focus()` call is needed here — unlike the conflict-audit-keyboard.mjs case above. Also update the now-stale comment a few lines below, "Tab from the now-focused outer tab into the panel...", to "Focus moves directly into the panel's own controls below, keyboard-only from here on" since nothing above it establishes tab-sequence focus anymore.)

In `frontend/e2e/scenarios/automation-approvals-keyboard.mjs`, replace:
```js
  await page.goto(baseURL)

  // Reach the Automation workspace tab via the top-level roving-tabindex
  // tablist, keyboard only.
  const todayTab = page.getByRole('tab', { name: 'Today' })
  await todayTab.focus()
  for (let step = 0; step < 11; step += 1) await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Automation')
  // ArrowRight/moveWorkspaceFocus already switches the view as focus moves
  // (WorkspaceNavigation.tsx's own `onMove` callback) -- no separate Enter
  // needed, matching `conflict-audit-keyboard.mjs`'s identical precedent.
  assert.equal(await page.getByRole('tab', { name: 'Automation' }).getAttribute('aria-selected'), 'true')
```
with:
```js
  await page.goto(`${baseURL}/automation`)

  // Top-level navigation is now real route links (SidebarNavigation.tsx),
  // not a roving-tabindex widget -- reaching this workspace is a direct
  // page.goto, matching every other scenario's own migration off the old
  // tablist. Seeding focus on the active link gives the next real Tab
  // press below (into the nested Workflows/Approvals tablist) a known
  // starting point.
  const automationLink = page.getByRole('link', { name: 'Automation' })
  await automationLink.waitFor()
  assert.equal(await automationLink.getAttribute('aria-current'), 'page')
  await automationLink.focus()
```
(the following `await page.getByRole('heading', { name: 'Workflows & approvals', level: 1 }).waitFor()` and the subsequent `page.keyboard.press('Tab')` / `ArrowRight` into the nested tablist stay unchanged — that's the in-page tablist, unaffected by this redesign.)

- [ ] **Step 3: Run the full e2e suite**

Run: `cd frontend && pnpm test:e2e`
Expected: PASS, all scenarios green. This step requires the 4 backend count endpoints from the companion plan (`docs/superpowers/plans/2026-09-11-nav-badge-count-endpoints.md`) to be present — if that branch hasn't merged yet, either merge `main` from that branch into this one first, or (for a faster local check) stub the 4 new endpoints' responses in `frontend/e2e/fixtures.mjs` if that file supports arbitrary route fixtures — check that file's own API before assuming which approach applies.

- [ ] **Step 4: Commit**

```bash
git add frontend/e2e/scenarios/
git commit -m "test(e2e): migrate top-level workspace navigation off the old tablist onto real routes"
```

---

### Task 8: DESIGN.md rewrite and final verification

**Files:**
- Modify: `DESIGN.md` (Navigation section)

- [ ] **Step 1: Rewrite the Navigation section**

In `DESIGN.md`, replace the entire existing "## Navigation" section (from its heading through the "why tabs, not links" note added in the prior design-system-review-fixes pass) with:

```markdown
## Navigation

**Top-level workspace nav** (`frontend/src/navigation/SidebarNavigation.tsx`): a persistent, fixed-width (224px) left sidebar, grouped into 5 sections — an unlabeled top group (Today, Attention, Recommendations), then WORK, RISK & KNOWLEDGE, SYSTEMS, and ACCOUNT. `frontend/src/navigation/workspaces.ts` is the single source of truth for every workspace's view/label/path/group; add a new workspace there, in whichever group it belongs to. Real routes now back this nav (`react-router-dom`), so it's genuine route navigation, not ARIA tabs — `<nav aria-label="Workspaces">` containing real `<NavLink>`s, `aria-current="page"` on the active one (set automatically by `NavLink`), native browser Tab/Enter/Cmd+click/back-forward semantics for free. Selected state: a light accent-tinted background, a small solid accent-colored left bar, primary-ink text, medium weight — not a solid accent fill (contrast with the in-workspace tabs below).

**Badge counts**: 6 of the 15 workspaces (Attention, Work, Risks, Knowledge, Automation, Recommendations) show a small count next to their label, from `useWorkspaceBadgeCounts` (`frontend/src/navigation/useWorkspaceBadgeCounts.ts`). Two reuse an existing unbounded list endpoint's own `items`/`approvals` length (Risks, Automation); four call a small dedicated `GET .../count` backend endpoint (Attention, Work, Knowledge, Recommendations). A workspace with an undefined or zero count renders no badge slot at all — never a badge reading "0". The other 9 workspaces never get a badge; there's no single natural number for them (a dashboard, a document list, a query-driven view, a settings panel).

**Mobile stopgap**: below the sidebar's breakpoint (`max-width: 800px`), `frontend/src/navigation/MobileWorkspaceNav.tsx` renders `WorkspaceNavigation.tsx` — the app's previous top-level nav, kept completely unchanged (same markup, same ARIA-tabs roving-tabindex keyboard behavior, same test file) — wrapped to translate router state into that component's existing `currentView`/`onNavigate` props. This is a deliberate stopgap, not a design: a real mobile nav (a drawer/sheet) is a separate, later sub-project. Both `SidebarNavigation` and `MobileWorkspaceNav` are always mounted; CSS shows exactly one per viewport width.

**In-workspace tabs** (`.tab-list`, `role="tablist"`): unchanged from before this redesign — sub-navigation within a single already-loaded workspace, used identically in `PersonalWorkspace.tsx`, `CollaborationWorkspace.tsx`, `AutomationWorkspace.tsx`, and `EngineeringWorkspace.tsx`. The contract: a `role="tablist"` with an `aria-label` naming the group, each tab is `role="tab"` with `aria-selected` driving both the visual state (Interaction states above) and the accessible state together, and exactly one `role="tabpanel"` below it with `aria-labelledby` pointing at the active tab's id. This is still exactly what ARIA tabs is for — switching which panel is visible within one already-loaded route, no navigation — unlike the old top-level nav, which really was route navigation wearing tab markup because no router existed yet.

There is no breadcrumb or pagination component (see Not yet part of the system below) — neither navigation system needs one today.
```

- [ ] **Step 2: Add a Known follow-ups entry**

In `DESIGN.md`'s "## Known follow-ups (deferred, not forgotten)" section, add:

```markdown
- **Real mobile navigation** — the sidebar redesign's own stopgap (`MobileWorkspaceNav.tsx`) keeps the pre-redesign pill nav working below 800px, unchanged. A real mobile nav (drawer/sheet) is deferred, agreed at design time, not discovered as debt afterward.
```

- [ ] **Step 3: Add a Provenance entry**

At the end of `DESIGN.md`'s "## Provenance" section, add:

```markdown
The navigation redesign (sub-project 2 of the "Calm Executive Workspace" direction, pulled forward ahead of sub-project 1's Foundation tokens at explicit request) landed [DATE], replacing the wrapped 15-pill top-level nav with a grouped, routed sidebar. `react-router-dom` is a new dependency; every workspace now has a real URL. The nav-semantics question the prior review raised (and this doc previously declined, correctly, for lack of a router) is settled for real now that one exists — see Navigation above. Badge counts, a mobile stopgap reusing the old pill nav unchanged, and the resulting e2e migration off the old roving-tabindex tablist are documented in the same section. See `docs/superpowers/specs/2026-09-11-navigation-redesign-design.md` and `docs/superpowers/plans/2026-09-11-navigation-redesign-sidebar.md` for the full design and implementation record.
```

Replace `[DATE]` with the actual date this task is executed.

- [ ] **Step 4: Full verification**

Run, in order:
```bash
cd frontend
pnpm typecheck
pnpm test -- --run
pnpm check:tokens
pnpm build
pnpm exec playwright install --with-deps chromium
pnpm test:e2e
```
Expected: all green.

- [ ] **Step 5: Commit and push**

```bash
git add DESIGN.md
git commit -m "docs: rewrite DESIGN.md Navigation section for the sidebar redesign"
git push -u origin navigation-redesign-sidebar
```

Open this as its own PR — merges independently of `feat/nav-badge-count-endpoints` and the already-open `design-system-review-fixes` PR, though it should be reviewed with the awareness that its e2e suite (Task 7) needs that backend branch merged first to actually pass in CI.
