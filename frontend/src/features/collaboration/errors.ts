import { ApiError } from '../../api/client'

/** Mirrors `features/personal/errors.ts`'s own shape -- one mapping
 * function for every error code the `ecc.platform.authz` sharing router
 * can return, rather than repeating a switch per call site.
 */
export function sharingErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CSRF_TOKEN_REQUIRED' || error.code === 'CSRF_TOKEN_INVALID') return 'Your session\'s security token is missing or stale. Reload the page and try again.'
  if (error.code === 'RESOURCE_TYPE_NOT_GRANTABLE') return 'This kind of resource cannot be shared.'
  if (error.code === 'RESOURCE_NOT_FOUND') return 'That resource does not exist, or you cannot see it.'
  if (error.code === 'GRANTEE_NOT_FOUND') return 'That account is not an active member of this workspace.'
  if (error.code === 'GRANT_REQUIRES_NARROW_VISIBILITY') return 'This resource is visible to the whole workspace today. Preview again and confirm narrowing before sharing.'
  if (error.code === 'GRANT_NOT_FOUND') return 'This grant no longer exists.'
  if (error.code === 'GRANT_ALREADY_REVOKED') return 'This grant was already revoked.'
  if (error.code === 'INSUFFICIENT_ROLE') return 'You are not permitted to manage sharing for this resource.'
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to manage sharing for this resource.'
  return error.message
}

export function formatTimestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'never'
}
