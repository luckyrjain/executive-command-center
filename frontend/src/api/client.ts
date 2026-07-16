import type { ApiErrorEnvelope, ApiRequestOptions } from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly current: unknown

  constructor(status: number, code: string, message: string, current?: unknown) {
    super(message)
    this.name = code
    this.status = status
    this.code = code
    this.current = current
  }
}

function cookieValue(name: string): string {
  if (typeof document === 'undefined') return ''
  const prefix = `${name}=`
  const value = document.cookie.split(';').map((part) => part.trim()).find((part) => part.startsWith(prefix))
  if (!value) return ''
  try {
    return decodeURIComponent(value.slice(prefix.length))
  } catch {
    return value.slice(prefix.length)
  }
}

function currentState(details: unknown): unknown {
  if (!details || typeof details !== 'object') return undefined
  return 'current' in details ? details.current : details
}

function requestHeaders(options: ApiRequestOptions, mutation: boolean): Record<string, string> {
  const headers = Object.fromEntries(new Headers(options.headers).entries())
  headers.Accept = 'application/json'
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (mutation) {
    headers['X-CSRF-Token'] = cookieValue('ecc_csrf')
    headers['Idempotency-Key'] = crypto.randomUUID()
  }
  return headers
}

export async function apiRequest<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const mutation = !SAFE_METHODS.has(method)
  const { body, ...requestOptions } = options
  let response: Response

  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...requestOptions,
      method,
      credentials: 'include',
      headers: requestHeaders(options, mutation),
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    const offline = typeof navigator !== 'undefined' && navigator.onLine === false
    throw new ApiError(0, offline ? 'OFFLINE' : 'NETWORK_ERROR', offline ? 'You are offline' : 'Network request failed')
  }

  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as ApiErrorEnvelope
    const code = payload.error?.code ?? 'REQUEST_FAILED'
    const message = payload.error?.message
      ?? (response.status === 401 ? 'Authentication required' : 'Request failed')
    throw new ApiError(response.status, code, message, currentState(payload.error?.details))
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export type { ApiRequestOptions } from './types'
