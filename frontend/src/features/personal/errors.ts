import { apiErrorMessage } from '../../api/errorMessage'

/** Shared across every panel in this feature -- the same error-code set
 * (`DOMAIN_NOT_FOUND`, `RECORD_NOT_FOUND`, `VERSION_CONFLICT`, ...) can
 * surface from any of the five personal-domain routers, so one mapping
 * function avoids repeating `ConnectorHealthPanel.tsx`'s per-panel
 * `errorMessage` five times over for the identical generic codes.
 */
export function personalErrorMessage(error: unknown): string {
  return apiErrorMessage(error, {
    DOMAIN_NOT_FOUND: 'This domain has not been enabled yet.',
    DOMAIN_NOT_ENABLED: 'Enable this domain before trying that again.',
    CONSENT_NOT_FOUND: 'This consent no longer exists.',
    RETENTION_ACKNOWLEDGEMENT_REQUIRED: 'This domain requires you to acknowledge its retention terms for every record before saving it.',
    RECORD_NOT_FOUND: 'This record no longer exists.',
    VERSION_CONFLICT: 'This record changed elsewhere. Reload and retry.',
    GRANT_NOT_FOUND: 'This grant no longer exists.',
    INSIGHT_NOT_FOUND: 'This insight no longer exists.',
    GOAL_NOT_FOUND: 'This goal no longer exists.',
    ROUTINE_NOT_FOUND: 'This routine no longer exists.',
    // Gmail-specific codes (Phase 10) -- `GmailPanel` lives in this feature
    // but reaches both the `email` domain endpoints (`EMAIL_CONSENT_NOT_
    // ACTIVE`/`THREAD_NOT_FOUND`, `gmail_threads.py`/`gmail_oauth.py`) and
    // the generic engineering connector endpoints it shares with `Connector
    // HealthPanel.tsx` for sync/status (`CONNECTOR_*`, that panel's own
    // `errorMessage` has the identical three) -- kept here rather than
    // duplicated into a second Gmail-only error module, matching this
    // function's own "one mapping function" rationale above.
    EMAIL_CONSENT_NOT_ACTIVE: 'Email consent is not active. Enable the email domain and grant consent to view Gmail data.',
    THREAD_NOT_FOUND: 'This thread no longer exists.',
    GMAIL_ACCOUNT_NOT_ALLOWLISTED: 'This Google account is not on the internal allowlist for Gmail access.',
    GMAIL_OAUTH_NOT_CONFIGURED: 'Gmail OAuth is not configured for this deployment.',
    GMAIL_OAUTH_STATE_INVALID: 'This Gmail sign-in link expired or was already used. Start again.',
    GMAIL_OAUTH_DENIED: 'Google sign-in was cancelled. Click Connect Gmail to try again.',
    GMAIL_OAUTH_FAILED: 'Google rejected this sign-in attempt. Try again.',
    GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT: 'Use the email domain\'s disable action to disconnect Gmail, not the generic connector action.',
    CONNECTOR_NOT_FOUND: 'This connector no longer exists in this workspace.',
    CONNECTOR_DISCONNECTED: 'This connector is already disconnected.',
    CONNECTOR_SYNC_IN_PROGRESS: 'A sync is already running for this connector -- wait for it to finish before starting another.',
    '401': 'Your session is no longer valid. Sign in again.',
    '403': 'You are not permitted to manage personal data in this workspace.',
  })
}

export function formatTimestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never'
}
