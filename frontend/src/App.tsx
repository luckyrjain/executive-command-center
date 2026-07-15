import { useQuery } from '@tanstack/react-query'

type DashboardItem = {
  id?: string
  entity_ref?: string
  title?: string
  summary?: string
  explanation?: string
  status?: string
  score?: number
  starts_at?: string
  due_at?: string
  due_date?: string
}

type TodayDashboard = {
  date?: string
  workspace_timezone?: string
  generated_at?: string
  degraded?: boolean
  meetings?: DashboardItem[]
  priorities?: DashboardItem[]
  overdue_commitments?: DashboardItem[]
  risks?: DashboardItem[]
  waiting_on?: DashboardItem[]
  recent_changes?: DashboardItem[]
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

async function fetchTodayDashboard(): Promise<TodayDashboard> {
  const response = await fetch(`${API_BASE}/api/v1/dashboard/today`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new Error(response.status === 401 ? 'Authentication required' : 'Dashboard unavailable')
  }
  return response.json()
}

function labelFor(item: DashboardItem): string {
  return item.title ?? item.summary ?? item.explanation ?? item.entity_ref ?? 'Untitled item'
}

function formatTime(value?: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(date)
}

function DashboardSection({
  title,
  items,
  emptyMessage,
}: {
  title: string
  items?: DashboardItem[]
  emptyMessage: string
}) {
  return (
    <section className="dashboard-card" aria-labelledby={`section-${title.replaceAll(' ', '-').toLowerCase()}`}>
      <div className="section-heading">
        <h2 id={`section-${title.replaceAll(' ', '-').toLowerCase()}`}>{title}</h2>
        <span>{items?.length ?? 0}</span>
      </div>
      {items?.length ? (
        <ol className="item-list">
          {items.map((item, index) => (
            <li key={item.id ?? item.entity_ref ?? `${title}-${index}`}>
              <div>
                <strong>{labelFor(item)}</strong>
                {item.status ? <small>{item.status.replaceAll('_', ' ')}</small> : null}
              </div>
              <div className="item-meta">
                {formatTime(item.starts_at) ? <time>{formatTime(item.starts_at)}</time> : null}
                {typeof item.score === 'number' ? <span>{Math.round(item.score)}</span> : null}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="empty-state">{emptyMessage}</p>
      )}
    </section>
  )
}

export default function App() {
  const dashboard = useQuery({
    queryKey: ['dashboard', 'today'],
    queryFn: fetchTodayDashboard,
    refetchInterval: 60_000,
    retry: 1,
  })

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">EXECUTIVE COMMAND CENTER</p>
          <h1>Today</h1>
          <p className="subtitle">
            {dashboard.data?.date ?? 'Your schedule, priorities, commitments and risks'}
            {dashboard.data?.workspace_timezone ? ` · ${dashboard.data.workspace_timezone}` : ''}
          </p>
        </div>
        <button type="button" onClick={() => dashboard.refetch()} disabled={dashboard.isFetching}>
          {dashboard.isFetching ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      {dashboard.isLoading ? <div className="status-panel" role="status">Loading today’s command center…</div> : null}
      {dashboard.isError ? (
        <div className="status-panel error-panel" role="alert">
          <strong>{dashboard.error.message}</strong>
          <span>Check your session and backend connection, then retry.</span>
        </div>
      ) : null}
      {dashboard.data?.degraded ? (
        <div className="status-panel degraded-panel" role="status">
          Deterministic dashboard is available, but one or more enrichments are temporarily unavailable.
        </div>
      ) : null}

      {dashboard.data ? (
        <div className="dashboard-grid">
          <DashboardSection title="Schedule" items={dashboard.data.meetings} emptyMessage="No meetings scheduled for today." />
          <DashboardSection title="Top priorities" items={dashboard.data.priorities} emptyMessage="No ranked priorities need attention." />
          <DashboardSection title="Overdue commitments" items={dashboard.data.overdue_commitments} emptyMessage="No overdue commitments." />
          <DashboardSection title="Open risks" items={dashboard.data.risks} emptyMessage="No active risks." />
          <DashboardSection title="Waiting on" items={dashboard.data.waiting_on} emptyMessage="Nothing is currently blocked on others." />
          <DashboardSection title="Recent changes" items={dashboard.data.recent_changes} emptyMessage="No recent changes." />
        </div>
      ) : null}
    </main>
  )
}
