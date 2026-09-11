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
