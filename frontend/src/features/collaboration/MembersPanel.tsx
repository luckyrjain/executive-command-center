import { useState } from 'react'
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
  onChanged,
}: {
  member: Member
  workspaceId: string
  myRole: string
  myUsersId: string | undefined
  onChanged: () => void
}) {
  const queryClient = useQueryClient()
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [transferForm, setTransferForm] = useState({ resourceType: '', resourceId: '', toAccountId: '' })
  const canManage = myRole === 'owner' || myRole === 'admin'
  const isSelf = member.user_id === myUsersId
  const canRemove = canManage || isSelf

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
    onSuccess: () => {
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
      setConfirmRemove(false)
      onChanged()
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
    onSuccess: () => {
      setTransferForm({ resourceType: '', resourceId: '', toAccountId: '' })
      removeMutation.reset()
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
          disabled={!canManage || roleMutation.isPending}
          onChange={(event) => roleMutation.mutate(event.target.value as Role)}
        >
          {ROLES.map((role) => <option key={role} value={role}>{role}</option>)}
        </select>
      </label>
      {roleMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(roleMutation.error)}</div> : null}

      {canRemove && !confirmRemove ? (
        <div className="work-actions">
          <button type="button" onClick={() => setConfirmRemove(true)}>
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
            <button type="button" disabled={removeMutation.isPending} onClick={() => removeMutation.mutate()}>
              {removeMutation.isPending ? 'Removing…' : 'Confirm removal'}
            </button>
            <button type="button" onClick={() => { setConfirmRemove(false); removeMutation.reset() }}>Cancel</button>
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
          <button type="button" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>Revoke</button>
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
    <div className="work-panel">
      <h3 id="accept-invitation-title">Accept an invitation</h3>
      <p>Have an invitation ID and token from another workspace? Accept it here, then use the workspace switcher above to open it.</p>
      <form
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

      <ul className="work-list" aria-label="Members">
        {memberItems.map((member) => (
          <MemberRow
            key={member.user_id}
            member={member}
            workspaceId={workspaceId ?? ''}
            myRole={myRole}
            myUsersId={me.data?.users_id}
            onChanged={refreshMembers}
          />
        ))}
      </ul>

      {canManage ? (
        <>
          <hr />
          <h3 id="invitations-title">Invitations</h3>
          <form
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
