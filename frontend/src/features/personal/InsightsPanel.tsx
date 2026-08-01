import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { personalErrorMessage, formatTimestamp } from './errors'
import { DOMAIN_KEYS, DOMAIN_LABELS } from './types'
import type { DomainKey, Insight, InsightGenerateResponse, InsightListResponse } from './types'

function FeedbackForm({ insight }: { insight: Insight }) {
  const [comment, setComment] = useState('')
  const [sent, setSent] = useState(false)

  const feedbackMutation = useMutation({
    mutationFn: (useful: boolean) =>
      apiRequest(`/api/v1/personal/insights/${insight.id}/feedback`, {
        method: 'POST',
        body: { useful, comment: comment.trim() || null },
      }),
    onSuccess: () => setSent(true),
  })

  if (sent) return <p className="inline-status">Thanks for the feedback.</p>

  return (
    <div>
      <label>Comment (optional)
        <input aria-label={`Feedback comment for ${insight.title}`} value={comment} onChange={(event) => setComment(event.target.value)} maxLength={1000} />
      </label>
      <div className="work-actions">
        <button type="button" disabled={feedbackMutation.isPending} onClick={() => feedbackMutation.mutate(true)}>Useful</button>
        <button type="button" disabled={feedbackMutation.isPending} onClick={() => feedbackMutation.mutate(false)}>Not useful</button>
      </div>
      {feedbackMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(feedbackMutation.error)}</div> : null}
    </div>
  )
}

function InsightCard({ insight, onDismissed }: { insight: Insight; onDismissed: () => void }) {
  const dismissMutation = useMutation({
    mutationFn: () => apiRequest<Insight>(`/api/v1/personal/insights/${insight.id}/dismiss`, { method: 'POST' }),
    onSuccess: onDismissed,
  })

  return (
    <li>
      <div>
        <strong>{insight.title}</strong>
        <small>{DOMAIN_LABELS[insight.domain_key as DomainKey] ?? insight.domain_key} · {insight.kind} · {insight.confidence} confidence</small>
      </div>
      <p>{insight.limitations}</p>
      {insight.missing_data ? <p className="inline-status degraded-panel">Incomplete data: {insight.missing_data}</p> : null}
      <p>
        {formatTimestamp(insight.source_period_start)} – {formatTimestamp(insight.source_period_end)}
      </p>
      {insight.professional_referral_note ? (
        // The "sensitive insight" boundary `UX-STATES.md` requires shown
        // near the insight itself -- non-empty specifically when a source
        // is `high_stakes` (`INSIGHT-CONTRACT.md`'s safety rubric).
        <div role="status" className="inline-status degraded-panel">
          {insight.professional_referral_note}
        </div>
      ) : null}

      {/* `GET /personal/insights` (habits.py:list_insights_endpoint) filters
          `dismissed_at IS NULL` server-side, so a dismissed insight is never
          returned to re-render here -- dismissing one simply removes it from
          the list on the next refetch, matching every other action in this
          feature that leaves a row's own fate to a query invalidation rather
          than tracking a terminal state client-side that the server itself
          never sends back. */}
      <div className="work-actions">
        <button type="button" disabled={dismissMutation.isPending} onClick={() => dismissMutation.mutate()}>Dismiss</button>
      </div>
      {dismissMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(dismissMutation.error)}</div> : null}

      <FeedbackForm insight={insight} />
    </li>
  )
}

export default function InsightsPanel() {
  const queryClient = useQueryClient()
  const [sourceDomains, setSourceDomains] = useState<DomainKey[]>([])

  const insights = useQuery({
    queryKey: ['personal', 'insights'],
    queryFn: () => apiRequest<InsightListResponse>('/api/v1/personal/insights'),
    retry: 1,
  })

  const generateMutation = useMutation({
    mutationFn: () =>
      apiRequest<InsightGenerateResponse>('/api/v1/personal/insights/generate', {
        method: 'POST',
        body: { source_domain_keys: sourceDomains },
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['personal', 'insights'] }),
  })

  function toggleSource(domainKey: DomainKey) {
    setSourceDomains((current) =>
      current.includes(domainKey) ? current.filter((key) => key !== domainKey) : [...current, domainKey],
    )
  }

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['personal', 'insights'] })
  }

  const items = insights.data?.insights ?? []

  return (
    <section className="work-panel" aria-labelledby="personal-insights-title">
      <h2 id="personal-insights-title">Insights</h2>
      <p>Deterministic and AI-generated insights across every domain you have enabled. Health and finance insights always name a professional to talk to when relevant.</p>

      <fieldset>
        <legend>Generate an insight from</legend>
        {DOMAIN_KEYS.map((domainKey) => (
          <label key={domainKey}>
            <input
              type="checkbox"
              checked={sourceDomains.includes(domainKey)}
              onChange={() => toggleSource(domainKey)}
            />
            {' '}{DOMAIN_LABELS[domainKey]}
          </label>
        ))}
        <div className="work-actions">
          <button type="button" disabled={generateMutation.isPending || sourceDomains.length === 0} onClick={() => generateMutation.mutate()}>
            Generate insight
          </button>
        </div>
      </fieldset>
      {generateMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(generateMutation.error)}</div> : null}
      {generateMutation.data && !generateMutation.data.available ? (
        <p className="inline-status degraded-panel">
          No insight available right now ({generateMutation.data.error_code ?? 'unknown reason'}).
        </p>
      ) : null}
      {generateMutation.data?.available ? <p className="inline-status">A new insight was generated below.</p> : null}

      {insights.isLoading ? <p role="status">Loading insights…</p> : null}
      {insights.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(insights.error)}</div> : null}
      {insights.data && items.length === 0 ? <p className="empty-state">No insights yet -- capture a few records first.</p> : null}

      <ul className="work-list">
        {items.map((insight) => <InsightCard key={insight.id} insight={insight} onDismissed={refresh} />)}
      </ul>
    </section>
  )
}
