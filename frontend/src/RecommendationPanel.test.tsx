import { describe, expect, it, vi } from 'vitest'

import {
  actionPayload,
  actionSummary,
  confidenceLabel,
  recommendationErrorMessage,
  type Recommendation,
} from './RecommendationPanel'

const recommendation: Recommendation = {
  id: 'rec-1',
  recommendation_type: 'complete_task',
  target_type: 'task',
  target_id: 'task-1',
  proposed_action: { operation: 'complete_task', completed: true },
  expected_version: 7,
  rationale: 'The task is complete.',
  confidence: 0.876,
  status: 'pending_confirmation',
  evidence_ids: [],
  source: 'rule',
  pinned: false,
  version: 3,
}

describe('recommendation action payloads', () => {
  it('binds confirmation to recommendation and target versions', () => {
    expect(actionPayload(recommendation, 'confirm')).toEqual({
      expected_version: 3,
      target_expected_version: 7,
    })
  })

  it('toggles pin state using optimistic versioning', () => {
    expect(actionPayload(recommendation, 'pin')).toEqual({ expected_version: 3, pinned: true })
    expect(actionPayload({ ...recommendation, pinned: true }, 'pin')).toEqual({
      expected_version: 3,
      pinned: false,
    })
  })

  it('defers for approximately 24 hours', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-15T09:00:00.000Z'))
    expect(actionPayload(recommendation, 'defer')).toEqual({
      expected_version: 3,
      defer_until: '2026-07-16T09:00:00.000Z',
    })
    vi.useRealTimers()
  })
})

describe('recommendation presentation', () => {
  it('renders action summaries and confidence consistently', () => {
    expect(actionSummary(recommendation.proposed_action)).toBe('complete task · completed')
    expect(confidenceLabel(recommendation.confidence)).toBe('88% confidence')
  })

  it('turns version conflicts into a reload-safe message', () => {
    const conflict = new Error('Conflict')
    conflict.name = 'TARGET_VERSION_CONFLICT'
    expect(recommendationErrorMessage(conflict)).toContain('latest version has been reloaded')
    expect(recommendationErrorMessage(new Error('Network unavailable'))).toBe('Network unavailable')
  })
})
