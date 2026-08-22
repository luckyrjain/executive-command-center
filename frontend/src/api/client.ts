import type { ApiErrorEnvelope, ApiRequestOptions } from './types'

// `??`, not `||`: vite.config.ts defines this as `env.VITE_API_BASE_URL ?? ''`
// (empty string, not undefined, when unset), so the frontend can make
// relative same-origin requests when a deployer forgets to configure it,
// matching frontend/nginx.conf.template's `connect-src 'self'` CSP
// fallback. `||` would treat that empty string as falsy and silently
// substitute the cross-origin localhost default instead, which the CSP
// would then block every request against.
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly current: unknown
  // Support can look up the exact backend request from either id -- the
  // backend's response_contract_middleware stamps both onto every error
  // (X-Request-ID/X-Correlation-ID headers, plus error.request_id and a
  // top-level correlation_id in the JSON body). Optional because the
  // client-synthesized OFFLINE/NETWORK_ERROR errors never reach the
  // backend and so have neither.
  readonly requestId?: string
  readonly correlationId?: string

  constructor(status: number, code: string, message: string, current?: unknown, requestId?: string, correlationId?: string) {
    super(message)
    this.name = code
    this.status = status
    this.code = code
    this.current = current
    this.requestId = requestId
    this.correlationId = correlationId
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
  // `options.csrf` is a separate condition from `mutation`, not folded into
  // it: a handful of GET endpoints have a real server-side side effect and
  // require `CsrfDep` despite being safe HTTP methods (see `ApiRequestOptions.
  // csrf`'s own comment) -- Idempotency-Key stays gated on `mutation` alone,
  // since none of those GETs declare an idempotency-key dependency at all.
  if (mutation || options.csrf) headers['X-CSRF-Token'] = cookieValue('ecc_csrf')
  if (mutation) headers['Idempotency-Key'] = crypto.randomUUID()
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
    // Headers are the fallback, not the primary source: the body is only
    // absent/unparsed for a non-JSON or malformed error response, but the
    // response_contract_middleware sets both headers on every response
    // regardless of body shape.
    const requestId = payload.error?.request_id ?? response.headers.get('X-Request-ID') ?? undefined
    const correlationId = payload.correlation_id ?? response.headers.get('X-Correlation-ID') ?? undefined
    throw new ApiError(response.status, code, message, currentState(payload.error?.details), requestId, correlationId)
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export type { ApiRequestOptions } from './types'
