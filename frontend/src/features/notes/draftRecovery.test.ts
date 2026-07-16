import { describe, expect, it } from 'vitest'

import { createNoteDraftRecoveryStore } from './draftRecovery'

describe('createNoteDraftRecoveryStore', () => {
  it('isolates drafts by app session namespace and clears a session explicitly', () => {
    const firstSession = createNoteDraftRecoveryStore({ namespace: 'session-a' })
    const secondSession = createNoteDraftRecoveryStore({ namespace: 'session-b' })

    firstSession.put('note-1', { text: 'private draft', baseVersion: 4 })
    expect(secondSession.get('note-1')).toBeUndefined()

    firstSession.clear()
    expect(firstSession.get('note-1')).toBeUndefined()
  })

  it('bounds entries by count and age', () => {
    let now = 1_000
    const store = createNoteDraftRecoveryStore({ namespace: 'session', maxEntries: 2, maxAgeMs: 100, now: () => now })

    store.put('oldest', { text: 'one', baseVersion: 1 })
    now += 1
    store.put('middle', { text: 'two', baseVersion: 2 })
    now += 1
    store.put('newest', { text: 'three', baseVersion: 3 })
    expect(store.get('oldest')).toBeUndefined()
    expect(store.get('middle')).toMatchObject({ text: 'two', baseVersion: 2 })

    now += 101
    expect(store.get('middle')).toBeUndefined()
    expect(store.get('newest')).toBeUndefined()
  })

  it('removes only the exact draft and base version that persisted', () => {
    const store = createNoteDraftRecoveryStore({ namespace: 'session' })
    store.put('note-1', { text: 'newer draft', baseVersion: 5 })

    store.removePersisted('note-1', { text: 'older request', baseVersion: 4 })
    expect(store.get('note-1')).toMatchObject({ text: 'newer draft', baseVersion: 5 })

    store.removePersisted('note-1', { text: 'newer draft', baseVersion: 5 })
    expect(store.get('note-1')).toBeUndefined()
  })
})
