// @vitest-environment jsdom

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup } from '@testing-library/react'

import MobileWorkspaceNav from './MobileWorkspaceNav'

afterEach(cleanup)

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<MobileWorkspaceNav />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('MobileWorkspaceNav', () => {
  it('selects the tab matching the current route', () => {
    renderAt('/risks')
    expect(screen.getByRole('tab', { name: 'Risks' }).getAttribute('aria-selected')).toBe('true')
  })

  it('defaults to Today for an unknown or root path', () => {
    renderAt('/')
    expect(screen.getByRole('tab', { name: 'Today' }).getAttribute('aria-selected')).toBe('true')
  })
})
