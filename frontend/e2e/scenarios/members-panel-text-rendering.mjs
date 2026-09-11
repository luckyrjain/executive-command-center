import { createCollaborationStore, createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

// A real member row renders `{member.email} · account {member.account_id}`
// verbatim (`MembersPanel.tsx`) inside a `<small>` styled by `.work-list
// small` -- a rule that used to carry `text-transform: capitalize`
// (`styles.css`). CSS `capitalize` upper-cases the first letter after every
// word boundary, and a browser's own word-boundary algorithm treats `@`,
// `.` and `-` as boundaries too, not just whitespace: it silently mangled
// an email into `Local@Example.Com` and a UUID account id into
// `B521529c-2613-44e0-8496-B797ac578dff` on screen, even though the
// underlying data was always lowercase. This account id and email are
// chosen to exercise exactly those three boundary characters.
const WORKSPACE_ID = 'workspace-rendering-check'
const ACCOUNT_ID = 'b521529c-2613-44e0-8496-b797ac578dff'
const MEMBER_EMAIL = 'local.user@example.com'

/**
 * Regression test for the `.work-list small` capitalize bug: a member row's
 * email and account id must render byte-for-byte as the API returned them,
 * not title-cased by CSS. `getByText(..., { exact: true })` is the
 * assertion that actually catches this -- a case-insensitive/substring
 * match would pass against either the correct or the mangled rendering.
 */
export async function run({ page, baseURL }) {
  const collaborationStore = createCollaborationStore({
    workspaces: [{ id: WORKSPACE_ID, name: 'Rendering Check Co', timezone: 'UTC', created_at: '2026-01-01T00:00:00Z' }],
    memberships: [{
      workspace_id: WORKSPACE_ID, account_id: ACCOUNT_ID, users_id: 'users-rendering-check',
      role: 'owner', status: 'active', created_at: '2026-01-01T00:00:00Z',
    }],
  })
  const collaborationSelf = {
    accountId: ACCOUNT_ID, usersId: 'users-rendering-check', email: MEMBER_EMAIL, displayName: 'local.user',
    workspaceId: WORKSPACE_ID,
  }
  await createFixtureApi(page, { collaborationStore, collaborationSelf })

  await page.goto(`${baseURL}/team`)

  const membersSection = page.locator('section[aria-labelledby="members-title"]')
  await membersSection.getByRole('heading', { name: 'Members', level: 2 }).waitFor()
  await membersSection.getByText(`· ${MEMBER_EMAIL} · account ${ACCOUNT_ID}`, { exact: true }).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="members-title"]' })
}
