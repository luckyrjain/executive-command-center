import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import RecommendationPanel from '../../RecommendationPanel'
import type { ConnectorAccount, ConnectorAccountListResponse, SyncRun, SyncRunListResponse } from '../engineering/types'
import { personalErrorMessage, formatTimestamp } from './errors'
import { DOMAIN_LABELS } from './types'
import type {
  DomainListResponse,
  GmailOAuthStartResponse,
  GmailThreadContent,
  GmailThreadForgetResponse,
  GmailThreadListResponse,
  GmailThreadSummary,
} from './types'

// This activation has no periodic freshness monitor for Gmail either --
// same disclosed heuristic `ConnectorHealthPanel.tsx`'s `STALE_AFTER_MS`
// uses for every other provider.
const STALE_AFTER_MS = 24 * 60 * 60 * 1000

function isStale(connector: ConnectorAccount, now: Date): boolean {
  if (!connector.last_synced_at) return false
  return now.getTime() - new Date(connector.last_synced_at).getTime() > STALE_AFTER_MS
}

function statusPanelClass(status: ConnectorAccount['status']): string {
  if (status === 'error' || status === 'disconnected') return 'inline-status error-panel'
  if (status === 'permission_lost' || status === 'rate_limited') return 'inline-status degraded-panel'
  return 'inline-status'
}

function statusLabel(status: ConnectorAccount['status']): string {
  if (status === 'pending') return 'first sync not yet run'
  if (status === 'permission_lost') return 'permission lost -- reconnect required before syncing or reading bodies'
  if (status === 'rate_limited') return 'rate limited by Google -- retry later, this connector will catch up'
  if (status === 'disconnected') return 'disconnected'
  if (status === 'error') return 'Gmail unavailable'
  return 'active'
}

function threadSummaryLine(thread: GmailThreadSummary): string {
  const sender = thread.last_sender ?? 'unknown sender'
  const direction = thread.last_direction === 'outbound' ? 'to' : 'from'
  return `${thread.message_count} message${thread.message_count === 1 ? '' : 's'} · latest ${direction} ${sender} · ${formatTimestamp(thread.last_message_at)}`
}

function ThreadDetail({ threadId, onForgotten }: { threadId: string; onForgotten: () => void }) {
  const queryClient = useQueryClient()
  const thread = useQuery({
    queryKey: ['personal', 'gmail', 'thread', threadId],
    queryFn: () => apiRequest<GmailThreadContent>(`/api/v1/personal/gmail/threads/${threadId}`),
    retry: 1,
  })
  const forgetMutation = useMutation({
    mutationFn: () =>
      apiRequest<GmailThreadForgetResponse>(`/api/v1/personal/gmail/threads/${threadId}/forget`, { method: 'POST' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['personal', 'gmail', 'threads'] })
      onForgotten()
    },
  })

  return (
    <div className="work-panel" role="region" aria-label={thread.data?.subject ?? 'Thread'}>
      {thread.isLoading ? <p role="status">Loading thread…</p> : null}
      {thread.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(thread.error)}</div> : null}
      {thread.data ? (
        <>
          <h3>{thread.data.subject ?? '(no subject)'}</h3>
          <ul className="work-list" aria-label="Messages">
            {thread.data.messages.map((message) => (
              <li key={message.id}>
                <small>{message.direction === 'outbound' ? 'To' : 'From'} {message.sender} · {formatTimestamp(message.sent_at)}</small>
                {message.body ? <p>{message.body}</p> : <p className="empty-state">(no text content in this message)</p>}
              </li>
            ))}
            {thread.data.messages.length === 0 ? <li className="empty-state">No message bodies are cached for this thread yet.</li> : null}
          </ul>
          <div className="work-actions">
            <button type="button" disabled={forgetMutation.isPending} onClick={() => forgetMutation.mutate()}>
              {forgetMutation.isPending ? 'Forgetting…' : 'Forget cached content for this thread'}
            </button>
          </div>
          {forgetMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(forgetMutation.error)}</div> : null}
        </>
      ) : null}
    </div>
  )
}

type OAuthReturnStatus = { kind: 'connected' } | { kind: 'error'; code: string }

// `gmail_oauth.py`'s `GET /oauth/complete` is the real Google-facing
// redirect target -- it does the token exchange server-side, then sends
// the browser's own top-level navigation back here with `?gmail=connected`
// or `?gmail=error&code=...`, since a bare backend JSON response is not
// something this SPA can render. Read once on mount, then strip the
// params from the URL (`history.replaceState`) so a page refresh doesn't
// keep re-showing a stale result -- the same one-shot-consume-then-clear
// shape `dev_bootstrap.py`'s own completion page uses for its own
// one-time code (that page parses `location.hash`, this reads
// `location.search` -- different URL part, identical "read once, then
// scrub the URL" intent).
function readOAuthReturnStatus(): OAuthReturnStatus | null {
  const params = new URLSearchParams(window.location.search)
  const gmail = params.get('gmail')
  if (gmail === 'connected') return { kind: 'connected' }
  if (gmail === 'error') return { kind: 'error', code: params.get('code') ?? 'GMAIL_OAUTH_FAILED' }
  return null
}

export default function GmailPanel() {
  const queryClient = useQueryClient()
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null)
  const [sinceInput, setSinceInput] = useState('')
  // The "Connect Gmail" step -- real-user setup feedback: "should be more
  // like a wizard, natural click, click" (see the connector-setup-wizard
  // design mockup this session also produced). Only two steps exist because
  // OAuth itself needs no data entry from this app; a third "connected" step
  // isn't modeled here since the real post-connect experience is the full
  // account panel below (`activeAccount ? ... : ...`), which already shows
  // far more than a generic confirmation screen would. Resets to 1 on every
  // fresh mount, which is correct: this only matters pre-connect, and a
  // real connect is always a full top-level navigation away and back.
  const [connectStep, setConnectStep] = useState<1 | 2>(1)
  // Moves focus to the incoming step's own heading on Continue/Back, the
  // same fix `ConnectorHealthPanel.tsx`'s own wizard needed -- without it,
  // the clicked button unmounts (each step is a different conditional
  // branch) and a keyboard/screen-reader user's focus silently drops to
  // `<body>` instead of landing on the new step.
  const connectStepHeadingRef = useRef<HTMLHeadingElement>(null)
  // `useEffect` also runs after the very first render, not only on a later
  // change to `connectStep` -- without this guard, simply opening the
  // Gmail tab with no account connected yet would yank focus onto "What
  // Gmail access gives you" on an ordinary page visit, not just a real
  // Continue/Back click. Mirrors `ConnectorHealthPanel.tsx`'s own guard.
  const isFirstConnectRenderRef = useRef(true)
  useEffect(() => {
    if (isFirstConnectRenderRef.current) {
      isFirstConnectRenderRef.current = false
      return
    }
    connectStepHeadingRef.current?.focus()
  }, [connectStep])
  // Lazy `useState` initializer (the function reference, not its called
  // result) -- React invokes `readOAuthReturnStatus` exactly once, on
  // first render, never again. The setter is intentionally never called:
  // this value never changes over the component's lifetime, only gets
  // read once and then cleared from the URL by the effect below.
  const [oauthReturn] = useState<OAuthReturnStatus | null>(readOAuthReturnStatus)

  useEffect(() => {
    if (oauthReturn === null) return
    const params = new URLSearchParams(window.location.search)
    params.delete('gmail')
    params.delete('code')
    const query = params.toString()
    window.history.replaceState(null, '', window.location.pathname + (query ? `?${query}` : ''))
  }, [oauthReturn])

  const domains = useQuery({
    queryKey: ['personal', 'domains'],
    queryFn: () => apiRequest<DomainListResponse>('/api/v1/personal/domains'),
    retry: 1,
  })
  const emailDomain = (domains.data?.domains ?? []).find((d) => d.domain_key === 'email') ?? null
  const consentActive = emailDomain?.enabled ?? false

  const connectors = useQuery({
    queryKey: ['engineering', 'connectors'],
    queryFn: () => apiRequest<ConnectorAccountListResponse>('/api/v1/engineering/connectors'),
    retry: 1,
  })
  const gmailAccounts = (connectors.data?.connectors ?? []).filter((c) => c.provider === 'gmail')
  const activeAccount = gmailAccounts.find((c) => c.status !== 'disconnected') ?? null

  const syncRuns = useQuery({
    queryKey: ['engineering', 'sync-runs'],
    queryFn: () => apiRequest<SyncRunListResponse>('/api/v1/engineering/sync-runs'),
    retry: 1,
    enabled: activeAccount !== null,
  })
  const accountSyncRuns: SyncRun[] = (syncRuns.data?.sync_runs ?? []).filter(
    (run) => run.connector_account_id === activeAccount?.id,
  )
  const latestRun = accountSyncRuns[0] ?? null
  const neverSynced = accountSyncRuns.length === 0

  const threads = useQuery({
    queryKey: ['personal', 'gmail', 'threads'],
    queryFn: () => apiRequest<GmailThreadListResponse>('/api/v1/personal/gmail/threads'),
    retry: 1,
    enabled: consentActive,
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'connectors'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'sync-runs'] })
    void queryClient.invalidateQueries({ queryKey: ['personal', 'gmail', 'threads'] })
    void queryClient.invalidateQueries({ queryKey: ['personal', 'domains'] })
  }

  const oauthStartMutation = useMutation({
    mutationFn: () => apiRequest<GmailOAuthStartResponse>('/api/v1/personal/gmail/oauth/start', { method: 'POST' }),
    onSuccess: (data) => {
      // Real top-level navigation, not a fetch -- Google's own consent
      // screen is not an origin this app can render inline. The mutation's
      // own `isPending` flag never resolves before this navigation actually
      // happens, which is what disables a duplicate "Connect" click
      // (UX-STATES.md's "OAuth pending" row: "prevents a duplicate click").
      window.location.href = data.authorization_url
    },
  })

  const syncMutation = useMutation({
    mutationFn: (since: string | null) =>
      apiRequest<ConnectorAccount>(`/api/v1/engineering/connectors/${activeAccount?.id}/sync`, {
        method: 'POST',
        body: { run_type: neverSynced || since ? 'backfill' : 'incremental', resource_type: 'message', since },
      }),
    onSuccess: refresh,
  })

  const disconnectMutation = useMutation({
    // The domain-level endpoint runs the Task 7 consent-revocation cascade
    // (best-effort token revoke, connector disconnect, and derived-data
    // purge) and is required once an `email` `personal_domains` row exists
    // -- the generic connector endpoint rejects that case with `409 GMAIL_
    // DISABLE_REQUIRES_DOMAIN_ENDPOINT` (`API-SCHEMAS.md`'s own "Current
    // reused connector endpoints" row). An owner who completed OAuth
    // without ever enabling the `email` domain has no such row, so the
    // domain endpoint would 404 `DOMAIN_NOT_FOUND` -- the generic connector
    // endpoint is the only one that can reach that account at all.
    mutationFn: () =>
      emailDomain !== null
        ? apiRequest(`/api/v1/personal/domains/email/disable`, { method: 'POST' })
        : apiRequest(`/api/v1/engineering/connectors/${activeAccount?.id}/disable`, { method: 'POST' }),
    onSuccess: () => {
      setSelectedThreadId(null)
      refresh()
    },
  })

  const now = new Date()
  const threadItems = threads.data?.threads ?? []
  const stale = activeAccount ? isStale(activeAccount, now) : false

  return (
    <section className="work-panel" aria-labelledby="personal-gmail-title">
      <h2 id="personal-gmail-title">Gmail</h2>
      <p>Read-only Gmail access, gated by an internal allowlist and your own explicit email-domain consent. Nothing here is visible to another workspace member unless they share it directly.</p>

      {oauthReturn?.kind === 'connected' ? (
        <p role="status" className="inline-status">Google account connected.</p>
      ) : null}
      {oauthReturn?.kind === 'error' ? (
        <div role="alert" className="inline-status error-panel">
          {personalErrorMessage(new ApiError(0, oauthReturn.code, oauthReturn.code))}
        </div>
      ) : null}

      {domains.isLoading ? <p role="status">Loading…</p> : null}
      {domains.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(domains.error)}</div> : null}
      {domains.data && !consentActive ? (
        <p className="empty-state">Enable {DOMAIN_LABELS.email} in the Domains tab to grant consent before reading Gmail data. You can still connect a Google account below -- nothing is read until consent is active.</p>
      ) : null}

      {connectors.isLoading ? <p role="status">Loading connector status…</p> : null}
      {connectors.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(connectors.error)}</div> : null}

      {activeAccount ? (
        <div className="work-panel">
          <div>
            <strong>{activeAccount.display_name}</strong>
            <small> · last synced {formatTimestamp(activeAccount.last_synced_at)}</small>
          </div>
          <div role="status" className={statusPanelClass(activeAccount.status)}>
            {statusLabel(activeAccount.status)}
            {activeAccount.status_detail ? ` -- ${activeAccount.status_detail}` : ''}
          </div>
          {activeAccount.status === 'error' && activeAccount.last_error ? (
            <p role="alert" className="inline-status error-panel">{activeAccount.last_error}</p>
          ) : null}
          {stale ? (
            <div role="status" className="inline-status degraded-panel">
              Last synced {formatTimestamp(activeAccount.last_synced_at)} -- this may be showing stale data.
            </div>
          ) : null}
          {latestRun?.status === 'running' ? (
            <p role="status">Syncing since {formatTimestamp(latestRun.started_at)}…</p>
          ) : null}
          {latestRun?.status === 'partial' ? (
            <p role="status" className="inline-status degraded-panel">
              Last sync was partial{latestRun.error_summary ? `: ${latestRun.error_summary}` : ''} -- resumes on the next sync, not yet caught up.
            </p>
          ) : null}
          {latestRun?.status === 'failed' ? (
            <p role="alert" className="inline-status error-panel">
              Last sync failed{latestRun.error_summary ? `: ${latestRun.error_summary}` : ''}.
            </p>
          ) : null}

          <div className="work-actions">
            <button
              type="button"
              disabled={syncMutation.isPending || activeAccount.status === 'disconnected' || activeAccount.status === 'permission_lost' || latestRun?.status === 'running'}
              onClick={() => syncMutation.mutate(null)}
            >
              {syncMutation.isPending ? 'Syncing…' : neverSynced ? 'Run first sync' : 'Sync now'}
            </button>
            <button type="button" disabled={disconnectMutation.isPending} onClick={() => disconnectMutation.mutate()}>
              {disconnectMutation.isPending ? 'Disconnecting…' : 'Disconnect'}
            </button>
          </div>
          {activeAccount.status === 'permission_lost' ? (
            <p>Google revoked or narrowed this connector's access. Reconnect below before syncing or reading message bodies again.</p>
          ) : null}

          <form
            className="field-form"
            onSubmit={(event) => {
              event.preventDefault()
              if (!sinceInput) return
              syncMutation.mutate(new Date(sinceInput).toISOString())
            }}
          >
            <label>Expand history from
              <input
                aria-label="Sync history from date"
                type="date"
                value={sinceInput}
                onChange={(event) => setSinceInput(event.target.value)}
              />
            </label>
            <div className="work-actions">
              <button type="submit" disabled={syncMutation.isPending || !sinceInput || activeAccount.status === 'disconnected' || activeAccount.status === 'permission_lost' || latestRun?.status === 'running'}>
                Sync from this date
              </button>
            </div>
          </form>
          {syncMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(syncMutation.error)}</div> : null}
          {disconnectMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(disconnectMutation.error)}</div> : null}
        </div>
      ) : connectStep === 1 ? (
        <div>
          <p className="eyebrow">Step 1 of 2 · What's shared</p>
          <h3 ref={connectStepHeadingRef} tabIndex={-1}>What Gmail access gives you</h3>
          <ul className="work-list">
            <li>
              <strong>Message metadata</strong>
              <small>Subjects, senders, timestamps and thread structure, synced automatically once connected.</small>
            </li>
            <li>
              <strong>Message bodies</strong>
              <small>Only once you enable {DOMAIN_LABELS.email} in the Domains tab -- off by default, and revocable anytime.</small>
            </li>
            <li>
              <strong>Internal allowlist only</strong>
              <small>Gmail connect is gated to explicitly approved accounts for this workspace.</small>
            </li>
          </ul>
          <div className="work-actions">
            <button type="button" onClick={() => setConnectStep(2)}>Continue</button>
          </div>
        </div>
      ) : (
        <div>
          <p className="eyebrow">Step 2 of 2 · Sign in</p>
          <h3 ref={connectStepHeadingRef} tabIndex={-1}>Sign in with your Google account</h3>
          <p>You'll leave Executive Command Center briefly for Google's own sign-in and consent screen, then land right back here.</p>
          <div className="work-actions">
            <button type="button" onClick={() => setConnectStep(1)}>Back</button>
            <button type="button" disabled={oauthStartMutation.isPending} onClick={() => oauthStartMutation.mutate()}>
              {oauthStartMutation.isPending ? 'Redirecting to Google…' : 'Connect Gmail'}
            </button>
          </div>
        </div>
      )}
      {oauthStartMutation.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(oauthStartMutation.error)}</div> : null}

      {consentActive ? (
        <>
          <h3>Threads</h3>
          {threads.isLoading ? <p role="status">Loading threads…</p> : null}
          {threads.isError ? <div role="alert" className="inline-status error-panel">{personalErrorMessage(threads.error)}</div> : null}
          {threads.data && threadItems.length === 0 ? (
            <p className="empty-state">
              {neverSynced ? 'No sync has run yet -- run a sync above to load threads.' : 'No messages in the synced window.'}
            </p>
          ) : null}
          <ul className="work-list" aria-label="Gmail threads">
            {threadItems.map((thread) => (
              <li key={thread.id}>
                <button type="button" onClick={() => setSelectedThreadId(thread.id)} aria-expanded={selectedThreadId === thread.id}>
                  {thread.subject ?? '(no subject)'}
                </button>
                <small>{threadSummaryLine(thread)}{thread.body_cached ? '' : ' · body not yet fetched'}</small>
              </li>
            ))}
          </ul>
          {selectedThreadId ? (
            <ThreadDetail threadId={selectedThreadId} onForgotten={() => setSelectedThreadId(null)} />
          ) : null}

          <RecommendationPanel recommendationType="email_action_detected" title="Pending email actions" />
        </>
      ) : null}
    </section>
  )
}
