// @vitest-environment jsdom

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import {
  AttentionIcon, AutomationIcon, EngineeringIcon, KnowledgeIcon, MeetingPrepIcon,
  NotesIcon, PersonalIcon, PlannerIcon, RecommendationsIcon, RisksIcon,
  ScheduleIcon, SearchAuditIcon, TeamIcon, TodayIcon, WorkIcon,
} from './icons'

afterEach(() => cleanup())

const ICONS = {
  TodayIcon, AttentionIcon, RecommendationsIcon, WorkIcon, NotesIcon, ScheduleIcon,
  PlannerIcon, MeetingPrepIcon, RisksIcon, KnowledgeIcon, SearchAuditIcon,
  AutomationIcon, EngineeringIcon, PersonalIcon, TeamIcon,
}

describe('sidebar workspace icons', () => {
  it('has exactly 15 icons, one per workspace', () => {
    expect(Object.keys(ICONS)).toHaveLength(15)
  })

  it.each(Object.entries(ICONS))('%s renders a 24x24 stroke svg and forwards props', (_name, Icon) => {
    const { container } = render(<Icon aria-hidden="true" className="sidebar-nav-icon" data-testid="icon" />)
    const svg = container.querySelector('svg')
    expect(svg).not.toBeNull()
    expect(svg?.getAttribute('viewBox')).toBe('0 0 24 24')
    expect(svg?.getAttribute('aria-hidden')).toBe('true')
    expect(svg?.getAttribute('class')).toBe('sidebar-nav-icon')
    expect(svg?.getAttribute('stroke')).toBe('currentColor')
  })
})
