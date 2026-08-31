import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

/**
 * "No risks yet" has no coverage anywhere else in the suite --
 * conflict-audit-keyboard.mjs, the only other scenario that visits the
 * Risks tab, always seeds one risk. `createFixtureApi` defaults `risks` to
 * `[]` when unset, so this scenario reaches the true empty state without
 * any fixture override.
 */
export async function run({ page, baseURL }) {
  await createFixtureApi(page)

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Risks' }).click()

  const risksSection = page.locator('section[aria-labelledby="risks-title"]')
  await risksSection.getByRole('heading', { name: 'Risks', level: 1 }).waitFor()
  await risksSection.getByText('No risks yet. Create one above to get started.').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="risks-title"]' })
}
