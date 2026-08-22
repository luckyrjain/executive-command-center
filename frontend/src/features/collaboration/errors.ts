import { apiErrorMessage } from '../../api/errorMessage'

/** Mirrors `features/personal/errors.ts`'s own shape -- one mapping
 * function for every error code the `ecc.platform.authz` sharing router
 * can return, rather than repeating a switch per call site.
 */
export function sharingErrorMessage(error: unknown): string {
  return apiErrorMessage(error, {
    RESOURCE_TYPE_NOT_GRANTABLE: 'This kind of resource cannot be shared.',
    RESOURCE_NOT_FOUND: 'That resource does not exist, or you cannot see it.',
    GRANTEE_NOT_FOUND: 'That account is not an active member of this workspace.',
    GRANT_REQUIRES_NARROW_VISIBILITY: 'This resource is visible to the whole workspace today. Preview again and confirm narrowing before sharing.',
    GRANT_NOT_FOUND: 'This grant no longer exists.',
    GRANT_ALREADY_REVOKED: 'This grant was already revoked.',
    INSUFFICIENT_ROLE: 'You are not permitted to manage sharing for this resource.',
    '401': 'Your session is no longer valid. Sign in again.',
    '403': 'You are not permitted to manage sharing for this resource.',
  })
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
  return apiErrorMessage(error, {
    WORKSPACE_NOT_FOUND: 'This workspace could not be found.',
    MEMBER_NOT_FOUND: 'That member could not be found.',
    LAST_OWNER_CANNOT_BE_DEMOTED: 'This is the workspace\'s only owner -- promote another member to owner first.',
    LAST_OWNER_CANNOT_BE_REMOVED: 'This is the workspace\'s only owner -- promote another member to owner first.',
    OWNED_RESOURCES_BLOCK_REMOVAL: 'This member still owns workspace resources. Transfer ownership of each one before removing them.',
    ALREADY_MEMBER: 'That person is already a member of this workspace.',
    INVITATION_ALREADY_PENDING: 'An invitation is already pending for that email.',
    INVITATION_NOT_FOUND: 'This invitation no longer exists.',
    INVITATION_ALREADY_RESOLVED: 'This invitation was already accepted, rejected or revoked.',
    INVITATION_TOKEN_INVALID: 'That invitation ID and token don\'t match. Double-check both and try again.',
    INVITATION_EXPIRED: 'This invitation has expired. Ask for a new one.',
    INVITATION_EMAIL_MISMATCH: 'This invitation was sent to a different email address than your account\'s.',
    INVITER_NO_LONGER_AUTHORIZED: 'The person who sent this invitation no longer has the authority to grant that role. Ask an owner to invite you again.',
    DELEGATION_NOT_FOUND: 'This delegation no longer exists, or you cannot see it.',
    DELEGATION_NOT_PROPOSED: 'This delegation is no longer awaiting a response.',
    DELEGATION_NOT_ACCEPTED: 'This delegation has not been accepted.',
    OBLIGATION_NOT_FOUND: 'That obligation resource does not exist, or you cannot see it.',
    EVIDENCE_NOT_FOUND: 'One of the named evidence resources does not exist, or you cannot see it.',
    RECIPIENT_NOT_FOUND: 'That account is not an active member of this workspace.',
    CANNOT_DELEGATE_TO_SELF: 'You cannot delegate to yourself.',
    DUE_AT_IN_PAST: 'The due date must be in the future.',
    RESOURCE_TYPE_NOT_GRANTABLE: 'This kind of resource cannot be shared or transferred.',
    RESOURCE_NOT_FOUND: 'That resource does not exist, or you cannot see it.',
    NOTIFICATION_NOT_FOUND: 'This notification no longer exists.',
    INSUFFICIENT_ROLE: 'You are not permitted to do that.',
    '401': 'Your session is no longer valid. Sign in again.',
    '403': 'You are not permitted to do that.',
  })
}
