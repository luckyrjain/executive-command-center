import type { ConnectorAccount } from '../features/engineering/types'

// No provider connector (Gmail included) has a periodic freshness monitor --
// "stale connector" (UX-STATES.md) is derived client-side from
// `last_synced_at`'s own age, not a separate backend field. 24 hours is a
// disclosed, deliberately conservative heuristic (`repositories`/
// `engineering_work_items`'s own `freshness_state` uses the identical
// concept per-row, computed by the sync adapters themselves -- this is the
// account-level analogue where no equivalent field exists). Shared by
// `ConnectorHealthPanel.tsx` (generic providers) and `GmailPanel.tsx`
// (Gmail), which used to each define this identically.
export const STALE_AFTER_MS = 24 * 60 * 60 * 1000

export function isStale(connector: ConnectorAccount, now: Date): boolean {
  if (!connector.last_synced_at) return false
  return now.getTime() - new Date(connector.last_synced_at).getTime() > STALE_AFTER_MS
}

/** Maps `ConnectorAccountResponse.status` (the one field this backend
 * exposes -- there is no separate "degraded" flag) onto the UX-STATES.md
 * required states this single enum must carry: `pending` is "first sync
 * not yet run", `permission_lost` is "partial permissions",
 * `rate_limited`/`disconnected` are named directly, and `error` is
 * "provider unavailable" (paired with `last_error`). */
export function statusPanelClass(status: ConnectorAccount['status']): string {
  if (status === 'error' || status === 'disconnected') return 'inline-status error-panel'
  if (status === 'permission_lost' || status === 'rate_limited') return 'inline-status degraded-panel'
  return 'inline-status'
}

/** Same status enum as `statusPanelClass` above, mapped onto the compact
 * `.status-badge` treatment instead -- `disconnected` gets its own neutral
 * tone here rather than `statusPanelClass`'s red `error-panel`: it's an
 * expected, often user-initiated state, not a fault, and a bright red
 * badge on every disconnected-but-otherwise-fine connector overstated it.
 * `active` gets a real success tone, absent from `statusPanelClass`
 * entirely (which only ever colors the bad states). */
export function statusBadgeClass(status: ConnectorAccount['status']): string {
  if (status === 'active') return 'status-badge is-active'
  if (status === 'error') return 'status-badge is-error'
  if (status === 'permission_lost' || status === 'rate_limited') return 'status-badge is-degraded'
  return 'status-badge is-neutral' // pending, disconnected
}
