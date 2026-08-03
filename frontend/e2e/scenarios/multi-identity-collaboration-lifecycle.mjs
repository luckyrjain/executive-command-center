import assert from 'node:assert/strict'

import { createFixtureApi, createCollaborationStore } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const now = new Date('2026-07-31T12:00:00Z')

function iso(offsetMs = 0) {
  return new Date(now.getTime() + offsetMs).toISOString()
}

const ALICE = {
  accountId: 'account-alice', usersId: 'users-alice', email: 'alice@example.test', displayName: 'Alice',
  workspaceId: 'workspace-northwind',
}
const BOB = {
  accountId: 'account-bob', usersId: 'users-bob', email: 'bob@example.test', displayName: 'Bob',
  workspaceId: 'workspace-bobco',
}

/**
 * Phase 8 Task 9's own required deliverable: "Playwright multi-identity
 * acceptance: two real accounts/sessions in one test, covering invite ->
 * accept -> share -> revoke -> delegate -> remove end to end". No existing
 * scenario in this directory drives two authenticated identities at once --
 * `createCollaborationStore` (see `fixtures.mjs`'s own docstring) is what
 * makes it possible: both identities' `createFixtureApi` calls below share
 * the exact same mutable store, so an invitation Alice creates is the same
 * object Bob's browser context reads back when accepting it, and a
 * membership Bob's accept mutates is what Alice's next members-list fetch
 * (after a reload) sees.
 *
 * Alice and Bob each start with their own pre-existing workspace (a
 * realistic starting point -- nobody's account is workspace-less) --
 * `workspace-northwind` is the one this whole scenario actually
 * collaborates in; `workspace-bobco` exists only so Bob has a *second*
 * workspace to originate from and so `WorkspaceSwitcher` has something
 * real to switch between once he joins Northwind.
 *
 * The owned-resources-blocks-removal sub-flow is deliberately NOT exercised
 * here -- it already has direct component coverage in
 * `MembersPanel.test.tsx`, and Bob never owns a Northwind resource in this
 * scenario, so removal at the end is the plain, unblocked path.
 */
export async function run({ page, baseURL }) {
  const store = createCollaborationStore({
    workspaces: [
      { id: 'workspace-northwind', name: 'Northwind', timezone: 'UTC', created_at: iso(-90 * 24 * 60 * 60 * 1000) },
      { id: 'workspace-bobco', name: 'Bob & Co', timezone: 'UTC', created_at: iso(-90 * 24 * 60 * 60 * 1000) },
    ],
    memberships: [
      { workspace_id: 'workspace-northwind', account_id: ALICE.accountId, users_id: ALICE.usersId, role: 'owner', status: 'active', created_at: iso(-90 * 24 * 60 * 60 * 1000) },
      { workspace_id: 'workspace-bobco', account_id: BOB.accountId, users_id: BOB.usersId, role: 'owner', status: 'active', created_at: iso(-90 * 24 * 60 * 60 * 1000) },
    ],
  })

  await createFixtureApi(page, { collaborationStore: store, collaborationSelf: ALICE })

  const browser = page.context().browser()
  const bobContext = await browser.newContext()
  const bobPage = await bobContext.newPage()
  await createFixtureApi(bobPage, { collaborationStore: store, collaborationSelf: BOB })

  try {
    // --- Invite: Alice invites bob@example.test into Northwind -------------
    await page.goto(baseURL)
    await page.getByRole('tab', { name: 'Team' }).click()
    await page.getByRole('heading', { name: 'Workspace collaboration', level: 1 }).waitFor()
    await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })

    const alicePanel = page.locator('#collaboration-panel')
    await alicePanel.getByLabel('Invitee email').fill(BOB.email)
    await alicePanel.getByRole('button', { name: 'Invite' }).click()
    await alicePanel.getByText(BOB.email, { exact: true }).waitFor()
    assert.equal(store.invitations.length, 1)
    const invitation = store.invitations[0]
    assert.equal(invitation.email, BOB.email)

    // --- Accept: Bob, starting from his own workspace, joins Northwind -----
    await bobPage.goto(baseURL)
    await bobPage.getByRole('tab', { name: 'Team' }).click()
    await bobPage.getByRole('heading', { name: 'Workspace collaboration', level: 1 }).waitFor()
    await assertNoSeriousAccessibilityViolations(bobPage, { include: '#workspace-panel' })

    const bobPanel = bobPage.locator('#collaboration-panel')
    await bobPanel.getByLabel('Invitation ID').fill(invitation.id)
    await bobPanel.getByLabel('Invitation token').fill(invitation.token)
    await bobPanel.getByRole('button', { name: 'Accept invitation' }).click()
    await bobPanel.getByText(/Joined\./).waitFor()
    assert.equal(store.memberships.some((m) => m.workspace_id === 'workspace-northwind' && m.account_id === BOB.accountId && m.status === 'active'), true)

    // Bob now belongs to two workspaces -- the switcher (mounted globally,
    // above the tab strip) becomes visible for the first time in this test.
    // `WorkspaceSwitcher.tsx` calls `window.location.reload()` on success --
    // `waitForEvent('load')` must be registered BEFORE the selection fires
    // (via `Promise.all`, the standard Playwright idiom for an action that
    // triggers navigation) or the reload can complete before this test
    // starts waiting for it.
    await Promise.all([
      bobPage.waitForEvent('load'),
      bobPage.getByLabel('Switch workspace').selectOption({ label: 'Northwind' }),
    ])
    await bobPage.getByRole('tab', { name: 'Team' }).click()
    await bobPage.getByRole('heading', { name: 'Workspace collaboration', level: 1 }).waitFor()

    // --- Alice now sees Bob as a Northwind member ---------------------------
    await page.reload()
    await page.getByRole('tab', { name: 'Team' }).click()
    await alicePanel.getByText('Bob', { exact: true }).waitFor()

    // --- Share: Alice grants Bob read access to an engineering resource ----
    await page.getByRole('tab', { name: 'Sharing' }).click()
    await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })
    await alicePanel.getByLabel('Resource type').fill('incidents')
    await alicePanel.getByLabel('Resource ID').fill('incident-42')
    await alicePanel.getByLabel('Grantee account ID').fill(BOB.accountId)
    await alicePanel.getByRole('button', { name: 'Preview sharing' }).click()
    await alicePanel.getByRole('button', { name: 'Confirm and share' }).click()
    await alicePanel.getByText(new RegExp(`grantee ${BOB.accountId}`)).waitFor()
    assert.equal(store.grants.length, 1)
    const grant = store.grants[0]
    assert.equal(grant.grantee_account_id, BOB.accountId)

    // --- Revoke: Alice revokes the grant she just created -------------------
    await alicePanel.getByRole('button', { name: 'Revoke' }).click()
    await alicePanel.getByText(/^Revoked/).waitFor()
    assert.equal(store.grants[0].revoked_at != null, true)

    // --- Delegate: Alice delegates an obligation to Bob ---------------------
    await page.getByRole('tab', { name: 'Delegations' }).click()
    await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })
    await alicePanel.getByLabel('Recipient account ID').fill(BOB.accountId)
    await alicePanel.getByLabel('Obligation resource type').fill('incidents')
    await alicePanel.getByLabel('Obligation resource ID').fill('incident-42')
    await alicePanel.getByLabel('Expected outcome').fill('Resolve incident-42')
    await alicePanel.getByLabel('Due date').fill('2099-01-01T00:00')
    await alicePanel.getByRole('button', { name: 'Propose delegation' }).click()
    await alicePanel.getByText('Resolve incident-42').waitFor()
    assert.equal(store.delegations.length, 1)

    await bobPage.reload()
    await bobPage.getByRole('tab', { name: 'Team' }).click()
    await bobPage.getByRole('tab', { name: 'Delegations' }).click()
    await bobPanel.getByText('Resolve incident-42').waitFor()
    await bobPanel.getByRole('button', { name: 'Accept' }).click()
    await bobPanel.getByText(/Accepted/).waitFor()
    assert.equal(store.delegations[0].status, 'accepted')

    // --- Shared activity: Alice can see delegation/grant events -------------
    await page.getByRole('tab', { name: 'Activity' }).click()
    await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })
    await alicePanel.getByText('grant.created').waitFor()

    // --- Remove: Alice removes Bob from Northwind ----------------------------
    await page.getByRole('tab', { name: 'Members' }).click()
    await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })
    // A plain string `hasText` filter is case-insensitive in Playwright, so
    // `'Bob'` would also match the Invitations list's own
    // "bob@example.test..." row -- a case-sensitive RegExp (no `i` flag)
    // narrows this to the member row's own `<strong>Bob</strong>`.
    const bobRow = alicePanel.locator('li', { hasText: /Bob/ })
    await bobRow.getByRole('button', { name: 'Remove' }).click()
    await bobRow.getByRole('button', { name: 'Confirm removal' }).click()
    await bobRow.waitFor({ state: 'detached' })
    assert.equal(
      store.memberships.find((m) => m.workspace_id === 'workspace-northwind' && m.account_id === BOB.accountId)?.status,
      'removed',
    )
  } finally {
    await bobContext.close()
  }
}
