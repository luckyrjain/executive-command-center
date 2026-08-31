# Design system

The frontend's actual, rendered design system — not aspirational, not a wishlist. Read this before styling anything new; it tells you the palette, the two reusable patterns (`.field-form`, wizards), and when to reach for each. Update it when the system itself changes, not for one-off feature work.

**Quick answer:** styling a labeled field? Use `.field-form`. Building a multi-field create flow? Check "When to wizardize" below before reaching for a flat form.

## Typography

One font, everywhere: `Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.

| Element | Size | Notes |
|---|---|---|
| Panel title (`.work-heading h2`) | `clamp(28px, 4vw, 44px)` | `letter-spacing: -.035em` |
| Section heading | `18px` | weight `700` |
| Eyebrow label | small, uppercase, muted | sits above a heading, or above a wizard step (`Step N of M · Label`) |
| Wizard review value (`.wizard-review dd`) | `13px` | `ui-monospace, SFMono-Regular, Menlo, monospace` — the one deliberate exception to "one font," for tabular/ID-shaped review values |

## Color

Near-monochrome grayscale plus one dark-navy anchor. No secondary accent in general use — don't introduce one without a reason.

- **Ink:** `#18212f`
- **Body text:** `#596579`, `#5a6472`, `#667085`
- **Borders:** `#cfd6df`, `#e3e8ef`, `#e7ebf0`
- **Surfaces:** `#fff` (cards), `#f3f5f7` (page), `#eef1f5` (recessed/upcoming)
- **Error:** border `#e7b8b8`, background `#fff7f7` (`.error-panel`)

## Layout primitives

| Class | What it does |
|---|---|
| `.work-panel` | Standard card: `border-radius: 28px`, soft shadow, `padding: clamp(24px, 4vw, 42px)` |
| `.work-heading` | Panel title block: eyebrow + h1/h2 + description |
| `.work-actions` | Button row: `flex`, `wrap`, `gap: 8px` |
| `.empty-state` | Centered muted text for a zero-item list |
| `.inline-status` / `.error-panel` | Status and error banners (`role="status"` / `role="alert"`) |

## Forms: `.field-form`

Every labeled-fields block in the app uses one class. Single column, `gap: 14px`, label text stacked above its input — no exceptions, no bespoke form CSS anywhere else.

```css
.field-form { display: grid; gap: 14px; margin-top: 28px; }
.field-form label { display: grid; gap: 7px; color: #596579; font-size: 13px; font-weight: 700; }
.field-form label:has(input[type="checkbox"]) { display: flex; align-items: center; gap: 8px; font-weight: 400; }
.field-form input, .field-form textarea, .field-form select { width: 100%; border: 1px solid #cfd6df; border-radius: 14px; background: #fff; padding: 12px 14px; color: #18212f; }
.field-form input[type="checkbox"] { width: auto; }
.field-form textarea { min-height: 150px; resize: vertical; }
.field-form button { justify-self: start; }
```

**One JSX rule this depends on:** the `<label>` must directly wrap its input and its visible text — `<label>Field label<input aria-label="Field label" .../></label>`. No wrapper divs between label and input, no label wrapping more than one input. Break that shape and the layout breaks with it.

## Wizards: when and how

### When to wizardize

5+ fields on one screen, or real structural complexity — a dynamic array of multi-field sub-items, genuine conditional branching between field groups. A 2-4 field form stays a single page; don't wizardize for its own sake. Edit forms stay flat even when their create-form sibling is a wizard — that's the established call across Risk, Task, Commitment, and Schedule.

### Reference implementations

Read one before building a new wizard: `ConnectorHealthPanel.tsx` (the original), `RiskWorkspace.tsx` (Create risk), `WorkflowList.tsx` (Draft a new workflow), `ScheduleWorkspace.tsx` (Create calendar event / Create meeting), `PolicyPanel.tsx` (Create a policy) — all under `frontend/src/features/`.

### The mechanics

**State.** `const [createStepIndex, setCreateStepIndex] = useState(0)`, a `const CREATE_STEPS = ['step-a', 'step-b', 'review'] as const` tuple, a label map, and `const createStep = CREATE_STEPS[createStepIndex] ?? 'step-a'`.

**Focus.** One `useRef<HTMLHeadingElement>` per step heading, plus a `useRef(true)` first-render guard, so a `useEffect` on `[createStep]` moves focus to the incoming step's heading — only on a real step change, never on mount. Give each heading `tabIndex={-1}` so it's a valid focus target.

**Stepper markup** — reuse verbatim, swap the labels:

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

**Each non-review step** is a `<div className="field-form">`: eyebrow (`Step {i+1} of {N} · {Label}`), the step's ref'd heading, its fields, then `.work-actions` with `Back`/`Continue` — both `type="button"`, never `type="submit"`. Continue is non-blocking by design; no per-step validation gate.

**The review step** is a `<div className="wizard-review">`: a `<dl>` of every field as `<dt>`/`<dd>` pairs, then `.work-actions` with `Back` and the terminal action button, `type="button"`, calling the mutation directly.

**No `<form>` anywhere in a wizard.** The old `submit(event: FormEvent)` becomes `attemptCreate()` — same validation, minus `event.preventDefault()`, wired to the terminal button's `onClick`. Drop any `required` attribute that only worked because of the removed `<form>`'s native validation. If the same field component is shared with a real, still-`<form>`-wrapped edit view (e.g. `ScheduleWorkspace.tsx`'s `TimingFields`), give it a `required` prop defaulting to `true` and pass `required={false}` only from the wizard call site — don't drop it for the edit form too.

**Reset on success.** `createStepIndex` back to `0` in the mutation's `onSuccess`, alongside the field-state reset.

**Two wizards on one screen** (Schedule's Create event + Create meeting) means their `Continue`/`Back` labels collide. Scope test queries to each wizard's own panel — `within()` in Vitest, a locator scoped to the panel in Playwright — not a bare role/name query.

**Accessibility scans cover DOM states, not pages.** A wizard has as many distinct states as it has steps. The first time an e2e scenario drives a wizard, add one scoped `assertNoSeriousAccessibilityViolations` scan per non-default step, not just one on the default first step.

## AI-slop check

Clean today, worth re-checking on any large new surface: no purple/gradient backgrounds, no 3-column icon-in-circle feature grids, no centered-everything, no decorative blobs or emoji-as-design, no generic hero copy. The palette and type scale above are the real system — don't default to unstyled `Inter` or a bootstrap-template look for anything new.

## Provenance

Extracted 2026-08-31 from the live app during a design pass (#203) and the wizard sweep that followed (#204).
