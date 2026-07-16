import { afterEach, describe, expect, it, vi } from 'vitest'

import { createAutosaveController, type AutosaveState } from './autosave'

afterEach(() => {
  vi.useRealTimers()
})

describe('createAutosaveController', () => {
  it('debounces for 750 ms and coalesces edits into the latest text', async () => {
    vi.useFakeTimers()
    const save = vi.fn(async (_text: string, version: number) => version + 1)
    const controller = createAutosaveController({ delayMs: 750, initialVersion: 4, save, onStateChange: vi.fn() })

    controller.update('First')
    await vi.advanceTimersByTimeAsync(500)
    controller.update('Latest')
    await vi.advanceTimersByTimeAsync(749)
    expect(save).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(save).toHaveBeenCalledOnce()
    expect(save).toHaveBeenCalledWith('Latest', 4)
  })

  it('flushes pending text immediately and uses the version returned by the prior save', async () => {
    vi.useFakeTimers()
    const save = vi.fn(async (_text: string, version: number) => version + 1)
    const controller = createAutosaveController({ delayMs: 750, initialVersion: 2, save, onStateChange: vi.fn() })

    controller.update('First save')
    await controller.flush()
    controller.update('Second save')
    await controller.flush()

    expect(save.mock.calls).toEqual([['First save', 2], ['Second save', 3]])
  })

  it('serializes saves and follows an in-flight save with the newest pending text', async () => {
    vi.useFakeTimers()
    let finishFirst!: (version: number) => void
    const save = vi.fn()
      .mockImplementationOnce(() => new Promise<number>((resolve) => { finishFirst = resolve }))
      .mockResolvedValueOnce(8)
    const controller = createAutosaveController({ delayMs: 750, initialVersion: 6, save, onStateChange: vi.fn() })

    controller.update('In flight')
    await vi.advanceTimersByTimeAsync(750)
    controller.update('Intermediate')
    controller.update('Newest')
    await vi.advanceTimersByTimeAsync(750)
    expect(save).toHaveBeenCalledTimes(1)

    finishFirst(7)
    await vi.runAllTimersAsync()
    expect(save.mock.calls).toEqual([['In flight', 6], ['Newest', 7]])
  })

  it('reports a rejected save while retaining the unsaved text for retry', async () => {
    vi.useFakeTimers()
    const states: AutosaveState[] = []
    const save = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(5)
    const controller = createAutosaveController({ delayMs: 750, initialVersion: 4, save, onStateChange: (state) => states.push(state) })

    controller.update('Keep this text')
    await controller.flush()

    expect(states.at(-1)).toMatchObject({ status: 'error', text: 'Keep this text', version: 4 })
    await controller.flush()
    expect(save).toHaveBeenLastCalledWith('Keep this text', 4)
    expect(states.at(-1)).toMatchObject({ status: 'saved', text: 'Keep this text', version: 5 })
  })
})
