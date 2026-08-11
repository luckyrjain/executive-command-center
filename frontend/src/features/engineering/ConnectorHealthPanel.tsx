import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type {
  ConnectorAccount,
  ConnectorAccountListResponse,
  ConnectorProvider,
  SyncRun,
  SyncRunListResponse,
  SyncRunType,
} from './types'

const PROVIDERS: ReadonlyArray<ConnectorProvider> = ['github', 'gitlab', 'jira', 'datadog', 'sandbox']

// `connector_accounts.ConnectorCreateRequest.credential` is one opaque
// string per provider, but the shape underneath differs (bare token,
// `host|token`, `site|email|api_token`, `site|api_key|app_key`) -- see
// each adapter's own `_parse_credential`. Rather than ask the operator to
// hand-assemble that string with the right delimiters, this form takes
// each part as its own labeled field and joins them per provider on
// submit (`buildCredential`), matching what the receiving adapter expects
// exactly.
const DATADOG_SITES = [
  'api.datadoghq.com',
  'api.us3.datadoghq.com',
  'api.us5.datadoghq.com',
  'api.datadoghq.eu',
  'api.ap1.datadoghq.com',
  'api.ddog-gov.com',
] as const

type CredentialFields = { host: string; site: string; email: string; token: string; appKey: string }

function emptyCredentialFields(provider: ConnectorProvider): CredentialFields {
  return {
    host: 'gitlab.com',
    site: provider === 'datadog' ? DATADOG_SITES[0] : '',
    email: '',
    token: '',
    appKey: '',
  }
}

// The secret/token field's label varies by provider (`datadog_adapter.py`'s
// own `_parse_credential` calls it an API key, `jira_adapter.py`'s an API
// token) -- sandbox has no real shape to name, so it keeps the generic
// "Credential" label this form used everywhere before this change.
const TOKEN_FIELD_LABEL: Partial<Record<ConnectorProvider, string>> = {
  github: 'Personal access token',
  gitlab: 'Personal access token',
  jira: 'API token',
  datadog: 'API key',
}

function tokenFieldLabel(provider: ConnectorProvider): string {
  return TOKEN_FIELD_LABEL[provider] ?? 'Credential'
}

function buildCredential(provider: ConnectorProvider, fields: CredentialFields): string {
  switch (provider) {
    case 'gitlab':
      return `${fields.host.trim() || 'gitlab.com'}|${fields.token}`
    case 'jira':
      return `${fields.site.trim()}|${fields.email.trim()}|${fields.token}`
    case 'datadog':
      return `${fields.site}|${fields.token}|${fields.appKey}`
    default:
      return fields.token
  }
}

function isCredentialComplete(provider: ConnectorProvider, fields: CredentialFields): boolean {
  switch (provider) {
    case 'jira':
      return fields.site.trim() !== '' && fields.email.trim() !== '' && fields.token.trim() !== ''
    case 'datadog':
      return fields.token.trim() !== '' && fields.appKey.trim() !== ''
    default:
      return fields.token.trim() !== ''
  }
}
const RESOURCE_TYPES = [
  'repository', 'work_item', 'change', 'review', 'deployment', 'incident',
  'monitor', 'service_definition', 'dashboard',
] as const
const RUN_TYPES: ReadonlyArray<Extract<SyncRunType, 'backfill' | 'incremental'>> = ['backfill', 'incremental']

// This activation has no periodic freshness monitor -- "stale connector"
// (UX-STATES.md) is derived client-side from `last_synced_at`'s own age,
// not a separate backend field. 24 hours is a disclosed, deliberately
// conservative heuristic (`repositories`/`engineering_work_items`'s own
// `freshness_state` uses the identical concept per-row, computed by the
// sync adapters themselves -- this is the account-level analogue where no
// equivalent field exists).
const STALE_AFTER_MS = 24 * 60 * 60 * 1000

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CSRF_TOKEN_REQUIRED' || error.code === 'CSRF_TOKEN_INVALID') return 'Your session\'s security token is missing or stale. Reload the page and try again.'
  if (error.code === 'CONNECTOR_PROVIDER_NOT_SUPPORTED') return 'This provider has no registered connector adapter.'
  if (error.code === 'CONNECTOR_ALREADY_CONNECTED') return 'This workspace already has a connector for this account.'
  if (error.code === 'CONNECTOR_AUTHORIZATION_FAILED') {
    // The backend's own detail dict is `{"code": ..., "error": &lt;sanitized
    // message&gt;}` (`create_connector_endpoint`'s `AdapterAuthorizationError`
    // handler) -- `main.py`'s `_error_payload` strips only `code`/`message`
    // out into `details`, so the real field name surviving onto `ApiError.
    // current` is `error`, never `reason`.
    const detail = error.current as { error?: string } | undefined
    return `The provider rejected this credential${detail?.error ? `: ${detail.error}` : '.'}`
  }
  if (error.code === 'CONNECTOR_NOT_FOUND') return 'This connector no longer exists in this workspace.'
  if (error.code === 'CONNECTOR_DISCONNECTED') return 'This connector is already disconnected.'
  if (error.code === 'CONNECTOR_SYNC_IN_PROGRESS') return 'A sync is already running for this connector -- wait for it to finish before starting another.'
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to manage connectors in this workspace.'
  return error.message
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never'
}

function isStale(connector: ConnectorAccount, now: Date): boolean {
  if (!connector.last_synced_at) return false
  return now.getTime() - new Date(connector.last_synced_at).getTime() > STALE_AFTER_MS
}

/** Maps `ConnectorAccountResponse.status` (the one field this backend
 * exposes -- there is no separate "degraded" flag) onto the UX-STATES.md
 * required states this single enum must carry: `pending` is "first sync
 * not yet run", `permission_lost` is "partial permissions",
 * `rate_limited`/`disconnected` are named directly, and `error` is
 * "provider unavailable" (paired with `last_error`). */
function statusPanelClass(status: ConnectorAccount['status']): string {
  if (status === 'error' || status === 'disconnected') return 'inline-status error-panel'
  if (status === 'permission_lost' || status === 'rate_limited') return 'inline-status degraded-panel'
  return 'inline-status'
}

function statusLabel(status: ConnectorAccount['status']): string {
  if (status === 'pending') return 'first sync not yet run'
  if (status === 'permission_lost') return 'partial permissions -- provider revoked some access'
  if (status === 'rate_limited') return 'rate limited by the provider'
  if (status === 'disconnected') return 'disconnected'
  if (status === 'error') return 'provider unavailable'
  return 'active'
}

function SyncRunRow({ run }: { run: SyncRun }) {
  return (
    <li>
      <strong>{run.run_type}</strong>
      <small>
        {run.status}
        {run.status === 'running' ? ' (in progress)' : ''}
        {' · '}
        {run.items_processed} item{run.items_processed === 1 ? '' : 's'} processed
        {' · started '}
        {timestamp(run.started_at)}
        {run.completed_at ? ` · completed ${timestamp(run.completed_at)}` : ''}
      </small>
      {run.error_summary ? <p role="alert" className="inline-status error-panel">{run.error_summary}</p> : null}
    </li>
  )
}

function ConnectorCard({ connector, syncRuns, now, onChanged }: {
  connector: ConnectorAccount
  syncRuns: SyncRun[]
  now: Date
  onChanged: () => void
}) {
  const [runType, setRunType] = useState<(typeof RUN_TYPES)[number]>('backfill')
  const [resourceType, setResourceType] = useState<(typeof RESOURCE_TYPES)[number]>('repository')

  const syncMutation = useMutation({
    mutationFn: () =>
      apiRequest<ConnectorAccount>(`/api/v1/engineering/connectors/${connector.id}/sync`, {
        method: 'POST',
        body: { run_type: runType, resource_type: resourceType },
      }),
    onSuccess: onChanged,
  })
  const disableMutation = useMutation({
    mutationFn: () =>
      apiRequest<ConnectorAccount>(`/api/v1/engineering/connectors/${connector.id}/disable`, { method: 'POST' }),
    onSuccess: onChanged,
  })

  const stale = isStale(connector, now)
  // Deliberately keyed on `syncRuns.length` alone, not `connector.status
  // === 'pending'` -- a connector can still report `pending` while its
  // very first backfill is already `running` (the account-level status
  // only flips once that sync completes), and showing "no sync has ever
  // run" alongside a visible, in-progress sync history would be a
  // contradiction, not a UX state that exists in this data model.
  const neverSynced = syncRuns.length === 0

  return (
    <li>
      <div>
        <strong>{connector.display_name}</strong>
        <small>{connector.provider} · last synced {timestamp(connector.last_synced_at)}</small>
      </div>

      <div role="status" className={statusPanelClass(connector.status)}>
        {statusLabel(connector.status)}
        {connector.status_detail ? ` -- ${connector.status_detail}` : ''}
      </div>
      {connector.status === 'error' && connector.last_error ? (
        <p role="alert" className="inline-status error-panel">{connector.last_error}</p>
      ) : null}
      {neverSynced ? (
        <p className="empty-state">No sync has ever run for this connector -- data will appear once a backfill completes.</p>
      ) : null}
      {stale ? (
        <div role="status" className="inline-status degraded-panel">
          Last synced {timestamp(connector.last_synced_at)} -- this connector may be showing stale data.
        </div>
      ) : null}

      {syncRuns.length ? (
        <details>
          <summary>Sync history ({syncRuns.length})</summary>
          <ul className="work-list" aria-label={`Sync runs for ${connector.display_name}`}>
            {syncRuns.map((run) => <SyncRunRow key={run.id} run={run} />)}
          </ul>
        </details>
      ) : null}

      <form onSubmit={(event) => { event.preventDefault(); syncMutation.mutate() }}>
        <label>Run type
          <select aria-label={`Run type for ${connector.display_name}`} value={runType} onChange={(e) => setRunType(e.target.value as typeof runType)}>
            {RUN_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
          </select>
        </label>
        <label>Resource type
          <select aria-label={`Resource type for ${connector.display_name}`} value={resourceType} onChange={(e) => setResourceType(e.target.value as typeof resourceType)}>
            {RESOURCE_TYPES.map((type) => <option key={type} value={type}>{type.replaceAll('_', ' ')}</option>)}
          </select>
        </label>
        <div className="work-actions">
          <button type="submit" disabled={syncMutation.isPending || connector.status === 'disconnected'}>Start sync</button>
          <button
            type="button"
            disabled={disableMutation.isPending || connector.status === 'disconnected'}
            onClick={() => disableMutation.mutate()}
          >
            Disconnect
          </button>
        </div>
      </form>
      {syncMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(syncMutation.error)}</div> : null}
      {disableMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(disableMutation.error)}</div> : null}
    </li>
  )
}

export default function ConnectorHealthPanel() {
  const queryClient = useQueryClient()
  const [provider, setProvider] = useState<ConnectorProvider>('github')
  const [fields, setFields] = useState<CredentialFields>(() => emptyCredentialFields('github'))

  const connectors = useQuery({
    queryKey: ['engineering', 'connectors'],
    queryFn: () => apiRequest<ConnectorAccountListResponse>('/api/v1/engineering/connectors'),
    retry: 1,
  })
  const syncRuns = useQuery({
    queryKey: ['engineering', 'sync-runs'],
    queryFn: () => apiRequest<SyncRunListResponse>('/api/v1/engineering/sync-runs'),
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      apiRequest<ConnectorAccount>('/api/v1/engineering/connectors', {
        method: 'POST',
        body: { provider, credential: buildCredential(provider, fields) },
      }),
    onSuccess: () => {
      setFields(emptyCredentialFields(provider))
      refresh()
    },
  })

  function selectProvider(next: ConnectorProvider) {
    setProvider(next)
    setFields(emptyCredentialFields(next))
  }

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'connectors'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'sync-runs'] })
  }

  const runsByConnector = new Map<string, SyncRun[]>()
  for (const run of syncRuns.data?.sync_runs ?? []) {
    const list = runsByConnector.get(run.connector_account_id) ?? []
    list.push(run)
    runsByConnector.set(run.connector_account_id, list)
  }

  const now = new Date()
  const items = connectors.data?.connectors ?? []

  return (
    <section className="work-panel" aria-labelledby="engineering-connector-health-title">
      <h2 id="engineering-connector-health-title">Connector health</h2>
      <p>Every connected GitHub, GitLab, or Jira account, its current status, and its full sync history. A degraded or errored connector is shown here before it silently produces stale data elsewhere.</p>

      <form onSubmit={(event) => { event.preventDefault(); createMutation.mutate() }}>
        <label>Provider
          <select aria-label="Provider" value={provider} onChange={(e) => selectProvider(e.target.value as ConnectorProvider)}>
            {PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        {provider === 'gitlab' ? (
          <label>Host
            <input
              aria-label="Host"
              type="text"
              value={fields.host}
              onChange={(e) => setFields({ ...fields, host: e.target.value })}
              autoComplete="off"
            />
          </label>
        ) : null}
        {provider === 'jira' ? (
          <>
            <label>Site
              <input
                aria-label="Site"
                type="text"
                value={fields.site}
                onChange={(e) => setFields({ ...fields, site: e.target.value })}
                autoComplete="off"
                placeholder="yoursite.atlassian.net"
              />
            </label>
            <label>Email
              <input
                aria-label="Email"
                type="email"
                value={fields.email}
                onChange={(e) => setFields({ ...fields, email: e.target.value })}
                autoComplete="off"
              />
            </label>
          </>
        ) : null}
        {provider === 'datadog' ? (
          <label>Site
            <select aria-label="Site" value={fields.site} onChange={(e) => setFields({ ...fields, site: e.target.value })}>
              {DATADOG_SITES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        ) : null}
        <label>{tokenFieldLabel(provider)}
          <input
            aria-label={tokenFieldLabel(provider)}
            type="password"
            value={fields.token}
            onChange={(e) => setFields({ ...fields, token: e.target.value })}
            autoComplete="off"
          />
        </label>
        {provider === 'datadog' ? (
          <label>Application key
            <input
              aria-label="Application key"
              type="password"
              value={fields.appKey}
              onChange={(e) => setFields({ ...fields, appKey: e.target.value })}
              autoComplete="off"
            />
          </label>
        ) : null}
        <div className="work-actions">
          <button type="submit" disabled={createMutation.isPending || !isCredentialComplete(provider, fields)}>Connect</button>
        </div>
      </form>
      {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}

      {connectors.isLoading ? <p role="status">Loading connectors…</p> : null}
      {connectors.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(connectors.error)}</div> : null}
      {connectors.data && items.length === 0 ? <p className="empty-state">No connectors are configured for this workspace yet.</p> : null}

      <ul className="work-list">
        {items.map((connector) => (
          <ConnectorCard
            key={connector.id}
            connector={connector}
            syncRuns={runsByConnector.get(connector.id) ?? []}
            now={now}
            onChanged={refresh}
          />
        ))}
      </ul>
    </section>
  )
}
