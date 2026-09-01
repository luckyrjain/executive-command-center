import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { personalErrorMessage, formatTimestamp } from './errors'
import { CLASSIFICATION_BY_DOMAIN, DOMAIN_KEYS, DOMAIN_LABELS } from './types'
import type { Classification, Domain, DomainKey, DomainListResponse } from './types'

function classificationLabel(classification: Classification): string {
  if (classification === 'high_stakes') return 'High-stakes'
  if (classification === 'sensitive') return 'Sensitive'
  return 'Standard'
}

// Calm, factual, non-alarming per Decision 4/`UX-STATES.md`'s own "no
// shame, addiction loops or false urgency" -- describes only what this
// activation actually enforces server-side (encryption for narrative
// fields, per-record retention acknowledgement for `high_stakes`), never a
// nudge/reminder mechanism that has no backend counterpart yet.
function classificationNote(classification: Classification): string | null {
  if (classification === 'high_stakes') {
    return 'Sensitive narrative fields in this domain are stored encrypted. Every record you add here asks you to confirm you have reviewed how long it is kept.'
  }
  if (classification === 'sensitive') {
    return 'Sensitive narrative fields in this domain are stored encrypted.'
  }
  return null
}

function DomainRow({ domainKey, domain, onChanged }: {
  domainKey: DomainKey
  domain: Domain | null
  onChanged: () => void
}) {
  const classification = CLASSIFICATION_BY_DOMAIN[domainKey]
  const enabled = domain?.enabled ?? false

  const enableMutation = useMutation({
    mutationFn: () => apiRequest<Domain>(`/api/v1/personal/domains/${domainKey}/enable`, { method: 'POST' }),
    onSuccess: onChanged,
  })
  const disableMutation = useMutation({
    mutationFn: () => apiRequest<Domain>(`/api/v1/personal/domains/${domainKey}/disable`, { method: 'POST' }),
    onSuccess: onChanged,
  })

  const note = classificationNote(classification)

  return (
    <li>
      <div>
        <strong>{DOMAIN_LABELS[domainKey]}</strong>
        <small>{classificationLabel(classification)} data</small>
      </div>

      <div role="status" className="inline-status">
        {enabled ? `Enabled ${formatTimestamp(domain?.enabled_at ?? null)}` : 'Not enabled -- no data is captured for this domain yet.'}
      </div>
      {note ? <p>{note}</p> : null}

      <div className="work-actions">
        {enabled ? (
          <button type="button" className="btn-destructive" disabled={disableMutation.isPending} onClick={() => disableMutation.mutate()}>
            Disable
          </button>
        ) : (
          <button type="button" disabled={enableMutation.isPending} onClick={() => enableMutation.mutate()}>
            Enable
          </button>
        )}
      </div>
      {enableMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(enableMutation.error)}</div> : null}
      {disableMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(disableMutation.error)}</div> : null}
    </li>
  )
}

export default function DomainsPanel() {
  const queryClient = useQueryClient()
  const domains = useQuery({
    queryKey: ['personal', 'domains'],
    queryFn: () => apiRequest<DomainListResponse>('/api/v1/personal/domains'),
    retry: 1,
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['personal', 'domains'] })
  }

  const byKey = new Map<string, Domain>()
  for (const domain of domains.data?.domains ?? []) byKey.set(domain.domain_key, domain)

  return (
    <section className="work-panel" aria-labelledby="personal-domains-title">
      <h2 id="personal-domains-title">Domains</h2>
      <p>Every domain starts disabled. Enabling one only affects that domain -- no domain's data is visible to another, and none of it appears in search or meeting context by default.</p>

      {domains.isLoading ? <p role="status">Loading domains…</p> : null}
      {domains.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(domains.error)}</div> : null}

      <ul className="work-list">
        {DOMAIN_KEYS.map((domainKey) => (
          <DomainRow key={domainKey} domainKey={domainKey} domain={byKey.get(domainKey) ?? null} onChanged={refresh} />
        ))}
      </ul>
    </section>
  )
}
