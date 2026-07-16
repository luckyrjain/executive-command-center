export type WorkspaceView =
  | 'today'
  | 'work'
  | 'notes'
  | 'schedule'
  | 'risks'
  | 'recommendations'
  | 'search-audit'

export type ApiRequestOptions = Omit<RequestInit, 'body' | 'headers'> & {
  body?: unknown
  headers?: HeadersInit
}

export type ApiErrorEnvelope = {
  error?: {
    code?: string
    message?: string
    details?: unknown
  }
}
