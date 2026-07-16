import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { createAutosaveController, type AutosaveController, type AutosaveState } from './autosave'

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
const recoverableDrafts = new Map<string, string>()

function listNotes(): Promise<NoteList> {
  return apiRequest('/api/v1/notes?include_archived=true&limit=100')
}

function displayTitle(note: Note): string {
  return note.title?.trim() || 'Untitled note'
}

const initialSaveState: AutosaveState = { status: 'idle', text: '', version: 1 }

export default function NoteWorkspace() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['notes', filters], queryFn: listNotes, retry: 1 })
  const [create, setCreate] = useState<NoteDraft>(emptyDraft)
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<Note | null>(null)
  const [body, setBody] = useState('')
  const [saveState, setSaveState] = useState<AutosaveState>(initialSaveState)
  const [conflictVersion, setConflictVersion] = useState<number | null>(null)
  const [conflictReloadFailed, setConflictReloadFailed] = useState(false)
  const controller = useRef<AutosaveController | null>(null)

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['notes'] })
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

  useEffect(() => () => {
    const activeController = controller.current
    controller.current = null
    void activeController?.dispose()
  }, [])

  function updateNoteCaches(saved: Note) {
    queryClient.setQueryData<Note>(['note', saved.id], saved)
    queryClient.setQueriesData<NoteList>({ queryKey: ['notes'] }, (current) => current ? {
      ...current,
      items: current.items.map((note) => note.id === saved.id ? saved : note),
    } : current)
    setEditing((current) => current?.id === saved.id ? saved : current)
  }

  async function reloadLatestNote(noteId: string) {
    try {
      const latest = await apiRequest<Note>(`/api/v1/notes/${noteId}`)
      queryClient.setQueryData(['note', noteId], latest)
      setConflictVersion(latest.version)
      setConflictReloadFailed(false)
    } catch {
      setConflictVersion(null)
      setConflictReloadFailed(true)
    }
  }

  function installController(note: Note) {
    controller.current = createAutosaveController({
      delayMs: 750,
      initialVersion: note.version,
      save: async (nextBody, version) => {
        try {
          const saved = await apiRequest<Note>(`/api/v1/notes/${note.id}`, {
            method: 'PATCH', body: { expected_version: version, body: nextBody },
          })
          updateNoteCaches(saved)
          if (recoverableDrafts.get(note.id) === nextBody) recoverableDrafts.delete(note.id)
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
    const draft = recoverableDrafts.get(note.id) ?? note.body
    setEditing(note)
    setBody(draft)
    setConflictVersion(null)
    setConflictReloadFailed(false)
    setSaveState({ ...initialSaveState, text: draft, version: note.version })
    installController(note)
    if (draft !== note.body) controller.current?.update(draft)
  }

  function beginEditing(note: Note) {
    if (!controller.current) {
      activateEditor(note)
      return
    }
    void retireController().then(() => activateEditor(note))
  }

  function submitCreate(event: FormEvent) {
    event.preventDefault()
    if (create.body.trim()) createMutation.mutate(create)
  }

  function updateBody(nextBody: string) {
    setBody(nextBody)
    if (editing) recoverableDrafts.set(editing.id, nextBody)
    setConflictVersion(null)
    setConflictReloadFailed(false)
    controller.current?.update(nextBody)
  }

  async function retryConflict() {
    if (!editing || conflictVersion === null) return
    setConflictVersion(null)
    setConflictReloadFailed(false)
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
      return <li key={note.id}><div><strong>{title}</strong><small>{note.note_type}{note.archived_at ? ' · archived' : ''}</small><p>{note.body}</p></div><div className="work-actions" aria-label={`Actions for ${title}`}>
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
      <button type="button" onClick={() => { void retireController().then(() => setEditing(null)) }}>Close editor</button>
    </section> : null}
  </section>
}
