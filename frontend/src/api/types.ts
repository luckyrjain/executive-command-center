export type WorkspaceView =
  | 'today'
  | 'work'
  | 'notes'
  | 'schedule'
  | 'risks'
  | 'knowledge'
  | 'recommendations'
  | 'search-audit'
  | 'attention'
  | 'planner'
  | 'meeting-prep'
  | 'automation'
  | 'engineering'
  | 'personal'
  | 'collaboration'

export type ApiRequestOptions = Omit<RequestInit, 'body' | 'headers'> & {
  body?: unknown
  headers?: HeadersInit
  // Forces the CSRF token to be attached even for a safe (GET/HEAD/OPTIONS)
  // method. Every state-changing request already gets this automatically
  // (see `client.ts`'s own `mutation` check) -- this flag exists only for
  // the rare GET with a real server-side side effect that still requires
  // `CsrfDep` (e.g. `GET /engineering/metrics`, which writes fresh metric
  // snapshot rows on every call; see that endpoint's own docstring for why).
  csrf?: boolean
}

export type ApiErrorEnvelope = {
  error?: {
    code?: string
    message?: string
    details?: unknown
    request_id?: string
  }
  correlation_id?: string
}
