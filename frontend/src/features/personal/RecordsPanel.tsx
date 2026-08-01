import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { personalErrorMessage, formatTimestamp } from './errors'
import { CLASSIFICATION_BY_DOMAIN, DOMAIN_KEYS, DOMAIN_LABELS } from './types'
import type { DomainKey, DomainListResponse, PersonalRecord, RecordListResponse } from './types'

type PayloadField = { key: string; value: string }

function payloadFromFields(fields: PayloadField[]): Record<string, string> {
  const payload: Record<string, string> = {}
  for (const field of fields) {
    const key = field.key.trim()
    if (key) payload[key] = field.value
  }
  return payload
}

function displayValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function RecordRow({ record }: { record: PersonalRecord }) {
  const entries = Object.entries(record.payload)
  const decryptMutation = useMutation({
    mutationFn: () => apiRequest<PersonalRecord>(`/api/v1/personal/records/${record.id}`),
  })
  // The list response always redacts an encrypted field to `***encrypted***`
  // (`_redact_payload`) -- only `GET /records/{id}` returns the real,
  // decrypted value (`DOMAIN-PRIVACY-CONTRACT.md`: "only a single-record
  // fetch returns the decrypted value"). Before this, nothing in this UI
  // ever called that endpoint, so a sensitive/high_stakes record's own
  // narrative field could never be viewed again after creation.
  const shownEntries = decryptMutation.data ? Object.entries(decryptMutation.data.payload) : entries

  return (
    <li>
      <div>
        <strong>{record.record_type}</strong>
        <small>effective {formatTimestamp(record.effective_at)}</small>
      </div>
      {shownEntries.length ? (
        <dl>
          {shownEntries.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{displayValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <div role="status" className="inline-status">
        {record.retention_acknowledged_at
          ? `Retention acknowledged ${formatTimestamp(record.retention_acknowledged_at)}`
          : 'No retention acknowledgement recorded for this record.'}
      </div>
      {entries.some(([, value]) => value === '***encrypted***') && !decryptMutation.data ? (
        <div className="work-actions">
          <button
            type="button"
            disabled={decryptMutation.isPending}
            onClick={() => decryptMutation.mutate()}
          >
            View decrypted
          </button>
        </div>
      ) : null}
      {decryptMutation.isError ? (
        <div role="alert" className="inline-status error-panel">{personalErrorMessage(decryptMutation.error)}</div>
      ) : null}
    </li>
  )
}

export default function RecordsPanel() {
  const queryClient = useQueryClient()
  const [domainKey, setDomainKey] = useState<DomainKey>('habits')
  const [recordType, setRecordType] = useState('')
  const [fields, setFields] = useState<PayloadField[]>([{ key: '', value: '' }])
  const [retentionAcknowledged, setRetentionAcknowledged] = useState(false)
  const [effectiveAt, setEffectiveAt] = useState('')
  const [effectiveFrom, setEffectiveFrom] = useState('')
  const [effectiveUntil, setEffectiveUntil] = useState('')

  // Read inside the mutation's `onSuccess` (via `.current`, not the render-
  // time `domainKey` closure) so a domain switch while a create request is
  // still in flight can't wipe a different domain's in-progress draft, and
  // so the query invalidated below is always the one the request actually
  // targeted -- `useMutation`'s `onSuccess` rebinds to the latest render's
  // closures for a still-pending mutation (TanStack Query v5), so closing
  // over `domainKey` directly here would invalidate/reset whichever domain
  // happened to be selected when the response arrived, not the one the
  // request was made for.
  const domainKeyRef = useRef(domainKey)
  domainKeyRef.current = domainKey

  const domains = useQuery({
    queryKey: ['personal', 'domains'],
    queryFn: () => apiRequest<DomainListResponse>('/api/v1/personal/domains'),
    retry: 1,
  })
  const enabled = (domains.data?.domains ?? []).some((d) => d.domain_key === domainKey && d.enabled)
  const classification = CLASSIFICATION_BY_DOMAIN[domainKey]

  const records = useQuery({
    queryKey: ['personal', 'records', domainKey, effectiveFrom, effectiveUntil],
    queryFn: () => {
      const params = new URLSearchParams({ domain_key: domainKey })
      if (effectiveFrom) params.set('effective_from', new Date(effectiveFrom).toISOString())
      if (effectiveUntil) params.set('effective_until', new Date(effectiveUntil).toISOString())
      return apiRequest<RecordListResponse>(`/api/v1/personal/records?${params.toString()}`)
    },
    enabled,
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: (variables: { domainKey: DomainKey; recordType: string; payload: Record<string, string>; retentionAcknowledged: boolean; effectiveAt: string }) =>
      apiRequest<PersonalRecord>('/api/v1/personal/records', {
        method: 'POST',
        body: {
          domain_key: variables.domainKey,
          record_type: variables.recordType,
          payload: variables.payload,
          retention_acknowledged: variables.retentionAcknowledged,
          effective_at: variables.effectiveAt ? new Date(variables.effectiveAt).toISOString() : undefined,
        },
      }),
    onSuccess: (_data, variables) => {
      if (domainKeyRef.current === variables.domainKey) {
        setRecordType('')
        setFields([{ key: '', value: '' }])
        setRetentionAcknowledged(false)
        setEffectiveAt('')
      }
      void queryClient.invalidateQueries({ queryKey: ['personal', 'records', variables.domainKey] })
    },
  })

  function updateField(index: number, patch: Partial<PayloadField>) {
    setFields((current) => current.map((field, fieldIndex) => (fieldIndex === index ? { ...field, ...patch } : field)))
  }

  const items = records.data?.records ?? []
  const canSubmit = recordType.trim().length > 0 && (classification !== 'high_stakes' || retentionAcknowledged)

  return (
    <section className="work-panel" aria-labelledby="personal-records-title">
      <h2 id="personal-records-title">Records</h2>
      <p>Capture and review the records you have entered for one domain at a time.</p>

      <label>Domain
        <select
          aria-label="Domain"
          value={domainKey}
          onChange={(event) => setDomainKey(event.target.value as DomainKey)}
        >
          {DOMAIN_KEYS.map((key) => <option key={key} value={key}>{DOMAIN_LABELS[key]}</option>)}
        </select>
      </label>

      {domains.isLoading ? <p role="status">Loading domains…</p> : null}
      {domains.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(domains.error)}</div> : null}

      {domains.data && !enabled ? (
        <p className="empty-state">Enable {DOMAIN_LABELS[domainKey]} in the Domains tab before capturing records here.</p>
      ) : null}
      {domains.data && enabled ? (
        <>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              createMutation.mutate({
                domainKey,
                recordType,
                payload: payloadFromFields(fields),
                retentionAcknowledged,
                effectiveAt,
              })
            }}
          >
            <label>Record type
              <input
                aria-label="Record type"
                value={recordType}
                onChange={(event) => setRecordType(event.target.value)}
                maxLength={50}
              />
            </label>

            <label>Effective date (optional -- defaults to now)
              <input
                aria-label="Effective date"
                type="datetime-local"
                value={effectiveAt}
                onChange={(event) => setEffectiveAt(event.target.value)}
              />
            </label>

            <fieldset>
              <legend>Fields</legend>
              {fields.map((field, index) => (
                <div className="work-actions" key={index}>
                  <input
                    aria-label={`Field name ${index + 1}`}
                    placeholder="field name"
                    value={field.key}
                    onChange={(event) => updateField(index, { key: event.target.value })}
                  />
                  <input
                    aria-label={`Field value ${index + 1}`}
                    placeholder="value"
                    value={field.value}
                    onChange={(event) => updateField(index, { value: event.target.value })}
                  />
                </div>
              ))}
              <button type="button" onClick={() => setFields((current) => [...current, { key: '', value: '' }])}>
                Add field
              </button>
            </fieldset>

            {classification === 'high_stakes' ? (
              <label>
                <input
                  type="checkbox"
                  checked={retentionAcknowledged}
                  onChange={(event) => setRetentionAcknowledged(event.target.checked)}
                />
                {' '}I understand how this record's data is retained.
              </label>
            ) : null}

            <div className="work-actions">
              <button type="submit" disabled={createMutation.isPending || !canSubmit}>Save record</button>
            </div>
          </form>
          {createMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(createMutation.error)}</div> : null}

          <fieldset>
            <legend>Filter by effective date</legend>
            <label>From
              <input
                aria-label="Effective from"
                type="datetime-local"
                value={effectiveFrom}
                onChange={(event) => setEffectiveFrom(event.target.value)}
              />
            </label>
            <label>Until
              <input
                aria-label="Effective until"
                type="datetime-local"
                value={effectiveUntil}
                onChange={(event) => setEffectiveUntil(event.target.value)}
              />
            </label>
          </fieldset>

          {records.isLoading ? <p role="status">Loading records…</p> : null}
          {records.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(records.error)}</div> : null}
          {records.data && items.length === 0 ? <p className="empty-state">No records yet for {DOMAIN_LABELS[domainKey]}.</p> : null}

          <ul className="work-list" aria-label={`Records for ${DOMAIN_LABELS[domainKey]}`}>
            {items.map((record) => <RecordRow key={record.id} record={record} />)}
          </ul>
        </>
      ) : null}
    </section>
  )
}
