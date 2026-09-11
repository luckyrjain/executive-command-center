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
