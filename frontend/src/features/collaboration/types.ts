export type Action = 'read' | 'write'

export type Grant = {
  id: string
  resource_type: string
  resource_id: string
  grantee_account_id: string
  actions: Action[]
  granted_by: string
  expires_at: string | null
  revoked_at: string | null
  created_at: string
}

export type GrantListResponse = { grants: Grant[] }

export type GrantPreview = {
  resource_type: string
  resource_id: string
  grantee_account_id: string
  current_visibility: string
  proposed_visibility: string
  requires_narrow_visibility_confirmation: boolean
  members_losing_default_access: string[]
  grantee_already_has_access: boolean
  grantee_gains_actions: Action[]
}

export type Role = 'owner' | 'admin' | 'member' | 'viewer'

export type Workspace = {
  id: string
  name: string
  timezone: string
  role: string
  created_at: string
  current: boolean
}

export type WorkspaceListResponse = { workspaces: Workspace[] }

export type Member = {
  user_id: string
  account_id: string
  email: string
  display_name: string
  role: string
  status: string
  created_at: string
  removed_at: string | null
}

export type MemberListResponse = { members: Member[] }

export type MemberExportSnapshot = {
  account_id: string
  email: string
  display_name: string
  role: string
  joined_at: string
  removed_at: string
}

export type MemberRemovalResponse = { user_id: string; export: MemberExportSnapshot }

export type OwnedResourceSummaryItem = { resource_type: string; count: number }

export type Invitation = {
  id: string
  email: string
  role: string
  expires_at: string
  accepted_at: string | null
  rejected_at: string | null
  revoked_at: string | null
  created_at: string
}

export type InvitationListResponse = { invitations: Invitation[] }

export type InvitationCreateResponse = {
  id: string
  email: string
  role: string
  token: string
  expires_at: string
  created_at: string
}

export type EvidenceRef = { resource_type: string; resource_id: string }

export type DelegationStatus =
  | 'proposed'
  | 'accepted'
  | 'rejected'
  | 'expired'
  | 'completed'
  | 'revoked'
  | 'cancelled'

export type Delegation = {
  id: string
  delegator_account_id: string
  recipient_account_id: string
  obligation_type: string
  obligation_resource_id: string
  expected_outcome: string
  due_at: string
  status: DelegationStatus
  evidence: EvidenceRef[]
  created_at: string
  updated_at: string
}

export type DelegationListResponse = { delegations: Delegation[] }

export type ActivityItem = {
  id: string
  event_type: string
  aggregate_type: string
  aggregate_id: string
  actor_id: string | null
  occurred_at: string
}

export type ActivityListResponse = { items: ActivityItem[]; next_cursor: string | null }

export type OwnershipTransfer = {
  id: string
  resource_type: string
  resource_id: string
  from_account_id: string
  to_account_id: string
  status: string
  initiated_by: string
  created_at: string
  completed_at: string | null
}

