import { useRef, useState, type KeyboardEvent } from 'react'

import { moveWorkspaceFocus } from '../../navigation/WorkspaceNavigation'
import MembersPanel from './MembersPanel'
import SharingReview from './SharingReview'
import DelegationsPanel from './DelegationsPanel'
import ActivityPanel from './ActivityPanel'

type CollaborationView = 'members' | 'sharing' | 'delegations' | 'activity'

const TABS: ReadonlyArray<{ view: CollaborationView; label: string }> = [
  { view: 'members', label: 'Members' },
  { view: 'sharing', label: 'Sharing' },
  { view: 'delegations', label: 'Delegations' },
  { view: 'activity', label: 'Activity' },
]

/**
 * Top-level shell for the Phase 8 collaboration surface
 * (`docs/phases/phase-008/UX-STATES.md`) -- Task 9's own "members/
 * invitations panel, sharing-review screen ... wired in here alongside the
 * rest, delegation inbox, shared activity feed, ownership-transfer and
 * member-removal flows" bullet, mirroring `PersonalWorkspace.tsx`'s
 * identical nested-tablist shell shape (own module docstring quoted
 * verbatim: "mirroring PersonalWorkspace's own domain-generic shell
 * pattern").
 *
 * Ownership-transfer and member-removal are not separate tabs -- both are
 * folded into `MembersPanel.tsx` (removal is the button on each member
 * row; ownership-transfer is the sub-flow that appears only once a removal
 * attempt is actually blocked by it), a disclosed design choice: the
 * plan's own bullet lists them alongside the other four surfaces, but
 * their real-world trigger is always "I tried to remove someone and it was
 * blocked," not an independent workflow a user would open on its own --
 * a fifth always-empty tab would be worse UX than surfacing the flow
 * exactly where it becomes relevant. The workspace switcher is not a fifth
 * tab either -- it is mounted globally in `App.tsx` (see that file's own
 * comment), since which company workspace you are in applies to every tab
 * in the whole app, not just this one.
 */
export default function CollaborationWorkspace() {
  const [view, setView] = useState<CollaborationView>('members')
  const tabs = useRef<Array<HTMLButtonElement | null>>([])

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, current: number) {
    const moved = moveWorkspaceFocus(current, event.key, tabs.current, (nextIndex) => {
      setView(TABS[nextIndex].view)
    })
    if (!moved) return
    event.preventDefault()
  }

  return (
    <section className="collaboration-workspace" aria-labelledby="collaboration-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">WORKSPACE</p>
          <h1 id="collaboration-title">Workspace collaboration</h1>
          <p>Members, invitations, sharing, delegation and shared activity for this workspace.</p>
        </div>
      </div>

      <div className="tab-list" role="tablist" aria-label="Collaboration views">
        {TABS.map((tab, tabIndex) => (
          <button
            key={tab.view}
            id={`collaboration-tab-${tab.view}`}
            ref={(element) => { tabs.current[tabIndex] = element }}
            type="button"
            role="tab"
            aria-controls="collaboration-panel"
            aria-selected={view === tab.view}
            tabIndex={view === tab.view ? 0 : -1}
            onClick={() => setView(tab.view)}
            onKeyDown={(event) => handleKeyDown(event, tabIndex)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div id="collaboration-panel" role="tabpanel" aria-labelledby={`collaboration-tab-${view}`}>
        {view === 'members' ? <MembersPanel />
          : view === 'sharing' ? <SharingReview />
          : view === 'delegations' ? <DelegationsPanel />
          : <ActivityPanel />}
      </div>
    </section>
  )
}
