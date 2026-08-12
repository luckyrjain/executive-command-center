import { ApiError } from '../../api/client'

/** Shared across every panel in this feature -- the same error-code set
 * (`DOMAIN_NOT_FOUND`, `RECORD_NOT_FOUND`, `VERSION_CONFLICT`, ...) can
 * surface from any of the five personal-domain routers, so one mapping
 * function avoids repeating `ConnectorHealthPanel.tsx`'s per-panel
 * `errorMessage` five times over for the identical generic codes.
 */
export function personalErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CSRF_TOKEN_REQUIRED' || error.code === 'CSRF_TOKEN_INVALID') return 'Your session\'s security token is missing or stale. Reload the page and try again.'
  if (error.code === 'DOMAIN_NOT_FOUND') return 'This domain has not been enabled yet.'
  if (error.code === 'DOMAIN_NOT_ENABLED') return 'Enable this domain before trying that again.'
  if (error.code === 'CONSENT_NOT_FOUND') return 'This consent no longer exists.'
  if (error.code === 'RETENTION_ACKNOWLEDGEMENT_REQUIRED') return 'This domain requires you to acknowledge its retention terms for every record before saving it.'
  if (error.code === 'RECORD_NOT_FOUND') return 'This record no longer exists.'
  if (error.code === 'VERSION_CONFLICT') return 'This record changed elsewhere. Reload and retry.'
  if (error.code === 'GRANT_NOT_FOUND') return 'This grant no longer exists.'
  if (error.code === 'INSIGHT_NOT_FOUND') return 'This insight no longer exists.'
  if (error.code === 'GOAL_NOT_FOUND') return 'This goal no longer exists.'
  if (error.code === 'ROUTINE_NOT_FOUND') return 'This routine no longer exists.'
  // Gmail-specific codes (Phase 10) -- `GmailPanel` lives in this feature
  // but reaches both the `email` domain endpoints (`EMAIL_CONSENT_NOT_
  // ACTIVE`/`THREAD_NOT_FOUND`, `gmail_threads.py`/`gmail_oauth.py`) and
  // the generic engineering connector endpoints it shares with `Connector
  // HealthPanel.tsx` for sync/status (`CONNECTOR_*`, that panel's own
  // `errorMessage` has the identical three) -- kept here rather than
  // duplicated into a second Gmail-only error module, matching this
  // function's own "one mapping function" rationale above.
  if (error.code === 'EMAIL_CONSENT_NOT_ACTIVE') return 'Email consent is not active. Enable the email domain and grant consent to view Gmail data.'
  if (error.code === 'THREAD_NOT_FOUND') return 'This thread no longer exists.'
  if (error.code === 'GMAIL_ACCOUNT_NOT_ALLOWLISTED') return 'This Google account is not on the internal allowlist for Gmail access.'
  if (error.code === 'GMAIL_OAUTH_NOT_CONFIGURED') return 'Gmail OAuth is not configured for this deployment.'
  if (error.code === 'GMAIL_OAUTH_STATE_INVALID') return 'This Gmail sign-in link expired or was already used. Start again.'
  if (error.code === 'GMAIL_OAUTH_DENIED') return 'Google sign-in was cancelled. Click Connect Gmail to try again.'
  if (error.code === 'GMAIL_OAUTH_FAILED') return 'Google rejected this sign-in attempt. Try again.'
  if (error.code === 'GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT') return 'Use the email domain\'s disable action to disconnect Gmail, not the generic connector action.'
  if (error.code === 'CONNECTOR_NOT_FOUND') return 'This connector no longer exists in this workspace.'
  if (error.code === 'CONNECTOR_DISCONNECTED') return 'This connector is already disconnected.'
  if (error.code === 'CONNECTOR_SYNC_IN_PROGRESS') return 'A sync is already running for this connector -- wait for it to finish before starting another.'
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to manage personal data in this workspace.'
  return error.message
}

export function formatTimestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never'
}
