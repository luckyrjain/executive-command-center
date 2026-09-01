import { useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { personalErrorMessage, formatTimestamp } from './errors'
import { DOMAIN_KEYS, DOMAIN_LABELS } from './types'
import type { DomainDeletion, DomainExport, DomainKey } from './types'

function exportRecordCount(exportData: DomainExport): number {
  return exportData.goals.length + exportData.routines.length + exportData.check_ins.length + exportData.domain_records.length
}

export default function ExportDeletePanel() {
  const [domainKey, setDomainKey] = useState<DomainKey>('habits')
  const [deleteConfirmed, setDeleteConfirmed] = useState(false)

  // Same reasoning as `RecordsPanel.tsx`'s identical ref: `mutate(domainKey)`
  // below passes the domain as the mutation's own variable rather than a
  // closure, so the request itself always targets the domain selected at
  // click time even if TanStack Query rebinds a still-pending mutation's
  // options to a later render. `domainKeyRef` covers the remaining case --
  // an `onSuccess` side effect (unchecking the delete confirmation) that
  // should only apply if the user is still looking at the domain the
  // now-resolving request was for.
  const domainKeyRef = useRef(domainKey)
  domainKeyRef.current = domainKey

  const exportMutation = useMutation({
    mutationFn: (forDomainKey: DomainKey) =>
      apiRequest<DomainExport>(`/api/v1/personal/domains/${forDomainKey}/export`, { method: 'POST' }),
  })
  const deleteMutation = useMutation({
    mutationFn: (forDomainKey: DomainKey) =>
      apiRequest<DomainDeletion>(`/api/v1/personal/domains/${forDomainKey}/delete`, { method: 'POST' }),
    onSuccess: (_data, forDomainKey) => {
      if (domainKeyRef.current === forDomainKey) setDeleteConfirmed(false)
    },
  })

  function selectDomain(next: DomainKey) {
    setDomainKey(next)
    exportMutation.reset()
    deleteMutation.reset()
    setDeleteConfirmed(false)
  }

  return (
    <section className="work-panel" aria-labelledby="personal-export-title">
      <h2 id="personal-export-title">Export &amp; delete</h2>
      <p>Every domain can be exported as machine-readable JSON, or permanently deleted, independently of every other domain.</p>

      <label>Domain
        <select aria-label="Export domain" value={domainKey} onChange={(event) => selectDomain(event.target.value as DomainKey)}>
          {DOMAIN_KEYS.map((key) => <option key={key} value={key}>{DOMAIN_LABELS[key]}</option>)}
        </select>
      </label>

      <div className="work-actions">
        <button type="button" disabled={exportMutation.isPending} onClick={() => exportMutation.mutate(domainKey)}>
          {exportMutation.isPending ? 'Exporting…' : `Export ${DOMAIN_LABELS[domainKey]}`}
        </button>
      </div>
      {exportMutation.isPending ? <p role="status" className="inline-status">Export running…</p> : null}
      {exportMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(exportMutation.error)}</div> : null}
      {exportMutation.data ? (
        <div role="status" className="inline-status">
          <p>Exported {exportRecordCount(exportMutation.data)} item(s) at {formatTimestamp(exportMutation.data.exported_at)}.</p>
          <details>
            <summary>Raw export data</summary>
            <pre>{JSON.stringify(exportMutation.data, null, 2)}</pre>
          </details>
        </div>
      ) : null}

      <hr />

      <label>
        <input type="checkbox" checked={deleteConfirmed} onChange={(event) => setDeleteConfirmed(event.target.checked)} />
        {' '}I understand deleting {DOMAIN_LABELS[domainKey]} permanently removes its records and cannot be undone.
      </label>
      <div className="work-actions">
        <button
          type="button"
          className="btn-destructive"
          disabled={!deleteConfirmed || deleteMutation.isPending}
          onClick={() => deleteMutation.mutate(domainKey)}
        >
          {deleteMutation.isPending ? 'Deletion pending…' : `Delete ${DOMAIN_LABELS[domainKey]}`}
        </button>
      </div>
      {deleteMutation.isPending ? <p role="status" className="inline-status">Deletion pending…</p> : null}
      {deleteMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(deleteMutation.error)}</div> : null}
      {deleteMutation.data ? (
        <p role="status" className="inline-status">
          Deletion {deleteMutation.data.status} at {formatTimestamp(deleteMutation.data.completed_at ?? deleteMutation.data.requested_at)}.
        </p>
      ) : null}
    </section>
  )
}
