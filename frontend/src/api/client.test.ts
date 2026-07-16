import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, apiRequest } from './client'

describe('apiRequest', () => {
  beforeEach(() => {
    vi.stubGlobal('document', { cookie: '' })
    vi.stubGlobal('navigator', { onLine: true })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('includes browser credentials for reads', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"items":[]}', { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    await apiRequest('/api/v1/tasks')

    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/tasks'), expect.objectContaining({
      credentials: 'include',
    }))
  })

  it('adds mutation protection headers and encodes JSON', async () => {
    document.cookie = 'other=value; ecc_csrf=token%201; Secure'
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'attempt-1') })
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetch)

    await apiRequest('/api/v1/tasks', { method: 'POST', body: { title: 'Plan' } })

    expect(fetch).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
      body: '{"title":"Plan"}',
      credentials: 'include',
      headers: expect.objectContaining({
        'Content-Type': 'application/json',
        'Idempotency-Key': 'attempt-1',
        'X-CSRF-Token': 'token 1',
      }),
    }))
  })

  it('classifies network failures while offline', async () => {
    vi.stubGlobal('navigator', { onLine: false })
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(apiRequest('/api/v1/tasks')).rejects.toMatchObject({
      status: 0,
      code: 'OFFLINE',
      message: 'You are offline',
    })
  })

  it('parses the current state from version conflicts', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: {
        code: 'VERSION_CONFLICT',
        message: 'Version conflict',
        details: { current_version: 4 },
      },
    }), { status: 409, headers: { 'Content-Type': 'application/json' } })))

    const error = await apiRequest('/api/v1/tasks/task-1', { method: 'PATCH', body: { expected_version: 3 } })
      .catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 409,
      code: 'VERSION_CONFLICT',
      message: 'Version conflict',
      current: { current_version: 4 },
    })
  })
})
