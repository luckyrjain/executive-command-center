import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'

export type MeResponse = {
  account_id: string
  users_id: string
  workspace_id: string
  email: string
  display_name: string
}

export function useMe() {
  return useQuery({
    queryKey: ['identity', 'me'],
    queryFn: () => apiRequest<MeResponse>('/api/v1/identity/me'),
    retry: 1,
  })
}
