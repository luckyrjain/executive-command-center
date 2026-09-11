import { describe, expect, it } from 'vitest'

import { pathForView, viewForPath, WORKSPACES } from './workspaces'

describe('workspaces', () => {
  it('has exactly 15 entries, each with a unique view and a unique path', () => {
    expect(WORKSPACES).toHaveLength(15)
    expect(new Set(WORKSPACES.map((w) => w.view)).size).toBe(15)
    expect(new Set(WORKSPACES.map((w) => w.path)).size).toBe(15)
  })

  it('maps collaboration to /team, not /collaboration', () => {
    const team = WORKSPACES.find((w) => w.view === 'collaboration')
    expect(team?.path).toBe('/team')
    expect(team?.label).toBe('Team')
  })

  it('resolves a path back to its view, and back again', () => {
    expect(viewForPath('/risks')).toBe('risks')
    expect(pathForView('risks')).toBe('/risks')
    expect(viewForPath('/team')).toBe('collaboration')
    expect(pathForView('collaboration')).toBe('/team')
  })

  it('returns null for an unknown path', () => {
    expect(viewForPath('/does-not-exist')).toBeNull()
  })

  it('groups Today/Attention/Recommendations under no header, and groups the rest into 4 named sections', () => {
    const ungrouped = WORKSPACES.filter((w) => w.group === null).map((w) => w.view)
    expect(ungrouped).toEqual(['today', 'attention', 'recommendations'])

    const work = WORKSPACES.filter((w) => w.group === 'work').map((w) => w.view)
    expect(work).toEqual(['work', 'notes', 'schedule', 'planner', 'meeting-prep'])

    const riskKnowledge = WORKSPACES.filter((w) => w.group === 'risk-knowledge').map((w) => w.view)
    expect(riskKnowledge).toEqual(['risks', 'knowledge', 'search-audit'])

    const systems = WORKSPACES.filter((w) => w.group === 'systems').map((w) => w.view)
    expect(systems).toEqual(['automation', 'engineering'])

    const account = WORKSPACES.filter((w) => w.group === 'account').map((w) => w.view)
    expect(account).toEqual(['personal', 'collaboration'])
  })
})
