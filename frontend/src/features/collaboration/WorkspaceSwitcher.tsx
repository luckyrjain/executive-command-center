import { useMutation } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { collaborationErrorMessage } from './errors'
import { currentWorkspace, useWorkspaces } from './useWorkspaces'

/** Mounted once, globally, in `App.tsx` -- above `WorkspaceNavigation`, not
 * inside it. Which company workspace an account is currently viewing
 * applies to every app tab, not just the collaboration surface, so this is
 * deliberately not one of `CollaborationWorkspace.tsx`'s own tabs (see
 * that component's own docstring). Switching calls `POST /auth/select-
 * workspace` (mints a new session cookie for the chosen workspace) and
 * then reloads the page -- every other query in the app is keyed without a
 * workspace-id parameter (the backend derives `auth.workspace_id` from the
 * session cookie alone), so a full reload is the simplest correct way to
 * get every already-mounted component refetching under the new session,
 * rather than trying to invalidate every query key in the tree by hand.
 */
export default function WorkspaceSwitcher() {
  const workspaces = useWorkspaces()
  const workspace = currentWorkspace(workspaces.data)

  const selectMutation = useMutation({
    mutationFn: (workspaceId: string) =>
      apiRequest('/api/v1/identity/auth/select-workspace', {
        method: 'POST',
        body: { workspace_id: workspaceId },
      }),
    onSuccess: () => {
      // The immediate `window.location.reload()` below discards the
      // entire JS runtime (and its query cache) before any refetch
      // triggered by `invalidateQueries()` could ever be observed -- a
      // leftover call from an earlier draft that had no effect. The
      // reload itself is this component's own real mechanism for getting
      // every already-mounted component to refetch under the new
      // session (see this file's own top-of-file docstring).
      window.location.reload()
    },
  })

  if (workspaces.isLoading) return <div className="workspace-switcher" role="status">Loading workspace…</div>
  if (workspaces.isError) {
    return <div className="workspace-switcher inline-status error-panel" role="alert">{collaborationErrorMessage(workspaces.error)}</div>
  }
  const options = workspaces.data?.workspaces ?? []
  if (options.length <= 1) return null

  return (
    <div className="workspace-switcher">
      <label>
        Workspace
        <select
          aria-label="Switch workspace"
          value={workspace?.id ?? ''}
          disabled={selectMutation.isPending}
          onChange={(event) => selectMutation.mutate(event.target.value)}
        >
          {options.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
        </select>
      </label>
      {selectMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(selectMutation.error)}</div> : null}
    </div>
  )
}
