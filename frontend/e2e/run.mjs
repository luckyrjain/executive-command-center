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
import * as knowledgeEntities from './scenarios/knowledge-entities.mjs'
import * as knowledgeResolution from './scenarios/knowledge-resolution.mjs'
import * as knowledgeKeyboard from './scenarios/knowledge-keyboard.mjs'
import * as attentionQueue from './scenarios/attention-queue.mjs'
import * as attentionPlanning from './scenarios/attention-planning.mjs'
import * as attentionMeetingPrep from './scenarios/attention-meeting-prep.mjs'
import * as attentionExplanation from './scenarios/attention-explanation.mjs'

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
  { name: 'knowledge-entities', module: knowledgeEntities },
  { name: 'knowledge-resolution', module: knowledgeResolution },
  { name: 'knowledge-keyboard', module: knowledgeKeyboard },
  { name: 'attention-queue', module: attentionQueue },
  { name: 'attention-planning', module: attentionPlanning },
  // attention-meeting-prep reads process.env.MEETING_PREP_AI_ENRICHMENT at
  // run time and branches its fixture accordingly -- run twice (this
  // process is invoked once per desired flag state) so both the
  // AI-enrichment-on and AI-disabled paths get real coverage, per
  // TEST-PLAN.md's Browser acceptance section. See that scenario's
  // docstring for why this is env-var-gated rather than a run.mjs param.
  { name: `attention-meeting-prep (AI enrichment ${process.env.MEETING_PREP_AI_ENRICHMENT === '1' ? 'on' : 'off'})`, module: attentionMeetingPrep },
  // attention-explanation reads process.env.AI_EXPLANATIONS_ENABLED at run
  // time and branches its fixture/frontend-runtime-override accordingly --
  // run twice for the same reason as attention-meeting-prep above (Task 6,
  // Step 3 requires proving the AI-disabled case leaves the existing
  // Attention Queue behaviorally unaffected, not just that the enabled case
  // works). See that scenario's docstring for why this is env-var-gated.
  { name: `attention-explanation (AI runtime ${process.env.AI_EXPLANATIONS_ENABLED === '0' ? 'off' : 'on'})`, module: attentionExplanation },
]

async function main() {
  const server = await startPreviewServer()
  // PLAYWRIGHT_CHROMIUM_EXECUTABLE lets a sandboxed dev environment point at
  // a pre-installed chromium binary that doesn't match the revision the
  // pinned `playwright` package would otherwise try to download. Unset in
  // CI, where `playwright install --with-deps chromium` already provisions
  // the matching revision playwright expects.
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
  const browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) })
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
