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
