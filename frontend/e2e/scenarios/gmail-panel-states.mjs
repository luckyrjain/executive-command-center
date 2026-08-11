import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const now = new Date('2026-08-11T12:00:00Z')

function iso(offsetMs = 0) {
  return new Date(now.getTime() + offsetMs).toISOString()
}

const emailDomain = {
  id: 'domain-email', domain_key: 'email', classification: 'high_stakes', enabled: true,
  enabled_at: iso(-2 * 60 * 60 * 1000), version: 1, created_at: iso(-2 * 60 * 60 * 1000), updated_at: iso(-2 * 60 * 60 * 1000),
}

function gmailConnector(overrides) {
  return {
    id: 'gmail-connector-1', provider: 'gmail', external_account_id: 'owner@example.test',
    display_name: 'owner@example.test', granted_scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
    status: 'active', status_detail: null, last_synced_at: null, last_error: null, disconnected_at: null,
    version: 1, created_at: iso(-2 * 60 * 60 * 1000), updated_at: iso(-2 * 60 * 60 * 1000),
    ...overrides,
  }
}

const seedEmailRecommendation = {
  id: 'rec-email-1',
  recommendation_type: 'email_action_detected',
  target_type: 'commitment',
  target_id: null,
  proposed_action: { operation: 'create', value: null },
  proposed_fields: { summary: 'Send signed contract to Priya', due_at: iso(3 * 24 * 60 * 60 * 1000) },
  expected_version: null,
  rationale: 'Priya asked for the signed contract by Friday.',
  confidence: 0.81,
  status: 'pending_confirmation',
  evidence_ids: [],
  execution_result: null,
  source: 'rule',
  pinned: false,
  version: 1,
}

const seedOtherRecommendation = {
  id: 'rec-other-1',
  recommendation_type: 'close_risk',
  target_type: 'risk',
  target_id: 'risk-other-1',
  proposed_action: { operation: 'update_status', status: 'closed' },
  expected_version: 1,
  rationale: 'Unrelated recommendation that must never appear inside the Gmail panel.',
  confidence: 0.5,
  status: 'pending_confirmation',
  evidence_ids: [],
  execution_result: null,
  source: 'rule',
  pinned: false,
  version: 1,
}

/**
 * Phase 10 Gmail Connector Task 8 (`docs/superpowers/plans/2026-08-04-
 * phase-10-gmail-connector.md` Task 8, `docs/phases/phase-010/UX-STATES.md`):
 * `GmailPanel` inside the Personal workspace shell. Three page loads, not
 * one continuous flow -- `GmailPanel`'s own "Connect" action is a real
 * top-level browser navigation to Google (unlike every other Personal-
 * workspace action, which stays inside the SPA), so nothing after that
 * click can be reached by continuing forward in the same page the way
 * `personal-domain-lifecycle.mjs`'s own single-fixture flow works. States
 * unreachable through any live action in this activation (Google's own
 * OAuth callback lands on a bare backend JSON endpoint, not a page this
 * app renders, per `API-SCHEMAS.md`) are pre-seeded instead, the same
 * "distinct fixture state" choice `engineering-connector-states.mjs`'s own
 * docstring explains.
 *
 * Load 1 -- allowlist-denied and consent-missing, both reached live.
 * Load 2 -- connected: first sync -> thread list -> thread read/forget ->
 *   pending-recommendation review (reusing `RecommendationPanel`, filtered
 *   to `email_action_detected`) -> expand-history sync -> disconnect via
 *   the domain-level endpoint (consent was active).
 * Load 3 -- permission-lost, a state Load 2's own live actions cannot
 *   produce (nothing in this app revokes a connector's own permissions).
 */
export async function run({ page, baseURL }) {
  // --- Load 1: allowlist-denied, then consent-missing + a real OAuth
  // redirect once an admin flips the allowlist -------------------------
  const fixtures = await createFixtureApi(page, {
    personal: { domains: [], gmailAllowlisted: false },
    engineering: { connectors: [] },
  })
  // The real OAuth redirect target is Google itself -- intercepted here so
  // the browser never actually leaves this test's control (no real network
  // call, no dependency on Google's own servers being reachable from CI).
  // Page-level routes take precedence over the context-level catch-all
  // `createFixtureApi` registers, so this only affects this one origin.
  await page.route('https://accounts.google.com/**', (route) => route.fulfill({
    status: 200, contentType: 'text/html', body: '<html><body>Fixture Google consent screen</body></html>',
  }))

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Personal' }).click()
  await page.getByRole('heading', { name: 'Personal workspace', level: 1 }).waitFor()
  await page.getByRole('tab', { name: 'Gmail' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })

  let panel = page.locator('#personal-panel')
  await panel.getByText(/Enable Email in the Domains tab/).waitFor()
  await panel.getByRole('button', { name: 'Connect Gmail' }).click()
  await panel.getByText(/not on the internal allowlist/).waitFor()

  fixtures.personal.gmailAllowlist.allowlisted = true
  await panel.getByRole('button', { name: 'Connect Gmail' }).click()
  await page.waitForURL(/^https:\/\/accounts\.google\.com\//)
  await page.getByText('Fixture Google consent screen').waitFor()

  // --- Load 2: connected -- simulates the user having completed OAuth and
  // enabled the email domain out of band, since this app has no live path
  // back from Google's own consent screen ---------------------------------
  fixtures.personal.domains.push({ ...emailDomain })
  fixtures.engineering.connectors.push(gmailConnector())
  fixtures.collections.recommendations.items.push({ ...seedEmailRecommendation }, { ...seedOtherRecommendation })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Personal' }).click()
  await page.getByRole('tab', { name: 'Gmail' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })
  panel = page.locator('#personal-panel')

  assert.equal(await panel.getByText(/Enable Email in the Domains tab/).count(), 0, 'consent-missing hint must not show once the email domain is enabled')
  await panel.getByText('owner@example.test').waitFor()
  // `status: 'active'` (this connector's own seed, matching the real OAuth
  // callback's own worked example in API-SCHEMAS.md) -- "first sync not yet
  // run" is `statusLabel`'s text for `status === 'pending'` specifically, a
  // distinct signal from "no sync run" (`neverSynced`, derived from `sync-
  // runs` alone), so the button label is the correct thing to wait on here.
  await panel.getByRole('button', { name: 'Run first sync' }).click()
  await panel.getByRole('button', { name: 'Sync now' }).waitFor()
  assert.equal(fixtures.engineering.syncRuns.some((r) => r.connector_account_id === 'gmail-connector-1'), true)

  // No threads seeded yet -- distinguishing "never synced" (above) from a
  // real, if empty, synced window (UX-STATES.md's own required distinction).
  await panel.getByText('No messages in the synced window.').waitFor()

  // Seeding threads after the first sync (rather than up front) proves the
  // panel actually reflects live fixture state rather than a static seed --
  // switching tabs away and back remounts `GmailPanel`, forcing a refetch.
  fixtures.personal.gmailThreads.push(
    {
      id: 'thread-1', subject: 'Signed contract needed by Friday', last_message_at: iso(-60 * 60 * 1000),
      messages: [{ id: 'msg-1', sender: 'priya@partner-co.test', sent_at: iso(-60 * 60 * 1000), direction: 'inbound', body: 'Could you please sign and return the attached contract by Friday?' }],
    },
    {
      id: 'thread-2', subject: 'Weekly newsletter', last_message_at: iso(-2 * 24 * 60 * 60 * 1000),
      messages: [{ id: 'msg-2', sender: 'newsletter@example.test', sent_at: iso(-2 * 24 * 60 * 60 * 1000), direction: 'inbound', body: null }],
    },
  )
  await page.getByRole('tab', { name: 'Domains' }).click()
  await page.getByRole('tab', { name: 'Gmail' }).click()

  const threadList = panel.getByLabel('Gmail threads')
  const rows = threadList.getByRole('listitem')
  await threadList.getByText('Signed contract needed by Friday').waitFor()
  const firstRow = rows.first()
  await firstRow.getByText('Signed contract needed by Friday').waitFor()
  const secondRow = rows.nth(1)
  await secondRow.getByText('Weekly newsletter').waitFor()
  await secondRow.getByText(/body not yet fetched/).waitFor()

  // --- Keyboard operation: reach and open the first thread with only the
  // keyboard, matching UX-STATES.md's "keyboard-reachable action" requirement
  // -- no custom widget here, so this proves native tab/enter semantics
  // rather than a bespoke roving-tabindex implementation. --------------------
  await page.getByRole('button', { name: 'Signed contract needed by Friday' }).focus()
  await page.keyboard.press('Enter')
  await panel.getByText('Could you please sign and return the attached contract by Friday?').waitFor()

  await panel.getByRole('button', { name: 'Forget cached content for this thread' }).click()
  // Waits (not a synchronous `.count()`): `onForgotten` only clears
  // `selectedThreadId` once the mutation's own async `onSuccess` fires, a
  // real gap between the click and the detail view actually unmounting.
  await panel.getByText('Could you please sign and return the attached contract by Friday?').waitFor({ state: 'detached' })
  assert.equal(fixtures.personal.gmailThreads.find((t) => t.id === 'thread-1').messages[0].body, null)

  // --- Pending-recommendation review, filtered to this domain only --------
  const recommendationsSection = panel.locator('section[aria-labelledby="recommendations-title-email_action_detected"]')
  await recommendationsSection.getByRole('heading', { name: 'Pending email actions' }).waitFor()
  await recommendationsSection.getByText('Send signed contract to Priya').waitFor()
  assert.equal(await recommendationsSection.getByText('Unrelated recommendation').count(), 0, 'a non-Gmail recommendation must never appear inside this embedded panel')
  await recommendationsSection.getByRole('button', { name: 'Confirm and execute' }).click()
  await recommendationsSection.getByText('Execution recorded.').waitFor()

  // --- Expand-history sync with a chosen since date ------------------------
  await panel.getByLabel('Sync history from date').fill('2026-01-01')
  await panel.getByRole('button', { name: 'Sync from this date' }).click()
  await panel.getByRole('button', { name: 'Sync from this date' }).waitFor() // returns to enabled once the mutation settles
  const expandRequest = fixtures.requests.find((r) => r.path === '/api/v1/engineering/connectors/gmail-connector-1/sync' && r.body?.since)
  assert.equal(expandRequest?.body.since, new Date('2026-01-01').toISOString())
  assert.equal(expandRequest?.body.run_type, 'backfill')

  // --- Disconnect through the domain-level endpoint (consent was active) --
  // The fixture's own `/domains/email/disable` route only flips `enabled`
  // (it does not replicate the real cascade's connector-disconnect/data-
  // purge side effects -- out of scope for a UI-focused fixture), so the
  // real, deterministic post-disconnect signal to wait on is the consent-
  // missing hint reappearing once `domains` refetches, not the connector
  // card disappearing.
  await panel.getByRole('button', { name: 'Disconnect' }).click()
  await panel.getByText(/Enable Email in the Domains tab/).waitFor()
  assert.equal(fixtures.personal.domains.find((d) => d.domain_key === 'email')?.enabled, false)
  assert.equal(
    fixtures.requests.some((r) => r.path === '/api/v1/personal/domains/email/disable' && r.method === 'POST'),
    true,
  )
  assert.equal(
    fixtures.requests.some((r) => r.path === '/api/v1/engineering/connectors/gmail-connector-1/disable'),
    false,
    'an active-consent disconnect must use the domain-level cascade endpoint, never the generic connector endpoint',
  )

  // --- Load 3: permission lost -- no live action in this app can revoke a
  // connector's own permissions, so this is its own pre-seeded fixture state,
  // matching `engineering-connector-states.mjs`'s identical precedent. -------
  const permissionFixtures = await createFixtureApi(page, {
    personal: { domains: [{ ...emailDomain }], gmailAllowlisted: true },
    engineering: {
      connectors: [gmailConnector({ status: 'permission_lost', last_synced_at: iso(-60 * 60 * 1000) })],
      syncRuns: [{ id: 'run-permission', connector_account_id: 'gmail-connector-1', run_type: 'incremental', status: 'succeeded', items_processed: 2, error_summary: null, started_at: iso(-60 * 60 * 1000), completed_at: iso(-59 * 60 * 1000) }],
    },
  })
  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Personal' }).click()
  await page.getByRole('tab', { name: 'Gmail' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })
  panel = page.locator('#personal-panel')
  await panel.getByText(/permission lost -- reconnect required/).waitFor()
  await panel.getByText(/Reconnect below before syncing/).waitFor()
  assert.equal(await panel.getByRole('button', { name: 'Sync now' }).isDisabled(), true)
  assert.equal(permissionFixtures.engineering.connectors[0].status, 'permission_lost')
}
