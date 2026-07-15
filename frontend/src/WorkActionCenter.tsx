import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

type Task = {
  id: string
  title: string
  status: string
  manual_priority: string
  due_date?: string | null
  due_at?: string | null
  version: number
}

type Commitment = {
  id: string
  summary: string
  status: string
  importance: string
  direction: string
  due_date?: string | null
  due_at?: string | null
  version: number
}

type ListResponse<T> = { items: T[]; next_cursor?: string | null }
type EntityKind = 'task' | 'commitment'
type EntityAction = 'complete' | 'cancel' | 'fulfil' | 'confirm'
type MutationInput = { kind: EntityKind; id: string; action: EntityAction; version: number }
type ErrorEnvelope = { error?: { code?: string; message?: string } }

function csrfToken(): string {
  return document.cookie
    .split('; ')
    .find((value) => value.startsWith('ecc_csrf='))
    ?.split('=')[1] ?? ''
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers,
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorEnvelope
    const error = new Error(payload.error?.message ?? 'Action failed')
    error.name = payload.error?.code ?? `HTTP_${response.status}`
    throw error
  }
  return response.json()
}

function fetchTasks(): Promise<ListResponse<Task>> {
  const params = new URLSearchParams({ limit: '20' })
  params.append('status[]', 'captured')
  params.append('status[]', 'planned')
  params.append('status[]', 'in_progress')
  params.append('status[]', 'blocked')
  return request(`/api/v1/tasks?${params}`)
}

function fetchCommitments(): Promise<ListResponse<Commitment>> {
  const params = new URLSearchParams({ limit: '20' })
  params.append('status[]', 'detected')
  params.append('status[]', 'confirmed')
  params.append('status[]', 'active')
  return request(`/api/v1/commitments?${params}`)
}

export function actionBody(version: number, action: EntityAction): Record<string, unknown> {
  return {
    expected_version: version,
    ...(action === 'cancel' ? { reason: 'Cancelled from executive action center' } : {}),
  }
}

export function actionErrorMessage(error: Error): string {
  if (error.name === 'VERSION_CONFLICT') {
    return 'This item changed while you were reviewing it. The latest version has been reloaded.'
  }
  return error.message
}

function mutateEntity({ kind, id, action, version }: MutationInput): Promise<unknown> {
  const plural = kind === 'task' ? 'tasks' : 'commitments'
  return request(`/api/v1/${plural}/${id}/${action}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
      'X-CSRF-Token': csrfToken(),
    },
    body: JSON.stringify(actionBody(version, action)),
  })
}

function dueLabel(dueDate?: string | null, dueAt?: string | null): string | null {
  if (dueDate) return `Due ${dueDate}`
  if (!dueAt) return null
  const parsed = new Date(dueAt)
  if (Number.isNaN(parsed.getTime())) return dueAt
  return `Due ${new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed)}`
}

export default function WorkActionCenter() {
  const queryClient = useQueryClient()
  const tasks = useQuery({ queryKey: ['work-actions', 'tasks'], queryFn: fetchTasks, retry: 1 })
  const commitments = useQuery({
    queryKey: ['work-actions', 'commitments'],
    queryFn: fetchCommitments,
    retry: 1,
  })
  const mutation = useMutation({
    mutationFn: mutateEntity,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['work-actions'] })
      void queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] })
      void queryClient.invalidateQueries({ queryKey: ['brief', 'morning'] })
    },
    onError: (error) => {
      if (error.name === 'VERSION_CONFLICT') {
        void queryClient.invalidateQueries({ queryKey: ['work-actions'] })
      }
    },
  })

  const taskItems = tasks.data?.items ?? []
  const commitmentItems = commitments.data?.items ?? []

  return (
    <section className="work-panel" aria-labelledby="work-actions-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">AUTHORITATIVE LOCAL ACTIONS</p>
          <h2 id="work-actions-title">Work Actions</h2>
          <p>Complete or cancel tasks, and confirm, fulfil or cancel commitments using the latest version.</p>
        </div>
        <button
          type="button"
          onClick={() => {
            void tasks.refetch()
            void commitments.refetch()
          }}
          disabled={tasks.isFetching || commitments.isFetching}
        >
          {tasks.isFetching || commitments.isFetching ? 'Refreshing…' : 'Refresh work'}
        </button>
      </div>

      {mutation.isError ? (
        <div className="inline-status error-panel" role="alert">
          {actionErrorMessage(mutation.error)}
        </div>
      ) : null}

      <div className="work-grid">
        <section aria-labelledby="task-actions-title">
          <h3 id="task-actions-title">Open tasks</h3>
          {tasks.isLoading ? <p role="status">Loading tasks…</p> : null}
          {tasks.isError ? <div className="inline-status error-panel" role="alert">{tasks.error.message}</div> : null}
          {tasks.isSuccess && taskItems.length === 0 ? <p className="explore-empty">No open tasks.</p> : null}
          <ol className="work-list">
            {taskItems.map((task) => {
              const busy = mutation.isPending && mutation.variables?.id === task.id
              return (
                <li key={task.id}>
                  <div>
                    <strong>{task.title}</strong>
                    <small>{task.status.replaceAll('_', ' ')} · {task.manual_priority}{dueLabel(task.due_date, task.due_at) ? ` · ${dueLabel(task.due_date, task.due_at)}` : ''}</small>
                  </div>
                  <div className="work-actions" aria-label={`Actions for ${task.title}`}>
                    <button type="button" disabled={busy} onClick={() => mutation.mutate({ kind: 'task', id: task.id, action: 'complete', version: task.version })}>Complete</button>
                    <button type="button" disabled={busy} onClick={() => mutation.mutate({ kind: 'task', id: task.id, action: 'cancel', version: task.version })}>Cancel</button>
                  </div>
                </li>
              )
            })}
          </ol>
        </section>

        <section aria-labelledby="commitment-actions-title">
          <h3 id="commitment-actions-title">Open commitments</h3>
          {commitments.isLoading ? <p role="status">Loading commitments…</p> : null}
          {commitments.isError ? <div className="inline-status error-panel" role="alert">{commitments.error.message}</div> : null}
          {commitments.isSuccess && commitmentItems.length === 0 ? <p className="explore-empty">No open commitments.</p> : null}
          <ol className="work-list">
            {commitmentItems.map((commitment) => {
              const busy = mutation.isPending && mutation.variables?.id === commitment.id
              const canConfirm = commitment.status === 'detected' || commitment.status === 'confirmed'
              const canFulfil = commitment.status === 'confirmed' || commitment.status === 'active'
              return (
                <li key={commitment.id}>
                  <div>
                    <strong>{commitment.summary}</strong>
                    <small>{commitment.status.replaceAll('_', ' ')} · {commitment.importance} · {commitment.direction.replaceAll('_', ' ')}{dueLabel(commitment.due_date, commitment.due_at) ? ` · ${dueLabel(commitment.due_date, commitment.due_at)}` : ''}</small>
                  </div>
                  <div className="work-actions" aria-label={`Actions for ${commitment.summary}`}>
                    {canConfirm ? <button type="button" disabled={busy} onClick={() => mutation.mutate({ kind: 'commitment', id: commitment.id, action: 'confirm', version: commitment.version })}>Confirm</button> : null}
                    {canFulfil ? <button type="button" disabled={busy} onClick={() => mutation.mutate({ kind: 'commitment', id: commitment.id, action: 'fulfil', version: commitment.version })}>Fulfil</button> : null}
                    <button type="button" disabled={busy} onClick={() => mutation.mutate({ kind: 'commitment', id: commitment.id, action: 'cancel', version: commitment.version })}>Cancel</button>
                  </div>
                </li>
              )
            })}
          </ol>
        </section>
      </div>
    </section>
  )
}
