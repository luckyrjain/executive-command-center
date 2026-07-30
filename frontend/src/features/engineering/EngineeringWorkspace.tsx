import { useRef, useState, type KeyboardEvent } from 'react'

import { moveWorkspaceFocus } from '../../navigation/WorkspaceNavigation'
import EngineeringOverview from './EngineeringOverview'
import DeliveryPanel from './DeliveryPanel'
import ReliabilityPanel from './ReliabilityPanel'
import RepositoriesPanel from './RepositoriesPanel'
import WorkItemsPanel from './WorkItemsPanel'
import IncidentsPanel from './IncidentsPanel'
import DecisionsPanel from './DecisionsPanel'
import ConnectorHealthPanel from './ConnectorHealthPanel'
import CoveragePanel from './CoveragePanel'
import type { EngineeringView } from './types'

const TABS: ReadonlyArray<{ view: EngineeringView; label: string }> = [
  { view: 'overview', label: 'Overview' },
  { view: 'delivery', label: 'Delivery' },
  { view: 'reliability', label: 'Reliability' },
  { view: 'repositories', label: 'Repositories' },
  { view: 'work-items', label: 'Work items' },
  { view: 'incidents', label: 'Incidents' },
  { view: 'decisions', label: 'Decisions' },
  { view: 'connector-health', label: 'Connector health' },
  { view: 'coverage', label: 'Source coverage' },
]

/**
 * Top-level shell for the Phase 6 Engineering Workspace surface
 * (`docs/phases/phase-006/UX-STATES.md`: "Engineering overview, delivery,
 * reliability, repositories, incidents, decisions, connector health and
 * source coverage"). `work-items` is a later addition beyond that
 * original eight-surface list -- `GET /engineering/work-items` and its
 * `POST .../team` confirm endpoint existed since migration `0050_phase6_
 * team_linkage.py` with no frontend surface reading or writing them at
 * all (`CoveragePanel.tsx` only ever used the list response for a rollup
 * count); `WorkItemsPanel.tsx` closes that gap, mirroring `Repositories
 * Panel.tsx`'s shape exactly. One `WorkspaceView` slot ('engineering'),
 * with its own internal tablist -- mirrors `AutomationWorkspace.tsx`'s
 * identical nested-tablist shape (reuses `WorkspaceNavigation.tsx`'s own
 * roving-tabindex helper rather than re-deriving the same keyboard logic
 * a second time).
 */
export default function EngineeringWorkspace() {
  const [view, setView] = useState<EngineeringView>('overview')
  const tabs = useRef<Array<HTMLButtonElement | null>>([])

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, current: number) {
    const moved = moveWorkspaceFocus(current, event.key, tabs.current, (nextIndex) => {
      setView(TABS[nextIndex].view)
    })
    if (!moved) return
    event.preventDefault()
  }

  return (
    <section className="engineering-workspace" aria-labelledby="engineering-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">ENGINEERING</p>
          <h1 id="engineering-title">Engineering workspace</h1>
          <p>Repositories from every connected GitHub or GitLab account, work items from every connected Jira account, plus delivery and reliability metrics, incidents and decisions.</p>
        </div>
      </div>

      <div className="tab-list" role="tablist" aria-label="Engineering views">
        {TABS.map((tab, tabIndex) => (
          <button
            key={tab.view}
            id={`engineering-tab-${tab.view}`}
            ref={(element) => { tabs.current[tabIndex] = element }}
            type="button"
            role="tab"
            aria-controls="engineering-panel"
            aria-selected={view === tab.view}
            tabIndex={view === tab.view ? 0 : -1}
            onClick={() => setView(tab.view)}
            onKeyDown={(event) => handleKeyDown(event, tabIndex)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div id="engineering-panel" role="tabpanel" aria-labelledby={`engineering-tab-${view}`}>
        {view === 'overview' ? <EngineeringOverview onNavigate={setView} />
          : view === 'delivery' ? <DeliveryPanel />
          : view === 'reliability' ? <ReliabilityPanel />
          : view === 'repositories' ? <RepositoriesPanel />
          : view === 'work-items' ? <WorkItemsPanel />
          : view === 'incidents' ? <IncidentsPanel />
          : view === 'decisions' ? <DecisionsPanel />
          : view === 'connector-health' ? <ConnectorHealthPanel />
          : <CoveragePanel />}
      </div>
    </section>
  )
}
