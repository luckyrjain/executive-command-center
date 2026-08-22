import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { apiRequest } from './api/client'
import type { WorkspaceView } from './api/types'
import { formatTime } from './lib/datetime'
import WorkspaceNavigation from './navigation/WorkspaceNavigation'
import MorningBrief from './MorningBrief'
import RecommendationPanel from './RecommendationPanel'
import SearchAuditPanel from './SearchAuditPanel'
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

type DashboardItem = {
  id?: string
  entity_id?: string
  entity_ref?: string
  entity_type?: string
  title?: string
  summary?: string
  message?: string
  why?: string
  explanation?: string
  status?: string
  score?: number
  starts_at?: string
  due_at?: string
  due_date?: string
  occurred_at?: string
  empty?: boolean
}

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

function labelFor(item: DashboardItem): string {
  return item.title ?? item.summary ?? item.why ?? item.explanation ?? item.message ?? item.entity_ref ?? 'Untitled item'
}

function visibleItems(items?: DashboardItem[]): DashboardItem[] {
  return (items ?? []).filter((item) => !item.empty)
}

function Section({ title, items, emptyMessage }: { title: string; items?: DashboardItem[]; emptyMessage: string }) {
  const visible = visibleItems(items)
  const headingId = `section-${title.replaceAll(' ', '-').toLowerCase()}`
  return (
    <section className="dashboard-card" aria-labelledby={headingId}>
      <div className="section-heading">
        <h2 id={headingId}>{title}</h2>
        <span aria-label={`${visible.length} items`}>{visible.length}</span>
      </div>
      {visible.length ? (
        <ol className="item-list">
          {visible.map((item, index) => (
            <li key={item.id ?? item.entity_id ?? item.entity_ref ?? `${title}-${index}`}>
              <div>
                <strong>{labelFor(item)}</strong>
                {item.status ? <small>{item.status.replaceAll('_', ' ')}</small> : null}
              </div>
              <div className="item-meta">
                {formatTime(item.starts_at ?? item.occurred_at) ? <time>{formatTime(item.starts_at ?? item.occurred_at)}</time> : null}
                {typeof item.score === 'number' ? <span>{Math.round(item.score)}</span> : null}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="empty-state">{items?.find((item) => item.empty)?.message ?? emptyMessage}</p>
      )}
    </section>
  )
}

export default function App() {
  const [currentView, setCurrentView] = useState<WorkspaceView>('today')
  const [noteDraftRecovery] = useState(() => createNoteDraftRecoveryStore({ namespace: crypto.randomUUID() }))
  const dashboard = useQuery({
    queryKey: ['dashboard', 'today'],
    queryFn: fetchDashboard,
    refetchInterval: 60_000,
    retry: 1,
  })

  const sections = dashboard.data?.sections

  return (
    <main id="workspace-main" className="app-shell">
      {/* Mounted globally, above the tab list -- which company workspace an
          account is viewing applies to every tab, not just the
          collaboration one; see WorkspaceSwitcher.tsx's own docstring. */}
      <WorkspaceSwitcher />
      <WorkspaceNavigation currentView={currentView} onNavigate={setCurrentView} />
      <div id="workspace-panel" role="tabpanel" aria-labelledby={`workspace-tab-${currentView}`}>
        {currentView === 'work' ? (
          <div className="work-grid">
            <TaskWorkspace />
            <CommitmentWorkspace />
          </div>
        ) : currentView === 'notes' ? <NoteWorkspace recoveryStore={noteDraftRecovery} />
        : currentView === 'schedule' ? <ScheduleWorkspace />
        : currentView === 'attention' ? (
          <div className="work-grid">
            <AttentionQueue />
            <WaitingView />
          </div>
        )
        : currentView === 'planner' ? <Planner />
        : currentView === 'meeting-prep' ? <MeetingPrep />
        : currentView === 'risks' ? (
          <div className="work-grid">
            <RiskWorkspace />
            <RiskReviewQueue />
          </div>
        )
        : currentView === 'knowledge' ? (
          <div className="work-grid">
            <EntityExplorer />
            <ResolutionInbox />
            <MergeReview />
          </div>
        )
        : currentView === 'recommendations' ? <RecommendationPanel />
        : currentView === 'search-audit' ? <SearchAuditPanel />
        : currentView === 'automation' ? <AutomationWorkspace />
        : currentView === 'engineering' ? <EngineeringWorkspace />
        : currentView === 'personal' ? <PersonalWorkspace />
        : currentView === 'collaboration' ? <CollaborationWorkspace />
        : <><header className="topbar">
          <div>
            <p className="eyebrow">EXECUTIVE COMMAND CENTER</p>
            <h1>Today</h1>
            <p className="subtitle">
              {dashboard.data?.date ?? 'Your schedule, priorities, commitments and risks'}
              {dashboard.data?.timezone ? ` · ${dashboard.data.timezone}` : ''}
            </p>
          </div>
          <button type="button" onClick={() => dashboard.refetch()} disabled={dashboard.isFetching}>
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
          <div className="dashboard-grid">
            <Section title="Schedule" items={sections.today_schedule} emptyMessage="No meetings scheduled for today." />
            <Section title="Top priorities" items={sections.top_priorities} emptyMessage="No ranked priorities need attention." />
            <Section title="Overdue commitments" items={sections.overdue_commitments} emptyMessage="No overdue commitments." />
            <Section title="Open risks" items={sections.risks} emptyMessage="No active risks." />
            <Section title="Waiting on" items={sections.waiting_on} emptyMessage="Nothing is currently blocked on others." />
            <Section title="Recent changes" items={sections.recently_changed} emptyMessage="No recent changes." />
          </div>
        ) : null}

        <MorningBrief />
        </>}
      </div>
    </main>
  )
}
