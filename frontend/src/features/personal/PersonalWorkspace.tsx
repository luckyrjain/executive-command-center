import { useRef, useState, type KeyboardEvent } from 'react'

import { moveWorkspaceFocus } from '../../navigation/WorkspaceNavigation'
import DomainsPanel from './DomainsPanel'
import RecordsPanel from './RecordsPanel'
import InsightsPanel from './InsightsPanel'
import GrantsPanel from './GrantsPanel'
import ExportDeletePanel from './ExportDeletePanel'
import type { PersonalView } from './types'

const TABS: ReadonlyArray<{ view: PersonalView; label: string }> = [
  { view: 'domains', label: 'Domains' },
  { view: 'records', label: 'Records' },
  { view: 'insights', label: 'Insights' },
  { view: 'grants', label: 'Cross-domain grants' },
  { view: 'export', label: 'Export & delete' },
]

/**
 * Top-level shell for the Phase 7 Personal workspace surface
 * (`docs/phases/phase-007/UX-STATES.md`). One `WorkspaceView` slot
 * ('personal'), with its own internal tablist -- mirrors `Engineering
 * Workspace.tsx`'s identical nested-tablist shape, reusing `Workspace
 * Navigation.tsx`'s own roving-tabindex helper.
 *
 * Deliberately domain-generic, not one bespoke surface per domain: every
 * backend endpoint here already takes a `domain_key` parameter rather than
 * exposing a per-domain route (`API-SCHEMAS.md`'s own "a caller names a
 * domain_key, never a domain-specific code path" convention), so a single
 * domain-aware UI covers all six domains with no per-domain duplication --
 * `habits`' own `goals`/`routines`/`check_ins` extras are intentionally out
 * of this task's scope (not named in the implementation plan's Task 8
 * coverage list: "enable -> capture -> grant/revoke -> inspect/dismiss
 * insight -> export -> delete").
 */
export default function PersonalWorkspace() {
  const [view, setView] = useState<PersonalView>('domains')
  const tabs = useRef<Array<HTMLButtonElement | null>>([])

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, current: number) {
    const moved = moveWorkspaceFocus(current, event.key, tabs.current, (nextIndex) => {
      setView(TABS[nextIndex].view)
    })
    if (!moved) return
    event.preventDefault()
  }

  return (
    <section className="personal-workspace" aria-labelledby="personal-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">PERSONAL</p>
          <h1 id="personal-title">Personal workspace</h1>
          <p>Your own habits, learning, travel, relationships, health and finance data -- each domain disabled by default, disconnected from every other domain and from search, and yours to export or delete at any time.</p>
        </div>
      </div>

      <div className="tab-list" role="tablist" aria-label="Personal views">
        {TABS.map((tab, tabIndex) => (
          <button
            key={tab.view}
            id={`personal-tab-${tab.view}`}
            ref={(element) => { tabs.current[tabIndex] = element }}
            type="button"
            role="tab"
            aria-controls="personal-panel"
            aria-selected={view === tab.view}
            tabIndex={view === tab.view ? 0 : -1}
            onClick={() => setView(tab.view)}
            onKeyDown={(event) => handleKeyDown(event, tabIndex)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div id="personal-panel" role="tabpanel" aria-labelledby={`personal-tab-${view}`}>
        {view === 'domains' ? <DomainsPanel />
          : view === 'records' ? <RecordsPanel />
          : view === 'insights' ? <InsightsPanel />
          : view === 'grants' ? <GrantsPanel />
          : <ExportDeletePanel />}
      </div>
    </section>
  )
}
