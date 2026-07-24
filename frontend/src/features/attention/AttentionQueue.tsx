import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import AttentionExplanation from './AttentionExplanation'

declare global {
  interface Window {
    // Runtime override for `frontend/e2e/scenarios/attention-explanation.mjs`
    // (see the constant below) -- never set outside a test harness.
    __ECC_AI_EXPLANATIONS_ENABLED__?: boolean
  }
}

/** `phase-004/UX-STATES.md` Task 6, Step 3: the existing Attention Queue
 * must be "pixel-for-pixel/behaviorally unaffected" when AI explanations
 * are globally off -- achieved here by not mounting `AttentionExplanation`
 * at all in that case, rather than mounting it and asking it to render
 * nothing (which would still add DOM nodes/effects a pixel-diff could
 * catch). Defaults to enabled: this is a deliberately optional, discardable,
 * clearly labelled affordance (never replacing the deterministic factor
 * list above it), not a feature that needs an explicit opt-in per real
 * deployment -- `VITE_AI_EXPLANATIONS_ENABLED=0` is the real, build-time
 * deployment killswitch.
 *
 * `window.__ECC_AI_EXPLANATIONS_ENABLED__` is a *runtime* override checked
 * first, purely so `attention-explanation.mjs` can exercise both the
 * enabled and disabled product states against the single production build
 * `frontend/e2e/run.mjs` already produces once for every scenario
 * (`page.addInitScript` sets it before the bundle evaluates) -- a Vite
 * `import.meta.env.VITE_*` value is inlined at build time and cannot be
 * flipped per-scenario without a second full build, which no other
 * scenario in this suite requires either. */
const AI_EXPLANATIONS_ENABLED =
  typeof window !== 'undefined' && typeof window.__ECC_AI_EXPLANATIONS_ENABLED__ === 'boolean'
    ? window.__ECC_AI_EXPLANATIONS_ENABLED__
    : import.meta.env.VITE_AI_EXPLANATIONS_ENABLED !== '0'

export type AttentionFactor = { code: string; label: string; points: number; source_field?: string }

export type AttentionItem = {
  id: string
  entity_type: 'task' | 'commitment' | 'risk' | 'waiting_link' | 'risk_review' | 'meeting'
  entity_id: string
  source_entity_version: number
  score: number
  confidence: number
  factors: AttentionFactor[]
  explanation: string
  generated_at: string
  expires_at: string
  pinned: boolean
  dismissed_at: string | null
  dismissed_entity_version: number | null
  deferred_until: string | null
  policy_version: number
  override_reason: string | null
}

type AttentionList = { items: AttentionItem[] }

type Group = 'needs_action' | 'waiting_on_others' | 'risks' | 'upcoming_meetings' | 'safely_deferred'

const GROUP_TITLES: Record<Group, string> = {
  needs_action: 'Needs action',
  waiting_on_others: 'Waiting on others',
  risks: 'Risks',
  upcoming_meetings: 'Upcoming meetings',
  safely_deferred: 'Safely deferred',
}
const GROUP_ORDER: Group[] = ['needs_action', 'waiting_on_others', 'risks', 'upcoming_meetings', 'safely_deferred']

/** UX-STATES.md: "Group by needs action, waiting on others, risks, upcoming
 * meetings and safely deferred." Grouping is derived client-side from the
 * shipped AttentionItem shape -- there is no separate "group" field on the
 * backend, entity_type plus override state is enough to derive it. */
export function groupOf(item: AttentionItem): Group {
  if (item.deferred_until) return 'safely_deferred'
  if (item.entity_type === 'waiting_link') return 'waiting_on_others'
  if (item.entity_type === 'risk' || item.entity_type === 'risk_review') return 'risks'
  if (item.entity_type === 'meeting') return 'upcoming_meetings'
  return 'needs_action'
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'OFFLINE') return 'You are offline. Reconnect to update attention items.'
  return error.message
}

function formatFreshness(generatedAt: string): string {
  const instant = new Date(generatedAt)
  if (Number.isNaN(instant.getTime())) return 'unknown freshness'
  return `updated ${instant.toLocaleString()}`
}

export default function AttentionQueue() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['attention'],
    queryFn: () => apiRequest<AttentionList>('/api/v1/attention?limit=100'),
    retry: 1,
  })
  const [reason, setReason] = useState('')

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['attention'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] })
    void queryClient.invalidateQueries({ queryKey: ['brief', 'morning'] })
  }

  const actionMutation = useMutation({
    mutationFn: ({ item, action }: { item: AttentionItem; action: 'dismiss' | 'defer' | 'restore' }) =>
      apiRequest<AttentionItem>(`/api/v1/attention/${item.id}/${action}`, {
        method: 'POST',
        body: action === 'defer'
          ? { deferred_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(), reason: reason || null }
          : action === 'dismiss' ? { reason: reason || null } : {},
      }),
    onSuccess: () => { setReason(''); refresh() },
  })
  const pending = actionMutation.isPending

  const groups = useMemo(() => {
    const items = query.data?.items ?? []
    const byGroup: Record<Group, AttentionItem[]> = {
      needs_action: [], waiting_on_others: [], risks: [], upcoming_meetings: [], safely_deferred: [],
    }
    for (const item of items) {
      if (item.dismissed_at) continue
      byGroup[groupOf(item)].push(item)
    }
    return byGroup
  }, [query.data])

  return (
    <section className="work-panel" aria-labelledby="attention-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">EXECUTIVE ATTENTION</p>
          <h1 id="attention-title">Attention queue</h1>
          <p>What needs your attention, why it matters, and how confident and fresh that judgment is.</p>
        </div>
      </div>

      {query.isLoading ? <p role="status">Loading attention queue…</p> : null}
      {query.isError ? (
        <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div>
      ) : null}
      {actionMutation.isError ? (
        <div role="alert" className="inline-status error-panel">{errorMessage(actionMutation.error)}</div>
      ) : null}

      <label>
        Reason for dismiss or defer (optional)
        <input aria-label="Reason for dismiss or defer" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>

      {query.data ? GROUP_ORDER.map((group) => {
        const items = groups[group]
        const headingId = `attention-group-${group}`
        return (
          <section key={group} className="dashboard-card" aria-labelledby={headingId}>
            <div className="section-heading">
              <h2 id={headingId}>{GROUP_TITLES[group]}</h2>
              <span aria-label={`${items.length} items`}>{items.length}</span>
            </div>
            {items.length ? (
              <ol className="item-list">
                {items.map((item) => (
                  <li key={item.id}>
                    <div>
                      <strong>{item.explanation}</strong>
                      <small>
                        confidence {Math.round(item.confidence * 100)}% · {formatFreshness(item.generated_at)}
                        {item.factors.length ? ` · ${item.factors.length} evidence factor${item.factors.length === 1 ? '' : 's'}` : ''}
                      </small>
                    </div>
                    <div className="item-meta">
                      <span aria-label="Score (secondary to the reason above)">{item.score}</span>
                    </div>
                    {item.factors.length ? (
                      <ul aria-label={`Evidence for ${item.explanation}`}>
                        {item.factors.map((factor) => (
                          <li key={factor.code}>{factor.label}</li>
                        ))}
                      </ul>
                    ) : null}
                    {AI_EXPLANATIONS_ENABLED ? <AttentionExplanation item={item} /> : null}
                    <div className="work-actions" role="group" aria-label={`Actions for ${item.explanation}`}>
                      <button type="button" disabled={pending} aria-label={`Defer ${item.explanation}`} onClick={() => actionMutation.mutate({ item, action: 'defer' })}>Defer</button>
                      <button type="button" disabled={pending} aria-label={`Dismiss ${item.explanation}`} onClick={() => actionMutation.mutate({ item, action: 'dismiss' })}>Dismiss</button>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="empty-state">Nothing in this group right now.</p>
            )}
          </section>
        )
      }) : null}

      {query.data && (query.data.items ?? []).some((item) => item.dismissed_at || item.deferred_until) ? (
        <section className="dashboard-card" aria-labelledby="attention-overridden">
          <div className="section-heading"><h2 id="attention-overridden">Dismissed or deferred (reversible)</h2></div>
          <ol className="item-list">
            {(query.data.items ?? []).filter((item) => item.dismissed_at || item.deferred_until).map((item) => (
              <li key={item.id}>
                <div><strong>{item.explanation}</strong>{item.override_reason ? <small>{item.override_reason}</small> : null}</div>
                <button type="button" disabled={pending} aria-label={`Restore ${item.explanation}`} onClick={() => actionMutation.mutate({ item, action: 'restore' })}>Restore</button>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
    </section>
  )
}
