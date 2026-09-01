import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest, ApiError } from '../../api/client'
import { collaborationErrorMessage, formatTimestamp } from './errors'
import { useMe } from './useMe'
import { currentWorkspace, useWorkspaces } from './useWorkspaces'
import type {
  Invitation,
  InvitationCreateResponse,
  InvitationListResponse,
  Member,
  MemberExportSnapshot,
  MemberListResponse,
  MemberRemovalResponse,
  OwnedResourceSummaryItem,
  Role,
} from './types'

const ROLES: Role[] = ['owner', 'admin', 'member', 'viewer']

function invitationStatus(invitation: Invitation, now: Date): string {
  if (invitation.revoked_at) return 'Revoked'
  if (invitation.rejected_at) return 'Rejected'
  if (invitation.accepted_at) return 'Accepted'
  if (new Date(invitation.expires_at).getTime() <= now.getTime()) return 'Expired'
  return 'Pending'
}

function ownedResources(error: unknown): OwnedResourceSummaryItem[] | null {
  if (!(error instanceof ApiError) || error.code !== 'OWNED_RESOURCES_BLOCK_REMOVAL') return null
  const current = error.current
  if (!current || typeof current !== 'object' || !('owned_resources' in current)) return null
  return (current as { owned_resources: OwnedResourceSummaryItem[] }).owned_resources
}

// The Remove button is shown when the caller can manage the workspace
// (`owner`/`admin`), OR for the caller's own row -- `membership_removal.py`'s
// `remove_member_endpoint` docstring: "a member may also remove themselves
// (self-service leave)". `member.user_id` is the workspace-scoped
// `workspace_memberships.users_id`/`sessions.user_id`, the same identifier
// `remove_member_endpoint`'s own `is_self = user_id == auth.user_id` check
// compares against server-side -- so `myUsersId` (from `GET /identity/me`,
// not `myAccountId`) is the correct comparison here.
function MemberRow({
  member,
  workspaceId,
  myRole,
  myUsersId,
  isSoleActiveOwner,
  onChanged,
  onRemoved,
}: {
  member: Member
  workspaceId: string
  myRole: string
  myUsersId: string | undefined
  isSoleActiveOwner: boolean
  onChanged: () => void
  onRemoved: (snapshot: MemberExportSnapshot) => void
}) {
  const queryClient = useQueryClient()
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [transferForm, setTransferForm] = useState({ resourceType: '', resourceId: '', toAccountId: '' })
  const removeTriggerRef = useRef<HTMLButtonElement>(null)
  const wasConfirmingRef = useRef(false)
  // Found in the third whole-phase review: `confirmRemove` also flips back
  // to `false` on the *success* path (`removeMutation`'s own `onSuccess`
  // below), not only on Cancel -- the effect below could not tell the two
  // apart and restored focus to the trigger button after a successful
  // removal too, moments before this entire row unmounts once `onChanged`'s
  // refetch drops it from the members list, dropping focus to `<body>`.
  // This ref is the one piece of state that distinguishes them: only the
  // success path sets it, and the effect clears it again on every run so a
  // later Cancel is never spuriously suppressed by an earlier removal.
  const removedSuccessfullyRef = useRef(false)
  // The trigger button (`ref={removeTriggerRef}` below) unmounts the
  // instant `confirmRemove` becomes `true` -- `canRemove && !confirmRemove`
  // guards its own JSX block -- so a plain `removeTriggerRef.current?.
  // focus()` call made from the Cancel button's own onClick (still inside
  // the *old*, about-to-unmount confirm block) would find `current` already
  // `null`. Waiting for the post-render effect below means the trigger
  // button has already remounted and populated the ref by the time this
  // runs. Found (and this exact wrong-first-attempt caught) during the
  // second whole-phase review's own frontend fixes.
  useEffect(() => {
    if (wasConfirmingRef.current && !confirmRemove && !removedSuccessfullyRef.current) {
      removeTriggerRef.current?.focus()
    }
    wasConfirmingRef.current = confirmRemove
    removedSuccessfullyRef.current = false
  }, [confirmRemove])
  const canManage = myRole === 'owner' || myRole === 'admin'
  const isSelf = member.user_id === myUsersId
  const canRemove = canManage || isSelf
  // Mirrors `update_member_role_endpoint`'s own owner-only-can-grant-owner
  // guard (`membership_removal.py`) -- an admin selecting "owner" here
  // would only ever get a 403 back, so the option is hidden rather than
  // offered and rejected.
  const selectableRoles = myRole === 'owner' ? ROLES : ROLES.filter((role) => role !== 'owner')
  // Found in the fourth whole-phase review: `membership_removal.py`'s own
  // `_is_sole_active_owner` guard unconditionally rejects a sole active
  // owner's own role change or removal (409 `LAST_OWNER_CANNOT_BE_
  // DEMOTED`/`LAST_OWNER_CANNOT_BE_REMOVED`) -- this can only ever be a
  // self-targeting case (if there is exactly one active owner and the
  // caller holds the `owner` role, that owner IS the caller). The role
  // select and the remove/leave trigger below both disable themselves for
  // this one row rather than let the user hit a guaranteed 409, mirroring
  // `selectableRoles`' own established discipline of not offering an
  // action the backend will definitely reject.
  const isSelfSoleActiveOwner = isSelf && isSoleActiveOwner

  const roleMutation = useMutation({
    mutationFn: (role: Role) =>
      apiRequest<Member>(`/api/v1/identity/workspaces/${workspaceId}/members/${member.user_id}`, {
        method: 'PATCH',
        body: { role },
      }),
    onSuccess: () => {
      onChanged()
      // A role change to my own row can flip `canManage` (e.g. an owner
      // demoting themselves) -- `myRole` is derived from the separate
      // `['identity','workspaces']` query, which `onChanged` (scoped to
      // `['identity','members', workspaceId]`) never touches. Without
      // this, the panel would keep offering admin-only actions (the
      // Invitations section, other rows' Role selects) to someone who no
      // longer holds that role until an unrelated refetch happened to
      // occur.
      if (isSelf) void queryClient.invalidateQueries({ queryKey: ['identity', 'workspaces'] })
    },
  })

  const removeMutation = useMutation({
    mutationFn: () =>
      apiRequest<MemberRemovalResponse>(
        `/api/v1/identity/workspaces/${workspaceId}/members/${member.user_id}`,
        { method: 'DELETE' },
      ),
    onSuccess: (data) => {
      // Removing myself revokes my own session in the same backend
      // transaction (`membership_removal.py`'s own "second, independent
      // propagation path" for session revocation) -- the very
      // `onChanged()` refetch below would otherwise run against an
      // already-dead session and surface as a confusing 401, rather than
      // taking the user back to a real, logged-out-looking state.
      if (isSelf) {
        window.location.reload()
        return
      }
      removedSuccessfullyRef.current = true
      setConfirmRemove(false)
      onChanged()
      onRemoved(data.export)
    },
  })

  const transferMutation = useMutation({
    mutationFn: (variables: { resourceType: string; resourceId: string; toAccountId: string }) =>
      apiRequest('/api/v1/ownership/transfers', {
        method: 'POST',
        body: {
          resource_type: variables.resourceType.trim(),
          resource_id: variables.resourceId.trim(),
          to_account_id: variables.toAccountId.trim(),
        },
      }),
    // Deliberately does NOT call `removeMutation.reset()` -- a real bug
    // found in the second whole-phase review: `blocked` (below) is derived
    // from `removeMutation.error`, so resetting it here immediately made
    // `blocked` `null` and the entire owned-resources panel -- including
    // this success message -- unmount in the same render, before the user
    // ever saw it. `removeMutation`'s own next `.mutate()` call (from
    // clicking "Confirm removal" again) already transitions it into a
    // fresh attempt and clears the stale error on its own; nothing here
    // needs to force that early.
    onSuccess: () => {
      setTransferForm({ resourceType: '', resourceId: '', toAccountId: '' })
    },
  })

  const blocked = ownedResources(removeMutation.error)

  return (
    <li>
      <div>
        <strong>{member.display_name}</strong>
        <small> · {member.email} · account {member.account_id}</small>
      </div>
      <label>
        Role
        <select
          aria-label={`Role for ${member.display_name}`}
          value={member.role}
          disabled={!canManage || roleMutation.isPending || isSelfSoleActiveOwner}
          onChange={(event) => roleMutation.mutate(event.target.value as Role)}
        >
          {selectableRoles.map((role) => <option key={role} value={role}>{role}</option>)}
        </select>
      </label>
      {roleMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(roleMutation.error)}</div> : null}

      {isSelfSoleActiveOwner ? (
        <p className="inline-status">You're the only owner, so you can't leave or change your own role. Promote another member to owner first.</p>
      ) : null}

      {canRemove && !confirmRemove && !isSelfSoleActiveOwner ? (
        <div className="work-actions">
          <button type="button" ref={removeTriggerRef} onClick={() => setConfirmRemove(true)}>
            {isSelf ? 'Leave workspace' : 'Remove'}
          </button>
        </div>
      ) : null}

      {confirmRemove ? (
        <div className="degraded-panel" role="group" aria-label={`Confirm removing ${member.display_name}`}>
          <p>
            {isSelf
              ? 'Leaving ends your access immediately and revokes your active sessions. This cannot be undone.'
              : `Removing ${member.display_name} ends their access immediately and revokes their active sessions. This cannot be undone.`}
          </p>
          <div className="work-actions">
            <button type="button" className="btn-destructive" disabled={removeMutation.isPending} onClick={() => removeMutation.mutate()}>
              {removeMutation.isPending ? 'Removing…' : 'Confirm removal'}
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(false)
                removeMutation.reset()
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {removeMutation.isError && !blocked ? (
        <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(removeMutation.error)}</div>
      ) : null}

      {blocked ? (
        <div className="degraded-panel" role="alert">
          <p>{member.display_name} still owns workspace resources and cannot be removed until each is transferred to someone else:</p>
          <ul>
            {blocked.map((item) => <li key={item.resource_type}>{item.count} × {item.resource_type}</li>)}
          </ul>
          <form
            className="field-form"
            onSubmit={(event) => {
              event.preventDefault()
              transferMutation.mutate(transferForm)
            }}
          >
            <label>Resource type
              <input aria-label="Resource type to transfer" value={transferForm.resourceType} onChange={(event) => setTransferForm((f) => ({ ...f, resourceType: event.target.value }))} />
            </label>
            <label>Resource ID
              <input aria-label="Resource ID to transfer" value={transferForm.resourceId} onChange={(event) => setTransferForm((f) => ({ ...f, resourceId: event.target.value }))} />
            </label>
            <label>Transfer to (account ID)
              <input aria-label="Transfer to account ID" value={transferForm.toAccountId} onChange={(event) => setTransferForm((f) => ({ ...f, toAccountId: event.target.value }))} />
            </label>
            <div className="work-actions">
              <button type="submit" disabled={transferMutation.isPending}>Transfer ownership</button>
            </div>
          </form>
          {transferMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(transferMutation.error)}</div> : null}
          {transferMutation.isSuccess ? <p role="status" className="inline-status">Transferred. Try removal again.</p> : null}
        </div>
      ) : null}
    </li>
  )
}

function InvitationRow({ invitation, onChanged }: { invitation: Invitation; onChanged: () => void }) {
  const now = new Date()
  const invStatus = invitationStatus(invitation, now)
  const revokeMutation = useMutation({
    mutationFn: () =>
      apiRequest(`/api/v1/identity/invitations/${invitation.id}/revoke`, { method: 'POST' }),
    onSuccess: onChanged,
  })

  return (
    <li>
      <div>
        <strong>{invitation.email}</strong>
        <small> · role {invitation.role}</small>
      </div>
      <div role="status" className={invStatus === 'Pending' ? 'inline-status' : 'inline-status degraded-panel'}>
        {invStatus} · expires {formatTimestamp(invitation.expires_at)}
      </div>
      {invStatus === 'Pending' ? (
        <div className="work-actions">
          <button type="button" className="btn-destructive" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>Revoke</button>
        </div>
      ) : null}
      {revokeMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(revokeMutation.error)}</div> : null}
    </li>
  )
}

/**
 * Accepting an invitation is a property of the logged-in *account*, not of
 * the workspace currently in view -- the recipient of an invitation to
 * workspace B is very often sitting in workspace A's collaboration tab
 * when they receive the token out of band (there is no email delivery or
 * in-app notification for invitations in this build; `POST
 * /identity/workspaces/{id}/invitations`'s own response is the only place
 * the raw token is ever returned, exactly once). Rather than build a
 * second top-level surface for this one form, it lives here, always
 * rendered regardless of `canManage`, since accepting an invitation
 * requires no role in the workspace you are about to join.
 */
function AcceptInvitationForm({ onAccepted }: { onAccepted: () => void }) {
  const [form, setForm] = useState({ invitationId: '', token: '' })
  const acceptMutation = useMutation({
    mutationFn: (variables: typeof form) =>
      apiRequest(`/api/v1/identity/invitations/${variables.invitationId.trim()}/accept`, {
        method: 'POST',
        body: { token: variables.token.trim() },
      }),
    onSuccess: () => {
      setForm({ invitationId: '', token: '' })
      onAccepted()
    },
  })

  return (
    <div>
      <h3 id="accept-invitation-title">Accept an invitation</h3>
      <p>Have an invitation ID and token from another workspace? Accept it here, then use the workspace switcher above to open it.</p>
      <form
        className="field-form"
        aria-labelledby="accept-invitation-title"
        onSubmit={(event) => {
          event.preventDefault()
          acceptMutation.mutate(form)
        }}
      >
        <label>Invitation ID
          <input aria-label="Invitation ID" value={form.invitationId} onChange={(event) => setForm((f) => ({ ...f, invitationId: event.target.value }))} />
        </label>
        <label>Token
          <input aria-label="Invitation token" value={form.token} onChange={(event) => setForm((f) => ({ ...f, token: event.target.value }))} />
        </label>
        <div className="work-actions">
          <button type="submit" disabled={acceptMutation.isPending || form.invitationId.trim() === '' || form.token.trim() === ''}>
            {acceptMutation.isPending ? 'Accepting…' : 'Accept invitation'}
          </button>
        </div>
      </form>
      {acceptMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(acceptMutation.error)}</div> : null}
      {acceptMutation.isSuccess ? <p role="status" className="inline-status">Joined. Use the workspace switcher above to open it.</p> : null}
    </div>
  )
}

const EMPTY_INVITE_FORM = { email: '', role: 'member' as Role }

export default function MembersPanel() {
  const queryClient = useQueryClient()
  const workspaces = useWorkspaces()
  const me = useMe()
  const workspace = currentWorkspace(workspaces.data)
  const workspaceId = workspace?.id
  const myRole = workspace?.role ?? 'viewer'
  const canManage = myRole === 'owner' || myRole === 'admin'
  const [inviteForm, setInviteForm] = useState(EMPTY_INVITE_FORM)
  const [lastInvite, setLastInvite] = useState<InvitationCreateResponse | null>(null)
  // `membership_removal.py`'s own docstring: `MemberRemovalResponse.export`
  // exists specifically so the removing admin gets an administrative
  // record of who was removed, in the same response that finalizes
  // removal -- the one chance to see it, since the row is gone from the
  // members list the moment this refetches. Found discarded (fetched,
  // never rendered) in the second whole-phase review.
  const [removedSnapshots, setRemovedSnapshots] = useState<MemberExportSnapshot[]>([])

  const members = useQuery({
    queryKey: ['identity', 'members', workspaceId],
    queryFn: () => apiRequest<MemberListResponse>(`/api/v1/identity/workspaces/${workspaceId}/members`),
    enabled: !!workspaceId,
    retry: 1,
  })

  const invitations = useQuery({
    queryKey: ['identity', 'invitations', workspaceId],
    queryFn: () => apiRequest<InvitationListResponse>(`/api/v1/identity/workspaces/${workspaceId}/invitations`),
    enabled: !!workspaceId && canManage,
    retry: 1,
  })

  const inviteMutation = useMutation({
    mutationFn: (variables: typeof inviteForm) =>
      apiRequest<InvitationCreateResponse>(`/api/v1/identity/workspaces/${workspaceId}/invitations`, {
        method: 'POST',
        body: { email: variables.email.trim(), role: variables.role },
      }),
    onSuccess: (invitation) => {
      setInviteForm(EMPTY_INVITE_FORM)
      setLastInvite(invitation)
      void queryClient.invalidateQueries({ queryKey: ['identity', 'invitations', workspaceId] })
    },
  })

  function refreshMembers() {
    void queryClient.invalidateQueries({ queryKey: ['identity', 'members', workspaceId] })
  }

  function refreshInvitations() {
    void queryClient.invalidateQueries({ queryKey: ['identity', 'invitations', workspaceId] })
  }

  const memberItems = members.data?.members ?? []
  const invitationItems = invitations.data?.invitations ?? []
  // Found in the fourth whole-phase review: feeds `MemberRow`'s own
  // `isSoleActiveOwner` guard (see that component's comment for the full
  // reasoning) -- `memberItems` only ever contains `active` memberships
  // (`list_members_endpoint`'s own `WHERE wm.status = 'active'`), so this
  // is exactly the same count `membership_removal.py`'s own `_is_sole_
  // active_owner` computes server-side.
  const activeOwnerCount = memberItems.filter((member) => member.role === 'owner').length

  return (
    <section className="work-panel" aria-labelledby="members-title">
      <h2 id="members-title">Members</h2>
      <p>Everyone in this workspace, their role, and pending invitations. Removing a member first requires transferring any workspace resources they still own.</p>

      {workspaces.isLoading ? <p role="status">Loading workspace…</p> : null}
      {workspaces.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(workspaces.error)}</div> : null}

      {/* `me` feeds every row's self-removal affordance (`myUsersId`
          below) -- surfacing its own loading/error state, rather than
          letting a failed `GET /identity/me` silently leave every row's
          "Leave workspace" button permanently unreachable with no
          explanation, matches DelegationsPanel.tsx's identical `useMe()`
          usage. */}
      {me.isLoading ? <p role="status">Loading your account…</p> : null}
      {me.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(me.error)}</div> : null}

      {members.isLoading ? <p role="status">Loading members…</p> : null}
      {members.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(members.error)}</div> : null}
      {members.data && memberItems.length === 0 ? <p className="empty-state">No active members.</p> : null}

      {removedSnapshots.map((snapshot) => (
        <div key={`${snapshot.account_id}-${snapshot.removed_at}`} role="status" className="inline-status">
          <p>
            Removed {snapshot.display_name} ({snapshot.email}) -- was {snapshot.role} since{' '}
            {formatTimestamp(snapshot.joined_at)}, removed {formatTimestamp(snapshot.removed_at)}.
          </p>
          <button
            type="button"
            onClick={() =>
              setRemovedSnapshots((current) => current.filter((s) => s !== snapshot))
            }
          >
            Dismiss
          </button>
        </div>
      ))}

      <ul className="work-list" aria-label="Members">
        {memberItems.map((member) => (
          <MemberRow
            key={member.user_id}
            member={member}
            workspaceId={workspaceId ?? ''}
            myRole={myRole}
            myUsersId={me.data?.users_id}
            isSoleActiveOwner={member.role === 'owner' && activeOwnerCount === 1}
            onChanged={refreshMembers}
            onRemoved={(snapshot) => setRemovedSnapshots((current) => [...current, snapshot])}
          />
        ))}
      </ul>

      {canManage ? (
        <>
          <hr />
          <h3 id="invitations-title">Invitations</h3>
          <form
            className="field-form"
            aria-labelledby="invitations-title"
            onSubmit={(event) => {
              event.preventDefault()
              inviteMutation.mutate(inviteForm)
            }}
          >
            <label>Email
              <input aria-label="Invitee email" type="email" value={inviteForm.email} onChange={(event) => setInviteForm((f) => ({ ...f, email: event.target.value }))} />
            </label>
            <label>Role
              <select aria-label="Invitee role" value={inviteForm.role} onChange={(event) => setInviteForm((f) => ({ ...f, role: event.target.value as Role }))}>
                {ROLES.map((role) => <option key={role} value={role}>{role}</option>)}
              </select>
            </label>
            <div className="work-actions">
              <button type="submit" disabled={inviteMutation.isPending || inviteForm.email.trim() === ''}>Invite</button>
            </div>
          </form>
          {inviteMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(inviteMutation.error)}</div> : null}
          {lastInvite ? (
            <p role="status" className="inline-status">
              Invitation sent. Share this token with {lastInvite.email} now -- it will not be shown again: <code>{lastInvite.id}</code> / <code>{lastInvite.token}</code>
            </p>
          ) : null}

          {invitations.isLoading ? <p role="status">Loading invitations…</p> : null}
          {invitations.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(invitations.error)}</div> : null}
          {invitations.data && invitationItems.length === 0 ? <p className="empty-state">No invitations yet.</p> : null}

          <ul className="work-list" aria-label="Invitations">
            {invitationItems.map((invitation) => (
              <InvitationRow key={invitation.id} invitation={invitation} onChanged={refreshInvitations} />
            ))}
          </ul>
        </>
      ) : null}

      <hr />
      <AcceptInvitationForm onAccepted={() => void queryClient.invalidateQueries({ queryKey: ['identity', 'workspaces'] })} />
    </section>
  )
}
