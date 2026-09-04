# Visual Foundation v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the token/primitive layer for the "Linear × Stripe × executive OS" redesign — new accent color, tighter geometry, a KPI numeric type role, a quiet/ghost button variant, and a documented priority-severity vocabulary — with zero component consumption changes.

**Architecture:** This is a CSS-token-and-documentation-only change. Every task edits `frontend/src/styles.css`'s `:root` block (or adds a small number of new standalone classes) and the corresponding section of `DESIGN.md` in the same task, so the doc never drifts from the CSS it describes — the pattern every prior change this session has followed. No `.tsx` file is touched; no component reads any of the new tokens/classes yet (that's sub-projects 2–6).

**Tech Stack:** React 18 + TypeScript + Vite frontend at `frontend/`; plain CSS custom properties in `frontend/src/styles.css` (no CSS-in-JS, no preprocessor); Vitest + Testing Library for the existing test suite; Playwright-driven `frontend/e2e/run.mjs` for the e2e suite.

**Spec:** `docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`

## Global Constraints

- No new saturated colors beyond the one accent (`--color-accent: #2C4A87`) — spec's "Color" section, carried over from the current system's own anti-patterns list.
- Existing CSS custom property **names** for the success/danger/degraded color families are NOT renamed — only documented under a new priority vocabulary (Critical/Needs attention/Normal/Healthy) in `DESIGN.md`. Renaming the actual `--color-*` identifiers would require updating every selector across `styles.css` that references them, which is real component-adjacent risk explicitly out of this spec's scope (spec: "no component consumption changes").
- `--radius-control` and `--radius-panel` are **existing** tokens whose *values* change (`14px→8px`, `28px→16px`) — this is a real, immediate, global visual change to every button/input/panel already in the app, not an inert addition. Every task that touches an existing token's value must be visually verified in the browser across at least one representative page, not just typechecked.
- Spacing scale is unchanged — no task touches `--space-*`.
- Every CSS task lands together with its `DESIGN.md` update in the same commit — the established pattern for every prior change this session.
- Full frontend test suite (`npx vitest run`) and `npx tsc --noEmit` must stay green after every task — these are pure regression checks here (nothing new to unit-test, since no component consumes the new tokens yet), not proof the new value is "correct" in isolation.

---

### Task 1: Add the accent color token, remove the "no accent" claim, document the priority language

**Files:**
- Modify: `frontend/src/styles.css:1-52` (the `:root` token block)
- Modify: `DESIGN.md` (Color section, lines 22–37)

**Interfaces:**
- Produces: `--color-accent` (`#2C4A87`), consumable by any later sub-project's components (navigation selected-state, primary links, focus ring) — none do yet.

- [ ] **Step 1: Add the token to `styles.css`**

In the `:root` block, immediately after the existing `--focus-ring` line (currently the last line before the Spacing scale comment), add:

```css
  --color-accent: #2C4A87;
```

- [ ] **Step 2: Update `DESIGN.md`'s Color section intro**

Replace:

```
Near-monochrome grayscale plus one dark-navy anchor. No secondary accent in general use — don't introduce one without a reason.
```

With:

```
Near-monochrome grayscale, one dark-navy anchor, and one deliberate accent (`--color-accent`, `#2C4A87`) — part of this app's Visual Foundation v2 redesign (see `docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`), chosen to feel like the restraint of Linear/Stripe's own dashboards without literally cloning either product's actual brand hue. As of this token landing, no component consumes it yet — that's deliberately deferred to the redesign's later sub-projects (navigation, dashboard). Still near-monochrome everywhere else: don't reach for a second accent without a reason.
```

- [ ] **Step 3: Add the accent to the token-groups bullet list**

After the existing `- **Elevation:** ...` bullet, add:

```
- **Accent:** `--color-accent` (`#2C4A87`) — not yet consumed by any component; reserved for the redesign's navigation/primary-action work
```

- [ ] **Step 4: Add the priority-language paragraph**

After the existing "Semantic state colors beyond error: ..." paragraph, add a new paragraph:

```
**Priority language.** The three families above are also documented as one deliberate 4-tier vocabulary for "how urgent is this" — **Critical** (the danger family), **Needs attention** (the degraded family), **Healthy** (the success family), and **Normal** (no color — default ink/border; "nothing to report" is not a hue). This is a naming/documentation layer only: the underlying `--color-*` custom property names are unchanged, so a new surface reaches for "Critical" or "Needs attention" as a named concept instead of reinventing the choice the way `EngineeringOverview.tsx`'s `.degraded-panel` usage already does informally, without any selector needing to change.
```

- [ ] **Step 5: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: no output (clean) — a CSS-only change cannot break TypeScript, this just confirms the working tree is otherwise sane.

- [ ] **Step 6: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: `56 passed`, `5xx passed` (all existing tests green) — adding an unused custom property changes nothing any test observes.

- [ ] **Step 7: Visually verify no regression**

Start the dev server (`preview_start` with the `frontend` launch config, or `node_modules/.bin/vite --port <port>` directly in a worktree), open the Today dashboard and one workspace page, confirm nothing changed visually (the new token is additive and unconsumed — this step exists to catch a typo in the `:root` block breaking the whole stylesheet, not to check the accent color itself, since nothing renders it yet).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/styles.css DESIGN.md
git commit -m "feat(design): add --color-accent token and priority-language vocabulary

Part of Visual Foundation v2 (docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md).
Adds --color-accent (#2C4A87), removes the now-inaccurate 'no secondary
accent' claim from DESIGN.md, and documents the existing danger/degraded/
success color families under one Critical/Needs attention/Normal/Healthy
priority vocabulary -- a naming layer only, no --color-* identifiers
renamed. Not yet consumed by any component."
```

---

### Task 2: Tighten geometry (radius + shadow tokens)

**Files:**
- Modify: `frontend/src/styles.css:77-84` (the Radius comment block) and the two `--shadow-*` lines in the same `:root` block
- Modify: `DESIGN.md` (Geometry section, line 70)

**Interfaces:**
- Consumes: nothing new.
- Modifies the *values* of `--radius-control`, `--radius-panel`, `--shadow-card`, `--shadow-panel` — every existing consumer (every button-adjacent input, every `.work-panel`/`.brief-panel`/`.recommendation-panel`/`.explore-panel`, every `.status-panel`/`.inline-status`/`.ai-explanation`) re-renders with the new values immediately. No code changes at any call site — the whole point of these being tokens.

- [ ] **Step 1: Update the radius token values**

In `styles.css`'s `:root` block, change:

```css
  --radius-control: 14px;
  --radius-panel: 28px;
```

To:

```css
  --radius-control: 8px;
  --radius-panel: 16px;
```

Update the comment immediately above them (currently explains the two-tier system and lists one-off values like `.dashboard-card` at 20px) — leave the comment's content otherwise unchanged, it still accurately describes which components use the systematic vs. one-off values; only the two token values themselves change.

- [ ] **Step 2: Update the shadow token values**

Change:

```css
  --shadow-card: 0 14px 36px rgba(24, 33, 47, .05);
  --shadow-panel: 0 18px 48px rgba(24, 33, 47, .06);
```

To:

```css
  --shadow-card: 0 8px 20px rgba(24, 33, 47, .06);
  --shadow-panel: 0 10px 28px rgba(24, 33, 47, .07);
```

(Smaller blur/spread and marginally higher opacity — reads crisper at the new, tighter radii instead of the previous soft/diffuse feel, per the spec's "less-diffuse" direction. These are the concrete values that resolve the spec's own "exact new shadow values are an implementation-time tuning detail" — tuned here, not deferred further.)

- [ ] **Step 3: Update `DESIGN.md`'s Geometry section**

Replace the first sentence of the Geometry section:

```
Two systematic radii: `--radius-control: 14px` (every text input/textarea/select, `.status-panel`/`.inline-status`, and the dashed-border `.ai-explanation` panel) and `--radius-panel: 28px` (`.brief-panel`, `.recommendation-panel`/`.explore-panel`/`.work-panel`).
```

With:

```
Two systematic radii, tightened as part of Visual Foundation v2 (`docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`) toward a crisper, less-rounded feel: `--radius-control: 8px` (was `14px` — every text input/textarea/select, `.status-panel`/`.inline-status`, and the dashed-border `.ai-explanation` panel) and `--radius-panel: 16px` (was `28px` — `.brief-panel`, `.recommendation-panel`/`.explore-panel`/`.work-panel`).
```

At the end of the same paragraph, after the existing "Elevation is the two shadow tokens defined in Color above..." sentence, add:

```
Shadow values were tightened alongside the radii (smaller blur/spread, marginally higher opacity) to read crisply at the new, less-rounded geometry instead of the previous soft/diffuse feel.
```

- [ ] **Step 4: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 5: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: all tests green — no test in this codebase asserts on radius or shadow pixel values (confirmed by grep: no test references `border-radius`, `boxShadow`, or a radius/shadow custom property), so this is a pure "nothing else broke" check.

- [ ] **Step 6: Visually verify across representative surfaces**

This is the one task in this plan with a real, app-wide visual footprint. Start the dev server, and check:
- The Today dashboard (`.work-panel`-tier "Top priorities" panel, `.dashboard-card`s in the grid, `MorningBrief`'s dark panel) — panels should look visibly less rounded, shadows crisper.
- A form (e.g. Tasks' create form) — inputs should look visibly less rounded.
- A wizard (e.g. Engineering → Connector health) — the wizard's `.work-panel` wrapper and its inputs should match the same tightened geometry.

Confirm nothing looks broken (no overflow, no clipped content at the new smaller radius) and that the change reads as "tighter/crisper," not "flat/broken."

- [ ] **Step 7: Commit**

```bash
git add frontend/src/styles.css DESIGN.md
git commit -m "feat(design): tighten radius and shadow tokens (Visual Foundation v2)

--radius-control 14px->8px, --radius-panel 28px->16px, shadow blur/
spread reduced and opacity marginally increased to read crisp at the
new geometry instead of soft/diffuse. Global, immediate visual change
to every existing consumer (every input, .work-panel-family surface) --
no code changes at any call site, that's the point of these being
tokens. Visually verified across the dashboard, a form, and a wizard."
```

---

### Task 3: Tighten the fast motion token

**Files:**
- Modify: `frontend/src/styles.css` (the `--motion-fast` line in `:root`)
- Modify: `DESIGN.md` (Motion section, line 198)

**Interfaces:**
- Modifies the value of `--motion-fast`, consumed today by exactly one existing rule: the button `border-color` hover transition (`styles.css`'s bare `button` rule).

- [ ] **Step 1: Update the token value**

Change:

```css
  --motion-fast: 120ms;
```

To:

```css
  --motion-fast: 100ms;
```

- [ ] **Step 2: Update `DESIGN.md`'s Motion section**

Replace:

```
Three durations and one easing curve: `--motion-fast: 120ms; --motion-standard: 180ms; --motion-slow: 280ms; --ease-standard: cubic-bezier(.2, .8, .2, 1);`.
```

With:

```
Three durations and one easing curve: `--motion-fast: 100ms; --motion-standard: 180ms; --motion-slow: 280ms; --ease-standard: cubic-bezier(.2, .8, .2, 1);` (`--motion-fast` tightened from `120ms` as part of Visual Foundation v2, `docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`, for a snappier feel consistent with the reference products' own micro-interactions).
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: all tests green — no test asserts on transition timing.

- [ ] **Step 5: Visually verify**

Hover a button in the browser; the border-color transition should still be smooth, just marginally snappier. A 20ms difference is not reliably perceptible on its own — this check is really "confirm the hover transition still exists and isn't broken," not "confirm 100ms specifically feels different from 120ms."

- [ ] **Step 6: Commit**

```bash
git add frontend/src/styles.css DESIGN.md
git commit -m "feat(design): tighten --motion-fast 120ms->100ms (Visual Foundation v2)

Snappier micro-interaction timing consistent with the Linear/Stripe
reference direction. Only existing consumer is the button hover
border-color transition; --motion-standard/--motion-slow/--ease-standard
unchanged."
```

---

### Task 4: Add the KPI numeric type role

**Files:**
- Modify: `frontend/src/styles.css` (new class, placed near the Typography-adjacent rules — after the `.subtitle` rule, before the `button` rule, matching where other small standalone utility classes like `.nested-section` already live)
- Modify: `DESIGN.md` (Typography section, the table at lines 15–20)

**Interfaces:**
- Produces: `.kpi-value` class, consumable by any later sub-project's KPI/metric component (sub-project 4) — none exists yet.

- [ ] **Step 1: Add the class to `styles.css`**

Immediately after the `.subtitle { margin: 14px 0 0; color: var(--color-text-muted); }` line, add:

```css
.kpi-value { font-variant-numeric: tabular-nums; font-weight: 650; letter-spacing: -.02em; }
```

- [ ] **Step 2: Add a row to `DESIGN.md`'s Typography table**

After the existing "Wizard review value" row, add a new table row:

```
| KPI/metric value (`.kpi-value`) | inherits surrounding font-size | `font-variant-numeric: tabular-nums; font-weight: 650; letter-spacing: -.02em` — Visual Foundation v2's numeric role for large standalone metric numbers (Inter's own tabular figures, not a second typeface); not yet consumed by any component, reserved for the redesign's KPI/metric sub-project |
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: all tests green — a new, unused CSS class cannot affect any existing render.

- [ ] **Step 5: Visually verify no regression**

Confirm the dev server still starts and a couple of pages render with no console errors (the class is unused, so there is nothing new to look at — this step exists only to catch a CSS syntax error).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/styles.css DESIGN.md
git commit -m "feat(design): add .kpi-value numeric type role (Visual Foundation v2)

Tabular-nums, weight 650, tight tracking -- Inter's own tabular figures
for large standalone metric numbers, not a second typeface. Not yet
consumed by any component; reserved for the redesign's KPI/metric
sub-project."
```

---

### Task 5: Add the quiet/ghost button variant

**Files:**
- Modify: `frontend/src/styles.css` (new class, placed immediately after the existing `.btn-destructive` rules, matching where the one other button-variant class lives)
- Modify: `DESIGN.md` (Buttons: action hierarchy section, line 74)

**Interfaces:**
- Produces: `.btn-quiet` class, consumable by any later sub-project's low-emphasis-action work — none consumes it yet.

- [ ] **Step 1: Add the class to `styles.css`**

Immediately after the existing:

```css
.btn-destructive { border-color: var(--color-border-danger); color: var(--color-text-danger-strong); }
.btn-destructive:hover:not(:disabled) { border-color: var(--color-text-danger-strong); }
```

Add:

```css
.btn-quiet { border-color: transparent; background: transparent; }
.btn-quiet:hover:not(:disabled) { background: var(--color-surface-recessed); }
```

- [ ] **Step 2: Update `DESIGN.md`'s Buttons section**

Replace the last sentence of the Buttons: action hierarchy paragraph:

```
There's no separate tertiary/quiet or link-action variant in the app today — routine low-emphasis actions currently use the same default button as everything else; add a quiet variant only once a real surface needs the distinction, not speculatively.
```

With:

```
**Quiet** is a fourth tier, added as part of Visual Foundation v2 (`docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`): `className="btn-quiet"` drops the border and fill entirely (a faint recessed-surface tint on hover, no border-color change), for an action low-emphasis enough that even the default bordered-pill button reads as too heavy. Not yet consumed by any component — the redesign's later sub-projects (dashboard, navigation) are expected to be its first real usage. There's still no separate link-action variant; add one only once a real surface needs that distinct treatment.
```

- [ ] **Step 3: Typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Run the full test suite**

Run: `cd frontend && npx vitest run`
Expected: all tests green — a new, unused CSS class cannot affect any existing render.

- [ ] **Step 5: Visually verify the new class renders as intended**

Since nothing in the app applies `.btn-quiet` yet, spot-check it directly: open the browser devtools console on any running page and run `document.body.insertAdjacentHTML('beforeend', '<button class="btn-quiet" style="position:fixed;top:10px;left:10px;z-index:9999">Quiet</button>')`, confirm it renders with no visible border/fill at rest and a faint recessed tint on hover, then reload to discard the injected element (this is a throwaway manual check, not a persisted change).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/styles.css DESIGN.md
git commit -m "feat(design): add .btn-quiet action variant (Visual Foundation v2)

Fourth action-hierarchy tier: no border, no fill, faint recessed-surface
tint on hover. Not yet consumed by any component; reserved for the
redesign's dashboard/navigation sub-projects. No separate link-action
variant yet -- unchanged from prior guidance."
```

---

### Task 6: Provenance note tying the landed tokens to the redesign initiative

**Files:**
- Modify: `DESIGN.md` (Provenance section, end of file)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Add a Provenance paragraph**

At the end of the Provenance section (after the existing final paragraph about the correctness/completeness/usability/navigation review), add:

```

Visual Foundation v2 (`docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`) landed the same day: sub-project 1 of a larger, explicitly-requested redesign toward a "Linear × Stripe Dashboard × executive operating system" direction, decomposed into 6 ordered sub-projects during brainstorming rather than built as one change. This sub-project added the token/primitive layer only — `--color-accent`, tightened `--radius-control`/`--radius-panel`/shadow values, a tightened `--motion-fast`, the `.kpi-value` numeric type role, the `.btn-quiet` action variant, and a documented Critical/Needs attention/Normal/Healthy priority vocabulary layered onto the existing danger/degraded/success color families (no `--color-*` identifiers renamed). Deliberately, nothing in the app consumes any of these new tokens/classes yet — every page renders identically to before this sub-project, aside from the two genuinely global value changes (radius/shadow geometry, motion timing), which were visually verified across the dashboard, a form, and a wizard. Component consumption (navigation, Today's information architecture, a KPI/card component system, an inspector/drawer pattern, a responsive pass) is later sub-projects, each getting its own spec and plan when reached.
```

- [ ] **Step 2: Commit**

```bash
git add DESIGN.md
git commit -m "docs(design): note Visual Foundation v2's landing in Provenance

Ties the 5 preceding token/class commits to the redesign initiative and
spec, and records explicitly that no component consumes the new tokens
yet -- that's later sub-projects."
```

---

## Post-plan verification (do once, after Task 6)

- [ ] Run `cd frontend && npx tsc --noEmit` one final time on the full accumulated diff.
- [ ] Run `cd frontend && npx vitest run` one final time — expect the same pass count as before Task 1 (no new tests were added, since there's nothing new to unit-test until a component consumes these tokens).
- [ ] Run the full e2e suite (`VITE_API_BASE_URL=http://127.0.0.1:4173 npm run build && node e2e/run.mjs` from `frontend/`) — the radius/shadow/motion value changes are global, so this is worth a final check even though no e2e scenario asserts on visual styling; confirms no scenario's timing-sensitive `waitFor` broke from the motion timing change.
- [ ] Push the branch and open a PR, following this session's established pattern (isolated worktree, fresh `npm install`, PR description summarizing the token changes and explicitly noting zero component consumption).
