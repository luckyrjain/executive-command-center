// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MembersPanel from './MembersPanel'
import type { Invitation, Member, Workspace } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return { id: 'workspace-1', name: 'Acme', timezone: 'UTC', role: 'owner', created_at: '2026-01-01T00:00:00Z', current: true, ...overrides }
}

function member(overrides: Partial<Member> = {}): Member {
  return {
    user_id: 'user-1',
    account_id: 'account-1',
    email: 'a@example.test',
    display_name: 'Ada',
    role: 'member',
    status: 'active',
    created_at: '2026-01-01T00:00:00Z',
    removed_at: null,
    ...overrides,
  }
}

function invitation(overrides: Partial<Invitation> = {}): Invitation {
  return {
    id: 'invite-1',
    email: 'invitee@example.test',
    role: 'member',
    expires_at: '2099-01-01T00:00:00Z',
    accepted_at: null,
    rejected_at: null,
    revoked_at: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MembersPanel /></QueryClientProvider>)
}

// Distinct from `member()`'s default `user_id: 'user-1'` on purpose -- most
// tests exercise the owner/admin-managing-someone-else path, and a "me" that
// happened to match the seeded member's id would silently flip every one of
// them into the self-removal path instead. Tests that specifically want to
// exercise self-removal pass `handlers.me` with a `users_id` matching the
// member under test.
function me(overrides: Partial<{ account_id: string; users_id: string; email: string; display_name: string }> = {}) {
  return { account_id: 'account-me', users_id: 'me-user-id', workspace_id: 'workspace-1', email: 'me@example.test', display_name: 'Me', ...overrides }
}

function stubFetch(handlers: {
  workspaces?: Workspace[]
  members?: Member[]
  invitations?: Invitation[]
  me?: ReturnType<typeof me>
  onPatch?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
  onDelete?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
  onPost?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
}) {
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method === 'PATCH') {
      const scripted = handlers.onPatch?.(input, init)
      if (scripted) return scripted
    }
    if (method === 'DELETE') {
      const scripted = handlers.onDelete?.(input, init)
      if (scripted) return scripted
    }
    if (method === 'POST') {
      const scripted = handlers.onPost?.(input, init)
      if (scripted) return scripted
    }
    if (url.endsWith('/identity/me')) return response(handlers.me ?? me())
    if (url.includes('/identity/workspaces') && !url.includes('/members') && !url.includes('/invitations')) {
      return response({ workspaces: handlers.workspaces ?? [workspace()] })
    }
    if (url.includes('/members')) return response({ members: handlers.members ?? [] })
    if (url.includes('/invitations')) return response({ invitations: handlers.invitations ?? [] })
    return response({})
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=members-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'members-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('MembersPanel', () => {
  it('shows the empty state when there are no members', async () => {
    stubFetch({ members: [] })
    renderPanel()
    expect(await screen.findByText('No active members.')).toBeTruthy()
  })

  it('lists members and lets an owner change a role', async () => {
    const fetch = stubFetch({
      members: [member()],
      onPatch: (input) => (String(input).includes('/members/user-1') ? response(member({ role: 'admin' })) : undefined),
    })
    renderPanel()
    await screen.findByText('Ada')
    // account_id must be visible somewhere -- it's the only value the
    // ownership-transfer form (used when removal is blocked) accepts as
    // its "transfer to" target.
    expect(screen.getByText(/account-1/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Role for Ada'), { target: { value: 'admin' } })
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/members/user-1'),
      expect.objectContaining({ method: 'PATCH' }),
    ))
  })

  it('requires confirmation before removing a member', async () => {
    stubFetch({ members: [member()] })
    renderPanel()
    await screen.findByText('Ada')

    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    expect(await screen.findByText(/ends their access immediately/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Confirm removal' })).toBeTruthy()
  })

  it('shows the owned-resources breakdown and an ownership-transfer form when removal is blocked', async () => {
    stubFetch({
      members: [member()],
      onDelete: (input) =>
        String(input).includes('/members/user-1')
          ? Promise.resolve(new Response(
              JSON.stringify({ error: { code: 'OWNED_RESOURCES_BLOCK_REMOVAL', details: { owned_resources: [{ resource_type: 'incidents', count: 2 }] } } }),
              { status: 409, headers: { 'Content-Type': 'application/json' } },
            ))
          : undefined,
    })
    renderPanel()
    await screen.findByText('Ada')
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm removal' }))

    expect(await screen.findByText('2 × incidents')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Transfer ownership' })).toBeTruthy()
  })

  it('lets a non-manager remove themselves ("leave workspace") and reloads afterward', async () => {
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { value: { ...window.location, reload }, writable: true })

    const fetch = stubFetch({
      workspaces: [workspace({ role: 'member' })],
      members: [member()],
      me: me({ users_id: 'user-1' }),
      onDelete: (input) => (String(input).includes('/members/user-1') ? response({ user_id: 'user-1', export: {} }) : undefined),
    })
    renderPanel()
    await screen.findByText('Ada')

    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: 'Leave workspace' }))
    expect(await screen.findByText(/Leaving ends your access immediately/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Confirm removal' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/members/user-1'),
      expect.objectContaining({ method: 'DELETE' }),
    ))
    // Self-removal revokes the caller's own session server-side, so the
    // component reloads the page instead of trying to refetch through an
    // already-dead session.
    await waitFor(() => expect(reload).toHaveBeenCalled())
  })

  it('does not show the role selector as editable, or the invitations section, for a non-manager role', async () => {
    stubFetch({ workspaces: [workspace({ role: 'member' })], members: [member()] })
    renderPanel()
    await screen.findByText('Ada')
    expect((screen.getByLabelText('Role for Ada') as HTMLSelectElement).disabled).toBe(true)
    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
    expect(screen.queryByText('Invitations')).toBeNull()
  })

  it('creates an invitation and lists pending invitations for an owner', async () => {
    const fetch = stubFetch({
      members: [],
      invitations: [invitation()],
      onPost: (input) =>
        String(input).endsWith('/invitations')
          ? response({ id: 'invite-2', email: 'new@example.test', role: 'member', token: 't', expires_at: '2099-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z' }, 201)
          : undefined,
    })
    renderPanel()
    await screen.findByText('invitee@example.test')

    fireEvent.change(screen.getByLabelText('Invitee email'), { target: { value: 'new@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/invitations'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('revokes a pending invitation', async () => {
    const fetch = stubFetch({
      members: [],
      invitations: [invitation()],
      onPost: (input) => (String(input).includes('/invitations/invite-1/revoke') ? response(invitation({ revoked_at: '2026-07-01T00:00:00Z' })) : undefined),
    })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/invitations/invite-1/revoke'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('shows the one-time invitation token after creating an invitation', async () => {
    stubFetch({
      members: [],
      invitations: [],
      onPost: (input) =>
        String(input).endsWith('/invitations')
          ? response({ id: 'invite-2', email: 'new@example.test', role: 'member', token: 'secret-token', expires_at: '2099-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z' }, 201)
          : undefined,
    })
    renderPanel()
    await screen.findByText('No invitations yet.')

    fireEvent.change(screen.getByLabelText('Invitee email'), { target: { value: 'new@example.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }))

    expect(await screen.findByText('secret-token')).toBeTruthy()
  })

  it('accepts an invitation to another workspace', async () => {
    const fetch = stubFetch({
      members: [],
      invitations: [],
      onPost: (input) =>
        String(input).includes('/invitations/invite-9/accept')
          ? response({ id: 'invite-9', status: 'accepted', workspace_id: 'workspace-2', role: 'member' })
          : undefined,
    })
    renderPanel()
    await screen.findByText('No invitations yet.')

    fireEvent.change(screen.getByLabelText('Invitation ID'), { target: { value: 'invite-9' } })
    fireEvent.change(screen.getByLabelText('Invitation token'), { target: { value: 'their-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Accept invitation' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/invitations/invite-9/accept'),
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(await screen.findByText(/Joined\./)).toBeTruthy()
  })
})
