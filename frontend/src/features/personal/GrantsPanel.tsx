import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { personalErrorMessage, formatTimestamp } from './errors'
import { DOMAIN_KEYS, DOMAIN_LABELS } from './types'
import type { DomainKey, DomainListResponse, Grant, GrantListResponse } from './types'

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
          <button type="button" className="btn-destructive" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>Revoke</button>
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

  // Same reasoning as `RecordsPanel.tsx`'s identical ref -- avoids a stale
  // in-flight mutation's `onSuccess` (rebound to the latest render's
  // closures by TanStack Query v5) clearing a different source domain's
  // in-progress draft after the user has switched away from the one the
  // request was actually made for.
  const sourceDomainKeyRef = useRef(sourceDomainKey)
  sourceDomainKeyRef.current = sourceDomainKey

  const domains = useQuery({
    queryKey: ['personal', 'domains'],
    queryFn: () => apiRequest<DomainListResponse>('/api/v1/personal/domains'),
    retry: 1,
  })
  const sourceDomainEnabled = (domains.data?.domains ?? []).some((d) => d.domain_key === sourceDomainKey && d.enabled)

  const grants = useQuery({
    queryKey: ['personal', 'grants'],
    queryFn: () => apiRequest<GrantListResponse>('/api/v1/personal/grants'),
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: (variables: { sourceDomainKey: DomainKey; categories: string[]; expiresAt: string | null }) =>
      apiRequest<Grant>('/api/v1/personal/grants', {
        method: 'POST',
        body: {
          source_domain_key: variables.sourceDomainKey,
          purpose: 'insight_generation',
          granted_categories: variables.categories,
          expires_at: variables.expiresAt,
        },
      }),
    onSuccess: (_data, variables) => {
      if (sourceDomainKeyRef.current === variables.sourceDomainKey) {
        setCategories('')
        setExpiresAt('')
      }
      void queryClient.invalidateQueries({ queryKey: ['personal', 'grants'] })
    },
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['personal', 'grants'] })
  }

  const now = new Date()
  const items = grants.data?.grants ?? []
  const canSubmit = sourceDomainEnabled && categories.split(',').map((c) => c.trim()).filter(Boolean).length > 0

  return (
    <section className="work-panel" aria-labelledby="personal-grants-title">
      <h2 id="personal-grants-title">Cross-domain grants</h2>
      <p>A grant lets an AI-generated insight draw specific categories of one enabled domain's data for a named purpose. Nothing is combined across domains without an active grant.</p>

      <form
        className="field-form"
        onSubmit={(event) => {
          event.preventDefault()
          createMutation.mutate({
            sourceDomainKey,
            categories: categories.split(',').map((c) => c.trim()).filter(Boolean),
            expiresAt: expiresAt ? new Date(expiresAt).toISOString() : null,
          })
        }}
      >
        <label>Source domain
          <select aria-label="Source domain" value={sourceDomainKey} onChange={(event) => setSourceDomainKey(event.target.value as DomainKey)}>
            {DOMAIN_KEYS.map((key) => <option key={key} value={key}>{DOMAIN_LABELS[key]}</option>)}
          </select>
        </label>
        <label>Purpose
          <input aria-label="Purpose" value="insight_generation" readOnly />
        </label>
        {/* No `aria-label` override here, unlike the sibling inputs above --
            the full wrapping label text ("Granted categories (comma-
            separated record types)") is this field's only formatting cue;
            an `aria-label` truncating it to just "Granted categories" would
            take that cue away from a screen-reader user specifically. */}
        <label>Granted categories (comma-separated record types)
          <input value={categories} onChange={(event) => setCategories(event.target.value)} />
        </label>
        <label>Expires (optional)
          <input aria-label="Expires at" type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} />
        </label>
        {domains.isLoading ? <p role="status">Loading domains…</p> : null}
        {domains.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(domains.error)}</div> : null}
        {domains.data && !sourceDomainEnabled ? (
          <p className="empty-state">Enable {DOMAIN_LABELS[sourceDomainKey]} in the Domains tab before granting access to its data.</p>
        ) : null}
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
