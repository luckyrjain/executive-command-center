// @vitest-environment jsdom

import type { ComponentProps } from 'react'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AttentionExplanation, { type AiRunResponse } from './AttentionExplanation'
import type { AttentionItem } from './AttentionQueue'

function item(overrides: Partial<AttentionItem> = {}): AttentionItem {
  return {
    id: 'attn-1',
    entity_type: 'task',
    entity_id: 'task-1',
    source_entity_version: 1,
    score: 60,
    confidence: 0.9,
    factors: [
      { code: 'overdue', label: 'Overdue by 2 days', points: 35 },
      { code: 'pinned', label: 'Manually pinned', points: 10 },
    ],
    explanation: 'Finish the board memo',
    generated_at: '2026-07-20T00:00:00Z',
    expires_at: '2026-07-21T00:00:00Z',
    pinned: false,
    dismissed_at: null,
    dismissed_entity_version: null,
    deferred_until: null,
    policy_version: 1,
    override_reason: null,
    ...overrides,
  }
}

function run(overrides: Partial<AiRunResponse> = {}): AiRunResponse {
  return {
    id: 'run-1',
    task: 'attention.explain_item',
    status: 'completed',
    data_class: 'sensitive',
    policy_version: 1,
    model_id: 'qwen2.5:1.5b-instruct-q4_K_M',
    provider: 'ollama',
    prompt_id: 'attention.explain_item.v1',
    prompt_version: 1,
    evidence: ['overdue'],
    output: { explanation_text: 'Overdue by two days and pinned for follow-up.', cited_factor_codes: ['overdue'] },
    error_code: null,
    usage: { prompt_tokens: 120, output_tokens: 24, cost: 0 },
    attempts: 1,
    started_at: '2026-07-23T00:00:00Z',
    completed_at: '2026-07-23T00:00:05Z',
    ...overrides,
  }
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function deferredResponse() {
  let resolve!: (value: Response) => void
  const promise = new Promise<Response>((res) => { resolve = res })
  return { promise, resolve }
}

function renderExplanation(props: Partial<ComponentProps<typeof AttentionExplanation>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AttentionExplanation item={item()} {...props} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=explain-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'explain-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('AttentionExplanation', () => {
  it('starts idle with a clearly labelled, discardable affordance -- not auto-run', () => {
    vi.stubGlobal('fetch', vi.fn())
    renderExplanation()

    expect(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' })).toBeTruthy()
    expect((vi.mocked(fetch) as ReturnType<typeof vi.fn>)).not.toHaveBeenCalled()
  })

  it('shows the AI-disabled state distinctly, with no request ever made', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    renderExplanation({ aiEnabled: false })

    expect(screen.getByText(/AI explanations are turned off/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Explain/ })).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('shows a real bounded progress indicator while a request is pending, not an indefinite spinner', async () => {
    const { promise } = deferredResponse()
    vi.stubGlobal('fetch', vi.fn(() => promise))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))

    const progress = await screen.findByRole('progressbar', { name: /up to 20s/ })
    expect(progress.getAttribute('aria-valuemax')).toBe('20')
    expect(progress.getAttribute('aria-valuemin')).toBe('0')
    expect(screen.getByText(/Generating an AI explanation \(up to 20s\)/)).toBeTruthy()
  })

  it('lets a pending request be cancelled client-side, showing the cancelled state', async () => {
    const { promise } = deferredResponse()
    vi.stubGlobal('fetch', vi.fn(() => promise))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await screen.findByRole('progressbar')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel AI explanation request' }))

    await waitFor(() => expect(screen.getByText(/request was cancelled/)).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('renders a completed explanation with cited factors, model/prompt version and a discard action', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run())))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))

    await waitFor(() => expect(screen.getByText('Overdue by two days and pinned for follow-up.')).toBeTruthy())
    expect(screen.getByText(/Overdue by 2 days/)).toBeTruthy()
    expect(screen.getByText(/qwen2\.5:1\.5b-instruct-q4_K_M/)).toBeTruthy()
    expect(screen.getByText(/prompt v1/i)).toBeTruthy()
    expect(screen.getByText(/does not change/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Discard AI explanation' }))
    expect(screen.queryByText('Overdue by two days and pinned for follow-up.')).toBeNull()
    expect(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' })).toBeTruthy()
  })

  it('never renders raw model output for a schema_invalid failure -- only a generic message', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'failed', output: null, error_code: 'schema_invalid' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))

    await waitFor(() => expect(screen.getByText(/could not produce a valid explanation/i)).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('shows a distinct local-model-unavailable state for circuit_open', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'failed', output: null, error_code: 'circuit_open' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/local AI model is temporarily unavailable/i)).toBeTruthy())
  })

  it('shows a distinct remote-not-permitted state for remote_not_configured', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'failed', output: null, error_code: 'remote_not_configured' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/only local, on-device AI/i)).toBeTruthy())
  })

  it('shows a distinct budget-exceeded state', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'failed', output: null, error_code: 'budget_exceeded' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/exceeded its time or length budget/i)).toBeTruthy())
  })

  it('shows a distinct timed-out state', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'failed', output: null, error_code: 'timeout' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/timed out/i)).toBeTruthy())
  })

  it('shows a distinct feature-disabled state reported by the backend', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'failed', output: null, error_code: 'feature_disabled' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/not available for this item/i)).toBeTruthy())
  })

  it('shows a degraded-fallback state without blocking retry, never showing a fabricated explanation', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'degraded', output: null, error_code: 'budget_exceeded' }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/degraded/i)).toBeTruthy())
    expect(screen.getByText(/deterministic factors above/i)).toBeTruthy()
  })

  it('shows a distinct cancelled state when the backend itself reports status=cancelled', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ status: 'cancelled', output: null, error_code: null }))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/request was cancelled/i)).toBeTruthy())
  })

  it('flags a stale result when the item changed after the explanation was generated', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ completed_at: '2026-07-19T00:00:00Z' }))))
    renderExplanation({ item: item({ generated_at: '2026-07-23T00:00:00Z' }) })

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/may be stale/i)).toBeTruthy())
  })

  it('does not flag a fresh result as stale', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(run({ completed_at: '2026-07-23T00:00:05Z' }))))
    renderExplanation({ item: item({ generated_at: '2026-07-20T00:00:00Z' }) })

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText('Overdue by two days and pinned for follow-up.')).toBeTruthy())
    expect(screen.queryByText(/may be stale/i)).toBeNull()
  })

  it('shows a generic, distinct error state on a network failure, offering retry', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText(/could not reach/i)).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('polls GET /api/v1/ai/runs/{id} until a running run reaches a terminal status', async () => {
    const fetchMock = vi.fn((url: string, _options?: RequestInit) => {
      if (url.includes('/cancel')) throw new Error('unexpected cancel call')
      if (url.endsWith('/api/v1/ai/runs') || url.includes('/api/v1/ai/runs?')) {
        return response(run({ status: 'running', completed_at: null, output: null }))
      }
      return response(run())
    })
    vi.stubGlobal('fetch', fetchMock)
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel AI explanation request' })).toBeTruthy())

    await waitFor(
      () => expect(screen.getByText('Overdue by two days and pinned for follow-up.')).toBeTruthy(),
      { timeout: 3000 },
    )
    const getCall = fetchMock.mock.calls.find(([url]) => (url as string).endsWith('/api/v1/ai/runs/run-1'))
    expect(getCall).toBeTruthy()
  })

  it('cancels a running run through POST /api/v1/ai/runs/{id}/cancel', async () => {
    const fetchMock = vi.fn((url: string, _options?: RequestInit) => {
      if (url.includes('/cancel')) return response(run({ status: 'cancelled', completed_at: null, output: null }))
      if (url.endsWith('/api/v1/ai/runs')) return response(run({ status: 'running', completed_at: null, output: null }))
      return response(run({ status: 'running', completed_at: null, output: null }))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    const cancelButton = await screen.findByRole('button', { name: 'Cancel AI explanation request' })
    fireEvent.click(cancelButton)

    await waitFor(() => expect(screen.getByText(/request was cancelled/i)).toBeTruthy())
    const cancelCall = fetchMock.mock.calls.find(([url, options]) => (url as string).includes('/cancel') && (options as RequestInit)?.method === 'POST')
    expect(cancelCall).toBeTruthy()
  })

  it('sends the expected request shape to POST /api/v1/ai/runs', async () => {
    const fetchMock = vi.fn((_url: string, _options?: RequestInit) => response(run()))
    vi.stubGlobal('fetch', fetchMock)
    renderExplanation()

    fireEvent.click(screen.getByRole('button', { name: 'Explain "Finish the board memo" with AI' }))
    await waitFor(() => expect(screen.getByText('Overdue by two days and pinned for follow-up.')).toBeTruthy())

    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/ai/runs')
    const body = JSON.parse(options.body as string)
    expect(body).toEqual({ task: 'attention.explain_item', attention_item_id: 'attn-1', data_class: 'sensitive' })
  })
})
