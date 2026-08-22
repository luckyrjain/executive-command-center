import { describe, expect, it } from 'vitest'

import { ApiError } from './client'
import { apiErrorMessage } from './errorMessage'

describe('apiErrorMessage', () => {
  it('returns a generic message for a non-Error value', () => {
    expect(apiErrorMessage('boom')).toBe('Request failed.')
  })

  it('returns a plain Error\'s own message when it is not an ApiError', () => {
    expect(apiErrorMessage(new Error('kaboom'))).toBe('kaboom')
  })

  it('classifies the generic transport codes the same for every caller', () => {
    expect(apiErrorMessage(new ApiError(0, 'OFFLINE', 'x'))).toBe('You are offline, so this request was not sent.')
    expect(apiErrorMessage(new ApiError(0, 'NETWORK_ERROR', 'x'))).toBe('Could not reach the server. Nothing was sent.')
    expect(apiErrorMessage(new ApiError(409, 'IDEMPOTENCY_CONFLICT', 'x')))
      .toBe('A different request was already recorded under this request key. Reload and retry.')
    expect(apiErrorMessage(new ApiError(403, 'CSRF_TOKEN_REQUIRED', 'x')))
      .toBe('Your session\'s security token is missing or stale. Reload the page and try again.')
    expect(apiErrorMessage(new ApiError(403, 'CSRF_TOKEN_INVALID', 'x')))
      .toBe('Your session\'s security token is missing or stale. Reload the page and try again.')
  })

  it('uses a code-keyed override when the caller supplies one', () => {
    expect(apiErrorMessage(new ApiError(409, 'VERSION_CONFLICT', 'x'), { VERSION_CONFLICT: 'Reload and retry.' }))
      .toBe('Reload and retry.')
  })

  it('uses a status-keyed override for a status the caller supplies', () => {
    expect(apiErrorMessage(new ApiError(403, 'FORBIDDEN', 'x'), { '403': 'Not permitted here.' }))
      .toBe('Not permitted here.')
  })

  it('falls back to the ApiError\'s own message when nothing else matches', () => {
    expect(apiErrorMessage(new ApiError(500, 'SOMETHING_ELSE', 'server exploded'))).toBe('server exploded')
  })
})
