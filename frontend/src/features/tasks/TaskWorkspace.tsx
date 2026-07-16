import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

type Task = {
  id: string
  title: string
  description?: string | null
  status: 'captured' | 'planned' | 'in_progress' | 'blocked' | 'completed' | 'cancelled' | string
  manual_priority: 'low' | 'medium' | 'high' | 'critical'
  due_date?: string | null
  due_at?: string | null
  version: number
  archived_at?: string | null
}

type TaskList = { items: Task[]; next_cursor?: string | null }
type TaskDraft = { title: string; description: string; priority: Task['manual_priority']; dueDate: string; dueAt: string; originalDueAt?: string }
type EditState = TaskDraft & { task: Task; latestVersion: number; conflict: boolean; reloadFailed: boolean }
type Action = 'complete' | 'cancel' | 'archive' | 'restore'

const emptyDraft: TaskDraft = { title: '', description: '', priority: 'medium', dueDate: '', dueAt: '' }
const filters = { include_archived: true }

function listTasks(): Promise<TaskList> {
  return apiRequest('/api/v1/tasks?include_archived=true&limit=100')
}

function duePayload(draft: TaskDraft): Record<string, string | null> {
  if (draft.dueDate) return { due_date: draft.dueDate, due_at: null }
  if (draft.dueAt) {
    const dueAt = draft.originalDueAt && serverInstantToLocalInput(draft.originalDueAt) === draft.dueAt
      ? draft.originalDueAt
      : new Date(draft.dueAt).toISOString()
    return { due_date: null, due_at: dueAt }
  }
  return { due_date: null, due_at: null }
}

function pad(value: number): string { return String(value).padStart(2, '0') }

export function serverInstantToLocalInput(value: string): string {
  const instant = new Date(value)
  if (Number.isNaN(instant.getTime())) return ''
  return `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}T${pad(instant.getHours())}:${pad(instant.getMinutes())}`
}

function taskDraft(task: Task): TaskDraft {
  return {
    title: task.title,
    description: task.description ?? '',
    priority: task.manual_priority,
    dueDate: task.due_date ?? '',
    dueAt: task.due_at ? serverInstantToLocalInput(task.due_at) : '',
    originalDueAt: task.due_at ?? undefined,
  }
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') return 'This task changed while you were editing it. Review your input and retry with the latest version.'
  return error.message
}

export default function TaskWorkspace() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['tasks', filters], queryFn: listTasks, retry: 1 })
  const [create, setCreate] = useState<TaskDraft>(emptyDraft)
  const [edit, setEdit] = useState<EditState | null>(null)

  async function reloadLatestTask(taskId: string, envelopeVersion?: number) {
    try {
      const current = await apiRequest<Task>(`/api/v1/tasks/${taskId}`)
      const latestVersion = Math.max(current.version, envelopeVersion ?? current.version)
      setEdit((value) => value?.task.id === taskId ? { ...value, latestVersion, conflict: true, reloadFailed: false } : value)
    } catch {
      setEdit((value) => value?.task.id === taskId ? { ...value, latestVersion: 0, conflict: false, reloadFailed: true } : value)
    }
  }

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['tasks'] })
  const createMutation = useMutation({
    mutationFn: (draft: TaskDraft) => apiRequest<Task>('/api/v1/tasks', {
      method: 'POST',
      body: {
        title: draft.title.trim(), description: draft.description.trim() || null,
        manual_priority: draft.priority, ...duePayload(draft), status: 'captured',
      },
    }),
    onSuccess: () => { setCreate(emptyDraft); void refresh() },
  })
  const editMutation = useMutation({
    mutationFn: ({ id, draft, version }: { id: string; draft: TaskDraft; version: number }) => apiRequest<Task>(`/api/v1/tasks/${id}`, {
      method: 'PATCH',
      body: {
        expected_version: version, title: draft.title.trim(), description: draft.description.trim() || null,
        manual_priority: draft.priority, ...duePayload(draft),
      },
    }),
    onSuccess: (_data, variables) => { setEdit((value) => value?.task.id === variables.id ? null : value); void refresh() },
    onError: async (error) => {
      if (!(error instanceof ApiError) || error.code !== 'VERSION_CONFLICT') return
      const detail = error.current as { current_version?: number } | undefined
      if (edit) await reloadLatestTask(edit.task.id, detail?.current_version)
    },
  })
  const actionMutation = useMutation({
    mutationFn: ({ task, action }: { task: Task; action: Action }) => apiRequest<Task>(`/api/v1/tasks/${task.id}/${action}`, {
      method: 'POST', body: { expected_version: task.version },
    }),
    onSuccess: () => { void refresh() },
    onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') void refresh() },
  })

  function submitCreate(event: FormEvent) {
    event.preventDefault()
    if (create.title.trim()) createMutation.mutate(create)
  }

  function submitEdit(event?: FormEvent) {
    event?.preventDefault()
    if (edit?.title.trim() && edit.latestVersion > 0) editMutation.mutate({ id: edit.task.id, draft: edit, version: edit.latestVersion })
  }

  const mutationError = createMutation.error ?? editMutation.error ?? actionMutation.error
  return (
    <section className="work-panel" aria-labelledby="tasks-title">
      <div className="work-heading"><div><p className="eyebrow">WORK</p><h1 id="tasks-title">Tasks</h1><p>Create, edit and move tasks through their lifecycle.</p></div></div>
      {mutationError ? <div className="inline-status error-panel" role="alert">{errorMessage(mutationError)}</div> : null}
      <form onSubmit={submitCreate}>
        <h2>Create task</h2>
        <label>Task title<input aria-label="Task title" value={create.title} onChange={(event) => setCreate({ ...create, title: event.target.value })} /></label>
        <label>Description<textarea value={create.description} onChange={(event) => setCreate({ ...create, description: event.target.value })} /></label>
        <label>Priority<select value={create.priority} onChange={(event) => setCreate({ ...create, priority: event.target.value as Task['manual_priority'] })}><option>low</option><option>medium</option><option>high</option><option>critical</option></select></label>
        <label>Due date<input aria-label="Due date" type="date" value={create.dueDate} disabled={Boolean(create.dueAt)} onChange={(event) => setCreate({ ...create, dueDate: event.target.value })} /></label>
        <label>Due time<input aria-label="Due time" type="datetime-local" value={create.dueAt} disabled={Boolean(create.dueDate)} onChange={(event) => setCreate({ ...create, dueAt: event.target.value })} /></label>
        <button type="submit" disabled={createMutation.isPending}>Create task</button>
      </form>

      {query.isLoading ? <p role="status">Loading tasks…</p> : null}
      {query.isError ? <div role="alert">{query.error.message}</div> : null}
      <ol className="work-list">
        {(query.data?.items ?? []).map((task) => {
          const archived = Boolean(task.archived_at)
          const terminal = ['completed', 'cancelled'].includes(task.status)
          return <li key={task.id}>
            <div><strong>{task.title}</strong><small>{task.status.replaceAll('_', ' ')} · {task.manual_priority}</small></div>
            <div className="work-actions" aria-label={`Actions for ${task.title}`}>
              {!archived && !terminal ? <><button type="button" disabled={actionMutation.isPending || editMutation.isPending} aria-label={`Edit ${task.title}`} onClick={() => setEdit({ task, ...taskDraft(task), latestVersion: task.version, conflict: false, reloadFailed: false })}>Edit</button><button type="button" disabled={actionMutation.isPending || editMutation.isPending} aria-label={`Complete ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'complete' })}>Complete</button><button type="button" disabled={actionMutation.isPending || editMutation.isPending} aria-label={`Cancel ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'cancel' })}>Cancel</button></> : null}
              {!archived ? <button type="button" disabled={actionMutation.isPending || editMutation.isPending} aria-label={`Archive ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'archive' })}>Archive</button> : <button type="button" disabled={actionMutation.isPending || editMutation.isPending} aria-label={`Restore ${task.title}`} onClick={() => actionMutation.mutate({ task, action: 'restore' })}>Restore</button>}
            </div>
          </li>
        })}
      </ol>

      {edit ? <form onSubmit={submitEdit}>
        <h2>Edit task</h2>
        <label>Edit task title<input aria-label="Edit task title" value={edit.title} onChange={(event) => setEdit({ ...edit, title: event.target.value })} /></label>
        <label>Edit description<textarea value={edit.description} onChange={(event) => setEdit({ ...edit, description: event.target.value })} /></label>
        <label>Edit priority<select value={edit.priority} onChange={(event) => setEdit({ ...edit, priority: event.target.value as Task['manual_priority'] })}><option>low</option><option>medium</option><option>high</option><option>critical</option></select></label>
        <label>Edit due date<input type="date" value={edit.dueDate} disabled={Boolean(edit.dueAt)} onChange={(event) => setEdit({ ...edit, dueDate: event.target.value })} /></label>
        <label>Edit due time<input type="datetime-local" value={edit.dueAt} disabled={Boolean(edit.dueDate)} onChange={(event) => setEdit({ ...edit, dueAt: event.target.value })} /></label>
        {edit.reloadFailed ? <><p role="alert">Could not reload the latest task. Your edits are preserved.</p><button type="button" onClick={() => void reloadLatestTask(edit.task.id)}>Reload latest task</button></> : edit.conflict ? <button type="button" disabled={editMutation.isPending} onClick={() => submitEdit()}>Retry with latest version</button> : <button type="submit" disabled={editMutation.isPending}>Save task</button>}
        <button type="button" disabled={editMutation.isPending} onClick={() => setEdit(null)}>Discard edit</button>
      </form> : null}
    </section>
  )
}
