import { useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'

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
import { viewForPath } from './navigation/workspaces'
import type { NoteDraftRecoveryStore } from './features/notes/draftRecovery'

type AppShellProps = {
  noteDraftRecovery: NoteDraftRecoveryStore
}

/** Everything below WorkspaceSwitcher -- split out from App() because
 * computing the ARIA-labelling tab id below needs the current route, and
 * useLocation() only works inside <BrowserRouter>, which App() itself
 * renders (same pattern MobileWorkspaceNav.tsx already uses). */
function AppShell({ noteDraftRecovery }: AppShellProps) {
  const location = useLocation()
  // Mirrors MobileWorkspaceNav.tsx's own fallback: an unmatched route (the
  // "*" catch-all below) has no workspace view, so default to 'today' rather
  // than leaving the tabpanel unlabelled.
  const currentWorkspaceView = viewForPath(location.pathname) ?? 'today'

  return (
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
  )
}

export default function App() {
  const [noteDraftRecovery] = useState(() => createNoteDraftRecoveryStore({ namespace: crypto.randomUUID() }))

  return (
    <BrowserRouter>
      {/* Mounted globally, above the sidebar -- which company workspace an
          account is viewing applies to every route, not just one; see
          WorkspaceSwitcher.tsx's own docstring. Framed via its own
          `.workspace-switcher` CSS rule (styles.css) rather than by nesting
          it inside `.app-shell`/`.app-frame` -- it's a global org-switcher,
          not part of either nav or the sidebar+content row. */}
      <WorkspaceSwitcher />
      <AppShell noteDraftRecovery={noteDraftRecovery} />
    </BrowserRouter>
  )
}
