import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const meetingId = 'meeting-1'

function buildPack(id) {
  if (id !== meetingId) return null
  return {
    id: 'pack-1',
    meeting_id: id,
    status: 'fresh',
    generated_at: '2026-07-23T00:00:00Z',
    stale_at: '2026-07-24T00:00:00Z',
    source_versions: {},
    objective: 'Review Q3 numbers',
    starts_at: '2026-07-24T09:00:00Z',
    ends_at: '2026-07-24T10:00:00Z',
    timezone: 'UTC',
    participants: [{ id: 'p-1', entity_id: 'entity-1', entity_name: 'Jordan Lee', role: 'organizer' }],
    timeline: [{ id: 't-1', entity_id: 'entity-1', effective_at: '2026-07-20T00:00:00Z', event_type: 'note_created', summary: 'Prior sync' }],
    commitments: [{ id: 'c-1', direction: 'made_to_me', summary: 'Send the report', status: 'active', due_at: null, counterparty_name: 'Jordan Lee' }],
    decisions: [{ id: 'd-1', title: 'Chose vendor', body: 'We picked Acme', note_type: 'decision', created_at: '2026-07-20T00:00:00Z' }],
    open_questions: [],
    notes: [],
    risks: [{ id: 'r-1', description: 'Vendor concentration', status: 'monitoring', probability: 3, impact: 4, review_at: null }],
    dependencies: [],
    evidence_gaps: [{ id: 'e-1', source_type: 'email', evidence_state: 'missing' }],
    enrichment: { available: true, summary: 'Open with the Q3 numbers, then vendor risk.', error_code: null },
  }
}

/**
 * Meeting prep journey: generate a pack, see facts/open-questions/
 * suggestions kept separate with inline citations and neutral evidence-gap
 * copy, then refresh it. Runs in both AI-enrichment states (on/off) via
 * MEETING_PREP_AI_ENRICHMENT env var -- registered under two names in
 * run.mjs -- since TEST-PLAN.md's Browser acceptance section requires core
 * flows working "in AI-disabled mode" explicitly, not just Task 7's
 * deterministic-pack unit tests. There is no real backend in this e2e
 * harness (see fixtures.mjs), so "the flag" here is which fixture pack the
 * mocked API returns, not the real ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED
 * setting.
 */
export async function run({ page, baseURL }) {
  const aiEnrichmentEnabled = process.env.MEETING_PREP_AI_ENRICHMENT === '1'
  const fixtures = await createFixtureApi(page, {
    attention: { buildMeetingPack: buildPack, aiEnrichmentEnabled },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Meeting prep' }).click()
  const section = page.locator('section[aria-labelledby="meeting-prep-title"]')
  await section.getByRole('heading', { name: 'Meeting prep', level: 1 }).waitFor()

  await section.getByLabel('Meeting ID').fill(meetingId)
  await section.getByRole('button', { name: 'Load meeting prep' }).click()
  await section.getByText('No preparation pack exists yet for this meeting. Generate one above.').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="meeting-prep-title"]' })

  await section.getByRole('button', { name: 'Generate pack' }).click()
  await section.getByText('Review Q3 numbers').waitFor()

  // Facts, open questions and suggestions are distinct sections; citations
  // are inline, not a separate footnote list.
  await section.getByRole('heading', { name: 'Facts', exact: true }).waitFor()
  await section.getByRole('heading', { name: 'Open questions' }).waitFor()
  await section.getByRole('heading', { name: /Suggested agenda/ }).waitFor()
  await section.getByText(/source: note d-1/).waitFor()

  // Evidence gap uses neutral, non-alarming language.
  await section.getByText('Evidence not yet captured').waitFor()

  if (aiEnrichmentEnabled) {
    await section.getByText('Open with the Q3 numbers, then vendor risk.').waitFor()
  } else {
    await section.getByText('AI-assisted suggestions are disabled; showing deterministic results only.').waitFor()
    // Deterministic sections remain fully usable while AI is unavailable.
    await section.getByText('Send the report', { exact: false }).waitFor()
  }

  await section.getByRole('button', { name: 'Refresh pack' }).click()
  const refreshRequest = fixtures.requests.find((request) => request.method === 'POST' && request.path === `/api/v1/meetings/${meetingId}/prep/refresh`)
  assert.ok(refreshRequest, 'expected a refresh request')
}
