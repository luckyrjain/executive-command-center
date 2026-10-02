import { describe, expect, it } from 'vitest'

import { PERSONAL_PROVIDERS, withoutPersonalProviders } from './personalProviders'
import type { ConnectorAccount, ConnectorProvider } from './types'

function connector(id: string, provider: ConnectorProvider): ConnectorAccount {
  return {
    id,
    provider,
    external_account_id: `${id}-ext`,
    display_name: id,
    granted_scopes: [],
    status: 'active',
    status_detail: null,
    last_synced_at: null,
    last_error: null,
    disconnected_at: null,
    version: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
  }
}

describe('withoutPersonalProviders', () => {
  it('treats gmail as a personal provider', () => {
    expect(PERSONAL_PROVIDERS.has('gmail')).toBe(true)
  })

  it('drops gmail rows and keeps every other row in its original order', () => {
    const rows = [
      connector('a', 'jira'),
      connector('g1', 'gmail'),
      connector('b', 'github'),
      connector('g2', 'gmail'),
      connector('c', 'datadog'),
    ]
    expect(withoutPersonalProviders(rows).map((row) => row.id)).toEqual(['a', 'b', 'c'])
  })

  it('returns an empty list for an empty input', () => {
    expect(withoutPersonalProviders([])).toEqual([])
  })

  it('returns an empty list when every row is personal', () => {
    expect(withoutPersonalProviders([connector('g1', 'gmail')])).toEqual([])
  })
})
