import { chromium } from 'playwright'

import { startPreviewServer } from './server.mjs'
import * as tasks from './scenarios/tasks.mjs'
import * as commitments from './scenarios/commitments.mjs'
import * as notes from './scenarios/notes.mjs'
import * as schedule from './scenarios/schedule.mjs'
import * as searchCalendar from './scenarios/search-calendar.mjs'
import * as dashboardBrief from './scenarios/dashboard-brief.mjs'
import * as recommendationExecution from './scenarios/recommendation-execution.mjs'
import * as recommendationDecisions from './scenarios/recommendation-decisions.mjs'
import * as recommendationTerminals from './scenarios/recommendation-terminals.mjs'
import * as conflictAuditKeyboard from './scenarios/conflict-audit-keyboard.mjs'

const scenarios = [
  { name: 'tasks', module: tasks },
  { name: 'commitments', module: commitments },
  { name: 'notes', module: notes },
  { name: 'schedule', module: schedule },
  { name: 'search-calendar', module: searchCalendar },
  { name: 'dashboard-brief', module: dashboardBrief },
  { name: 'recommendation-execution', module: recommendationExecution },
  { name: 'recommendation-decisions', module: recommendationDecisions },
  { name: 'recommendation-terminals', module: recommendationTerminals },
  { name: 'conflict-audit-keyboard', module: conflictAuditKeyboard },
]

async function main() {
  const server = await startPreviewServer()
  const browser = await chromium.launch({ headless: true })
  const failures = []

  try {
    for (const scenario of scenarios) {
      const context = await browser.newContext()
      const page = await context.newPage()
      const startedAt = Date.now()
      try {
        await scenario.module.run({ page, baseURL: server.baseURL })
        console.log(`✓ ${scenario.name} (${Date.now() - startedAt}ms)`)
      } catch (error) {
        console.error(`✗ ${scenario.name}`)
        console.error(error)
        failures.push(scenario.name)
      } finally {
        await context.close()
      }
    }
  } finally {
    await browser.close()
    server.stop()
  }

  if (failures.length) {
    throw new Error(`${failures.length}/${scenarios.length} scenario(s) failed: ${failures.join(', ')}`)
  }

  console.log(`All ${scenarios.length} scenarios (each including an accessibility check) passed.`)
}

await main()
