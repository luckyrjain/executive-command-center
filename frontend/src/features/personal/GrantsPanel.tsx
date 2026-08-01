import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { personalErrorMessage, formatTimestamp } from './errors'
import { DOMAIN_KEYS, DOMAIN_LABELS } from './types'
import type { DomainKey, Grant, GrantListResponse } from './types'

type GrantState = 'active' | 'expired' | 'revoked'

function grantState(grant: Grant, now: Date): GrantState {
  if (grant.revoked_at) return 'revoked'
  if (grant.expires_at && new Date(grant.expires_at).getTime() <= now.getTime()) return 'expired'
  return 'active'
}

function grantStateLabel(state: GrantState): string {
  if (state === 'revoked') return 'Revoked'
  if (state === 'expired') return 'Expired'
  return 'Active'
}

function GrantRow({ grant, now, onChanged }: { grant: Grant; now: Date; onChanged: () => void }) {
  const state = grantState(grant, now)
  const revokeMutation = useMutation({
    mutationFn: () => apiRequest<Grant>(`/api/v1/personal/grants/${grant.id}/revoke`, { method: 'POST' }),
    onSuccess: onChanged,
  })

  return (
    <li>
      <div>
        <strong>{DOMAIN_LABELS[grant.source_domain_key as DomainKey] ?? grant.source_domain_key}</strong>
        <small>{grant.purpose} · categories: {grant.granted_categories.join(', ')}</small>
      </div>
      <div role="status" className={state === 'active' ? 'inline-status' : 'inline-status degraded-panel'}>
        {grantStateLabel(state)}
        {grant.expires_at ? ` · expires ${formatTimestamp(grant.expires_at)}` : ' · no expiry'}
        {grant.revoked_at ? ` · revoked ${formatTimestamp(grant.revoked_at)}` : ''}
      </div>
      {state !== 'revoked' ? (
        <div className="work-actions">
          <button type="button" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>Revoke</button>
        </div>
      ) : null}
      {revokeMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(revokeMutation.error)}</div> : null}
    </li>
  )
}

export default function GrantsPanel() {
  const queryClient = useQueryClient()
  const [sourceDomainKey, setSourceDomainKey] = useState<DomainKey>('habits')
  const [categories, setCategories] = useState('')
  const [expiresAt, setExpiresAt] = useState('')

  const grants = useQuery({
    queryKey: ['personal', 'grants'],
    queryFn: () => apiRequest<GrantListResponse>('/api/v1/personal/grants'),
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      apiRequest<Grant>('/api/v1/personal/grants', {
        method: 'POST',
        body: {
          source_domain_key: sourceDomainKey,
          purpose: 'insight_generation',
          granted_categories: categories.split(',').map((c) => c.trim()).filter(Boolean),
          expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        },
      }),
    onSuccess: () => {
      setCategories('')
      setExpiresAt('')
      void queryClient.invalidateQueries({ queryKey: ['personal', 'grants'] })
    },
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['personal', 'grants'] })
  }

  const now = new Date()
  const items = grants.data?.grants ?? []
  const canSubmit = categories.split(',').map((c) => c.trim()).filter(Boolean).length > 0

  return (
    <section className="work-panel" aria-labelledby="personal-grants-title">
      <h2 id="personal-grants-title">Cross-domain grants</h2>
      <p>A grant lets an AI-generated insight draw specific categories of one enabled domain's data for a named purpose. Nothing is combined across domains without an active grant.</p>

      <form onSubmit={(event) => { event.preventDefault(); createMutation.mutate() }}>
        <label>Source domain
          <select aria-label="Source domain" value={sourceDomainKey} onChange={(event) => setSourceDomainKey(event.target.value as DomainKey)}>
            {DOMAIN_KEYS.map((key) => <option key={key} value={key}>{DOMAIN_LABELS[key]}</option>)}
          </select>
        </label>
        <label>Purpose
          <input aria-label="Purpose" value="insight_generation" readOnly />
        </label>
        <label>Granted categories (comma-separated record types)
          <input aria-label="Granted categories" value={categories} onChange={(event) => setCategories(event.target.value)} />
        </label>
        <label>Expires (optional)
          <input aria-label="Expires at" type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} />
        </label>
        <div className="work-actions">
          <button type="submit" disabled={createMutation.isPending || !canSubmit}>Create grant</button>
        </div>
      </form>
      {createMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(createMutation.error)}</div> : null}

      {grants.isLoading ? <p role="status">Loading grants…</p> : null}
      {grants.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(grants.error)}</div> : null}
      {grants.data && items.length === 0 ? <p className="empty-state">No cross-domain grants yet.</p> : null}

      <ul className="work-list">
        {items.map((grant) => <GrantRow key={grant.id} grant={grant} now={now} onChanged={refresh} />)}
      </ul>
    </section>
  )
}
