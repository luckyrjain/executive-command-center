/** Finds a wizard field by its accessible name -- either an explicit
 * `aria-label`, or the visible text of its wrapping `<label>` (WCAG 2.5.3
 * fields that rely on native label association instead of a redundant
 * aria-label). Used to focus and mark aria-invalid the first field a failed
 * final submit found invalid, after navigating back to the step it lives on. */
export function findWizardField(container: HTMLElement, label: string): HTMLElement | null {
  const byAria = container.querySelector<HTMLElement>(`[aria-label="${label}"]`)
  if (byAria) return byAria
  for (const labelEl of container.querySelectorAll('label')) {
    const control = labelEl.querySelector<HTMLElement>('input, select, textarea')
    if (control && (labelEl.textContent ?? '').trim().startsWith(label)) return control
  }
  return null
}

/** Clears aria-invalid/aria-describedby from every field in the container,
 * then applies them to the field found for `invalidField` (if any) and
 * focuses it. Falls back to focusing `fallbackRef` (typically the current
 * step's own heading) when there's no invalid field to redirect to. */
export function applyWizardFieldInvalidState(
  container: HTMLElement | null,
  invalidField: string | null,
  errorId: string,
  fallbackRef: HTMLElement | null,
): void {
  container?.querySelectorAll('[aria-invalid]').forEach((node) => {
    node.removeAttribute('aria-invalid')
    node.removeAttribute('aria-describedby')
  })
  const invalidEl = invalidField && container ? findWizardField(container, invalidField) : null
  if (invalidEl) {
    invalidEl.setAttribute('aria-invalid', 'true')
    invalidEl.setAttribute('aria-describedby', errorId)
    invalidEl.focus()
    return
  }
  fallbackRef?.focus()
}
