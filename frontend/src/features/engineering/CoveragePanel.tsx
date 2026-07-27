import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type {
  ConnectorAccount,
  ConnectorAccountListResponse,
  Repository,
  RepositoryListResponse,
  WorkItem,
  WorkItemListResponse,
} from './types'

type StateCounts = { fresh: number; stale: number; disconnected: number; permission_lost: number; deleted: number }

function countStates(rows: Array<Pick<Repository | WorkItem, 'freshness_state' | 'permission_state'>>): StateCounts {
  const counts: StateCounts = { fresh: 0, stale: 0, disconnected: 0, permission_lost: 0, deleted: 0 }
  for (const row of rows) {
    if (row.freshness_state === 'fresh') counts.fresh += 1
    if (row.freshness_state === 'stale') counts.stale += 1
    if (row.freshness_state === 'disconnected') counts.disconnected += 1
    if (row.permission_state === 'permission_lost') counts.permission_lost += 1
    if (row.permission_state === 'deleted') counts.deleted += 1
  }
  return counts
}

function ConnectorCoverageRow({ connector, repositories, workItems }: {
  connector: ConnectorAccount
  repositories: Repository[]
  workItems: WorkItem[]
}) {
  const repoCounts = countStates(repositories)
  const itemCounts = countStates(workItems)
  const degraded = repoCounts.stale + repoCounts.disconnected + repoCounts.permission_lost + repoCounts.deleted
    + itemCounts.stale + itemCounts.disconnected + itemCounts.permission_lost + itemCounts.deleted

  return (
    <li>
      <div>
        <strong>{connector.display_name}</strong>
        <small>{connector.provider}</small>
      </div>
      <p>
        {repositories.length} repositor{repositories.length === 1 ? 'y' : 'ies'}
        {' ('}{repoCounts.fresh} fresh, {repoCounts.stale} stale, {repoCounts.disconnected} disconnected, {repoCounts.permission_lost + repoCounts.deleted} permission-affected{')'}
      </p>
      <p>
        {workItems.length} work item{workItems.length === 1 ? '' : 's'}
        {' ('}{itemCounts.fresh} fresh, {itemCounts.stale} stale, {itemCounts.disconnected} disconnected, {itemCounts.permission_lost + itemCounts.deleted} permission-affected{')'}
      </p>
      {degraded > 0 ? (
        <div role="status" className="inline-status degraded-panel">
          {degraded} record{degraded === 1 ? '' : 's'} from this connector {degraded === 1 ? 'is' : 'are'} stale, disconnected, or missing permission -- see Connector health or Repositories for detail.
        </div>
      ) : (
        <div role="status" className="inline-status">All synced content from this connector is fresh with active permissions.</div>
      )}
    </li>
  )
}

/**
 * "Source Coverage" -- how complete and how fresh this workspace's own
 * synced data is, aggregated across every connector rather than repeating
 * `ConnectorHealthPanel`/`RepositoriesPanel`'s own per-row detail.
 * Entirely derived client-side from three already-existing endpoints
 * (`connectors`, `repositories`, `work-items`) -- no dedicated coverage
 * endpoint exists, and none is needed for a workspace-scoped rollup this
 * small.
 */
export default function CoveragePanel() {
  const connectors = useQuery({
    queryKey: ['engineering', 'connectors'],
    queryFn: () => apiRequest<ConnectorAccountListResponse>('/api/v1/engineering/connectors'),
    retry: 1,
  })
  const repositories = useQuery({
    queryKey: ['engineering', 'repositories'],
    queryFn: () => apiRequest<RepositoryListResponse>('/api/v1/engineering/repositories'),
    retry: 1,
  })
  const workItems = useQuery({
    queryKey: ['engineering', 'work-items'],
    queryFn: () => apiRequest<WorkItemListResponse>('/api/v1/engineering/work-items'),
    retry: 1,
  })

  const loading = connectors.isLoading || repositories.isLoading || workItems.isLoading
  const anyError = connectors.error ?? repositories.error ?? workItems.error

  const connectorList = connectors.data?.connectors ?? []
  const repositoryList = repositories.data?.repositories ?? []
  const workItemList = workItems.data?.work_items ?? []

  const reporters = new Set(workItemList.map((item) => item.reporter_external_id).filter((id): id is string => Boolean(id)))
  const assignees = new Set(workItemList.map((item) => item.assignee_external_id).filter((id): id is string => Boolean(id)))
  const unresolvedIdentityCount = new Set([...reporters, ...assignees]).size

  return (
    <section className="work-panel" aria-labelledby="engineering-coverage-title">
      <h2 id="engineering-coverage-title">Source coverage</h2>
      <p>How much of this workspace's engineering data has actually synced, and how fresh it is, per connector.</p>

      {loading ? <p role="status">Loading source coverage…</p> : null}
      {anyError ? <div role="alert" className="inline-status error-panel">{anyError instanceof Error ? anyError.message : 'Request failed.'}</div> : null}
      {!loading && !anyError && connectorList.length === 0 ? (
        <p className="empty-state">No connectors are configured yet -- connect one from Connector health to see coverage here.</p>
      ) : null}

      <ul className="work-list">
        {connectorList.map((connector) => (
          <ConnectorCoverageRow
            key={connector.id}
            connector={connector}
            repositories={repositoryList.filter((repo) => repo.connector_account_id === connector.id)}
            workItems={workItemList.filter((item) => item.connector_account_id === connector.id)}
          />
        ))}
      </ul>

      {workItemList.length > 0 ? (
        <details>
          <summary>Unresolved identities ({unresolvedIdentityCount})</summary>
          <p>
            This many distinct reporter/assignee identifiers across synced work items are raw provider
            IDs, not yet linked to a known person. There is no identity-resolution mechanism for
            engineering data in this activation (unlike Phase 2's knowledge-entity resolution) --
            this is a disclosure of the gap, not an interactive merge flow.
          </p>
          <ul aria-label="A sample of unresolved identities">
            {[...new Set([...reporters, ...assignees])].slice(0, 10).map((id) => <li key={id}><code>{id}</code></li>)}
          </ul>
        </details>
      ) : null}
    </section>
  )
}
