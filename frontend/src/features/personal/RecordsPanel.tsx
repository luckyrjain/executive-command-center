import { useState } from 'react'
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

function RecordRow({ record }: { record: PersonalRecord }) {
  const entries = Object.entries(record.payload)
  return (
    <li>
      <div>
        <strong>{record.record_type}</strong>
        <small>effective {formatTimestamp(record.effective_at)}</small>
      </div>
      {entries.length ? (
        <dl>
          {entries.map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{String(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <div role="status" className="inline-status">
        {record.retention_acknowledged_at
          ? `Retention acknowledged ${formatTimestamp(record.retention_acknowledged_at)}`
          : 'No retention acknowledgement recorded for this record.'}
      </div>
    </li>
  )
}

export default function RecordsPanel() {
  const queryClient = useQueryClient()
  const [domainKey, setDomainKey] = useState<DomainKey>('habits')
  const [recordType, setRecordType] = useState('')
  const [fields, setFields] = useState<PayloadField[]>([{ key: '', value: '' }])
  const [retentionAcknowledged, setRetentionAcknowledged] = useState(false)

  const domains = useQuery({
    queryKey: ['personal', 'domains'],
    queryFn: () => apiRequest<DomainListResponse>('/api/v1/personal/domains'),
    retry: 1,
  })
  const enabled = (domains.data?.domains ?? []).some((d) => d.domain_key === domainKey && d.enabled)
  const classification = CLASSIFICATION_BY_DOMAIN[domainKey]

  const records = useQuery({
    queryKey: ['personal', 'records', domainKey],
    queryFn: () => apiRequest<RecordListResponse>(`/api/v1/personal/records?domain_key=${domainKey}`),
    enabled,
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      apiRequest<PersonalRecord>('/api/v1/personal/records', {
        method: 'POST',
        body: {
          domain_key: domainKey,
          record_type: recordType,
          payload: payloadFromFields(fields),
          retention_acknowledged: retentionAcknowledged,
        },
      }),
    onSuccess: () => {
      setRecordType('')
      setFields([{ key: '', value: '' }])
      setRetentionAcknowledged(false)
      void queryClient.invalidateQueries({ queryKey: ['personal', 'records', domainKey] })
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

      {!enabled ? (
        <p className="empty-state">Enable {DOMAIN_LABELS[domainKey]} in the Domains tab before capturing records here.</p>
      ) : (
        <>
          <form onSubmit={(event) => { event.preventDefault(); createMutation.mutate() }}>
            <label>Record type
              <input
                aria-label="Record type"
                value={recordType}
                onChange={(event) => setRecordType(event.target.value)}
                maxLength={50}
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

          {records.isLoading ? <p role="status">Loading records…</p> : null}
          {records.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(records.error)}</div> : null}
          {records.data && items.length === 0 ? <p className="empty-state">No records yet for {DOMAIN_LABELS[domainKey]}.</p> : null}

          <ul className="work-list" aria-label={`Records for ${DOMAIN_LABELS[domainKey]}`}>
            {items.map((record) => <RecordRow key={record.id} record={record} />)}
          </ul>
        </>
      )}
    </section>
  )
}
