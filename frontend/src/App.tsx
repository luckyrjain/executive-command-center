import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { WorkspaceView } from './api/types'
import WorkspaceNavigation from './navigation/WorkspaceNavigation'
import RecommendationPanel from './RecommendationPanel'
import SearchAuditPanel from './SearchAuditPanel'
import WorkActionCenter from './WorkActionCenter'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

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

type MorningBriefResponse = {
  id: string
  briefing_date: string
  generation_version: number
  sections: Record<string, DashboardItem[]>
  source_versions: Record<string, number>
  evidence_ids: string[]
  generated_at: string
  timezone: string
  algorithm_version: string
  ai_status: string
  stale: boolean
  stale_reason?: string | null
}

type ErrorEnvelope = {
  error?: { code?: string; message?: string }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers,
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorEnvelope
    throw new Error(payload.error?.message ?? (response.status === 401 ? 'Authentication required' : 'Request failed'))
  }
  return response.json()
}

function fetchDashboard(): Promise<DashboardResponse> {
  return api('/api/v1/dashboard/today')
}

function fetchMorningBrief(): Promise<MorningBriefResponse> {
  return api('/api/v1/briefs/morning')
}

function refreshMorningBrief(): Promise<MorningBriefResponse> {
  return api('/api/v1/briefs/morning', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
      'X-CSRF-Token': document.cookie
        .split('; ')
        .find((value) => value.startsWith('ecc_csrf='))
        ?.split('=')[1] ?? '',
    },
  })
}

function labelFor(item: DashboardItem): string {
  return item.title ?? item.summary ?? item.why ?? item.explanation ?? item.message ?? item.entity_ref ?? 'Untitled item'
}

function formatTime(value?: string): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return null
  return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(parsed)
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

function MorningBrief() {
  const queryClient = useQueryClient()
  const brief = useQuery({ queryKey: ['brief', 'morning'], queryFn: fetchMorningBrief, retry: 1 })
  const refresh = useMutation({
    mutationFn: refreshMorningBrief,
    onSuccess: (data) => queryClient.setQueryData(['brief', 'morning'], data),
  })

  return (
    <section className="brief-panel" aria-labelledby="morning-brief-title">
      <div className="brief-heading">
        <div>
          <p className="eyebrow">PERSISTED DAILY BRIEF</p>
          <h2 id="morning-brief-title">Morning Brief</h2>
          <p>{brief.data ? `Generation ${brief.data.generation_version} · ${brief.data.ai_status.replaceAll('_', ' ')}` : 'A deterministic briefing of today’s attention.'}</p>
        </div>
        <button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending || brief.isLoading}>
          {refresh.isPending ? 'Refreshing…' : 'Refresh brief'}
        </button>
      </div>

      {brief.isLoading ? <div className="inline-status" role="status">Preparing your morning brief…</div> : null}
      {brief.isError ? <div className="inline-status error-panel" role="alert">{brief.error.message}</div> : null}
      {refresh.isError ? <div className="inline-status error-panel" role="alert">{refresh.error.message}</div> : null}
      {brief.data?.stale ? (
        <div className="inline-status degraded-panel" role="status">
          This brief is stale{brief.data.stale_reason ? `: ${brief.data.stale_reason.replaceAll('_', ' ')}` : ''}. Refresh to regenerate it.
        </div>
      ) : null}

      {brief.data ? (
        <div className="brief-grid">
          <Section title="Brief schedule" items={brief.data.sections.today_schedule} emptyMessage="No meetings in the brief." />
          <Section title="Brief priorities" items={brief.data.sections.top_priorities} emptyMessage="No priorities in the brief." />
          <Section title="Brief commitments" items={brief.data.sections.overdue_commitments} emptyMessage="No overdue commitments." />
          <Section title="Brief risks" items={brief.data.sections.risks} emptyMessage="No open risks." />
        </div>
      ) : null}
    </section>
  )
}

export default function App() {
  const [currentView, setCurrentView] = useState<WorkspaceView>('today')
  const dashboard = useQuery({
    queryKey: ['dashboard', 'today'],
    queryFn: fetchDashboard,
    refetchInterval: 60_000,
    retry: 1,
  })

  const sections = dashboard.data?.sections

  return (
    <main id="workspace-main" className="app-shell">
      <WorkspaceNavigation currentView={currentView} onNavigate={setCurrentView} />
      <header className="topbar">
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
      <WorkActionCenter />
      <RecommendationPanel />
      <SearchAuditPanel />
    </main>
  )
}
