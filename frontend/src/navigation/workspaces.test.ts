import { describe, expect, it } from 'vitest'

import {
  AttentionIcon, AutomationIcon, EngineeringIcon, KnowledgeIcon, MeetingPrepIcon,
  NotesIcon, PersonalIcon, PlannerIcon, RecommendationsIcon, RisksIcon,
  ScheduleIcon, SearchAuditIcon, TeamIcon, TodayIcon, WorkIcon,
} from './icons'
import { compositionForPath, pathForView, viewForPath, WORKSPACES } from './workspaces'

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

  it('gives every workspace a composition and pins the canvas/cards split', () => {
    const canvas = WORKSPACES.filter((w) => w.composition === 'canvas').map((w) => w.view)
    const cards = WORKSPACES.filter((w) => w.composition === 'cards').map((w) => w.view)
    expect(canvas).toEqual([
      'recommendations', 'notes', 'planner', 'meeting-prep', 'search-audit',
      'automation', 'engineering', 'personal', 'collaboration',
    ])
    expect(cards).toEqual(['today', 'attention', 'work', 'schedule', 'risks', 'knowledge'])
    expect(canvas.length + cards.length).toBe(WORKSPACES.length)
  })

  it('resolves a path to its composition, and falls back to cards for an unknown path', () => {
    expect(compositionForPath('/notes')).toBe('canvas')
    expect(compositionForPath('/team')).toBe('canvas')
    expect(compositionForPath('/today')).toBe('cards')
    expect(compositionForPath('/schedule')).toBe('cards')
    expect(compositionForPath('/does-not-exist')).toBe('cards')
  })

  it('treats a trailing slash as the same path (a route also matches it)', () => {
    expect(compositionForPath('/notes/')).toBe('canvas')
    expect(compositionForPath('/today/')).toBe('cards')
    expect(viewForPath('/notes/')).toBe('notes')
  })

  it('does not treat the root path itself as a trailing slash to strip', () => {
    expect(viewForPath('/')).toBeNull()
    expect(compositionForPath('/')).toBe('cards')
  })

  it('maps each workspace to its own distinct icon component', () => {
    const expected: Record<string, unknown> = {
      today: TodayIcon, attention: AttentionIcon, recommendations: RecommendationsIcon,
      work: WorkIcon, notes: NotesIcon, schedule: ScheduleIcon, planner: PlannerIcon,
      'meeting-prep': MeetingPrepIcon, risks: RisksIcon, knowledge: KnowledgeIcon,
      'search-audit': SearchAuditIcon, automation: AutomationIcon, engineering: EngineeringIcon,
      personal: PersonalIcon, collaboration: TeamIcon,
    }
    for (const entry of WORKSPACES) {
      expect(entry.icon).toBe(expected[entry.view])
    }
    expect(new Set(WORKSPACES.map((w) => w.icon)).size).toBe(15)
  })
})
