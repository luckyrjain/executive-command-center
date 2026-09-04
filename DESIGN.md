# Design system

The frontend's actual, rendered design system — not aspirational, not a wishlist — plus process guidance for whoever is building the next page with it. Read this before styling anything new; it tells you the palette, the two reusable patterns (`.field-form`, wizards), how to compose a whole page out of them, and when to reach for each. Update it when the system itself changes, not for one-off feature work.

**Quick answer:** styling a labeled field? Use `.field-form`. Building a multi-field create flow? Check "When to wizardize" below before reaching for a flat form. Adding a new top-level view or in-page tab? Check Navigation below. Building a whole new page? Start at "Building a new page" below.

Two kinds of content live here, and they're marked differently. Most of this doc documents CSS that exists — a class, a token, a value you can go read in `styles.css` right now. **Building a new page** is different: it's judgment calls (what's the primary anchor, how many cards is too many) that no CSS enforces, written down so those calls stay consistent across pages instead of being reinvented per feature. Nothing in that section is a class you can apply; it's what to do with the classes above.

**Contents:** [Typography](#typography) · [Color](#color) · [Spacing](#spacing) · [Container & grid](#container--grid) · [Layout primitives](#layout-primitives) · [Navigation](#navigation) · [Geometry](#geometry-radius--elevation) · [Buttons](#buttons-action-hierarchy) · [Forms](#forms-field-form) · [Wizards](#wizards-when-and-how) · [Interaction states](#interaction-states) · [Responsive behavior](#responsive-behavior) · [Motion](#motion) · [Not yet part of the system](#not-yet-part-of-the-system) · [Building a new page](#building-a-new-page) · [Anti-patterns](#anti-patterns) · [Known follow-ups](#known-follow-ups-deferred-not-forgotten) · [Provenance](#provenance)

## Typography

One font, everywhere: `Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`.

| Element | Size | Notes |
|---|---|---|
| Panel title (`.work-heading h2`) | `clamp(28px, 4vw, 44px)` | `letter-spacing: -.035em` |
| Section heading | `18px` | weight `700` |
| Eyebrow label | small, uppercase, muted | sits above a heading, or above a wizard step (`Step N of M · Label`) |
| Wizard review value (`.wizard-review dd.is-machine-value`) | `13px` | `ui-monospace, SFMono-Regular, Menlo, monospace` — the one deliberate exception to "one font," scoped to genuinely machine-shaped review values (IDs, timestamps, timezones, credential secrets) via an explicit class, not applied to every review value |

## Color

Near-monochrome grayscale, one dark-navy anchor, and one deliberate accent (`--color-accent`, `#2C4A87`) — part of this app's Visual Foundation v2 redesign (see `docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`), chosen to feel like the restraint of Linear/Stripe's own dashboards without literally cloning either product's actual brand hue. As of this token landing, no component consumes it yet — that's deliberately deferred to the redesign's later sub-projects (navigation, dashboard). Still near-monochrome everywhere else: don't reach for a second accent without a reason.

Every color is a CSS custom property on `:root` in `styles.css` — style new work against a token (`var(--color-ink)`, `var(--color-border-default)`), never a raw hex value. The full token list lives in that `:root` block; the groups below are the ones you'll reach for most:

- **Ink:** `--color-ink` (`#18212f`)
- **Body text:** `--color-text-secondary`, `--color-text-tertiary`, `--color-text-muted`, `--color-text-copy`
- **Borders:** `--color-border-default`, `--color-border-subtle`, `--color-border-hairline`
- **Surfaces:** `--color-white` (cards), `--color-page-bg` (page), `--color-surface-recessed` (recessed/upcoming)
- **Error:** `--color-border-error` / `--color-bg-error` (`.error-panel`)
- **Elevation:** `--shadow-card` (`.dashboard-card`) and `--shadow-panel` (`.recommendation-panel`/`.explore-panel`/`.work-panel`) — the app's only two shadow values, see Geometry below for when each applies
- **Accent:** `--color-accent` (`#2C4A87`) — not yet consumed by any component; reserved for the redesign's navigation/primary-action work

Token names describe role, not shade — `--color-border-panel` and `--color-border-panel-alt` are two visually-close-but-distinct border colors kept as separate tokens rather than merged, because the token migration was a pure refactor (every raw value mapped 1:1) and consolidating near-duplicates is a separate design decision nobody's made yet.

Semantic state colors beyond error: **success** (`--color-text-success`, `--color-border-success`/`--color-bg-success`, used in `.evidence-preview .evidence-available`) and **danger** (`--color-border-danger`/`--color-bg-danger`/`--color-text-danger-strong`, used in `.evidence-preview .evidence-missing` and the destructive button variant below) both exist and are muted enough to sit next to the grayscale palette without reading as decoration. `--color-border-degraded`/`--color-bg-degraded` (`.degraded-panel`) fills the **warning** role under a name specific to how it's actually used (a kill-switch-unknown or confirm-before-destructive state), not a renamed generic token. There is no **info** token — no surface in the app currently needs a generic informational banner distinct from status/error/degraded, so none was invented; add one only when a real surface needs it.

**Priority language.** The three families above are also documented as one deliberate 4-tier vocabulary for "how urgent is this" — **Critical** (the danger family), **Needs attention** (the degraded family), **Healthy** (the success family), and **Normal** (no color — default ink/border; "nothing to report" is not a hue). This is a naming/documentation layer only: the underlying `--color-*` custom property names are unchanged, so a new surface reaches for "Critical" or "Needs attention" as a named concept instead of reinventing the choice the way `EngineeringOverview.tsx`'s `.degraded-panel` usage already does informally, without any selector needing to change.

## Spacing

A global scale, `--space-1` (4px) through `--space-24` (96px), doubling roughly every two steps: `--space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 20px; --space-6: 24px; --space-8: 32px; --space-10: 40px; --space-12: 48px; --space-16: 64px; --space-20: 80px; --space-24: 96px;`. New CSS should reach for a token rather than a new arbitrary number. The scale documents the rhythm already visible in the app (most in-component gaps fall in the 8–24px range the low end of the scale covers; the `.app-shell`/`.brief-panel`/`.work-panel` top margins fall in the 32–40px range the high end covers) — existing rules were not force-migrated to reference the tokens where doing so wouldn't change the rendered value, so you'll still see raw numbers throughout `styles.css` alongside the new token block.

## Container & grid

`.app-shell` is the app's only page frame, and the tokens name its real values rather than introduce new ones: `--page-gutter: clamp(20px, 4vw, 64px); --content-max: 1440px;`. Two additional tokens exist for content measure: `--content-reading: 720px` (applied to `.work-heading p`/`.recommendation-heading p`/`.explore-heading p`; `.recommendation-copy > p` uses a pre-existing, unreconciled 760px and was left alone rather than silently changed) and `--content-form: 680px`, applied as a `max-width` on `.field-form` — every labeled-fields block in the app, wizard steps included, now caps at a readable measure instead of stretching to its panel's full width.

## Layout primitives

| Class | What it does |
|---|---|
| `.work-panel` | Standard card: `border-radius: 28px`, soft shadow, `padding: clamp(24px, 4vw, 42px)` |
| `.work-heading` | Panel title block: eyebrow + h1/h2 + description |
| `.work-actions` | Button row: `flex`, `wrap`, `gap: 8px` |
| `.empty-state` | Centered muted text for a zero-item list |
| `.inline-status` / `.error-panel` | Status and error banners (`role="status"` / `role="alert"`) |
| `.dashboard-grid` / `.dashboard-card` | 12-column grid of `--shadow-card`-elevated cards (`grid-column: span 6` by default), used by the Today dashboard and the brief panel |

## Navigation

Two distinct, real navigation systems — both ARIA-tab-based, neither optional decoration.

**Top-level workspace nav** (`frontend/src/navigation/WorkspaceNavigation.tsx`): the pill row across the very top of every page (Today, Attention, Work, … Team). A single `WORKSPACES` array drives it, with full roving-tabindex keyboard support (`Home`/`End`/`ArrowLeft`/`ArrowRight`, see `nextWorkspaceIndex`/`moveWorkspaceFocus` in that file). **New entries go at the end of the array, never inserted earlier** — the array's position feeds fixed `ArrowRight`-count assertions in `frontend/e2e/scenarios/`, and several existing entries have inline comments explaining label choices made specifically to avoid substring-colliding with an existing `getByRole('tab', {name})` query (e.g. `'Team'` instead of `'Workspace'`, since `'Work'` is already taken). Adding a workspace means appending to `WORKSPACES` and checking for that same substring-collision risk against every existing label, not just picking a name that reads well on its own.

**In-workspace tabs** (`.tab-list`, `role="tablist"`): sub-navigation within a single workspace, used identically in `PersonalWorkspace.tsx`, `CollaborationWorkspace.tsx`, `AutomationWorkspace.tsx`, and `EngineeringWorkspace.tsx`. The contract: a `role="tablist"` with an `aria-label` naming the group, each tab is `role="tab"` with `aria-selected` driving both the visual state (Interaction states above) and the accessible state together, and exactly one `role="tabpanel"` below it with `aria-labelledby` pointing at the active tab's id. Copy this pattern for a new workspace's sub-navigation rather than reaching for `.tab-list` styling without the ARIA structure behind it — the CSS alone isn't the pattern.

There is no breadcrumb or pagination component (see Not yet part of the system below) — neither navigation system needs one today.

## Geometry: radius & elevation

Two systematic radii, tightened as part of Visual Foundation v2 (`docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`) toward a crisper, less-rounded feel: `--radius-control: 8px` (was `14px` — every text input/textarea/select, `.status-panel`/`.inline-status`, and the dashed-border `.ai-explanation` panel) and `--radius-panel: 16px` (was `28px` — `.brief-panel`, `.recommendation-panel`/`.explore-panel`/`.work-panel`). Buttons and pill-shaped chips (`.item-meta span`, `.recommendation-meta span`, wizard step circles) use `999px` for a true pill, which isn't part of this two-tier scale because it isn't a size choice — it's "fully round" regardless of the element's dimensions. A handful of components — `.dashboard-card` (20px), `.recommendation-list > li.is-pinned` and `.simulation-panel` (18px), the mobile-collapsed panel radius (16px), `.evidence-preview li`/`.risk-factors li` (also 999px) — use one-off values that predate this scale; they're left as raw numbers rather than forced into a token that would misname them. Elevation is the two shadow tokens defined in Color above — a panel earns one of these only when it's a distinct surface over the page background, never stacked on top of another already-elevated surface. Shadow values were tightened alongside the radii (smaller blur/spread, marginally higher opacity) to read crisply at the new, less-rounded geometry instead of the previous soft/diffuse feel.

## Buttons: action hierarchy

Every button in the app is the same shape by default (see Interaction states below) — hierarchy comes from one additional class, not a family of variants. **Destructive** actions (irreversible or high-risk: permanent deletion, revoking access, disabling a connector, activating a kill switch) get `className="btn-destructive"`, which swaps the border and text color to the muted danger tokens above — never a loud red, so it stays in the near-monochrome system while still reading as distinct from an ordinary or primary button. Look at `ExportDeletePanel.tsx`'s delete button or `KillSwitchPanel.tsx`'s activate buttons for the pattern; **deactivating** a kill switch or **enabling** a domain is the return-to-normal action, not the destructive one, and stays unstyled. **Primary** already has an established convention — `.recommendation-actions .primary-action` (solid ink fill) — used for the one dominant action in that list's action column; it isn't a general-purpose class, and a second one wasn't invented since no other surface has needed it yet. Everything else is the default (secondary) button. There's no separate tertiary/quiet or link-action variant in the app today — routine low-emphasis actions currently use the same default button as everything else; add a quiet variant only once a real surface needs the distinction, not speculatively.

## Forms: `.field-form`

Every labeled-fields block in the app should use one class. Single column, `gap: 14px`, label text stacked above its input.

**One real exception exists today:** `SearchAuditPanel.tsx`'s `.search-form`/`.audit-toolbar` (styles.css, near the `.field-form` rules) duplicate `.field-form`'s label and input styling by hand instead of using the class — same border, radius, padding, and label weight, declared a second time under different selectors. This predates being caught; it isn't a second sanctioned form pattern. Use `.field-form` for anything new, including a search/filter toolbar — don't treat `.search-form` as a second precedent the way `.field-form` is documented here.

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

5+ fields on one screen, or real structural complexity — a dynamic array of multi-field sub-items, genuine conditional branching between field groups. A 2-4 field form stays a single page; don't wizardize for its own sake. Edit forms stay flat even when their create-form sibling is a wizard — that's the established call across Risk, Workflow, Policy, Connector, and Schedule (the five actual wizards; see Reference implementations below).

**Task and Commitment are the field-count rule's real exceptions, not confirmations of it.** `TaskWorkspace.tsx`'s create form has 5 fields and `CommitmentWorkspace.tsx`'s has 7 — both clear the "5+ fields" threshold above — yet both stay flat single-page `.field-form`s, create and edit alike, with no wizard anywhere in either file. Nobody has revisited whether they should wizardize; they predate the wizard pattern and haven't been touched since. Don't copy this as precedent for a new 5+ field form — it's undocumented debt, not a second valid path.

**`WaitingView.tsx` and `DelegationsPanel.tsx`'s propose-delegation form were reviewed against this same threshold and deliberately left flat — reviewed debt, not the undocumented kind above.** Both have exactly 5 fields (`WaitingView`: subject type, subject ID, counterparty ID, direction, note; `DelegationsPanel`: recipient, obligation type, obligation resource ID, expected outcome, due date) and both clear the threshold on a literal count. But every field in both is short — a dropdown or a short id/date, no long-form text, no per-field help copy — the same shape Task's 5 and Commitment's 7 already are. The "5+ fields" number is a proxy for "too much to hold in one view," not a literal trigger; a short, flat, quickly-scannable form doesn't become harder to fill just because its field count crosses an arbitrary line. Treat this as confirming the Task/Commitment shape as a real, load-bearing exception (short independent fields, no branching), not as license to wizardize on field count alone or to skip it on a hunch — the next 5+ field form should be checked against this same "short and independent, or not" question, not assumed flat by default.

**`RecordsPanel.tsx`'s dynamic field array was also reviewed and left flat — but for a different reason: the wizardize rule's own trigger doesn't fit this shape.** DESIGN.md's structural-complexity trigger ("a dynamic array of multi-field sub-items") technically names this form's "Add field" key/value list. But every existing wizard in this app (Connector, Risk, Policy, Schedule) wizardizes a **sequential decision funnel** — each step gates on a genuinely different question, answered once, in order. `RecordsPanel`'s array isn't sequential steps; it's a **repeating field-group within one task** ("attach however many key/value pairs describe this record," answered all at once, in no particular order, with no natural stopping point between entries). Splitting that into wizard steps wouldn't reduce the field count on any one screen — the array still needs its own screen either way — it would just add a stepper, a review step, and Back/Continue navigation around a task that's fundamentally single-screen data entry. The dynamic-array trigger is scoped to sequential structural complexity, not any use of a growable list; a future array-shaped form should be checked against "are these genuinely separate steps, or one repeating group," not wizardized on the presence of an array alone.

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
| Hover | any `button` | `border-color` steps from `--color-border-default` to `--color-text-muted` (`button:hover:not(:disabled)`), animated over `--motion-fast` — no background/color change |
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
| `max-width: 800px` | Dashboard/brief cards go full-width (`grid-column: 1 / -1`); `.recommendation-list` items and `.work-grid` collapse to a single column; `.recommendation-actions` switches from its default column layout to row (wraps), `.audit-list` items switch from row to column, to fit the narrower measure |
| `max-width: 520px` | Panel heading rows (`.topbar`, `.brief-heading`, etc.) stack vertically instead of side-by-side; panel corner radius shrinks; `.recommendation-actions` flips back to column (a real double-flip: column at rest → row at 800px → column again at 520px, not a typo); `.search-form > div` and `.tab-list` go full-width and stack |

No component-level responsive behavior beyond this — a wizard, a `.field-form`, and a stepper all render identically from mobile through desktop widths; only the surrounding page chrome (headings, card grids, action rows) reflows. If a new surface needs its own breakpoint behavior, it's a deviation from the current pattern, not an extension of it — call that out explicitly rather than adding a third silent breakpoint.

## Motion

Three durations and one easing curve: `--motion-fast: 100ms; --motion-standard: 180ms; --motion-slow: 280ms; --ease-standard: cubic-bezier(.2, .8, .2, 1);` (`--motion-fast` tightened from `120ms` as part of Visual Foundation v2, `docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`, for a snappier feel consistent with the reference products' own micro-interactions). Usage today is deliberately small — the button hover border-color transition (Interaction states above) and the pre-existing `.ai-explanation-progress-fill` width transition are the only two. A global `prefers-reduced-motion: reduce` query collapses every animation/transition duration to near-zero; this was previously absent and is a real gap fix, not a restatement of existing behavior. Reach for a token when a new state change needs a transition; this app has no ambient/decorative animation and shouldn't grow any.

## Not yet part of the system

Named explicitly so a new feature doesn't invent one of these ad hoc: no table component (no surface in the app renders tabular data — every list is `<ul>`/`<ol>` with `.item-list`/`.work-list`/`.audit-list`-style rows; build one only when a real dataset needs row/column comparison, and base it on the left-align-text/right-align-numeric/quiet-header conventions rather than reinventing them from scratch), no icon library (zero icons anywhere in the app today — text labels do this work; picking a library is a real decision to make once a surface actually needs one, not before), no breadcrumbs or pagination (nothing in the app is deep enough or long-enough-listed to need either yet), no generic **info** semantic color (see Color above), no tertiary/quiet or link-action button variant (see Buttons above). This list will get shorter as real surfaces need these things — it should not get shorter by adding speculative CSS for a pattern nothing uses yet.

## Building a new page

This is process, not CSS — read Layout primitives, Buttons, Forms, and Wizards above first; this section is about arranging those primitives into a whole page, not adding new ones.

### Page anatomy

Every existing page in this app follows the same shape, whether or not it was designed that way on purpose — naming it makes it a decision instead of an accident:

1. **Page frame** — `.app-shell`. Fixed, boring, never touched per-page.
2. **Heading zone** — `.work-heading`/`.brief-heading`/`.recommendation-heading`/etc.: eyebrow, title, one-line description, optional primary action. Says what this page is for before anything else does.
3. **Primary work zone** — the page's actual reason to exist: the wizard, the list, the review queue, the panel a user came here to act on.
4. **Secondary zones** — supporting panels: related lists, detail views, configuration. Present, but visually quieter than zone 3.
5. **Terminal zone** — destructive actions, low-priority metadata, audit trails. Last on the page, visually separated from the happy path above it (see Buttons: action hierarchy).

Not every page needs all five — a single-panel page (most of this app) collapses 3–5 into one `.work-panel`. The ordering is what matters: nothing destructive or secondary should out-rank the page's actual job.

### Hierarchy rules

- **One dominant anchor per page.** A visitor should be able to say what this page is *for* within a couple seconds — a title, a wizard, a table, one clearly primary panel. If a new page has two `.work-panel`s of equal size fighting for attention above the fold, that's the tell something needs to be demoted to a secondary zone or cut from the first viewport.
- **Primary, secondary, destructive stay visually distinguishable**, per Buttons: action hierarchy above — a row shouldn't present two equally loud actions.
- **Whitespace and the type ramp do the grouping work before a new border does.** This app is already sparing with borders (`.work-panel` uses one 1px hairline border plus a soft shadow, nothing heavier) — reach for a spacing-scale gap or a heading-weight change before wrapping a new box in its own card.
- **Don't nest bordered panels inside bordered panels.** Nothing in the app currently does this; a new feature that needs a sub-grouping inside a `.work-panel` should reach for a heading + spacing, not another card.
- **Left-align by default.** Every page in the app composes left-aligned; centered content is reserved for genuinely centered things (the empty-state message pattern), not page-level layout.

### New-page recipe

1. Say the page's one primary task in a sentence. If you can't, the page doesn't have a clear anchor yet.
2. Pick the anatomy zones it actually needs (most pages: heading zone + one primary work zone; some also need a secondary zone).
3. Pick from existing primitives first — `.work-panel`/`.work-heading`/`.field-form`/a wizard — before writing new CSS. Something here almost always fits.
4. Decide the primary action (if any) and whether any action on the page is destructive; style accordingly (Buttons above).
5. Write the loading/empty/error states — every data-backed panel in this app has all three (`.empty-state`, `role="status"`, `.error-panel`/`role="alert"`); a new one shouldn't ship without them.
6. Check it at the two breakpoints (Responsive behavior above) and confirmed against Interaction states — hover, focus-visible, disabled, loading are not optional per-component styling, they're the existing rule.

### Aesthetic quality gate

Before calling a new or materially changed page done, these should all be true:

- The primary task is obvious without reading every panel on the page.
- Primary/secondary/destructive actions are visually distinguishable at a glance.
- No two elements are almost-but-not-quite aligned to the same rail — a small accidental offset is more noticeable than a missing decorative flourish.
- No unnecessary nested cards or nesting boxes-in-boxes.
- Loading, empty, error, and (where relevant) disabled/permission states are all designed, not just the happy path.
- It survives both breakpoints from Responsive behavior without losing task order.
- Focus is visible everywhere, and nothing depends on color alone to convey state (this app already leans on text — "Deletion pending…", "Enabled 04/08/2026" — never a color chip by itself).

### Content design

- Headings are concrete and task-oriented ("Authority & policy review", not "Overview"), matching what's already on every panel in this app.
- Descriptions are 1–2 short sentences under the heading, not a restatement of the heading itself.
- Error messages say what happened and, where there's a next step, what it is — this codebase's own convention (see `errorMessage()` helpers throughout `features/*`) already writes toward "you're offline, so X could not be read" rather than a raw backend message; a new panel's errors should read the same way.
- Labels use the words a user would use, not the backend's field names.

## Anti-patterns

Clean today, worth re-checking on any large new surface: no purple/gradient backgrounds, no 3-column icon-in-circle feature grids, no centered-everything, no decorative blobs or emoji-as-design, no generic hero copy. The palette and type scale above are the real system — don't default to unstyled `Inter` or a bootstrap-template look for anything new.

Also avoid, per Building a new page above: a card around every section; a card inside a card; every heading centered; two equally-weighted primary buttons in one region; a new arbitrary spacing, radius, or shadow value instead of a token from Spacing/Geometry above; a new icon family (there is no icon family — see Not yet part of the system); a dense wall of helper copy where a shorter sentence would do; animation with no state-change reason (see Motion above); a new accent color introduced for decoration rather than a semantic role (see Color above).

## Known follow-ups (deferred, not forgotten)

An external design-system review of this document raised a P1/P2 list beyond the P0s folded in above. All of it is now resolved, one item by a deliberately different route than the review proposed:

- **Design tokens** — done. Every color in `styles.css` is a `:root` custom property; see Color above.
- **`<ol>`/`<li>` stepper semantics** — done; see the stepper markup under Wizards above.
- **Centralized focus-management hook** — done (`useWizardStepFocus` in `wizardFocus.ts`), replacing the 6 per-wizard `useRef`+`useRef(true)`+`useEffect` blocks.
- **`.wizard-review dd` monospace scoping** — done via an explicit `is-machine-value` class; see the review-step note under Wizards above.
- **Interaction-states catalogue** and **responsive-behavior spec** — both documented above as new sections; no CSS changed, this was a documentation gap.
- **`.field`/`.field-label`/`.field-control` primitives** (to replace the `.field-form label:has(...)` DOM-shape dependency) — resolved differently than proposed. The root problem (a selector inferring layout from DOM shape) is fixed with a narrower change: an explicit `field-checkbox` class on the label that needs the row layout, rather than `:has(input[type="checkbox"])`. The fuller wrapper-div primitive was considered and rejected — it would mean touching every field in every `.field-form` consumer to add no new behavior, working against the flat `<label>text<input/></label>` shape this doc otherwise documents as load-bearing. Reopen only if a real requirement (not structural purity alone) needs the extra wrapper.

A full app-wide conformance audit against this document (all 47 feature files, cross-checked file by file rather than sampled) found and fixed ~40 real violations across every category above — unmarked destructive actions, card-in-card nesting, bare `<label>`s outside `.field-form`, `role="alert"` divs missing `.error-panel`, missing loading text, missing empty states, and `ConnectorHealthPanel.tsx`'s wizard (this doc's own cited "original" reference) missing its `<form>` wrapper entirely. Two categories were deliberately **not** fixed as part of that pass, because they're new-feature-scale product decisions, not conformance bugs, and don't belong in a mechanical fix-up:

- **Three more unwizardized 5+-field forms** — done. `WaitingView.tsx`, `DelegationsPanel.tsx`'s propose-delegation form, and `RecordsPanel.tsx`'s dynamic field array were each reviewed case-by-case against "When to wizardize" above (not left as unexamined debt the way Task/Commitment were) and deliberately kept flat, each for its own stated reason — see that section. `RecordsPanel.tsx` also got a small, unrelated fix found during the review: its field array had an "Add field" button but no way to remove a row once added; it now has one, resetting to a single empty row rather than zero when the last one is removed.
- **No single dominant anchor on the Today dashboard** — done. "Top priorities" is the primary anchor (the actual reason this page gets opened, per product judgment call — not derivable from the code alone), promoted out of `.dashboard-grid` into its own full-width `.work-panel`, positioned first. `MorningBrief` sits directly after it; the remaining 5 sections (Schedule, Overdue commitments, Open risks, Waiting on, Recent changes) stay in `.dashboard-grid` as the clearly secondary zone. No new CSS — `Section` (`dashboard/Sections.tsx`) gained one prop, `variant?: 'card' | 'panel'`, that swaps its outer wrapper between the two existing primitives; everything else about a section (heading, item list, empty state) is identical between variants.
- **A milder, related issue in `EngineeringOverview.tsx`** — done, differently than the dashboard's fix. Its 4 summary rows aren't competing panels the way the dashboard's cards were (it's already one `.work-panel`), and no single row deserves permanent promotion — which one matters changes day to day. Instead, each of the first three rows (Connectors, Open incidents, Proposed decisions — each a genuine action queue, something waiting on a person) picks up the app's existing `.degraded-panel` treatment on its own status line whenever that row's count is nonzero; Headline metrics stays neutral always, since it's a disclosure, not a queue with a zero state. No new CSS or component — same semantic this app already uses elsewhere for "needs attention, not broken." Dynamic and honest: on a calm day every row still looks identical, correctly.

## Provenance

Extracted 2026-08-31 from the live app during a design pass (#203) and the wizard sweep that followed (#204). P0 fixes (semantic `<form>`, terminal-validation error navigation) applied the same day after an external design-system review. P1/P2 follow-ups (design tokens, list-semantic stepper, centralized focus hook, monospace scoping, interaction-states/responsive documentation, and the checkbox-field selector fix) applied 2026-09-01. A second external review the same day proposed a broader foundational/aesthetic extension (spacing scale, container/grid, geometry, action hierarchy, motion, and a page-composition philosophy); the token- and primitive-level parts of it were implemented the same day — spacing scale, container/grid tokens applied to `.app-shell` and `.field-form`, radius tokens formalized for the two systematic values, a `.btn-destructive` class applied to the app's 13 genuinely irreversible/high-risk actions, motion tokens plus a previously-absent `prefers-reduced-motion` query, and semantic color roles documented under Color. Speculative additions for patterns nothing in the app uses yet (tables, icons, breadcrumbs/pagination, a generic info color) were deliberately not built — see Not yet part of the system. Page-anatomy/composition, content-density, and aesthetic-QA-gate guidance from that review was initially left out on the grounds that this doc's stated job is documenting the actual rendered system, and process guidance has no corresponding CSS to verify against. On revisiting the same day, that was too narrow a reading — a design system that only tells you what a class does, and never how to arrange those classes into a page, doesn't actually help someone build a page. Building a new page, Anti-patterns (expanded from the original AI-slop check), and the content-design/quality-gate guidance were added as explicitly-marked process sections, distinct from the CSS-backed sections above them, rather than silently blended in as if they were also "the actual rendered system."

A full correctness/completeness/usability/navigation review the same day, cross-checked line by line against `styles.css` and the actual `features/*` components rather than taken on faith, found and fixed four factual errors this doc had accumulated: the Geometry section claimed shadow tokens were "defined in Color above" when Color never mentioned them (fixed by adding an Elevation bullet to Color); the same section, and this file's own `:root` comment, misattributed the 18px radius to "wizard-stepper-adjacent panels" when no wizard element uses it — the real users are `.recommendation-list > li.is-pinned` and `.simulation-panel`; "When to wizardize" claimed Task and Commitment have wizard create-forms with flat edit siblings, but neither has a wizard at all despite both exceeding the stated 5-field threshold; and Forms claimed `.field-form` had "no exceptions, no bespoke form CSS anywhere else" when `SearchAuditPanel.tsx`'s `.search-form`/`.audit-toolbar` duplicate it by hand. The review also found a real completeness gap — no Navigation section existed despite two load-bearing, ARIA-tab-based nav systems in the app (`WorkspaceNavigation.tsx`'s top-level roving-tabindex bar and the in-workspace `.tab-list` pattern used across 4 workspaces) — and added one. A table of contents was added for the same reason: at 20+ sections, the doc itself had no navigation.

A follow-up conformance audit the same day checked every one of the app's 47 feature files against every section above (not a sample) and fixed what it found: ~40 real violations, spanning unmarked destructive actions, nested `.work-panel`s, bare `<label>`s outside `.field-form`, missing `.error-panel` classes, missing loading text, and missing empty states — plus, notably, the doc's own cited "original" reference wizard (`ConnectorHealthPanel.tsx`) was missing the `<form>` wrapper "A wizard IS a `<form>`" requires. All fixes verified by full typecheck and the complete 515-test suite passing, not asserted from the audit report alone. Two categories were deliberately left unfixed and logged instead under Known follow-ups above, because they're new-feature-scale product decisions (building three new wizards; choosing a dominant anchor for two pages) rather than mechanical conformance bugs.
