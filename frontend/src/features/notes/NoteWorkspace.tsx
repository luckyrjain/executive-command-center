import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { createAutosaveController, type AutosaveController, type AutosaveState } from './autosave'
import { createNoteDraftRecoveryStore, type NoteDraftRecoveryStore } from './draftRecovery'

type Note = {
  id: string
  title?: string | null
  body: string
  note_type: 'general' | 'meeting' | 'decision' | 'journal' | string
  version: number
  archived_at?: string | null
}

type NoteList = { items: Note[]; next_cursor?: string | null }
type NoteDraft = { title: string; body: string; noteType: 'general' | 'decision' | 'journal' }
type Action = 'archive' | 'restore'

const emptyDraft: NoteDraft = { title: '', body: '', noteType: 'general' }
const filters = { include_archived: true }
function listNotes(): Promise<NoteList> {
  return apiRequest('/api/v1/notes?include_archived=true&limit=100')
}

function displayTitle(note: Note): string {
  return note.title?.trim() || 'Untitled note'
}

const initialSaveState: AutosaveState = { status: 'idle', text: '', version: 1 }

type NoteWorkspaceProps = { recoveryStore?: NoteDraftRecoveryStore }

export default function NoteWorkspace({ recoveryStore }: NoteWorkspaceProps) {
  const queryClient = useQueryClient()
  const fallbackRecoveryStore = useRef<NoteDraftRecoveryStore | null>(null)
  if (!fallbackRecoveryStore.current) {
    fallbackRecoveryStore.current = createNoteDraftRecoveryStore({ namespace: crypto.randomUUID() })
  }
  const drafts = recoveryStore ?? fallbackRecoveryStore.current
  const query = useQuery({ queryKey: ['notes', filters], queryFn: listNotes, retry: 1 })
  const [create, setCreate] = useState<NoteDraft>(emptyDraft)
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<Note | null>(null)
  const [body, setBody] = useState('')
  const [saveState, setSaveState] = useState<AutosaveState>(initialSaveState)
  const [conflictVersion, setConflictVersion] = useState<number | null>(null)
  const [conflictReloadFailed, setConflictReloadFailed] = useState(false)
  const controller = useRef<AutosaveController | null>(null)
  const baseVersion = useRef(1)
  const mounted = useRef(true)
  const transition = useRef(0)

  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: ['notes'] }),
    queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] }),
    queryClient.invalidateQueries({ queryKey: ['brief', 'morning'] }),
  ])
  const createMutation = useMutation({
    mutationFn: (draft: NoteDraft) => apiRequest<Note>('/api/v1/notes', {
      method: 'POST',
      body: { title: draft.title.trim() || null, body: draft.body, note_type: draft.noteType },
    }),
    onSuccess: () => { setCreate(emptyDraft); void refresh() },
  })
  const actionMutation = useMutation({
    mutationFn: ({ note, action }: { note: Note; action: Action }) => apiRequest<Note>(`/api/v1/notes/${note.id}/${action}`, {
      method: 'POST', body: { expected_version: note.version },
    }),
    onSuccess: () => { void refresh() },
    onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') void refresh() },
  })

  const visibleNotes = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase()
    if (!needle) return query.data?.items ?? []
    return (query.data?.items ?? []).filter((note) =>
      `${note.title ?? ''}\n${note.body}`.toLocaleLowerCase().includes(needle),
    )
  }, [query.data?.items, search])

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      transition.current += 1
      const activeController = controller.current
      controller.current = null
      void activeController?.dispose()
    }
  }, [])

  function updateNoteCaches(saved: Note) {
    queryClient.setQueryData<Note>(['note', saved.id], saved)
    queryClient.setQueriesData<NoteList>({ queryKey: ['notes'] }, (current) => current ? {
      ...current,
      items: current.items.map((note) => note.id === saved.id ? saved : note),
    } : current)
    if (mounted.current) setEditing((current) => current?.id === saved.id ? saved : current)
  }

  async function reloadLatestNote(noteId: string) {
    try {
      const latest = await apiRequest<Note>(`/api/v1/notes/${noteId}`)
      queryClient.setQueryData(['note', noteId], latest)
      if (mounted.current) {
        setConflictVersion(latest.version)
        setConflictReloadFailed(false)
      }
    } catch {
      if (mounted.current) {
        setConflictVersion(null)
        setConflictReloadFailed(true)
      }
    }
  }

  function installController(note: Note, initialVersion = note.version) {
    controller.current = createAutosaveController({
      delayMs: 750,
      initialVersion,
      save: async (nextBody, version) => {
        try {
          const saved = await apiRequest<Note>(`/api/v1/notes/${note.id}`, {
            method: 'PATCH', body: { expected_version: version, body: nextBody },
          })
          updateNoteCaches(saved)
          // Note edits, not just create/archive/restore, produce a
          // note.updated audit event that the dashboard's recently_changed
          // feed surfaces -- autosave must invalidate it too, or that feed
          // shows stale content until some other mutation happens to
          // refresh it.
          void queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] })
          void queryClient.invalidateQueries({ queryKey: ['brief', 'morning'] })
          drafts.removePersisted(note.id, { text: nextBody, baseVersion: version })
          if (drafts.get(note.id)) drafts.rebase(note.id, saved.version)
          baseVersion.current = saved.version
          return saved.version
        } catch (error) {
          if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
            await reloadLatestNote(note.id)
          }
          throw error
        }
      },
      onStateChange: setSaveState,
    })
  }

  async function retireController() {
    const activeController = controller.current
    if (!activeController) return
    await activeController.dispose()
    if (controller.current === activeController) controller.current = null
  }

  function activateEditor(note: Note) {
    const recovered = drafts.get(note.id)
    const draft = recovered?.text ?? note.body
    const draftBaseVersion = recovered?.baseVersion ?? note.version
    baseVersion.current = draftBaseVersion
    setEditing(note)
    setBody(draft)
    setConflictVersion(recovered && draftBaseVersion !== note.version ? note.version : null)
    setConflictReloadFailed(false)
    setSaveState(recovered && draftBaseVersion !== note.version
      ? { status: 'error', text: draft, version: draftBaseVersion, error: new Error('Recovered draft needs reconciliation') }
      : { ...initialSaveState, text: draft, version: draftBaseVersion })
    installController(note, draftBaseVersion)
    if (recovered && draftBaseVersion === note.version) controller.current?.update(draft)
  }

  function beginEditing(note: Note) {
    const generation = ++transition.current
    if (!controller.current) {
      if (mounted.current) activateEditor(note)
      return
    }
    void retireController().then(() => {
      if (mounted.current && transition.current === generation) activateEditor(note)
    })
  }

  function submitCreate(event: FormEvent) {
    event.preventDefault()
    if (create.body.trim()) createMutation.mutate(create)
  }

  function updateBody(nextBody: string) {
    setBody(nextBody)
    if (editing) drafts.put(editing.id, { text: nextBody, baseVersion: baseVersion.current })
    if (conflictVersion !== null || conflictReloadFailed) return
    controller.current?.update(nextBody)
  }

  async function retryConflict() {
    if (!editing || conflictVersion === null) return
    drafts.rebase(editing.id, conflictVersion)
    baseVersion.current = conflictVersion
    setConflictVersion(null)
    setConflictReloadFailed(false)
    controller.current?.update(body)
    controller.current?.rebase(conflictVersion)
    await controller.current?.flush()
  }

  const mutationError = createMutation.error ?? actionMutation.error
  const saveMessage = saveState.status === 'saving' ? 'Saving note…'
    : saveState.status === 'saved' ? 'Note saved.'
      : saveState.status === 'error' ? 'Note not saved. Your text is preserved.' : ''

  return <section className="work-panel note-workspace" aria-labelledby="notes-title">
    <div className="work-heading"><div><p className="eyebrow">KNOWLEDGE</p><h1 id="notes-title">Notes</h1><p>Capture and safely refine your working context.</p></div></div>
    {mutationError ? <div role="alert" className="inline-status error-panel">{mutationError.message}</div> : null}
    <form onSubmit={submitCreate} className="note-create-form">
      <h2>Create note</h2>
      <label>Note title<input aria-label="Note title" value={create.title} onChange={(event) => setCreate({ ...create, title: event.target.value })} /></label>
      <label>Note body<textarea aria-label="Note body" required value={create.body} onChange={(event) => setCreate({ ...create, body: event.target.value })} /></label>
      <label>Note type<select aria-label="Note type" value={create.noteType} onChange={(event) => setCreate({ ...create, noteType: event.target.value as NoteDraft['noteType'] })}><option value="general">General</option><option value="decision">Decision</option><option value="journal">Journal</option></select></label>
      <button type="submit" disabled={createMutation.isPending}>Create note</button>
    </form>

    <label className="note-search">Search notes<input aria-label="Search notes" type="search" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
    {query.isLoading ? <p role="status">Loading notes…</p> : null}
    {query.isError ? <div role="alert">{query.error.message}</div> : null}
    <ol className="work-list note-list">{visibleNotes.map((note) => {
      const title = displayTitle(note)
      return <li key={note.id}><div><strong>{title}</strong><small>{note.note_type}{note.archived_at ? ' · archived' : ''}</small><p>{note.body}</p></div><div className="work-actions" role="group" aria-label={`Actions for ${title}`}>
        {!note.archived_at ? <button type="button" aria-label={`Edit ${title}`} onClick={() => { void beginEditing(note) }}>Edit</button> : null}
        {!note.archived_at ? <button type="button" aria-label={`Archive ${title}`} disabled={actionMutation.isPending} onClick={() => actionMutation.mutate({ note, action: 'archive' })}>Archive</button> : <button type="button" aria-label={`Restore ${title}`} disabled={actionMutation.isPending} onClick={() => actionMutation.mutate({ note, action: 'restore' })}>Restore</button>}
      </div></li>
    })}</ol>

    {editing ? <section className="note-editor" aria-labelledby="note-editor-title"><h2 id="note-editor-title">Edit {displayTitle(editing)}</h2>
      <label>Edit note body<textarea aria-label="Edit note body" value={body} onChange={(event) => updateBody(event.target.value)} onBlur={() => { void controller.current?.flush() }} /></label>
      {saveMessage ? <div role={saveState.status === 'error' ? 'alert' : 'status'} aria-live={saveState.status === 'error' ? 'assertive' : 'polite'} className={`inline-status${saveState.status === 'error' ? ' error-panel' : ''}`}>{saveMessage}</div> : null}
      {saveState.status === 'error' && conflictVersion === null && !conflictReloadFailed ? <button type="button" onClick={() => { void controller.current?.flush() }}>Retry save</button> : null}
      {conflictReloadFailed ? <><p>Your note changed elsewhere, but the latest version could not be loaded. Your text is preserved.</p><button type="button" onClick={() => { void reloadLatestNote(editing.id) }}>Reload latest note</button></> : null}
      {conflictVersion !== null ? <><p>Your note changed elsewhere. Your text is preserved.</p><button type="button" onClick={() => { void retryConflict() }}>Retry with latest version</button></> : null}
      <button type="button" onClick={() => {
        const generation = ++transition.current
        void retireController().then(() => {
          if (mounted.current && transition.current === generation) setEditing(null)
        })
      }}>Close editor</button>
    </section> : null}
  </section>
}
