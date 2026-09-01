import { useEffect, useRef, type DependencyList, type RefObject } from 'react'

/** Every wizard's step-heading focus management is the same three pieces --
 * a heading ref, a first-render guard (so mounting the wizard doesn't yank
 * focus onto its own heading before any real step change happened), and an
 * effect that runs `onStepChange` on every later change to `deps`. Centralizes
 * what used to be duplicated per wizard (`RiskWorkspace`, `WorkflowList`,
 * `PolicyPanel`, `ScheduleWorkspace` x2, `ConnectorHealthPanel`). `deps` is
 * forwarded straight to the internal `useEffect`, so pass exactly what that
 * wizard's step change actually depends on -- `[step]`, `[step, invalidField]`,
 * or (`ConnectorHealthPanel`) `[step, connected]`. */
export function useWizardStepFocus(onStepChange: () => void, deps: DependencyList): RefObject<HTMLHeadingElement | null> {
  const headingRef = useRef<HTMLHeadingElement>(null)
  const isFirstRenderRef = useRef(true)
  useEffect(() => {
    if (isFirstRenderRef.current) { isFirstRenderRef.current = false; return }
    onStepChange()
    // deps is caller-supplied on purpose -- each wizard passes exactly its own step-change dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return headingRef
}

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
