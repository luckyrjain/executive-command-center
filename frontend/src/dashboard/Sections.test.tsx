// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Section } from './Sections'

afterEach(() => { cleanup() })

describe('Section', () => {
  it('defaults to a section- heading id prefix', () => {
    render(<Section title="Schedule" items={[{ id: '1', title: 'Standup' }]} emptyMessage="Nothing here." />)
    expect(document.getElementById('section-schedule')).toBeTruthy()
  })

  it('uses a custom heading id prefix when given one', () => {
    render(<Section headingIdPrefix="brief-section-" title="Schedule" items={[{ id: '1', title: 'Standup' }]} emptyMessage="Nothing here." />)
    expect(document.getElementById('brief-section-schedule')).toBeTruthy()
    expect(document.getElementById('section-schedule')).toBeNull()
  })

  it('lets App.tsx and MorningBrief.tsx render the same-titled section on one page without a duplicate id', () => {
    render(<>
      <Section title="Schedule" items={[]} emptyMessage="Nothing here." />
      <Section headingIdPrefix="brief-section-" title="Schedule" items={[]} emptyMessage="Nothing here." />
    </>)
    expect(document.getElementById('section-schedule')).toBeTruthy()
    expect(document.getElementById('brief-section-schedule')).toBeTruthy()
  })

  it('shows the empty message when there are no visible items', () => {
    render(<Section title="Schedule" items={[]} emptyMessage="No meetings scheduled for today." />)
    expect(screen.getByText('No meetings scheduled for today.')).toBeTruthy()
  })

  it('defaults to the dashboard-card surface', () => {
    render(<Section title="Schedule" items={[]} emptyMessage="Nothing here." />)
    expect(document.getElementById('section-schedule')?.closest('section')?.className).toBe('dashboard-card')
  })

  it('renders as the promoted work-panel surface when variant="panel"', () => {
    render(<Section title="Top priorities" items={[]} emptyMessage="Nothing here." variant="panel" />)
    expect(document.getElementById('section-top-priorities')?.closest('section')?.className).toBe('work-panel')
  })
})
