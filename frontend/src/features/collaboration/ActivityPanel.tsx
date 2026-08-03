import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { collaborationErrorMessage, formatTimestamp } from './errors'
import type { ActivityItem, ActivityListResponse } from './types'

function activityQueryKey(cursor: string | null) {
  return ['shared-activity', cursor] as const
}

export default function ActivityPanel() {
  const [cursors, setCursors] = useState<Array<string | null>>([null])
  const currentCursor = cursors[cursors.length - 1]

  const activity = useQuery({
    queryKey: activityQueryKey(currentCursor),
    queryFn: () =>
      apiRequest<ActivityListResponse>(
        `/api/v1/shared/activity${currentCursor ? `?cursor=${encodeURIComponent(currentCursor)}` : ''}`,
      ),
    retry: 1,
  })

  const items: ActivityItem[] = activity.data?.items ?? []

  function loadMore() {
    if (activity.data?.next_cursor) setCursors((current) => [...current, activity.data.next_cursor])
  }

  function reset() {
    setCursors([null])
  }

  return (
    <section className="work-panel" aria-labelledby="activity-title">
      <h2 id="activity-title">Shared activity</h2>
      <p>Redacted workspace activity -- only shows events on resources you can currently see. Some activity is not yet surfaced here (see the docs disclosure for which domains).</p>

      {activity.isLoading ? <p role="status">Loading activity…</p> : null}
      {activity.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(activity.error)}</div> : null}
      {activity.data && items.length === 0 && cursors.length === 1 ? <p className="empty-state">No visible activity yet.</p> : null}

      <ul className="work-list">
        {items.map((item) => (
          <li key={item.id}>
            <div>
              <strong>{item.event_type}</strong>
              <small> · {item.aggregate_type} {item.aggregate_id}</small>
            </div>
            <div className="inline-status">{formatTimestamp(item.occurred_at)}</div>
          </li>
        ))}
      </ul>

      <div className="work-actions">
        {cursors.length > 1 ? <button type="button" onClick={reset}>Back to latest</button> : null}
        {activity.data?.next_cursor ? (
          <button type="button" disabled={activity.isFetching} onClick={loadMore}>
            {activity.isFetching ? 'Loading…' : 'Load more'}
          </button>
        ) : null}
      </div>
    </section>
  )
}
