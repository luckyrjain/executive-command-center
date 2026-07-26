import { useRef, useState, type KeyboardEvent } from 'react'

import { moveWorkspaceFocus } from '../../navigation/WorkspaceNavigation'
import WorkflowsPanel from './WorkflowsPanel'
import ApprovalInbox from './ApprovalInbox'
import RunWorkspace from './RunWorkspace'
import PolicyPanel from './PolicyPanel'
import KillSwitchPanel from './KillSwitchPanel'

type AutomationView = 'workflows' | 'approvals' | 'runs' | 'policies' | 'kill-switches'

const TABS: ReadonlyArray<{ view: AutomationView; label: string }> = [
  { view: 'workflows', label: 'Workflows' },
  { view: 'approvals', label: 'Approvals' },
  { view: 'runs', label: 'Runs' },
  { view: 'policies', label: 'Policies' },
  { view: 'kill-switches', label: 'Kill switches' },
]

/**
 * Top-level shell for the Phase 5 automation surface (`docs/phases/
 * PHASE-005-automation.md`'s Frontend changes: "workflow list/builder,
 * simulation, authority/policy review, approval inbox, schedule controls,
 * run history and recovery views"). One `WorkspaceView` slot ('automation'),
 * with its own internal tablist -- mirrors `SearchAuditPanel.tsx`'s nested-
 * tablist shape (reuses `WorkspaceNavigation.tsx`'s own roving-tabindex
 * helper rather than re-deriving the same keyboard logic a second time).
 * "Schedule controls" and simulation live inside the Workflows tab
 * (`WorkflowsPanel`/`WorkflowDetail`) since both are properties of one
 * specific workflow, not a standalone list of their own -- see this
 * feature's own judgment-calls note in IMPLEMENTATION-STATUS.md.
 */
export default function AutomationWorkspace() {
  const [view, setView] = useState<AutomationView>('workflows')
  const tabs = useRef<Array<HTMLButtonElement | null>>([])

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, current: number) {
    const moved = moveWorkspaceFocus(current, event.key, tabs.current, (nextIndex) => {
      setView(TABS[nextIndex].view)
    })
    if (!moved) return
    event.preventDefault()
  }

  return (
    <section className="automation-workspace" aria-labelledby="automation-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">AUTOMATION</p>
          <h1 id="automation-title">Workflows &amp; approvals</h1>
          <p>Author, simulate, authorize and run bounded local workflows. Every side effect is previewed and approved before it happens.</p>
        </div>
      </div>

      <div className="tab-list" role="tablist" aria-label="Automation views">
        {TABS.map((tab, tabIndex) => (
          <button
            key={tab.view}
            id={`automation-tab-${tab.view}`}
            ref={(element) => { tabs.current[tabIndex] = element }}
            type="button"
            role="tab"
            aria-controls="automation-panel"
            aria-selected={view === tab.view}
            tabIndex={view === tab.view ? 0 : -1}
            onClick={() => setView(tab.view)}
            onKeyDown={(event) => handleKeyDown(event, tabIndex)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div id="automation-panel" role="tabpanel" aria-labelledby={`automation-tab-${view}`}>
        {view === 'workflows' ? <WorkflowsPanel />
          : view === 'approvals' ? <ApprovalInbox />
          : view === 'runs' ? <RunWorkspace />
          : view === 'policies' ? <PolicyPanel />
          : <KillSwitchPanel />}
      </div>
    </section>
  )
}
