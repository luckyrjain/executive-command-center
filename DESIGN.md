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
| Wizard review value (`.wizard-review dd.is-machine-value`) | `13px` | `ui-monospace, SFMono-Regular, Menlo, monospace` — the one deliberate exception to "one font," scoped to genuinely machine-shaped review values (IDs, timestamps, timezones, credential secrets) via an explicit class, not applied to every review value |

## Color

Near-monochrome grayscale plus one dark-navy anchor. No secondary accent in general use — don't introduce one without a reason.

Every color is a CSS custom property on `:root` in `styles.css` — style new work against a token (`var(--color-ink)`, `var(--color-border-default)`), never a raw hex value. The full token list lives in that `:root` block; the groups below are the ones you'll reach for most:

- **Ink:** `--color-ink` (`#18212f`)
- **Body text:** `--color-text-secondary`, `--color-text-tertiary`, `--color-text-muted`, `--color-text-copy`
- **Borders:** `--color-border-default`, `--color-border-subtle`, `--color-border-hairline`
- **Surfaces:** `--color-white` (cards), `--color-page-bg` (page), `--color-surface-recessed` (recessed/upcoming)
- **Error:** `--color-border-error` / `--color-bg-error` (`.error-panel`)

Token names describe role, not shade — `--color-border-panel` and `--color-border-panel-alt` are two visually-close-but-distinct border colors kept as separate tokens rather than merged, because the token migration was a pure refactor (every raw value mapped 1:1) and consolidating near-duplicates is a separate design decision nobody's made yet.

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
.field-form label { display: grid; gap: 7px; color: var(--color-text-secondary); font-size: 13px; font-weight: 700; }
.field-form label.field-checkbox { display: flex; align-items: center; gap: 8px; font-weight: 400; }
.field-form input, .field-form textarea, .field-form select { width: 100%; border: 1px solid var(--color-border-default); border-radius: 14px; background: var(--color-white); padding: 12px 14px; color: var(--color-ink); }
.field-form input[type="checkbox"] { width: auto; }
.field-form textarea { min-height: 150px; resize: vertical; }
.field-form button { justify-self: start; }
```

**One JSX rule this depends on:** the `<label>` must directly wrap its input and its visible text — `<label>Field label<input aria-label="Field label" .../></label>`. No wrapper divs between label and input, no label wrapping more than one input. Break that shape and the layout breaks with it.

**Checkbox fields get an explicit `field-checkbox` class**, not implicit detection: `<label className="field-checkbox"><input type="checkbox" .../> Label text</label>`. This used to be `.field-form label:has(input[type="checkbox"])` — a selector that inferred layout from DOM shape (does this label happen to contain a checkbox?) rather than stating it. The explicit class is the fix; a fuller `.field`/`.field-label`/`.field-control` wrapper-div primitive (the other shape an external review proposed) was evaluated and deliberately not adopted — it would mean rewriting every field in every `.field-form` consumer away from the flat, working `<label>text<input/></label>` shape above for no behavior change, which is disruption without a corresponding win. Revisit only if a real new requirement (not just structural purity) needs it.

## Wizards: when and how

### When to wizardize

5+ fields on one screen, or real structural complexity — a dynamic array of multi-field sub-items, genuine conditional branching between field groups. A 2-4 field form stays a single page; don't wizardize for its own sake. Edit forms stay flat even when their create-form sibling is a wizard — that's the established call across Risk, Task, Commitment, and Schedule.

### Reference implementations

Read one before building a new wizard: `ConnectorHealthPanel.tsx` (the original), `RiskWorkspace.tsx` (Create risk), `WorkflowList.tsx` (Draft a new workflow), `ScheduleWorkspace.tsx` (Create calendar event / Create meeting), `PolicyPanel.tsx` (Create a policy) — all under `frontend/src/features/`.

### The mechanics

**State.** `const [createStepIndex, setCreateStepIndex] = useState(0)`, a `const CREATE_STEPS = ['step-a', 'step-b', 'review'] as const` tuple, a label map, and `const createStep = CREATE_STEPS[createStepIndex] ?? 'step-a'`.

**Focus.** Call the shared `useWizardStepFocus(onStepChange, deps)` hook from `frontend/src/lib/wizardFocus.ts` — it owns the heading ref, the first-render guard, and the `useEffect` that runs `onStepChange` on every later change to `deps`, so it never fires on mount:

```ts
const stepHeadingRef = useWizardStepFocus(
  () => applyWizardFieldInvalidState(formRef.current, invalidField, ERROR_ID, stepHeadingRef.current),
  [step, invalidField],
)
```

`deps` is forwarded straight through to the internal `useEffect`, so pass exactly what that wizard's step change depends on — `[step, invalidField]` for the terminal-validation wizards below, or just `[step]` (or `[step, someOtherState]`, per `ConnectorHealthPanel`) for a wizard that doesn't need it. This replaced 6 near-identical `useRef`+`useRef(true)`+`useEffect` blocks (one per wizard) with one shared hook. Give each step's heading `tabIndex={-1}` so it's a valid focus target.

**Stepper markup** — reuse verbatim, swap the labels. It's a real `<ol>`/`<li>` list, not a `role="group"` div — the browser's own list semantics (`role="list"`/`"listitem"`) are the correct fit for an ordered sequence of steps, and `aria-label` on the `<ol>` still names the whole group:

```jsx
<ol className="wizard-stepper" aria-label="...">
  {STEPS.map((step, i) => (
    <li className="wizard-step-node" key={step} aria-current={i === stepIndex ? 'step' : undefined}>
      <span className={i < stepIndex ? 'wizard-step-circle done' : i === stepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>{i < stepIndex ? '✓' : i + 1}</span>
      <span className={i <= stepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{LABELS[step]}</span>
      {i < STEPS.length - 1 ? <span className={i < stepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
    </li>
  ))}
</ol>
```

A test querying the stepper needs `getByRole('list', { name: '...' })`, not `'group'`.

**Each non-review step** is a `<div className="field-form">`: eyebrow (`Step {i+1} of {N} · {Label}`), the step's ref'd heading, its fields, then `.work-actions` with `Back`/`Continue` — both `type="button"`, never `type="submit"`. Continue is non-blocking by design; no per-step validation gate.

**The review step** is a `<div className="wizard-review">`: a `<dl>` of every field as `<dt>`/`<dd>` pairs, then `.work-actions` with `Back` and the terminal action button — `type="submit"`. Add `className="is-machine-value"` to a `<dd>` only when its value is genuinely machine-shaped — an ID, a timestamp, a timezone string, a credential secret — not to every value by default; a description or a status word stays in the regular sans font.

**A wizard IS a `<form>`.** It creates a resource; it's semantically a form regardless of how many steps it takes to fill out, and removing `<form>` throws away real behavior for no reason: Enter-to-submit, form landmark semantics for assistive tech, and the native submit event conventional tests rely on. Wrap the whole wizard — stepper and every step's content — in one `<form ref={formRef} noValidate onSubmit={attemptCreate} aria-labelledby="...">`. Only the terminal action is `type="submit"`; Continue/Back stay `type="button"` so they never trigger a submit. `attemptCreate` takes `(event: FormEvent)` and calls `event.preventDefault()`, same as any other form handler in this codebase.

**`noValidate`, not `required` removal.** A multi-step draft's fields aren't all mounted at once, so the browser's native constraint validation can't find "the first invalid field" the way this app's own validation does (below) — it would just silently block a submit from a step where nothing looks wrong, or, worse, refuse to report a required field that isn't currently rendered at all. Put `noValidate` on the form so custom JS validation owns the UX; leave `required` attributes in place (they're inert under `noValidate`, but still a correct semantic hint for assistive tech that doesn't run this component's JS, e.g. a screen reader's forms-list view). Don't strip `required` just because the enclosing element used to be a `<div>`.

**Reset on success.** `createStepIndex` back to `0` in the mutation's `onSuccess`, alongside the field-state reset and the invalid-field state (below).

### Terminal validation and error navigation

Continue never validates, so a failed final submit can discover an invalid field several steps back — the wizard must handle that gracefully, not just show a banner and strand the user wherever they happened to be. The contract: **on a failed submit, find the first invalid field, jump the wizard to the step it lives on, focus it, and mark it up so assistive tech can find it too.**

Mechanically, reuse `frontend/src/lib/wizardFocus.ts`:

- `findWizardField(container, label)` looks a field up by `aria-label`, or — for the fields that rely on native label association instead of a redundant `aria-label` (WCAG 2.5.3) — by its wrapping `<label>`'s visible text.
- `applyWizardFieldInvalidState(container, invalidField, errorId, fallbackRef)` clears stale `aria-invalid`/`aria-describedby` from the whole form, then applies both to the field matching `invalidField` and focuses it, or falls back to focusing `fallbackRef` (the current step's heading) when there's no invalid field to redirect to. Call it from the same step-change `useEffect` that already handles heading focus — pass it `invalidField` as an extra dependency.

Per wizard: keep a `const [invalidField, setInvalidField] = useState<string | null>(null)`, and change every validation-failure branch to set three things together — the error message, `invalidField` (the field's accessible name, exactly as `findWizardField` will look it up), and `createStepIndex` (via `CREATE_STEPS.indexOf(step)`). A small `fail(message, field, step)` closure per wizard keeps this from turning into three-line repetition at every check. **Keep the validation logic's check order and the failure-branch order in lock step** — if a field's own validator (e.g. `validateDraft`) checks fields in a different order than the failure branches that report which step to jump to, the error *message* stays right but the navigation can point at the wrong field. Clear `invalidField` on `goNext`/`goBack` (manual navigation shouldn't leave a stale field marked invalid) and in the mutation's `onSuccess`.

A thrown error that isn't tied to one obvious field (e.g. `ScheduleWorkspace.tsx`'s `wallTimeToInstant` can fail for a bad start, a bad end, or a bad timezone, and throws the same generic message for a malformed value) still needs per-field attribution, not a single hardcoded guess — validate the individual fields in a defined order before attempting the mutation, and inspect the specific error to decide which field it actually points at.

**Two wizards on one screen** (Schedule's Create event + Create meeting) means their `Continue`/`Back` labels collide. Scope test queries to each wizard's own panel — `within()` in Vitest, a locator scoped to the panel in Playwright — not a bare role/name query.

**Accessibility scans cover DOM states, not pages.** A wizard has as many distinct states as it has steps. The first time an e2e scenario drives a wizard, add one scoped `assertNoSeriousAccessibilityViolations` scan per non-default step, not just one on the default first step.

## Interaction states

What's actually implemented today, by state — reach for these rather than inventing a new treatment:

| State | Where | Treatment |
|---|---|---|
| Hover | any `button` | `border-color` steps from `--color-border-default` to `--color-text-muted` (`button:hover:not(:disabled)`) — no background/color change |
| Focus-visible | `button`, `input`, `textarea`, `select` | `outline: 3px solid var(--focus-ring)` with `3px` offset — one rule, every focusable control, never a custom per-component focus style |
| Disabled | `button:disabled` | `opacity: .6; cursor: wait` — same shape for every button, no per-button disabled styling |
| Loading | in-flight mutation button | swap the label text for a present-participle string (`"Connecting…"`, `"Creating…"`) *and* set `disabled` — text alone isn't enough for a user who can't see it change, and `disabled` alone isn't enough for a user who can't see the button gray out |
| Selected / active tab | `.tab-list button[aria-selected="true"]` | filled `--color-ink` background — `aria-selected` drives both the visual and the accessible state together, never one without the other |
| Wizard step (done / active / upcoming) | `.wizard-step-circle`, `.wizard-step-label` | three explicit modifier classes (`done`/`active`/`upcoming`), never inferred from index math in CSS |

Nothing here is a new pattern — this table names what the app already does so a new feature copies the existing treatment instead of reinventing a fourth way to gray out a button.

## Responsive behavior

Two breakpoints, both in `styles.css`'s trailing media queries — there's no separate responsive spec file, this section just names what's already there so it doesn't need rediscovering by reading CSS top to bottom.

| Breakpoint | What changes |
|---|---|
| `max-width: 800px` | Dashboard/brief cards go full-width (`grid-column: 1 / -1`); `.recommendation-list` items and `.work-grid` collapse to a single column; `.recommendation-actions` and `.audit-list` items switch from column to row layout (or the reverse) to fit the narrower measure |
| `max-width: 520px` | Panel heading rows (`.topbar`, `.brief-heading`, etc.) stack vertically instead of side-by-side; panel corner radius shrinks; `.search-form > div` and `.tab-list` go full-width and stack |

No component-level responsive behavior beyond this — a wizard, a `.field-form`, and a stepper all render identically from mobile through desktop widths; only the surrounding page chrome (headings, card grids, action rows) reflows. If a new surface needs its own breakpoint behavior, it's a deviation from the current pattern, not an extension of it — call that out explicitly rather than adding a third silent breakpoint.

## AI-slop check

Clean today, worth re-checking on any large new surface: no purple/gradient backgrounds, no 3-column icon-in-circle feature grids, no centered-everything, no decorative blobs or emoji-as-design, no generic hero copy. The palette and type scale above are the real system — don't default to unstyled `Inter` or a bootstrap-template look for anything new.

## Known follow-ups (deferred, not forgotten)

An external design-system review of this document raised a P1/P2 list beyond the P0s folded in above. All of it is now resolved, one item by a deliberately different route than the review proposed:

- **Design tokens** — done. Every color in `styles.css` is a `:root` custom property; see Color above.
- **`<ol>`/`<li>` stepper semantics** — done; see the stepper markup under Wizards above.
- **Centralized focus-management hook** — done (`useWizardStepFocus` in `wizardFocus.ts`), replacing the 6 per-wizard `useRef`+`useRef(true)`+`useEffect` blocks.
- **`.wizard-review dd` monospace scoping** — done via an explicit `is-machine-value` class; see the review-step note under Wizards above.
- **Interaction-states catalogue** and **responsive-behavior spec** — both documented above as new sections; no CSS changed, this was a documentation gap.
- **`.field`/`.field-label`/`.field-control` primitives** (to replace the `.field-form label:has(...)` DOM-shape dependency) — resolved differently than proposed. The root problem (a selector inferring layout from DOM shape) is fixed with a narrower change: an explicit `field-checkbox` class on the label that needs the row layout, rather than `:has(input[type="checkbox"])`. The fuller wrapper-div primitive was considered and rejected — it would mean touching every field in every `.field-form` consumer to add no new behavior, working against the flat `<label>text<input/></label>` shape this doc otherwise documents as load-bearing. Reopen only if a real requirement (not structural purity alone) needs the extra wrapper.

## Provenance

Extracted 2026-08-31 from the live app during a design pass (#203) and the wizard sweep that followed (#204). P0 fixes (semantic `<form>`, terminal-validation error navigation) applied the same day after an external design-system review. P1/P2 follow-ups (design tokens, list-semantic stepper, centralized focus hook, monospace scoping, interaction-states/responsive documentation, and the checkbox-field selector fix) applied 2026-09-01.
