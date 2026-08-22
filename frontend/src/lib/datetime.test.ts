import { describe, expect, it } from 'vitest'

import { formatTime, serverInstantToLocalInput } from './datetime'

describe('serverInstantToLocalInput', () => {
  it('zero-pads a server instant into a datetime-local input value', () => {
    const instant = new Date(2026, 0, 5, 9, 3).toISOString()
    expect(serverInstantToLocalInput(instant)).toBe('2026-01-05T09:03')
  })

  it('returns an empty string for an unparseable value', () => {
    expect(serverInstantToLocalInput('not-a-date')).toBe('')
  })
})

describe('formatTime', () => {
  it('returns null for an undefined value', () => {
    expect(formatTime(undefined)).toBeNull()
  })

  it('returns null for an unparseable value', () => {
    expect(formatTime('not-a-date')).toBeNull()
  })

  it('formats a valid instant as a short local time', () => {
    const instant = new Date(2026, 0, 5, 14, 30).toISOString()
    expect(formatTime(instant)).toMatch(/2:30/)
  })
})
