import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from './api/client'

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

function fetchMorningBrief(): Promise<MorningBriefResponse> {
  return apiRequest('/api/v1/briefs/morning')
}

function refreshMorningBrief(): Promise<MorningBriefResponse> {
  return apiRequest('/api/v1/briefs/morning', { method: 'POST' })
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
  const headingId = `brief-section-${title.replaceAll(' ', '-').toLowerCase()}`
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

export default function MorningBrief() {
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
      {brief.data?.ai_status === 'disabled' ? (
        <div className="inline-status" role="status">AI-assisted sections are disabled; showing deterministic results only.</div>
      ) : null}
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
