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

export type EffectivePermissions = {
  resource_type: string
  resource_id: string
  visibility: string
  owner_id: string
  is_owner: boolean
  role: string
  via: 'owner' | 'workspace_role' | 'resource_grant'
  granted_actions: Action[]
}
