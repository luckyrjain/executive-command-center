import { useState } from 'react'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { EntityList, KnowledgeEntity } from '../knowledge/types'
import type {
  TeamSuggestionActionResponse,
  TeamSuggestionConfirmRequest,
  TeamSuggestionDismissRequest,
  TeamSuggestionGroup,
  TeamSuggestionListResponse,
} from './types'

function listTeams(): Promise<EntityList> {
  return apiRequest('/api/v1/knowledge/entities?kind=team&status=active&limit=100')
}

function confirmSuggestion(suggestedTeamName: string, teamEntityId: string): Promise<TeamSuggestionActionResponse> {
  const body: TeamSuggestionConfirmRequest = { suggested_team_name: suggestedTeamName, team_entity_id: teamEntityId }
  return apiRequest('/api/v1/engineering/team-suggestions/confirm', { method: 'POST', body })
}

function dismissSuggestion(suggestedTeamName: string): Promise<TeamSuggestionActionResponse> {
  const body: TeamSuggestionDismissRequest = { suggested_team_name: suggestedTeamName }
  return apiRequest('/api/v1/engineering/team-suggestions/dismiss', { method: 'POST', body })
}

function createTeamEntity(canonicalName: string): Promise<KnowledgeEntity> {
  return apiRequest('/api/v1/knowledge/entities', { method: 'POST', body: { kind: 'team', canonical_name: canonicalName } })
}

/**
 * One group's row: bulk-confirm (via a team picker), bulk-confirm (via
 * inline create), or bulk-dismiss every `repositories`/`engineering_work_items`
 * row sharing this `suggested_team_name`. See `docs/superpowers/specs/2026-08-06-
 * team-suggestions-review-page-design.md` and `docs/superpowers/specs/2026-08-10-
 * team-suggestions-inline-create-design.md`.
 */
function SuggestionRow({ group, teamsById }: { group: TeamSuggestionGroup; teamsById: Map<string, string> }) {
  const queryClient = useQueryClient()
  const [teamEntityId, setTeamEntityId] = useState('')
  const [lastResult, setLastResult] = useState<TeamSuggestionActionResponse | null>(null)
  const [newTeamName, setNewTeamName] = useState(group.suggested_team_name)

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'team-suggestions'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'repositories'] })
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'work-items'] })
  }

  const confirmMutation = useMutation({
    mutationFn: () => confirmSuggestion(group.suggested_team_name, teamEntityId),
    onSuccess: (result) => { setLastResult(result); invalidate() },
  })
  const dismissMutation = useMutation({
    mutationFn: () => dismissSuggestion(group.suggested_team_name),
    onSuccess: (result) => { setLastResult(result); invalidate() },
  })
  const createAndConfirmMutation = useMutation({
    mutationFn: async () => {
      const entity = await createTeamEntity(newTeamName.trim())
      return confirmSuggestion(group.suggested_team_name, entity.id)
    },
    onSuccess: (result) => {
      setLastResult(result)
      invalidate()
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entities', 'team'] })
    },
    onError: () => {
      // When create succeeds but confirm fails, the created team entity is orphaned but real.
      // Invalidate the entities query so the newly created team appears in the dropdown for retry.
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entities', 'team'] })
    },
  })
  const busy = confirmMutation.isPending || dismissMutation.isPending || createAndConfirmMutation.isPending
  const total = group.repository_count + group.work_item_count

  return (
    <li>
      <div>
        <strong>{group.suggested_team_name}</strong>
        <small>{`${group.repository_count} repositories · ${group.work_item_count} work items (${total} total)`}</small>
      </div>
      <ul>
        {group.sample_items.map((item) => (
          <li key={`${item.resource_type}-${item.id}`}>{`${item.name} (${item.resource_type})`}</li>
        ))}
      </ul>
      <div className="work-actions">
        <label>
          {`Assign team for ${group.suggested_team_name}`}
          <select
            aria-label={`Assign team for ${group.suggested_team_name}`}
            value={teamEntityId}
            disabled={busy}
            onChange={(event) => setTeamEntityId(event.target.value)}
          >
            <option value="">Select a team…</option>
            {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
        </label>
        <button type="button" disabled={!teamEntityId || busy} onClick={() => confirmMutation.mutate()}>
          Confirm
        </button>
        <button type="button" disabled={busy} onClick={() => dismissMutation.mutate()}>
          Dismiss
        </button>
      </div>
      <div className="work-actions">
        <label>
          {`New team name for ${group.suggested_team_name}`}
          <input
            aria-label={`New team name for ${group.suggested_team_name}`}
            type="text"
            value={newTeamName}
            disabled={busy}
            onChange={(event) => setNewTeamName(event.target.value)}
          />
        </label>
        <button
          type="button"
          disabled={!newTeamName.trim() || busy}
          onClick={() => createAndConfirmMutation.mutate()}
        >
          Create & confirm
        </button>
      </div>
      {confirmMutation.isError ? <span role="alert" className="inline-status error-panel">{confirmMutation.error.message}</span> : null}
      {dismissMutation.isError ? <span role="alert" className="inline-status error-panel">{dismissMutation.error.message}</span> : null}
      {createAndConfirmMutation.isError ? <span role="alert" className="inline-status error-panel">{createAndConfirmMutation.error.message}</span> : null}
      {lastResult && lastResult.skipped_unauthorized.length > 0 ? (
        <p role="status">
          {`Applied to ${lastResult.updated.length} of ${lastResult.updated.length + lastResult.skipped_unauthorized.length} — ${lastResult.skipped_unauthorized.length} skipped: insufficient permission.`}
        </p>
      ) : null}
    </li>
  )
}

/**
 * `GET /api/v1/engineering/team-suggestions` -- the grouped, bulk-action
 * review surface for migration `0050_phase6_team_linkage.py`'s "hybrid:
 * auto-suggest, human confirms" design. `RepositoriesPanel.tsx`'s own
 * per-row `TeamAssignment` component still exists unchanged for browsing
 * one repository; this panel clears every pending suggestion across a
 * workspace's connected GitHub, GitLab, and Jira accounts in as few
 * actions as possible.
 */
export default function TeamSuggestionsPanel() {
  const query = useQuery({
    queryKey: ['engineering', 'team-suggestions'],
    queryFn: () => apiRequest<TeamSuggestionListResponse>('/api/v1/engineering/team-suggestions'),
    retry: 1,
  })
  const teamsQuery = useQuery({ queryKey: ['knowledge', 'entities', 'team'], queryFn: listTeams, retry: 1 })

  const items = query.data?.items ?? []
  const teamsById = new Map((teamsQuery.data?.items ?? []).map((entity) => [entity.id, entity.canonical_name]))

  return (
    <section className="work-panel" aria-labelledby="engineering-team-suggestions-title">
      <h2 id="engineering-team-suggestions-title">Team suggestions</h2>
      <p>Repositories and work items still waiting on a confirmed team, grouped by their suggested name so you can confirm or dismiss every one sharing a name in one action.</p>

      {query.isLoading ? <p role="status">Loading team suggestions…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && items.length === 0 ? <p className="empty-state">No pending team suggestions.</p> : null}

      {teamsQuery.isLoading ? <p role="status">Loading teams…</p> : null}
      {teamsQuery.isError ? (
        <div role="alert" className="inline-status error-panel">
          {`Could not load teams to assign: ${teamsQuery.error.message}. Confirm is unavailable until this loads -- Create & confirm and Dismiss still work.`}
        </div>
      ) : null}

      <ul className="work-list">
        {items.map((group) => <SuggestionRow key={group.suggested_team_name} group={group} teamsById={teamsById} />)}
      </ul>
    </section>
  )
}
