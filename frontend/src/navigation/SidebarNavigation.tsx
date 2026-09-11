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
