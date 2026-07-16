export type NoteDraftRecovery = {
  text: string
  baseVersion: number
}

export type NoteDraftRecoveryStore = {
  get: (noteId: string) => NoteDraftRecovery | undefined
  put: (noteId: string, draft: NoteDraftRecovery) => void
  rebase: (noteId: string, baseVersion: number) => void
  removePersisted: (noteId: string, draft: NoteDraftRecovery) => void
  clear: () => void
}

type StoredDraft = NoteDraftRecovery & { updatedAt: number }
type RecoveryOptions = {
  namespace: string
  maxEntries?: number
  maxAgeMs?: number
  now?: () => number
}

export function createNoteDraftRecoveryStore({
  namespace,
  maxEntries = 50,
  maxAgeMs = 30 * 60 * 1000,
  now = Date.now,
}: RecoveryOptions): NoteDraftRecoveryStore {
  const entries = new Map<string, StoredDraft>()
  const key = (noteId: string) => `${namespace}:${noteId}`

  function prune() {
    const cutoff = now() - maxAgeMs
    for (const [entryKey, draft] of entries) {
      if (draft.updatedAt < cutoff) entries.delete(entryKey)
    }
    while (entries.size > Math.max(1, maxEntries)) {
      let oldestKey: string | undefined
      let oldestTime = Number.POSITIVE_INFINITY
      for (const [entryKey, draft] of entries) {
        if (draft.updatedAt < oldestTime) {
          oldestKey = entryKey
          oldestTime = draft.updatedAt
        }
      }
      if (!oldestKey) break
      entries.delete(oldestKey)
    }
  }

  return {
    get(noteId) {
      prune()
      const draft = entries.get(key(noteId))
      return draft ? { text: draft.text, baseVersion: draft.baseVersion } : undefined
    },
    put(noteId, draft) {
      entries.set(key(noteId), { ...draft, updatedAt: now() })
      prune()
    },
    rebase(noteId, baseVersion) {
      const entryKey = key(noteId)
      const draft = entries.get(entryKey)
      if (draft) entries.set(entryKey, { ...draft, baseVersion, updatedAt: now() })
    },
    removePersisted(noteId, draft) {
      const entryKey = key(noteId)
      const current = entries.get(entryKey)
      if (current?.text === draft.text && current.baseVersion === draft.baseVersion) entries.delete(entryKey)
    },
    clear() {
      entries.clear()
    },
  }
}
