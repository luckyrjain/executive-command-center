import { describe, expect, it } from 'vitest'

import { actionBody, actionErrorMessage } from './WorkActionCenter'

describe('work action payloads', () => {
  it('binds actions to the visible entity version', () => {
    expect(actionBody(7, 'complete')).toEqual({ expected_version: 7 })
    expect(actionBody(3, 'fulfil')).toEqual({ expected_version: 3 })
  })

  it('adds a human-readable reason when cancelling', () => {
    expect(actionBody(5, 'cancel')).toEqual({
      expected_version: 5,
      reason: 'Cancelled from executive action center',
    })
  })
})

describe('work action conflicts', () => {
  it('turns version conflicts into a reload-safe message', () => {
    const conflict = new Error('Conflict')
    conflict.name = 'VERSION_CONFLICT'
    expect(actionErrorMessage(conflict)).toContain('latest version has been reloaded')
    expect(actionErrorMessage(new Error('Network unavailable'))).toBe('Network unavailable')
  })
})
