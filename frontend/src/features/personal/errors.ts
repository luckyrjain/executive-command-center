import { ApiError } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'

/** Lead sentence for the caller's own `EMAIL_CONSENT_NOT_ACTIVE`. (The
 * recommendation review panel words its confirm refusal separately: there
 * the check is the email owner's consent and Gmail connection, not the
 * caller's.) */
export const EMAIL_CONSENT_NOT_ACTIVE_MESSAGE = 'Email consent is not active.'

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
    EMAIL_CONSENT_NOT_ACTIVE: `${EMAIL_CONSENT_NOT_ACTIVE_MESSAGE} Enable the email domain and grant consent to view Gmail data.`,
    THREAD_NOT_FOUND: 'This thread no longer exists.',
    GMAIL_ACCOUNT_NOT_ALLOWLISTED: 'This Google account is not on the internal allowlist for Gmail access.',
    GMAIL_OAUTH_NOT_CONFIGURED: 'Gmail OAuth is not configured for this deployment.',
    GMAIL_OAUTH_STATE_INVALID: 'This Gmail sign-in link expired or was already used. Start again.',
    GMAIL_OAUTH_DENIED: 'Google sign-in was cancelled. Click Connect Gmail to try again.',
    GMAIL_OAUTH_FAILED: 'Google rejected this sign-in attempt. Try again.',
    // Spec A codes (`gmail_oauth.py`'s callback, also reaching the OAuth
    // return banner via `?gmail=error&code=`, and the sync route).
    GMAIL_ACCOUNT_IDENTITY_MISMATCH: 'The Google account you chose isn\'t the one signed in to ECC. Nothing was connected -- connect again and choose the Google account you use to sign in to ECC.',
    GMAIL_ACCOUNT_ALREADY_CONNECTED: 'This Google account is already connected in this workspace.',
    CONNECTOR_OWNED_BY_ANOTHER_MEMBER: 'Another member of this workspace has already connected this Google account, so you cannot connect it. Ask that member or a workspace admin if you need it here.',
    CONNECTOR_ACCOUNT_PERSIST_FAILED: 'Gmail could not be saved because of a server error. Nothing was connected -- try again.',
    MEMBERSHIP_INACTIVE: 'Your membership in this workspace is no longer active, so this was stopped and nothing was saved. Ask a workspace owner if you think this is a mistake.',
    GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT: 'Use the email domain\'s disable action to disconnect Gmail, not the generic connector action.',
    CONNECTOR_NOT_FOUND: 'This connector no longer exists in this workspace.',
    CONNECTOR_DISCONNECTED: 'This connector is already disconnected.',
    CONNECTOR_SYNC_IN_PROGRESS: 'A sync is already running for this connector -- wait for it to finish before starting another.',
    '401': 'Your session is no longer valid. Sign in again.',
    '403': 'You are not permitted to manage personal data in this workspace.',
  })
}

/** The Gmail OAuth return banner (`?gmail=error&code=`). Only a code
 * reaches it, rebuilt as `ApiError(0, code)`, so status-keyed overrides
 * never match. `INSUFFICIENT_ROLE` there means the caller's role was lowered
 * below write during the sign-in round trip (`gmail_oauth.py`), so it gets
 * connect-specific copy -- kept out of `personalErrorMessage`, which every
 * personal panel shares and where a 403 `INSUFFICIENT_ROLE` must keep the
 * generic '403' wording. */
export const GMAIL_CONNECT_ROLE_REFUSED_MESSAGE =
  'Your workspace role does not allow connecting Gmail, so nothing was connected. Ask a workspace admin for access.'

export function gmailOAuthReturnErrorMessage(code: string): string {
  if (code === 'INSUFFICIENT_ROLE') return GMAIL_CONNECT_ROLE_REFUSED_MESSAGE
  return personalErrorMessage(new ApiError(0, code, code))
}

export function formatTimestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never'
}
