// @vitest-environment jsdom

import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { WorkspaceView } from '../api/types'
import WorkspaceNavigation, { moveWorkspaceFocus, nextWorkspaceIndex } from './WorkspaceNavigation'

afterEach(cleanup)

function NavigationHarness() {
  const [view, setView] = useState<WorkspaceView>('today')
  return (
    <>
      <WorkspaceNavigation currentView={view} onNavigate={setView} />
      <main id="workspace-main">
        <section id="workspace-panel" role="tabpanel" aria-labelledby={`workspace-tab-${view}`} />
      </main>
    </>
  )
}

describe('WorkspaceNavigation', () => {
  it('renders named workspace navigation with exactly one selected surface', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceNavigation currentView="today" onNavigate={() => undefined} />,
    )

    expect(markup).toContain('aria-label="Workspace"')
    expect(markup).toContain('Today')
    expect(markup).toContain('Work')
    expect(markup).toContain('Notes')
    expect(markup).toContain('Schedule')
    expect(markup).toContain('Risks')
    expect(markup).toContain('Knowledge')
    expect(markup).toContain('Recommendations')
    expect(markup).toContain('Search &amp; audit')
    expect(markup.match(/aria-selected="true"/g)).toHaveLength(1)
  })

  it('moves focus with horizontal arrow keys and wraps at either end', () => {
    expect(nextWorkspaceIndex(0, 'ArrowRight', 8)).toBe(1)
    expect(nextWorkspaceIndex(7, 'ArrowRight', 8)).toBe(0)
    expect(nextWorkspaceIndex(0, 'ArrowLeft', 8)).toBe(7)
    expect(nextWorkspaceIndex(3, 'Home', 8)).toBe(0)
    expect(nextWorkspaceIndex(3, 'End', 8)).toBe(7)

    const focus = [vi.fn(), vi.fn()]
    const navigate = vi.fn()
    expect(moveWorkspaceFocus(0, 'ArrowRight', focus.map((handler) => ({ focus: handler })), navigate)).toBe(true)
    expect(focus[1]).toHaveBeenCalledOnce()
    expect(navigate).toHaveBeenCalledWith(1)
  })

  it('moves rendered tab focus and selection with ArrowLeft, ArrowRight, Home, and End', () => {
    render(<NavigationHarness />)
    const tabs = screen.getAllByRole('tab')

    tabs[0].focus()
    expect(fireEvent.keyDown(tabs[0], { key: 'ArrowRight' })).toBe(false)
    expect(document.activeElement).toBe(tabs[1])
    expect(tabs[1].getAttribute('aria-selected')).toBe('true')
    expect(tabs[1].tabIndex).toBe(0)
    expect(tabs[0].tabIndex).toBe(-1)

    expect(fireEvent.keyDown(tabs[1], { key: 'End' })).toBe(false)
    expect(document.activeElement).toBe(tabs[7])
    expect(tabs[7].getAttribute('aria-selected')).toBe('true')

    expect(fireEvent.keyDown(tabs[7], { key: 'Home' })).toBe(false)
    expect(document.activeElement).toBe(tabs[0])
    expect(tabs[0].getAttribute('aria-selected')).toBe('true')

    expect(fireEvent.keyDown(tabs[0], { key: 'ArrowLeft' })).toBe(false)
    expect(document.activeElement).toBe(tabs[7])
    expect(tabs[7].tabIndex).toBe(0)
    expect(tabs.filter((tab) => tab.getAttribute('aria-selected') === 'true')).toHaveLength(1)
  })

  it('controls a labelled tab panel distinct from the application main landmark', () => {
    render(<NavigationHarness />)

    const selectedTab = screen.getByRole('tab', { selected: true })
    const panel = screen.getByRole('tabpanel')
    expect(selectedTab.getAttribute('aria-controls')).toBe(panel.id)
    expect(panel.getAttribute('aria-labelledby')).toBe(selectedTab.id)
    expect(panel.closest('main')?.id).toBe('workspace-main')
    expect(panel.id).not.toBe('workspace-main')
  })
})
