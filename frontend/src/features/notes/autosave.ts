export type AutosaveStatus = 'idle' | 'saving' | 'saved' | 'error'

export type AutosaveState = {
  status: AutosaveStatus
  text: string
  version: number
  error?: Error
}

type AutosaveOptions = {
  delayMs: number
  initialVersion: number
  save: (text: string, version: number) => Promise<number>
  onStateChange: (state: AutosaveState) => void
}

export type AutosaveController = {
  update: (text: string) => void
  flush: () => Promise<void>
  dispose: () => void
}

export function createAutosaveController({ delayMs, initialVersion, save, onStateChange }: AutosaveOptions): AutosaveController {
  let text = ''
  let version = initialVersion
  let revision = 0
  let dirty = false
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let activeSave: Promise<void> | undefined
  let saveAfterActive = false

  const notify = (status: AutosaveStatus, error?: Error) => {
    if (!disposed) onStateChange({ status, text, version, ...(error ? { error } : {}) })
  }

  const runSave = (): Promise<void> => {
    if (activeSave) {
      saveAfterActive = true
      return activeSave.then(() => saveAfterActive ? runSave() : undefined)
    }
    if (!dirty || disposed) return Promise.resolve()

    const savedText = text
    const savedRevision = revision
    dirty = false
    notify('saving')
    activeSave = save(savedText, version)
      .then((nextVersion) => {
        version = nextVersion
        notify('saved')
      })
      .catch((reason: unknown) => {
        if (revision === savedRevision) dirty = true
        const error = reason instanceof Error ? reason : new Error('Autosave failed')
        notify('error', error)
        saveAfterActive = false
      })
      .finally(() => {
        activeSave = undefined
      })

    return activeSave.then(() => {
      if (!saveAfterActive) return
      saveAfterActive = false
      return runSave()
    })
  }

  const clearTimer = () => {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  }

  return {
    update(nextText) {
      if (disposed) return
      text = nextText
      revision += 1
      dirty = true
      clearTimer()
      timer = setTimeout(() => {
        timer = undefined
        void runSave()
      }, delayMs)
    },
    flush() {
      clearTimer()
      return runSave()
    },
    dispose() {
      disposed = true
      clearTimer()
    },
  }
}
