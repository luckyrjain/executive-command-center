# Design system

This is the frontend's actual, rendered design system (extracted from the live app, not aspirational). Calibrate any future visual work against this before introducing new patterns.

## Typography

- One font family app-wide: `Inter, ui-sans-serif, system-ui, -apple-system, "system-ui", "Segoe UI", sans-serif`.
- Panel titles (`.work-heading h2`, and equivalents): `font-size: clamp(28px, 4vw, 44px)`, `letter-spacing: -.035em`.
- Section headings inside a panel: `18px`, weight `700`.
- Eyebrow labels (`.eyebrow`): small, uppercase, muted -- used above a heading or above a wizard step (`Step N of M · Label`).

## Color

Near-monochrome grayscale plus one dark navy anchor. No secondary accent color in general use.

- Text/ink: `#18212f` (near-black navy)
- Body/secondary text: `#596579`, `#5a6472`, `#667085`
- Borders/dividers: `#cfd6df`, `#e3e8ef`, `#e7ebf0`
- Surfaces: `#fff` (cards), `#f3f5f7` (page background), `#eef1f5` (recessed/upcoming)
- Error: border `#e7b8b8`, background `#fff7f7` (`.error-panel`)

Keep new UI within this palette. Don't introduce a new accent color without a reason.

## Layout primitives

- `.work-panel`: the standard card -- `border-radius: 28px`, `box-shadow: 0 18px 48px rgba(24,33,47,.06)`, `padding: clamp(24px, 4vw, 42px)`.
- `.work-heading`: panel title block (eyebrow + h1/h2 + description).
- `.work-actions`: button row -- `display: flex; flex-wrap: wrap; gap: 8px`.
- `.empty-state`: centered muted text for zero-item lists.
- `.inline-status` / `.error-panel`: status and error banners (`role="status"` / `role="alert"`).

## Forms: `.field-form`

Every labeled-fields block in the app uses this one class -- single-column grid, `gap: 14px`, each `<label>` stacks its text above its input.

```css
.field-form { display: grid; gap: 14px; margin-top: 28px; }
.field-form label { display: grid; gap: 7px; color: #596579; font-size: 13px; font-weight: 700; }
.field-form label:has(input[type="checkbox"]) { display: flex; align-items: center; gap: 8px; font-weight: 400; }
.field-form input, .field-form textarea, .field-form select { width: 100%; border: 1px solid #cfd6df; border-radius: 14px; background: #fff; padding: 12px 14px; color: #18212f; }
.field-form input[type="checkbox"] { width: auto; }
.field-form textarea { min-height: 150px; resize: vertical; }
.field-form button { justify-self: start; }
```

**Required JSX shape:** the `<label>` element must directly wrap its input/textarea/select and its visible text, e.g. `<label>Field label<input aria-label="Field label" .../></label>`. Nested wrapper divs between label and input, or a label wrapping multiple inputs, break the layout -- don't do that inside a `.field-form`.

Apply `.field-form` to any new labeled-fields container, full stop. There is no unstyled form left in the app; keep it that way.

## Wizards: when and how

**When to wizardize a form:** 5+ fields crammed on one screen, or genuine structural complexity (a dynamic array of multi-field sub-items, real conditional branching between field groups). A 2-4 field form is fine as one page -- don't wizardize for its own sake. Edit forms are left as flat single-page forms even when their create-form counterpart is a wizard (established precedent: Risk, Task, Commitment, Schedule event/meeting all keep flat edit forms).

**Reference implementations** (read one of these before building a new wizard): `frontend/src/features/engineering/ConnectorHealthPanel.tsx` (the original), `frontend/src/features/risks/RiskWorkspace.tsx` (Create risk), `frontend/src/features/automation/WorkflowList.tsx` (Draft a new workflow), `frontend/src/features/schedule/ScheduleWorkspace.tsx` (Create calendar event / Create meeting), `frontend/src/features/automation/PolicyPanel.tsx` (Create a policy).

**The pattern, mechanically:**

1. State: `const [createStepIndex, setCreateStepIndex] = useState(0)`, a `const CREATE_STEPS = ['step-a', 'step-b', 'review'] as const` tuple, and a `Record<..., string>` of display labels. Compute `const createStep = CREATE_STEPS[createStepIndex] ?? 'step-a'`.
2. Focus management: a `useRef<HTMLHeadingElement>` per step heading plus a `useRef(true)` "is this the first render" guard, so a `useEffect` on `[createStep]` moves focus to the incoming step's own heading -- but only on a real step change, not on initial mount. Give each step's heading `tabIndex={-1}` so it's a valid `.focus()` target.
3. Stepper markup -- reuse verbatim:
   ```jsx
   <div className="wizard-stepper" role="group" aria-label="...">
     {STEPS.map((step, i) => (
       <div className="wizard-step-node" key={step} aria-current={i === stepIndex ? 'step' : undefined}>
         <span className={i < stepIndex ? 'wizard-step-circle done' : i === stepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>{i < stepIndex ? '✓' : i + 1}</span>
         <span className={i <= stepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{LABELS[step]}</span>
         {i < STEPS.length - 1 ? <span className={i < stepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
       </div>
     ))}
   </div>
   ```
4. Each non-review step: `<div className="field-form">` containing an eyebrow (`Step {i+1} of {N} · {Label}`), the step's own `ref`+`tabIndex={-1}` heading, its fields, then a `.work-actions` row with `Back`/`Continue` -- both `type="button"`, never `type="submit"`. **Continue is non-blocking** (no per-step validation gate) -- this is a deliberate, established choice, not an oversight.
5. Final review step: `<div className="wizard-review">` with a `<dl>` summarizing every field (`<dt>`/`<dd>` pairs), then `.work-actions` with `Back` and the terminal action button (`type="button"`, calls the mutation directly).
6. There is no `<form>` element anywhere in a wizard. The old `submit(event: FormEvent)` becomes `attemptCreate()` -- same validation logic, minus `event.preventDefault()`, called via the terminal button's `onClick`. Drop any now-inert `required` attributes that depended on the removed `<form>`'s native validation.
7. Reset `createStepIndex` back to `0` in the mutation's `onSuccess`, alongside the existing field-state reset.
8. If two wizards render on the same page simultaneously (e.g. Schedule's Create event + Create meeting), their `Continue`/`Back` button labels collide -- tests must scope queries to each wizard's own panel (`within()` in Vitest, a locator scoped to the panel in Playwright), not query by role/name alone.

**Accessibility scanning:** an e2e scenario's existing `assertNoSeriousAccessibilityViolations` scan only covers whatever DOM state was on screen when it ran. A wizard has as many distinct DOM states as it has steps -- add one scoped scan per non-default step (after each `Continue` click) the first time a scenario drives that wizard, not just one scan on the default first step.

## AI-slop check

Already verified clean and worth re-checking on any large new surface: no purple/gradient backgrounds, no 3-column icon-in-circle feature grids, no centered-everything headings, no decorative blobs/emoji-as-design, no generic hero copy. Palette and typography above are the actual system -- don't default to `Inter`-as-primary-with-no-opinion or a bootstrap-template look for anything new.

## Provenance

Extracted 2026-08-31 from the live app (`/design-review`, `#203`) and the follow-up wizard sweep (`#204`). Update this file when the design system itself changes -- a new component class, a new color, a revised wizard convention -- not for one-off feature work.
