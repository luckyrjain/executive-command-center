import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import WorkspaceNavigation, { moveWorkspaceFocus, nextWorkspaceIndex } from './WorkspaceNavigation'

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
    expect(markup).toContain('Recommendations')
    expect(markup).toContain('Search &amp; audit')
    expect(markup.match(/aria-selected="true"/g)).toHaveLength(1)
  })

  it('moves focus with horizontal arrow keys and wraps at either end', () => {
    expect(nextWorkspaceIndex(0, 'ArrowRight', 7)).toBe(1)
    expect(nextWorkspaceIndex(6, 'ArrowRight', 7)).toBe(0)
    expect(nextWorkspaceIndex(0, 'ArrowLeft', 7)).toBe(6)
    expect(nextWorkspaceIndex(3, 'Home', 7)).toBe(0)
    expect(nextWorkspaceIndex(3, 'End', 7)).toBe(6)

    const focus = [vi.fn(), vi.fn()]
    const navigate = vi.fn()
    expect(moveWorkspaceFocus(0, 'ArrowRight', focus.map((handler) => ({ focus: handler })), navigate)).toBe(true)
    expect(focus[1]).toHaveBeenCalledOnce()
    expect(navigate).toHaveBeenCalledWith(1)
  })

  it('targets the application main landmark', () => {
    const markup = renderToStaticMarkup(
      <WorkspaceNavigation currentView="work" onNavigate={() => undefined} />,
    )

    expect(markup).toContain('aria-controls="workspace-main"')
  })
})
