import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { EntityList } from '../knowledge/types'
import type { Repository, RepositoryListResponse, TeamAssignmentRequest } from './types'

function badgeClass(state: Repository['permission_state'] | Repository['freshness_state']): string {
  if (state === 'permission_lost' || state === 'deleted' || state === 'disconnected') return 'inline-status error-panel'
  if (state === 'stale') return 'inline-status degraded-panel'
  return 'inline-status'
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'unknown'
}

function listTeams(): Promise<EntityList> {
  return apiRequest('/api/v1/knowledge/entities?kind=team&limit=100')
}

function assignTeam(repositoryId: string, teamEntityId: string | null): Promise<Repository> {
  const body: TeamAssignmentRequest = { team_entity_id: teamEntityId }
  return apiRequest(`/api/v1/engineering/repositories/${repositoryId}/team`, { method: 'POST', body })
}

/**
 * `POST /api/v1/engineering/repositories/{id}/team` -- the "human confirms"
 * half of migration `0050_phase6_team_linkage.py`'s hybrid design.
 * `suggested_team_name` (refreshed by the owning connector adapter on every
 * sync) is shown as a hint only, never auto-applied -- a select bound to
 * `team_entity_id` is the only thing that writes the confirmed link,
 * populated from the same team entities `frontend/src/features/knowledge/
 * EntityExplorer.tsx` already lets a user create (`kind: 'team'`).
 */
function TeamAssignment({
  repository,
  teamsById,
}: {
  repository: Repository
  teamsById: Map<string, string>
}) {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: (teamEntityId: string | null) => assignTeam(repository.id, teamEntityId),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['engineering', 'repositories'] }) },
  })

  return (
    <div className="work-actions">
      <label>
        {`Team for ${repository.name}`}
        <select
          aria-label={`Team for ${repository.name}`}
          value={repository.team_entity_id ?? ''}
          disabled={mutation.isPending}
          onChange={(event) => mutation.mutate(event.target.value === '' ? null : event.target.value)}
        >
          <option value="">Unassigned</option>
          {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>
      {!repository.team_entity_id && repository.suggested_team_name ? (
        <small>suggested: {repository.suggested_team_name}</small>
      ) : null}
      {mutation.isError ? <span role="alert" className="inline-status error-panel">{mutation.error.message}</span> : null}
    </div>
  )
}

function RepositoryRow({ repository, teamsById }: { repository: Repository; teamsById: Map<string, string> }) {
  return (
    <li>
      <div>
        <strong>{repository.name}</strong>
        <small>{repository.provider} · observed {timestamp(repository.observed_at)}</small>
      </div>
      <div className="work-actions">
        <span role="status" className={badgeClass(repository.permission_state)}>
          {repository.permission_state === 'active' ? 'permissions active' : repository.permission_state.replaceAll('_', ' ')}
        </span>
        <span role="status" className={badgeClass(repository.freshness_state)}>
          {repository.freshness_state === 'fresh' ? 'fresh' : repository.freshness_state}
        </span>
      </div>
      <TeamAssignment repository={repository} teamsById={teamsById} />
      <a href={repository.source_url} target="_blank" rel="noreferrer">View on {repository.provider}</a>
    </li>
  )
}

/**
 * `GET /api/v1/engineering/repositories` (Task 8's own additive endpoint --
 * see `connector_accounts.py`'s comment above `RepositoryResponse`).
 * `permission_state`/`freshness_state` are this endpoint's own per-row
 * source of the "partial permissions" and "stale connector" UX-STATES.md
 * requirements at the *content* level, distinct from `ConnectorHealthPanel`'s
 * account-level `status` -- a connector account can be fully `active` while
 * one specific repository it synced has lost permission or gone stale.
 */
export default function RepositoriesPanel() {
  const query = useQuery({
    queryKey: ['engineering', 'repositories'],
    queryFn: () => apiRequest<RepositoryListResponse>('/api/v1/engineering/repositories'),
    retry: 1,
  })
  const teamsQuery = useQuery({ queryKey: ['knowledge', 'entities', 'team'], queryFn: listTeams, retry: 1 })

  const repositories = query.data?.repositories ?? []
  const teamsById = new Map((teamsQuery.data?.items ?? []).map((entity) => [entity.id, entity.canonical_name]))

  return (
    <section className="work-panel" aria-labelledby="engineering-repositories-title">
      <h2 id="engineering-repositories-title">Repositories</h2>
      <p>Every repository synced from a connected GitHub or GitLab account, with its own permission and freshness state -- never rolled up into the connector account's own status.</p>

      {query.isLoading ? <p role="status">Loading repositories…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && repositories.length === 0 ? (
        <p className="empty-state">No repositories have synced yet. Connect a GitHub or GitLab account and run a backfill from Connector health.</p>
      ) : null}

      <ul className="work-list">
        {repositories.map((repository) => (
          <RepositoryRow key={repository.id} repository={repository} teamsById={teamsById} />
        ))}
      </ul>
    </section>
  )
}
