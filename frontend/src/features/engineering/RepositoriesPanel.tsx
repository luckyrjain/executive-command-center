import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { Repository, RepositoryListResponse } from './types'

function badgeClass(state: Repository['permission_state'] | Repository['freshness_state']): string {
  if (state === 'permission_lost' || state === 'deleted' || state === 'disconnected') return 'inline-status error-panel'
  if (state === 'stale') return 'inline-status degraded-panel'
  return 'inline-status'
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'unknown'
}

function RepositoryRow({ repository }: { repository: Repository }) {
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

  const repositories = query.data?.repositories ?? []

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
        {repositories.map((repository) => <RepositoryRow key={repository.id} repository={repository} />)}
      </ul>
    </section>
  )
}
