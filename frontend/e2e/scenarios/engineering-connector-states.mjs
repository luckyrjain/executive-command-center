import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const now = new Date('2026-07-27T12:00:00Z')

function iso(offsetMs = 0) {
  return new Date(now.getTime() + offsetMs).toISOString()
}

function connector(overrides) {
  return {
    external_account_id: 'fixture-account',
    granted_scopes: ['repo'],
    status_detail: null,
    last_synced_at: null,
    last_error: null,
    disconnected_at: null,
    version: 1,
    created_at: iso(-30 * 24 * 60 * 60 * 1000),
    updated_at: iso(),
    ...overrides,
  }
}

/**
 * `docs/phases/phase-006/UX-STATES.md`'s required degraded states, covered
 * through Connector health and Repositories: first sync, backfill, partial
 * permissions, stale connector, rate limited, provider unavailable, and a
 * live disconnect action -- each connector below is a distinct, pre-seeded
 * fixture row rather than one connector cycling through every state (these
 * are independent account-level conditions, not a single lifecycle), mirror-
 * ing how `automation-lifecycle.mjs` seeds distinct runs for distinct states
 * it cannot otherwise reach without a real worker.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    engineering: {
      connectors: [
        connector({ id: 'conn-pending', provider: 'github', display_name: 'Acme GitHub (new)', status: 'pending' }),
        connector({ id: 'conn-backfilling', provider: 'github', display_name: 'Acme GitHub (backfilling)', status: 'pending' }),
        connector({ id: 'conn-active', provider: 'github', display_name: 'Acme GitHub (active)', status: 'active', last_synced_at: iso(-60 * 60 * 1000) }),
        connector({ id: 'conn-permission', provider: 'gitlab', display_name: 'Acme GitLab', status: 'permission_lost', status_detail: 'Scope revoked by an administrator', last_synced_at: iso(-60 * 60 * 1000) }),
        connector({ id: 'conn-stale', provider: 'github', display_name: 'Acme GitHub (stale)', status: 'active', last_synced_at: iso(-30 * 24 * 60 * 60 * 1000) }),
        connector({ id: 'conn-rate-limited', provider: 'gitlab', display_name: 'Acme GitLab (rate limited)', status: 'rate_limited', last_synced_at: iso(-60 * 60 * 1000) }),
        connector({ id: 'conn-error', provider: 'jira', display_name: 'Acme Jira', status: 'error', last_error: 'Jira returned 503 Service Unavailable', last_synced_at: iso(-60 * 60 * 1000) }),
        connector({ id: 'conn-to-disconnect', provider: 'github', display_name: 'Acme GitHub (to disconnect)', status: 'active', last_synced_at: iso(-60 * 60 * 1000) }),
      ],
      syncRuns: [
        { id: 'run-backfill', connector_account_id: 'conn-backfilling', run_type: 'backfill', status: 'running', items_processed: 4, error_summary: null, started_at: iso(-60_000), completed_at: null },
      ],
      repositories: [
        { id: 'repo-active', connector_account_id: 'conn-active', provider: 'github', external_id: 'gh-1', name: 'acme/widgets', source_url: 'https://github.com/acme/widgets', default_branch: 'main', permission_state: 'active', freshness_state: 'fresh', provider_updated_at: iso(-60 * 60 * 1000), observed_at: iso(-60 * 60 * 1000), created_at: iso(-30 * 24 * 60 * 60 * 1000), updated_at: iso(-60 * 60 * 1000) },
        { id: 'repo-permission', connector_account_id: 'conn-permission', provider: 'gitlab', external_id: 'gl-1', name: 'acme/internal-tools', source_url: 'https://gitlab.com/acme/internal-tools', default_branch: 'main', permission_state: 'permission_lost', freshness_state: 'stale', provider_updated_at: iso(-30 * 24 * 60 * 60 * 1000), observed_at: iso(-30 * 24 * 60 * 60 * 1000), created_at: iso(-40 * 24 * 60 * 60 * 1000), updated_at: iso(-30 * 24 * 60 * 60 * 1000) },
      ],
      workItems: [
        { id: 'item-active', connector_account_id: 'conn-error', provider: 'jira', external_id: 'ACME-1', title: 'Investigate 503s', source_url: 'https://acme.atlassian.net/browse/ACME-1', item_type: 'Bug', status: 'In Progress', reporter_external_id: 'reporter-1', assignee_external_id: 'assignee-1', permission_state: 'active', freshness_state: 'fresh', provider_created_at: iso(-2 * 24 * 60 * 60 * 1000), observed_at: iso(-60 * 60 * 1000), created_at: iso(-2 * 24 * 60 * 60 * 1000), updated_at: iso(-60 * 60 * 1000) },
        { id: 'item-permission', connector_account_id: 'conn-permission', provider: 'gitlab', external_id: 'gl-issue-1', title: 'Rotate internal tooling credentials', source_url: 'https://gitlab.com/acme/internal-tools/-/issues/1', item_type: null, status: 'To Do', reporter_external_id: 'reporter-2', assignee_external_id: null, permission_state: 'permission_lost', freshness_state: 'stale', provider_created_at: iso(-40 * 24 * 60 * 60 * 1000), observed_at: iso(-30 * 24 * 60 * 60 * 1000), created_at: iso(-40 * 24 * 60 * 60 * 1000), updated_at: iso(-30 * 24 * 60 * 60 * 1000) },
      ],
    },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Engineering' }).click()
  await page.getByRole('heading', { name: 'Engineering workspace', level: 1 }).waitFor()
  await page.getByRole('tab', { name: 'Connector health' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })

  const panel = page.locator('#engineering-panel')

  // --- First sync: a pending connector with no sync history at all -------
  const pendingCard = panel.locator('li', { hasText: 'Acme GitHub (new)' })
  await pendingCard.getByText('first sync not yet run').waitFor()
  await pendingCard.getByText('No sync has ever run for this connector').waitFor()

  // --- Backfill: a distinct connector whose first sync is already
  // running -- "no sync has ever run" must NOT also appear here, since a
  // sync genuinely has run (started, at least) -------------------------
  const backfillingCard = panel.locator('li', { hasText: 'Acme GitHub (backfilling)' })
  await backfillingCard.getByText(/Sync history/).click()
  const backfillingSyncHistory = backfillingCard.getByLabel('Sync runs for Acme GitHub (backfilling)')
  await backfillingSyncHistory.getByText('backfill', { exact: true }).waitFor()
  await backfillingSyncHistory.getByText(/in progress/).waitFor()
  assert.equal(
    await backfillingCard.getByText('No sync has ever run for this connector').count(),
    0,
    'a connector with an already-running backfill must not also claim no sync has ever run',
  )

  // --- Partial permissions -------------------------------------------------
  const permissionCard = panel.locator('li', { hasText: 'Acme GitLab' }).filter({ hasText: 'partial permissions' })
  await permissionCard.getByText(/partial permissions -- provider revoked some access/).waitFor()
  await permissionCard.getByText(/Scope revoked by an administrator/).waitFor()

  // --- Stale connector, derived from last_synced_at's own age -------------
  const staleCard = panel.locator('li', { hasText: 'Acme GitHub (stale)' })
  await staleCard.getByText(/this connector may be showing stale data/).waitFor()

  // --- Rate limited ---------------------------------------------------------
  const rateLimitedCard = panel.locator('li', { hasText: 'rate limited' })
  await rateLimitedCard.getByText('rate limited by the provider').waitFor()

  // --- Provider unavailable, with the real error surfaced ------------------
  const errorCard = panel.locator('li', { hasText: 'Acme Jira' })
  await errorCard.getByText('provider unavailable').waitFor()
  await errorCard.getByText('Jira returned 503 Service Unavailable').waitFor()

  // --- Disconnected, reached via a real action -----------------------------
  const toDisconnectCard = panel.locator('li', { hasText: 'Acme GitHub (to disconnect)' })
  await toDisconnectCard.getByRole('button', { name: 'Disconnect' }).click()
  await toDisconnectCard.getByText('disconnected', { exact: true }).waitFor()
  assert.equal(
    fixtures.engineering.connectors.find((c) => c.id === 'conn-to-disconnect').status,
    'disconnected',
    'the disconnect action must actually flip the connector\'s own status',
  )
  assert.equal((await toDisconnectCard.getByRole('button', { name: 'Start sync' }).isDisabled()), true)

  // --- Repositories: permission/freshness states shown independently of
  // the connector account's own status ------------------------------------
  await page.getByRole('tab', { name: 'Repositories' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const reposPanel = panel.locator('section[aria-labelledby="engineering-repositories-title"]')
  // `{ exact: true }` -- migration `0050_phase6_team_linkage.py`'s new
  // per-repository team-assignment `<label>` ("Team for acme/widgets")
  // also contains this substring, so a non-exact match now resolves to
  // two elements (the repository name itself and that label).
  await reposPanel.getByText('acme/widgets', { exact: true }).waitFor()
  const permissionRepoRow = reposPanel.locator('li', { hasText: 'acme/internal-tools' })
  await permissionRepoRow.getByText('permission lost').waitFor()
  await permissionRepoRow.getByText('stale').waitFor()

  // --- Work items: the identical per-row permission/freshness disclosure
  // as Repositories, on a panel that had no frontend surface at all until
  // this task -- `GET /engineering/work-items` and its `POST .../team`
  // confirm endpoint existed since migration `0050_phase6_team_linkage.py`
  // with only `CoveragePanel`'s own unresolved-identity rollup ever
  // reading the response, never a per-item list --------------------------
  await page.getByRole('tab', { name: 'Work items' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const workItemsPanel = panel.locator('section[aria-labelledby="engineering-work-items-title"]')
  await workItemsPanel.getByText('Investigate 503s', { exact: true }).waitFor()
  const permissionItemRow = workItemsPanel.locator('li', { hasText: 'Rotate internal tooling credentials' })
  await permissionItemRow.getByText('permission lost').waitFor()
  await permissionItemRow.getByText('stale').waitFor()
}
