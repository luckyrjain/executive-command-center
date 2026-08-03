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

/** Covers `ecc.domains.identity.membership_removal`, `ecc.domains.identity.
 * invitations`, `ecc.domains.collaboration.delegations` and `ecc.platform.
 * authz`'s ownership-transfer endpoints -- one mapping for every panel this
 * task adds, since they share the same generic codes
 * (`INSUFFICIENT_ROLE`/`*_NOT_FOUND`/offline/CSRF) plus a handful of
 * endpoint-specific ones. `sharingErrorMessage` above stays untouched and
 * `SharingReview.tsx` keeps using it -- this is additive, not a rename.
 */
export function collaborationErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CSRF_TOKEN_REQUIRED' || error.code === 'CSRF_TOKEN_INVALID') return 'Your session\'s security token is missing or stale. Reload the page and try again.'
  if (error.code === 'WORKSPACE_NOT_FOUND') return 'This workspace could not be found.'
  if (error.code === 'MEMBER_NOT_FOUND') return 'That member could not be found.'
  if (error.code === 'LAST_OWNER_CANNOT_BE_DEMOTED') return 'This is the workspace\'s only owner -- promote another member to owner first.'
  if (error.code === 'LAST_OWNER_CANNOT_BE_REMOVED') return 'This is the workspace\'s only owner -- promote another member to owner first.'
  if (error.code === 'OWNED_RESOURCES_BLOCK_REMOVAL') return 'This member still owns workspace resources. Transfer ownership of each one before removing them.'
  if (error.code === 'ALREADY_MEMBER') return 'That person is already a member of this workspace.'
  if (error.code === 'INVITATION_ALREADY_PENDING') return 'An invitation is already pending for that email.'
  if (error.code === 'INVITATION_NOT_FOUND') return 'This invitation no longer exists.'
  if (error.code === 'INVITATION_ALREADY_RESOLVED') return 'This invitation was already accepted, rejected or revoked.'
  if (error.code === 'DELEGATION_NOT_FOUND') return 'This delegation no longer exists, or you cannot see it.'
  if (error.code === 'DELEGATION_NOT_PROPOSED') return 'This delegation is no longer awaiting a response.'
  if (error.code === 'DELEGATION_NOT_ACCEPTED') return 'This delegation has not been accepted.'
  if (error.code === 'OBLIGATION_NOT_FOUND') return 'That obligation resource does not exist, or you cannot see it.'
  if (error.code === 'EVIDENCE_NOT_FOUND') return 'One of the named evidence resources does not exist, or you cannot see it.'
  if (error.code === 'RECIPIENT_NOT_FOUND') return 'That account is not an active member of this workspace.'
  if (error.code === 'CANNOT_DELEGATE_TO_SELF') return 'You cannot delegate to yourself.'
  if (error.code === 'DUE_AT_IN_PAST') return 'The due date must be in the future.'
  if (error.code === 'RESOURCE_TYPE_NOT_GRANTABLE') return 'This kind of resource cannot be shared or transferred.'
  if (error.code === 'RESOURCE_NOT_FOUND') return 'That resource does not exist, or you cannot see it.'
  if (error.code === 'NOTIFICATION_NOT_FOUND') return 'This notification no longer exists.'
  if (error.code === 'INSUFFICIENT_ROLE') return 'You are not permitted to do that.'
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to do that.'
  return error.message
}
