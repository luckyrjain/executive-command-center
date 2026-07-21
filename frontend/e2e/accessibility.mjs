import assert from 'node:assert/strict'

import AxeBuilder from '@axe-core/playwright'

const SERIOUS_IMPACTS = new Set(['serious', 'critical'])

function describeViolation(violation) {
  const targets = violation.nodes.map((node) => node.target.join(' ')).join(', ')
  return `${violation.id} (${violation.impact}): ${violation.help} — targets: ${targets}`
}

/**
 * Runs an axe-core accessibility scan against the current page state and fails
 * (throws) if any violation with impact "serious" or "critical" is found.
 * Moderate/minor findings are ignored so the suite stays focused on defects
 * that materially block assistive technology users.
 */
export async function assertNoSeriousAccessibilityViolations(page, { include } = {}) {
  let builder = new AxeBuilder({ page })
  if (include) builder = builder.include(include)
  const results = await builder.analyze()
  const serious = results.violations.filter((violation) => SERIOUS_IMPACTS.has(violation.impact))
  assert.equal(
    serious.length,
    0,
    `Expected no serious/critical accessibility violations, found ${serious.length}:\n${serious.map(describeViolation).join('\n')}`,
  )
}
