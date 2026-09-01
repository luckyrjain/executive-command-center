import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
import { useWizardStepFocus } from '../../lib/wizardFocus'
import type {
  ConnectorAccount,
  ConnectorAccountListResponse,
  ConnectorProvider,
  SyncRun,
  SyncRunListResponse,
  SyncRunType,
} from './types'

const PROVIDERS: ReadonlyArray<ConnectorProvider> = ['github', 'gitlab', 'jira', 'datadog', 'sandbox']

// `ConnectorProvider` also includes `gmail`, managed entirely by its own
// Phase 10 `GmailPanel` (OAuth, not a credential this form ever collects) --
// listed here only so this map satisfies `Record<ConnectorProvider, string>`,
// never rendered since `PROVIDERS` above omits it.
const PROVIDER_LABELS: Record<ConnectorProvider, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
  jira: 'Jira',
  datadog: 'Datadog',
  sandbox: 'Sandbox (developer testing only)',
  gmail: 'Gmail',
}

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

// The API host stored in the credential (above) is not the host that
// serves Datadog's web UI pages -- mirrors `datadog_adapter.py`'s own
// `_SITE_TO_UI_HOST` (https://docs.datadoghq.com/getting_started/site/),
// which exists for the same reason: these two hosts are not
// interchangeable, and some regions' UI host has no `app.` prefix at all.
const DATADOG_UI_HOSTS: Record<(typeof DATADOG_SITES)[number], string> = {
  'api.datadoghq.com': 'app.datadoghq.com',
  'api.us3.datadoghq.com': 'us3.datadoghq.com',
  'api.us5.datadoghq.com': 'us5.datadoghq.com',
  'api.datadoghq.eu': 'app.datadoghq.eu',
  'api.ap1.datadoghq.com': 'ap1.datadoghq.com',
  'api.ddog-gov.com': 'app.ddog-gov.com',
}

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

// The wizard below asks for one field per step rather than the whole
// credential at once (real-user setup feedback: "should be more like a
// wizard, natural click, click" -- see connector-setup-wizard mockup).
// Each provider's step list is exactly the fields `buildCredential`/
// `isCredentialComplete` below already know how to join/validate --
// this only decides what order they're shown in, one at a time.
type FieldKey = 'host' | 'site' | 'email' | 'token' | 'appKey'
type WizardStep = 'provider' | FieldKey | 'review'

const PROVIDER_FIELD_STEPS: Record<ConnectorProvider, FieldKey[]> = {
  github: ['token'],
  gitlab: ['host', 'token'],
  jira: ['site', 'email', 'token'],
  datadog: ['site', 'token', 'appKey'],
  sandbox: ['token'],
  gmail: [],
}

function stepsFor(provider: ConnectorProvider): WizardStep[] {
  return ['provider', ...PROVIDER_FIELD_STEPS[provider], 'review']
}

function stepShortLabel(provider: ConnectorProvider, step: WizardStep): string {
  switch (step) {
    case 'provider': return 'Provider'
    case 'host': return 'Host'
    case 'site': return provider === 'datadog' ? 'Region' : 'Site'
    case 'email': return 'Email'
    case 'token': return provider === 'datadog' ? 'API key' : 'Token'
    case 'appKey': return 'App key'
    case 'review': return 'Review'
  }
}

// `host`/`provider`/`review` are never blocking: `host` always falls back
// to `gitlab.com` (`buildCredential`), and Datadog's `site` step is a
// `<select>` that always carries a real value, never free text.
function isStepComplete(provider: ConnectorProvider, step: WizardStep, fields: CredentialFields): boolean {
  switch (step) {
    case 'site': return provider === 'jira' ? fields.site.trim() !== '' : true
    case 'email': return fields.email.trim() !== ''
    case 'token': return fields.token.trim() !== ''
    case 'appKey': return fields.appKey.trim() !== ''
    default: return true
  }
}

function maskTail(value: string): string {
  if (value.trim() === '') return '—'
  return '•••• ' + value.slice(-4).padStart(4, '•')
}

function reviewRows(provider: ConnectorProvider, fields: CredentialFields): Array<{ label: string; value: string; machine?: boolean }> {
  // `Provider` is a human display name (PROVIDER_LABELS), not a machine-shaped
  // value, so it's the one row that stays off `machine: true` -- everything
  // else here (host, site, email, masked token/app key) is genuinely
  // ID/credential-shaped and gets the review step's monospace treatment.
  const rows: Array<{ label: string; value: string; machine?: boolean }> = [{ label: 'Provider', value: PROVIDER_LABELS[provider] }]
  for (const step of PROVIDER_FIELD_STEPS[provider]) {
    if (step === 'host') rows.push({ label: 'Host', value: fields.host.trim() || 'gitlab.com', machine: true })
    else if (step === 'site') rows.push({ label: stepShortLabel(provider, 'site'), value: fields.site, machine: true })
    else if (step === 'email') rows.push({ label: 'Email', value: fields.email, machine: true })
    else if (step === 'token') rows.push({ label: tokenFieldLabel(provider), value: maskTail(fields.token), machine: true })
    else if (step === 'appKey') rows.push({ label: 'Application key', value: maskTail(fields.appKey), machine: true })
  }
  return rows
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
// One field, and its own input, per wizard step -- `step` is always a
// `FieldKey` here (the 'provider'/'review' steps render their own markup
// directly in the wizard body below, never through this component).
function FieldStepInput({ provider, step, fields, onChange }: {
  provider: ConnectorProvider
  step: FieldKey
  fields: CredentialFields
  onChange: (patch: Partial<CredentialFields>) => void
}) {
  switch (step) {
    case 'host':
      return (
        <label>Host
          <input aria-label="Host" type="text" value={fields.host} onChange={(e) => onChange({ host: e.target.value })} autoComplete="off" />
        </label>
      )
    case 'site':
      if (provider === 'datadog') {
        return (
          <label>Site
            <select aria-label="Site" value={fields.site} onChange={(e) => onChange({ site: e.target.value })}>
              {DATADOG_SITES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        )
      }
      return (
        <label>Site
          <input
            aria-label="Site"
            type="text"
            value={fields.site}
            onChange={(e) => onChange({ site: e.target.value })}
            autoComplete="off"
            placeholder="yoursite.atlassian.net"
          />
        </label>
      )
    case 'email':
      return (
        <label>Email
          <input aria-label="Email" type="email" value={fields.email} onChange={(e) => onChange({ email: e.target.value })} autoComplete="off" />
        </label>
      )
    case 'token':
      return (
        <label>{tokenFieldLabel(provider)}
          <input
            aria-label={tokenFieldLabel(provider)}
            type="password"
            value={fields.token}
            onChange={(e) => onChange({ token: e.target.value })}
            autoComplete="off"
          />
        </label>
      )
    case 'appKey':
      return (
        <label>Application key
          <input
            aria-label="Application key"
            type="password"
            value={fields.appKey}
            onChange={(e) => onChange({ appKey: e.target.value })}
            autoComplete="off"
          />
        </label>
      )
  }
}

// Each hint names the exact scope/field the receiving adapter checks
// (`github_adapter.py`'s `_REQUIRED_SCOPES`, `gitlab_adapter.py`'s own) and
// links to the provider's real token-creation page, so filling this step
// correctly doesn't require reading `docs/SETUP.md` or the adapter source
// first. GitLab's link depends on the host step's own value (already set,
// since host is always asked before token); a self-hosted host's settings
// path can vary by version, so that case gets guidance text instead of a
// guessed link.
function FieldHelp({ provider, step, fields }: { provider: ConnectorProvider; step: FieldKey; fields: CredentialFields }) {
  if (provider === 'github' && step === 'token') {
    return (
      <p className="field-hint">
        Create a classic personal access token with the <code>repo</code> scope, then paste it below.{' '}
        <a
          href="https://github.com/settings/tokens/new?scopes=repo&description=Executive+Command+Center"
          target="_blank"
          rel="noreferrer"
        >
          Create a GitHub token
        </a>
      </p>
    )
  }
  if (provider === 'gitlab' && step === 'token') {
    const host = fields.host.trim() || 'gitlab.com'
    return (
      <p className="field-hint">
        Create a personal access token with the <code>read_api</code> and <code>read_repository</code> scopes.{' '}
        {host === 'gitlab.com' ? (
          <a href="https://gitlab.com/-/user_settings/personal_access_tokens" target="_blank" rel="noreferrer">
            Create a GitLab token
          </a>
        ) : (
          <span>Find it under Settings → Access Tokens on {host}.</span>
        )}
      </p>
    )
  }
  if (provider === 'jira' && step === 'token') {
    return (
      <p className="field-hint">
        Use the Atlassian account email from the last step, plus an API token -- not your account password.{' '}
        <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noreferrer">
          Create a Jira API token
        </a>
      </p>
    )
  }
  if (provider === 'datadog' && (step === 'token' || step === 'appKey')) {
    const uiHost = DATADOG_UI_HOSTS[fields.site as (typeof DATADOG_SITES)[number]] ?? fields.site
    const isAppKey = step === 'appKey'
    return (
      <p className="field-hint">
        Your {isAppKey ? 'application' : 'API'} key comes from your Datadog organization settings.{' '}
        <a
          href={`https://${uiHost}/organization-settings/${isAppKey ? 'application-keys' : 'api-keys'}`}
          target="_blank"
          rel="noreferrer"
        >
          {isAppKey ? 'Open Application keys' : 'Open API keys'}
        </a>
      </p>
    )
  }
  return null
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
  if (error instanceof ApiError && error.code === 'CONNECTOR_AUTHORIZATION_FAILED') {
    // The backend's own detail dict is `{"code": ..., "error": &lt;sanitized
    // message&gt;}` (`create_connector_endpoint`'s `AdapterAuthorizationError`
    // handler) -- `main.py`'s `_error_payload` strips only `code`/`message`
    // out into `details`, so the real field name surviving onto `ApiError.
    // current` is `error`, never `reason`.
    const detail = error.current as { error?: string } | undefined
    return `The provider rejected this credential${detail?.error ? `: ${detail.error}` : '.'}`
  }
  return apiErrorMessage(error, {
    CONNECTOR_PROVIDER_NOT_SUPPORTED: 'This provider has no registered connector adapter.',
    CONNECTOR_ALREADY_CONNECTED: 'This workspace already has a connector for this account.',
    CONNECTOR_NOT_FOUND: 'This connector no longer exists in this workspace.',
    CONNECTOR_DISCONNECTED: 'This connector is already disconnected.',
    CONNECTOR_SYNC_IN_PROGRESS: 'A sync is already running for this connector -- wait for it to finish before starting another.',
    '401': 'Your session is no longer valid. Sign in again.',
    '403': 'You are not permitted to manage connectors in this workspace.',
  })
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

      <form className="field-form" onSubmit={(event) => { event.preventDefault(); syncMutation.mutate() }}>
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
            className="btn-destructive"
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
  const [stepIndex, setStepIndex] = useState(0)
  const [connected, setConnected] = useState<ConnectorAccount | null>(null)
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
    onSuccess: (data) => {
      setConnected(data)
      setFields(emptyCredentialFields(provider))
      refresh()
    },
  })

  function selectProvider(next: ConnectorProvider) {
    if (next === provider) return // re-clicking the already-selected tile must not wipe fields already typed for it
    setProvider(next)
    setFields(emptyCredentialFields(next))
    setStepIndex(0)
    setConnected(null)
  }

  function updateFields(patch: Partial<CredentialFields>) {
    setFields((f) => ({ ...f, ...patch }))
  }

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'connectors'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'sync-runs'] })
  }

  const steps = stepsFor(provider)
  const currentStep = steps[stepIndex] ?? 'provider'

  // Moves focus to the incoming step's own heading on every step change --
  // each step is a different conditional branch below, so without this a
  // keyboard/screen-reader user's focus (on the Continue/Back/tile button
  // they just activated) is simply dropped to `<body>` when that button
  // unmounts, same class of problem `MembersPanel.tsx`'s own trigger-focus
  // effect exists to prevent. `tabIndex={-1}` on the heading (below) is
  // what makes a non-interactive element a valid `.focus()` target. The
  // first-render guard (mounting the Engineering tab must not yank focus
  // onto the wizard before any real step change happened) lives inside
  // `useWizardStepFocus` itself now.
  const stepHeadingRef = useWizardStepFocus(() => stepHeadingRef.current?.focus(), [currentStep, connected])

  function goNext() {
    setStepIndex((i) => Math.min(i + 1, steps.length - 1))
  }
  function goBack() {
    setStepIndex((i) => Math.max(i - 1, 0))
  }
  function startOver() {
    setConnected(null)
    setStepIndex(0)
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

      <h3 id="connector-connect-title">Connect an integration</h3>
      <p>Add a GitHub, GitLab, Jira, or Datadog account to sync its data into this workspace.</p>

      <div role="region" aria-labelledby="connector-connect-title">
        {connected ? (
          <div>
            <p className="eyebrow">Connected</p>
            <h4 ref={stepHeadingRef} tabIndex={-1}>{connected.display_name} is connected</h4>
            <p>The first backfill starts automatically -- no action needed.</p>
            <div className="work-actions">
              <button type="button" onClick={startOver}>Connect another integration</button>
            </div>
          </div>
        ) : (
          <>
            <ol className="wizard-stepper" aria-label="Setup progress">
              {steps.map((step, i) => (
                <li className="wizard-step-node" key={step} aria-current={i === stepIndex ? 'step' : undefined}>
                  <span className={i < stepIndex ? 'wizard-step-circle done' : i === stepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>
                    {i < stepIndex ? '✓' : i + 1}
                  </span>
                  <span className={i <= stepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{stepShortLabel(provider, step)}</span>
                  {i < steps.length - 1 ? <span className={i < stepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
                </li>
              ))}
            </ol>

            {currentStep === 'provider' ? (
              <div>
                <p className="eyebrow">Step {stepIndex + 1} of {steps.length} · Provider</p>
                <h4 ref={stepHeadingRef} tabIndex={-1}>Choose a provider</h4>
                <div role="group" aria-label="Provider" className="work-actions">
                  {PROVIDERS.map((p) => (
                    <button key={p} type="button" aria-pressed={provider === p} onClick={() => selectProvider(p)}>
                      {PROVIDER_LABELS[p]}
                    </button>
                  ))}
                </div>
                <div className="work-actions">
                  <button type="button" onClick={goNext}>Continue</button>
                </div>
              </div>
            ) : currentStep === 'review' ? (
              <div className="wizard-review">
                <p className="eyebrow">Step {stepIndex + 1} of {steps.length} · Review</p>
                <h4 ref={stepHeadingRef} tabIndex={-1}>Review and connect</h4>
                <dl>
                  {reviewRows(provider, fields).map((row) => (
                    <div key={row.label}>
                      <dt>{row.label}</dt>
                      <dd className={row.machine ? 'is-machine-value' : undefined}>{row.value}</dd>
                    </div>
                  ))}
                </dl>
                <div className="work-actions">
                  <button type="button" onClick={goBack}>Back</button>
                  <button type="button" disabled={createMutation.isPending || !isCredentialComplete(provider, fields)} onClick={() => createMutation.mutate()}>
                    {createMutation.isPending ? 'Connecting…' : 'Connect'}
                  </button>
                </div>
                {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}
              </div>
            ) : (
              <div>
                <p className="eyebrow">Step {stepIndex + 1} of {steps.length} · {stepShortLabel(provider, currentStep)}</p>
                <h4 ref={stepHeadingRef} tabIndex={-1}>{stepShortLabel(provider, currentStep)}</h4>
                <FieldStepInput provider={provider} step={currentStep} fields={fields} onChange={updateFields} />
                <FieldHelp provider={provider} step={currentStep} fields={fields} />
                <div className="work-actions">
                  <button type="button" onClick={goBack}>Back</button>
                  <button type="button" disabled={!isStepComplete(provider, currentStep, fields)} onClick={goNext}>Continue</button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <hr />
      <h3 id="connector-list-title">Connected integrations</h3>
      <p>Every connected account, its current status, and its full sync history. A degraded or errored connector is shown here before it silently produces stale data elsewhere.</p>

      {connectors.isLoading ? <p role="status">Loading connectors…</p> : null}
      {connectors.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(connectors.error)}</div> : null}
      {connectors.data && items.length === 0 ? <p className="empty-state">No connectors are configured for this workspace yet.</p> : null}

      <ul className="work-list" aria-labelledby="connector-list-title">
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
