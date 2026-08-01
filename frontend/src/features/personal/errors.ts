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
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to manage personal data in this workspace.'
  return error.message
}

export function formatTimestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never'
}
