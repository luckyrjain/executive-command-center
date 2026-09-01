import { formatTime } from '../lib/datetime'

// Shared by App.tsx's own dashboard sections and MorningBrief.tsx's brief
// sections -- both render the same underlying `_build_sections()` shape
// (see backend/ecc/domains/platform/dashboard_briefs.py), one live, one
// persisted/versioned. Was duplicated byte-for-byte between the two files
// aside from the heading-id prefix.
export type DashboardItem = {
  id?: string
  entity_id?: string
  entity_ref?: string
  entity_type?: string
  title?: string
  summary?: string
  message?: string
  why?: string
  explanation?: string
  status?: string
  score?: number
  starts_at?: string
  due_at?: string
  due_date?: string
  occurred_at?: string
  empty?: boolean
}

export function labelFor(item: DashboardItem): string {
  return item.title ?? item.summary ?? item.why ?? item.explanation ?? item.message ?? item.entity_ref ?? 'Untitled item'
}

export function visibleItems(items?: DashboardItem[]): DashboardItem[] {
  return (items ?? []).filter((item) => !item.empty)
}

type SectionProps = {
  title: string
  items?: DashboardItem[]
  emptyMessage: string
  // App.tsx's own sections use `section-`; MorningBrief.tsx's use
  // `brief-section-` -- distinct ids since both can render on the same
  // page (the 'today' tab mounts both).
  headingIdPrefix?: string
  // 'panel' promotes this section to the page's single dominant anchor
  // (DESIGN.md's "Building a new page" Hierarchy rules) -- same .work-panel
  // primitive every other page's primary content uses, not a new one.
  // Everything else about the section (heading, item list, empty state)
  // stays identical; only the outer surface changes weight.
  variant?: 'card' | 'panel'
}

export function Section({ title, items, emptyMessage, headingIdPrefix = 'section-', variant = 'card' }: SectionProps) {
  const visible = visibleItems(items)
  const headingId = `${headingIdPrefix}${title.replaceAll(' ', '-').toLowerCase()}`
  return (
    <section className={variant === 'panel' ? 'work-panel' : 'dashboard-card'} aria-labelledby={headingId}>
      <div className="section-heading">
        <h2 id={headingId}>{title}</h2>
        <span aria-label={`${visible.length} items`}>{visible.length}</span>
      </div>
      {visible.length ? (
        <ol className="item-list">
          {visible.map((item, index) => (
            <li key={item.id ?? item.entity_id ?? item.entity_ref ?? `${title}-${index}`}>
              <div>
                <strong>{labelFor(item)}</strong>
                {item.status ? <small>{item.status.replaceAll('_', ' ')}</small> : null}
              </div>
              <div className="item-meta">
                {formatTime(item.starts_at ?? item.occurred_at) ? <time>{formatTime(item.starts_at ?? item.occurred_at)}</time> : null}
                {typeof item.score === 'number' ? <span>{Math.round(item.score)}</span> : null}
              </div>
            </li>
          ))}
        </ol>
      ) : (
        <p className="empty-state">{items?.find((item) => item.empty)?.message ?? emptyMessage}</p>
      )}
    </section>
  )
}
