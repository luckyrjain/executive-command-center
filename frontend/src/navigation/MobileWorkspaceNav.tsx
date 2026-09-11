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
