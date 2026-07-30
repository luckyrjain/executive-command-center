import { useState } from 'react'

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { EntityList } from '../knowledge/types'
import type { TeamAssignmentRequest, WorkItem, WorkItemListResponse } from './types'

function badgeClass(state: WorkItem['permission_state'] | WorkItem['freshness_state']): string {
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

function assignTeam(
  workItemId: string,
  expectedVersion: number,
  teamEntityId: string | null,
): Promise<WorkItem> {
  const body: TeamAssignmentRequest = { expected_version: expectedVersion, team_entity_id: teamEntityId }
  return apiRequest(`/api/v1/engineering/work-items/${workItemId}/team`, { method: 'POST', body })
}

/**
 * `POST /api/v1/engineering/work-items/{id}/team` -- the identical
 * "human confirms" half of migration `0050_phase6_team_linkage.py`'s
 * hybrid design `RepositoriesPanel.tsx`'s own `TeamAssignment` already
 * implements for repositories. This was a real, undocumented gap: the
 * backend confirm endpoint and full response typing existed since
 * migration `0050`, but no frontend surface ever read or wrote a work
 * item's team -- `CoveragePanel.tsx` only ever reads `GET /engineering/
 * work-items` to count unresolved reporter/assignee identities, never
 * rendering the list itself or this endpoint.
 */
function TeamAssignment({
  workItem,
  teamsById,
}: {
  workItem: WorkItem
  teamsById: Map<string, string>
}) {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: (teamEntityId: string | null) =>
      assignTeam(workItem.id, workItem.team_assignment_version, teamEntityId),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['engineering', 'work-items'] }) },
  })

  // See RepositoriesPanel.tsx's identical `TeamAssignment` comment: past
  // 100 teams in a workspace (`listTeams`'s own `limit=100`, the backend's
  // hard ceiling), an already-confirmed `team_entity_id` outside that page
  // would otherwise leave the `<select>`'s controlled `value` matching no
  // `<option>`, silently misrepresenting an assigned work item as
  // unassigned.
  const assignedTeamMissing = workItem.team_entity_id !== null && !teamsById.has(workItem.team_entity_id)

  return (
    <div className="work-actions">
      <label>
        {`Team for ${workItem.title}`}
        <select
          aria-label={`Team for ${workItem.title}`}
          value={workItem.team_entity_id ?? ''}
          disabled={mutation.isPending}
          onChange={(event) => mutation.mutate(event.target.value === '' ? null : event.target.value)}
        >
          <option value="">Unassigned</option>
          {assignedTeamMissing ? (
            <option value={workItem.team_entity_id ?? ''}>Assigned team (not in first 100)</option>
          ) : null}
          {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>
      {!workItem.team_entity_id && workItem.suggested_team_name ? (
        <small>suggested: {workItem.suggested_team_name}</small>
      ) : null}
      {mutation.isError ? <span role="alert" className="inline-status error-panel">{mutation.error.message}</span> : null}
    </div>
  )
}

function WorkItemRow({ workItem, teamsById }: { workItem: WorkItem; teamsById: Map<string, string> }) {
  return (
    <li>
      <div>
        <strong>{workItem.title}</strong>
        <small>{workItem.provider} · {workItem.item_type ?? 'unknown type'} · observed {timestamp(workItem.observed_at)}</small>
      </div>
      <div className="work-actions">
        <span role="status" className={badgeClass(workItem.permission_state)}>
          {workItem.permission_state === 'active' ? 'permissions active' : workItem.permission_state.replaceAll('_', ' ')}
        </span>
        <span role="status" className={badgeClass(workItem.freshness_state)}>
          {workItem.freshness_state === 'fresh' ? 'fresh' : workItem.freshness_state}
        </span>
        {workItem.status ? <span className="inline-status">{workItem.status}</span> : null}
      </div>
      <TeamAssignment workItem={workItem} teamsById={teamsById} />
      <a href={workItem.source_url} target="_blank" rel="noreferrer">View on {workItem.provider}</a>
    </li>
  )
}

/**
 * `GET /api/v1/engineering/work-items` (Task 8's own additive endpoint --
 * see `connector_accounts.py`'s comment above `WorkItemResponse`) had no
 * list surface of its own until now -- `CoveragePanel.tsx` reads it only
 * for a rollup count, never rendering an individual row or exposing the
 * team-assignment endpoint. Mirrors `RepositoriesPanel.tsx`'s shape
 * exactly, including its own identical `permission_state`/`freshness_state`
 * per-row disclosure (distinct from `ConnectorHealthPanel`'s account-level
 * `status`) and team-linkage UX.
 */
export default function WorkItemsPanel() {
  // Read-only view filter -- see `RepositoriesPanel.tsx`'s identical
  // filter for the full "this was previously unreachable" reasoning; the
  // backend's own `?team_entity_id=` filter has existed on this endpoint
  // since migration `0050_phase6_team_linkage.py`.
  const [teamFilter, setTeamFilter] = useState('')
  const query = useQuery({
    queryKey: ['engineering', 'work-items', teamFilter || null],
    queryFn: () => apiRequest<WorkItemListResponse>(
      `/api/v1/engineering/work-items${teamFilter ? `?team_entity_id=${encodeURIComponent(teamFilter)}` : ''}`,
    ),
    retry: 1,
  })
  const teamsQuery = useQuery({ queryKey: ['knowledge', 'entities', 'team'], queryFn: listTeams, retry: 1 })

  const workItems = query.data?.work_items ?? []
  const teamsById = new Map((teamsQuery.data?.items ?? []).map((entity) => [entity.id, entity.canonical_name]))

  return (
    <section className="work-panel" aria-labelledby="engineering-work-items-title">
      <h2 id="engineering-work-items-title">Work items</h2>
      <p>Every work item synced from a connected GitHub, GitLab, or Jira account, with its own permission and freshness state -- never rolled up into the connector account's own status.</p>

      <label>
        Filter by team
        <select
          aria-label="Filter work items by team"
          value={teamFilter}
          onChange={(event) => setTeamFilter(event.target.value)}
        >
          <option value="">All teams</option>
          {[...teamsById.entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </label>

      {query.isLoading ? <p role="status">Loading work items…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && workItems.length === 0 ? (
        <p className="empty-state">
          {teamFilter
            ? 'No work items are assigned to this team yet.'
            : 'No work items have synced yet. Connect a GitHub, GitLab, or Jira account and run a backfill from Connector health.'}
        </p>
      ) : null}

      <ul className="work-list">
        {workItems.map((workItem) => (
          <WorkItemRow key={workItem.id} workItem={workItem} teamsById={teamsById} />
        ))}
      </ul>
    </section>
  )
}
