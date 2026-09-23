import type { WorkspaceView } from '../api/types'

export type WorkspaceGroupKey = 'work' | 'risk-knowledge' | 'systems' | 'account'

/** How a workspace's panels are drawn. `cards`: panels keep their card chrome
 * (side-by-side panels, or a dashboard of independent cards). `canvas`: panels
 * drop the chrome and the page sits on a white open canvas (content that
 * stacks vertically). See DESIGN.md's Composition section. */
export type Composition = 'canvas' | 'cards'

export type WorkspaceEntry = {
  view: WorkspaceView
  label: string
  path: string
  group: WorkspaceGroupKey | null
  composition: Composition
  // Accessible label for this workspace's sidebar badge count, e.g. "2
  // risks due for review" -- set only on the 6 workspaces
  // useWorkspaceBadgeCounts() gives a real count to (see that file). The
  // badge's visible text stays a bare number; this names what it counts
  // for assistive tech instead of a generic "N items".
  badgeCountLabel?: (count: number) => string
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

/** Route matching ignores a trailing slash (`<Route path="/notes">` also
 * matches `/notes/`), so strip one before the exact-path lookup below -- a
 * bookmarked `/notes/` still renders the workspace and must resolve to its
 * real view and composition, not fall through to the defaults. Never strips
 * the root path itself (`'/'` stays `'/'`; nothing in `WORKSPACES` has that
 * path anyway). */
function normalizePath(pathname: string): string {
  return pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
}

export function viewForPath(pathname: string): WorkspaceView | null {
  return WORKSPACES.find((entry) => entry.path === normalizePath(pathname))?.view ?? null
}

export function pathForView(view: WorkspaceView): string {
  return WORKSPACES.find((entry) => entry.view === view)?.path ?? '/today'
}

/** Unmatched paths (the not-found route) fall back to `cards`, so an
 * unknown URL keeps the app's original look. */
export function compositionForPath(pathname: string): Composition {
  return WORKSPACES.find((entry) => entry.path === normalizePath(pathname))?.composition ?? 'cards'
}
