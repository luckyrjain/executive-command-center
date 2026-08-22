import { ApiError } from './client'

// The generic, transport-level codes (offline/network/idempotency/CSRF) and
// the instanceof/fallback boilerplate were duplicated near-verbatim across
// every feature's own `errorMessage`/`*ErrorMessage` function. Domain-
// specific codes and their wording stay at each call site via `overrides`
// -- keyed by `error.code` for ApiError codes, or `'401'`/`'403'` (as
// strings) for a status-based override, since not every domain wants the
// same "you are not permitted..." wording.
export function apiErrorMessage(error: unknown, overrides: Record<string, string> = {}): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code in overrides) return overrides[error.code]
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CSRF_TOKEN_REQUIRED' || error.code === 'CSRF_TOKEN_INVALID') return 'Your session\'s security token is missing or stale. Reload the page and try again.'
  if (String(error.status) in overrides) return overrides[String(error.status)]
  return error.message
}
