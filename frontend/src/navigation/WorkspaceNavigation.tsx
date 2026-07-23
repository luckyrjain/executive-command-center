import { useRef, type KeyboardEvent } from 'react'

import type { WorkspaceView } from '../api/types'

type WorkspaceNavigationProps = {
  currentView: WorkspaceView
  onNavigate: (view: WorkspaceView) => void
}

const WORKSPACES: ReadonlyArray<{ view: WorkspaceView; label: string }> = [
  { view: 'today', label: 'Today' },
  { view: 'attention', label: 'Attention' },
  { view: 'work', label: 'Work' },
  { view: 'notes', label: 'Notes' },
  { view: 'schedule', label: 'Schedule' },
  { view: 'planner', label: 'Planner' },
  { view: 'meeting-prep', label: 'Meeting prep' },
  { view: 'risks', label: 'Risks' },
  { view: 'knowledge', label: 'Knowledge' },
  { view: 'recommendations', label: 'Recommendations' },
  { view: 'search-audit', label: 'Search & audit' },
]

export function nextWorkspaceIndex(current: number, key: string, count: number): number {
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  if (key === 'ArrowRight') return (current + 1) % count
  if (key === 'ArrowLeft') return (current - 1 + count) % count
  return current
}

export function moveWorkspaceFocus(
  current: number,
  key: string,
  tabs: ReadonlyArray<{ focus: () => void } | null>,
  onMove: (index: number) => void,
): boolean {
  const next = nextWorkspaceIndex(current, key, tabs.length)
  if (next === current && !['Home', 'End'].includes(key)) return false
  tabs[next]?.focus()
  onMove(next)
  return true
}

export default function WorkspaceNavigation({ currentView, onNavigate }: WorkspaceNavigationProps) {
  const tabs = useRef<Array<HTMLButtonElement | null>>([])

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const moved = moveWorkspaceFocus(index, event.key, tabs.current, (nextIndex) => {
      onNavigate(WORKSPACES[nextIndex].view)
    })
    if (!moved) return
    event.preventDefault()
  }

  return (
    <nav aria-label="Workspace">
      <div role="tablist" aria-label="Executive workspaces">
        {WORKSPACES.map(({ view, label }, index) => {
          const selected = currentView === view
          return (
            <button
              key={view}
              id={`workspace-tab-${view}`}
              ref={(element) => { tabs.current[index] = element }}
              type="button"
              role="tab"
              aria-controls="workspace-panel"
              aria-selected={selected}
              tabIndex={selected ? 0 : -1}
              onClick={() => onNavigate(view)}
              onKeyDown={(event) => handleKeyDown(event, index)}
            >
              {label}
            </button>
          )
        })}
      </div>
    </nav>
  )
}
