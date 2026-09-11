import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../api/client'
import type { WorkspaceView } from '../api/types'

type CountResponse = { count: number }
type ItemsResponse = { items: unknown[] }
type ApprovalsResponse = { approvals: unknown[] }

function useCount(key: string, path: string) {
  return useQuery({
    queryKey: ['nav-badge', key],
    queryFn: () => apiRequest<CountResponse>(path),
    select: (data) => data.count,
    retry: 1,
  })
}

function useReviewQueueCount() {
  return useQuery({
    queryKey: ['nav-badge', 'risks'],
    queryFn: () => apiRequest<ItemsResponse>('/api/v1/risks/review-queue'),
    select: (data) => data.items.length,
    retry: 1,
  })
}

function usePendingApprovalsCount() {
  return useQuery({
    queryKey: ['nav-badge', 'automation'],
    queryFn: () => apiRequest<ApprovalsResponse>('/api/v1/automations/approvals?status=pending'),
    select: (data) => data.approvals.length,
    retry: 1,
  })
}

/** Badge counts for the 6 sidebar workspaces with a natural single number
 * (spec: "Badge counts" section). A workspace with an undefined or zero
 * count is simply absent from the returned map -- the sidebar renders no
 * badge slot at all for it, never a badge reading "0". */
export function useWorkspaceBadgeCounts(): Partial<Record<WorkspaceView, number>> {
  const attention = useCount('attention', '/api/v1/attention/count')
  const work = useCount('work', '/api/v1/tasks/count')
  const risks = useReviewQueueCount()
  const knowledge = useCount('knowledge', '/api/v1/knowledge/resolution/candidates/count')
  const automation = usePendingApprovalsCount()
  const recommendations = useCount('recommendations', '/api/v1/recommendations/count')

  const counts: Partial<Record<WorkspaceView, number>> = {}
  if (attention.data) counts.attention = attention.data
  if (work.data) counts.work = work.data
  if (risks.data) counts.risks = risks.data
  if (knowledge.data) counts.knowledge = knowledge.data
  if (automation.data) counts.automation = automation.data
  if (recommendations.data) counts.recommendations = recommendations.data
  return counts
}
