import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from './api/client'
import type { DashboardItem } from './dashboard/Sections'

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
        <dl className="brief-stats">
          <div>
            <dt>Schedule</dt>
            <dd>{brief.data.sections.today_schedule?.length ?? 0}</dd>
          </div>
          <div>
            <dt>Priorities</dt>
            <dd>{brief.data.sections.top_priorities?.length ?? 0}</dd>
          </div>
          <div>
            <dt>Overdue</dt>
            <dd>{brief.data.sections.overdue_commitments?.length ?? 0}</dd>
          </div>
          <div>
            <dt>Risks</dt>
            <dd>{brief.data.sections.risks?.length ?? 0}</dd>
          </div>
        </dl>
      ) : null}
    </section>
  )
}
