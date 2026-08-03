import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { Workspace, WorkspaceListResponse } from './types'

/** Shared by `WorkspaceSwitcher.tsx` and `MembersPanel.tsx` -- both need to
 * know the account's full workspace list and which one the current session
 * is scoped to. `GET /identity/workspaces` has no separate `/me`/`/whoami`
 * endpoint alongside it (`accounts.py`'s own `WorkspaceResponse.current`
 * field, added for exactly this task, is the only signal). React Query
 * dedupes both callers against the same `queryKey`, so this is one network
 * request shared across however many components mount it, not two.
 */
export function useWorkspaces() {
  return useQuery({
    queryKey: ['identity', 'workspaces'],
    queryFn: () => apiRequest<WorkspaceListResponse>('/api/v1/identity/workspaces'),
    retry: 1,
  })
}

export function currentWorkspace(data: WorkspaceListResponse | undefined): Workspace | undefined {
  return data?.workspaces.find((workspace) => workspace.current)
}
