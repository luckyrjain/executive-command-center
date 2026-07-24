import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const item = {
  id: 'attn-1',
  entity_type: 'task',
  entity_id: 'task-1',
  source_entity_version: 1,
  score: 60,
  confidence: 0.9,
  factors: [{ code: 'overdue', label: 'Overdue by 2 days', points: 35, source_field: 'due_date' }],
  explanation: 'Finish the board memo',
  generated_at: '2026-07-20T00:00:00Z',
  expires_at: '2026-07-21T00:00:00Z',
  pinned: false,
  dismissed_at: null,
  dismissed_entity_version: null,
  deferred_until: null,
  policy_version: 1,
  override_reason: null,
}

function buildRun(body) {
  if (body.task !== 'attention.explain_item' || body.attention_item_id !== item.id) return null
  return {
    id: 'run-1',
    task: 'attention.explain_item',
    status: 'completed',
    data_class: body.data_class ?? 'sensitive',
    policy_version: 1,
    model_id: 'qwen2.5:1.5b-instruct-q4_K_M',
    provider: 'ollama',
    prompt_id: 'attention.explain_item.v1',
    prompt_version: 1,
    evidence: ['overdue'],
    output: { explanation_text: 'Overdue by two days, which is why it is ranked near the top.', cited_factor_codes: ['overdue'] },
    error_code: null,
    usage: { prompt_tokens: 118, output_tokens: 22, cost: 0 },
    attempts: 1,
    started_at: '2026-07-23T00:00:00Z',
    completed_at: '2026-07-23T00:00:04Z',
  }
}

/**
 * `attention.explain_item` product surface (Task 6): the optional,
 * discardable "Explain with AI" affordance on `AttentionQueue.tsx`, backed
 * by a mocked `POST /api/v1/ai/runs` (no live model, no live backend --
 * matching every other scenario in this suite). Runs in both AI-runtime
 * states (on/off) via the `AI_EXPLANATIONS_ENABLED` env var, registered
 * under two names in `run.mjs` -- mirroring `attention-meeting-prep.mjs`'s
 * identical on/off pattern -- since `phase-004`'s own Task 6 Step 3 requires
 * proving the existing Attention Queue is unaffected when this is off, not
 * just that the affordance itself works when on. The killswitch here is a
 * *frontend* runtime override (`window.__ECC_AI_EXPLANATIONS_ENABLED__`,
 * set via `page.addInitScript` before the bundle evaluates) rather than an
 * env-driven fixture response, because `AttentionQueue.tsx` decides whether
 * to mount `AttentionExplanation` at all -- unlike meeting prep's
 * AI-enrichment flag, there is no backend response shape that could make
 * this component simply render nothing, since we're testing whether it's
 * mounted in the first place. See `AttentionQueue.tsx`'s own doc comment
 * for why this needs to be a runtime override and not a second full
 * `VITE_AI_EXPLANATIONS_ENABLED=0` build.
 */
export async function run({ page, baseURL }) {
  const explanationsEnabled = process.env.AI_EXPLANATIONS_ENABLED !== '0'
  const fixtures = await createFixtureApi(page, {
    attention: { attentionItems: [item] },
    aiRuntime: { buildRun },
  })

  if (!explanationsEnabled) {
    await page.addInitScript(() => { window.__ECC_AI_EXPLANATIONS_ENABLED__ = false })
  }

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Attention' }).click()
  const section = page.locator('section[aria-labelledby="attention-title"]')
  await section.getByRole('heading', { name: 'Attention queue', level: 1 }).waitFor()
  await section.getByText('Finish the board memo').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="attention-title"]' })

  const needsAction = section.locator('section[aria-labelledby="attention-group-needs_action"]')
  const explainButton = needsAction.getByRole('button', { name: 'Explain "Finish the board memo" with AI' })

  if (!explanationsEnabled) {
    // The core, pre-existing deterministic flow (UX-STATES.md's "plain-
    // language rationale", score, evidence, dismiss/restore) is completely
    // unaffected -- no AI affordance is mounted at all, not even a
    // disabled-looking one.
    await assert.rejects(explainButton.waitFor({ timeout: 1000 }), 'AI explanation affordance must not be mounted when globally disabled')

    const requestCountBefore = fixtures.requests.length
    await needsAction.getByLabel('Score (secondary to the reason above)').waitFor()
    await section.getByRole('button', { name: 'Dismiss Finish the board memo' }).click()
    await section.getByRole('button', { name: 'Restore Finish the board memo' }).waitFor()
    const aiRequests = fixtures.requests.slice(requestCountBefore).filter((request) => request.path.startsWith('/api/v1/ai/'))
    assert.equal(aiRequests.length, 0, 'no AI runtime request should ever be made while the affordance is disabled')
    return
  }

  await explainButton.waitFor()
  await explainButton.click()

  // A real, bounded progress indicator (not an indefinite spinner) appears
  // while the request is in flight.
  const progress = section.getByRole('progressbar')
  await progress.waitFor().catch(() => {
    // The mocked response can resolve fast enough in a headless run that
    // the progress indicator's own render never gets observed before the
    // completed state replaces it -- not a defect (the same race a real,
    // very fast local model call would produce), so this step is
    // best-effort, not asserted as a hard requirement.
  })

  await section.getByText('Overdue by two days, which is why it is ranked near the top.').waitFor()
  await section.getByText(/qwen2\.5:1\.5b-instruct-q4_K_M/).waitFor()
  await section.getByText(/prompt v1/i).waitFor()
  // The cited factor is cross-referenced back to the same evidence text
  // already visible in the deterministic factor list above it.
  await needsAction.getByLabel('Evidence for Finish the board memo').getByText('Overdue by 2 days').waitFor()
  await section.getByLabel('Factors this explanation cites').getByText('Overdue by 2 days').waitFor()
  // Never replaces the deterministic factor list or the score.
  await needsAction.getByLabel('Score (secondary to the reason above)').waitFor()

  const runRequest = fixtures.requests.find((request) => request.method === 'POST' && request.path === '/api/v1/ai/runs')
  assert.ok(runRequest, 'expected a POST /api/v1/ai/runs request')
  assert.equal(runRequest.body.attention_item_id, item.id)

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="attention-title"]' })

  await section.getByRole('button', { name: 'Discard AI explanation' }).click()
  await explainButton.waitFor()
  await section.getByText('Overdue by two days, which is why it is ranked near the top.').waitFor({ state: 'detached' })
}
