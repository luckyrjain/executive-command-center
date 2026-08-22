import { useState } from 'react'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { EntityList } from '../knowledge/types'
import { safeHref, type Repository, type RepositoryListResponse, type TeamAssignmentRequest } from './types'

function badgeClass(state: Repository['permission_state'] | Repository['freshness_state']): string {
  if (state === 'permission_lost' || state === 'deleted' || state === 'disconnected') return 'inline-status error-panel'
  if (state === 'stale') return 'inline-status degraded-panel'
  return 'inline-status'
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'unknown'
}

function listTeams(): Promise<EntityList> {
  // `status=active` excludes an archived/redirected team -- without it, a
  // team merged away via the Knowledge Platform's own resolution flow
  // (`MergeReview.tsx`) stayed selectable here forever, and confirming it
  // 404'd (`_validate_team_entity` requires `status='active'`) with no
  // indication in the UI of why.
  return apiRequest('/api/v1/knowledge/entities?kind=team&status=active&limit=100')
}

function assignTeam(
  repositoryId: string,
  expectedVersion: number,
  teamEntityId: string | null,
): Promise<Repository> {
  const body: TeamAssignmentRequest = { expected_version: expectedVersion, team_entity_id: teamEntityId }
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
    mutationFn: (teamEntityId: string | null) =>
      assignTeam(repository.id, repository.team_assignment_version, teamEntityId),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['engineering', 'repositories'] }) },
    // A VERSION_CONFLICT means this row's team_assignment_version is
    // already stale -- without a refetch here, the select stays bound to
    // the stale version, so a retry submits the same now-wrong
    // expected_version and just 409s again in a loop. Matches
    // TaskWorkspace.tsx's/RiskWorkspace.tsx's onError-refetch precedent.
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
        void queryClient.invalidateQueries({ queryKey: ['engineering', 'repositories'] })
      }
    },
  })

  // `teamsById` is capped at the first 100 teams (`listTeams`'s own
  // `limit=100`, itself the backend's hard ceiling on this list endpoint --
  // see that function's comment). Past 100 teams in a workspace, an already
  // -confirmed `team_entity_id` outside that page would otherwise leave the
  // `<select>`'s controlled `value` matching no `<option>`, silently
  // misrepresenting an assigned repository as unassigned. Injecting a
  // synthetic option for the current value guarantees it's always
  // selectable/visible even when its name hasn't been fetched.
  const assignedTeamMissing = repository.team_entity_id !== null && !teamsById.has(repository.team_entity_id)

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
          {assignedTeamMissing ? (
            <option value={repository.team_entity_id ?? ''}>Assigned team (not in first 100)</option>
          ) : null}
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
      {safeHref(repository.source_url) ? <a href={safeHref(repository.source_url)} target="_blank" rel="noreferrer">View on {repository.provider}</a> : null}
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
  // Read-only view filter -- distinct from `TeamAssignment`'s own select,
  // which writes the confirmed link. Previously the only place a team
  // entity was consumed at all was that write-side dropdown; there was no
  // way to view just one team's repositories, despite the backend's own
  // `?team_entity_id=` filter (migration `0050_phase6_team_linkage.py`)
  // supporting exactly this since it shipped.
  const [teamFilter, setTeamFilter] = useState('')
  const query = useQuery({
    queryKey: ['engineering', 'repositories', teamFilter || null],
    queryFn: () => apiRequest<RepositoryListResponse>(
      `/api/v1/engineering/repositories${teamFilter ? `?team_entity_id=${encodeURIComponent(teamFilter)}` : ''}`,
    ),
    retry: 1,
  })
  const teamsQuery = useQuery({ queryKey: ['knowledge', 'entities', 'team'], queryFn: listTeams, retry: 1 })

  const repositories = query.data?.repositories ?? []
  const teamsById = new Map((teamsQuery.data?.items ?? []).map((entity) => [entity.id, entity.canonical_name]))

  return (
    <section className="work-panel" aria-labelledby="engineering-repositories-title">
      <h2 id="engineering-repositories-title">Repositories</h2>
      <p>Every repository synced from a connected GitHub or GitLab account, with its own permission and freshness state -- never rolled up into the connector account's own status.</p>

      <label>
        Filter by team
        <select
          aria-label="Filter repositories by team"
          value={teamFilter}
          onChange={(event) => setTeamFilter(event.target.value)}
        >
          <option value="">All teams</option>
          {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>

      {query.isLoading ? <p role="status">Loading repositories…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && repositories.length === 0 ? (
        <p className="empty-state">
          {teamFilter
            ? 'No repositories are assigned to this team yet.'
            : 'No repositories have synced yet. Connect a GitHub or GitLab account and run a backfill from Connector health.'}
        </p>
      ) : null}

      <ul className="work-list">
        {repositories.map((repository) => (
          <RepositoryRow key={repository.id} repository={repository} teamsById={teamsById} />
        ))}
      </ul>
    </section>
  )
}
