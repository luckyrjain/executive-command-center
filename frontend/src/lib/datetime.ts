function pad(value: number): string { return String(value).padStart(2, '0') }

// Converts a server ISO instant into the local-timezone value a
// `<input type="datetime-local">` expects. Was duplicated byte-for-byte
// across TaskWorkspace.tsx, RiskWorkspace.tsx, CommitmentWorkspace.tsx
// and Planner.tsx (there as `toLocalInputValue`).
export function serverInstantToLocalInput(value: string): string {
  const instant = new Date(value)
  if (Number.isNaN(instant.getTime())) return ''
  return `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}T${pad(instant.getHours())}:${pad(instant.getMinutes())}`
}

// Renders a server ISO instant as a short local time (e.g. "2:30 PM") for
// display, not editing. Was duplicated byte-for-byte across App.tsx and
// MorningBrief.tsx.
export function formatTime(value?: string): string | null {
  if (!value) return null
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return null
  return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(parsed)
}
