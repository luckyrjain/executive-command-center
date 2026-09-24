# Today Page Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Today's two verified problems — Morning Brief visually outranking the page's promoted dominant anchor, and its stat counts being able to actively disagree with the live dashboard's own counts — by demoting Morning Brief to a borderless status row and removing its stats.

**Architecture:** A pure presentation change to `MorningBrief.tsx` and `styles.css` (no data-shape, query, or mutation change), plus the test and e2e fixes that change requires, plus a documentation update. Three tasks: the component/CSS/unit-test restructure, the e2e fixes plus full verification, and DESIGN.md.

**Tech Stack:** React 19, existing `@tanstack/react-query` calls (unchanged), Vitest + Testing Library, Playwright (existing e2e harness), plain CSS custom properties.

**Spec:** `docs/superpowers/specs/2026-09-24-today-rebuild-design.md`

## Global Constraints

- **Files this plan may touch:** `frontend/src/dashboard/MorningBrief.tsx`, `frontend/src/dashboard/MorningBrief.test.tsx`, `frontend/src/styles.css`, `frontend/e2e/scenarios/dashboard-brief.mjs`, `frontend/e2e/scenarios/layout-integrity.mjs`, `DESIGN.md`. No other file — `Sections.tsx`, `TodayPage.tsx`, `.dashboard-grid`/`.dashboard-card`, and the backend are all untouched.
- **No data-shape or query change.** Same `useQuery`/`useMutation` calls, same conditionals, same `MorningBriefResponse` type.
- **`.brief-status` is borderless** — no border, no background fill, no radius. This is load-bearing: giving it a border/fill identical to `.inline-status` would nest an identical bordered box inside itself, which is exactly what the spec's design section rejected and DESIGN.md's Hierarchy rules forbid. Do not "improve" this by adding a border back.
- **Reuse the existing shared heading-flex selector** (`.topbar, .brief-heading, .recommendation-heading, .explore-heading, .work-heading` at `styles.css:150-155`) by renaming `.brief-heading` to `.brief-status-heading` inside it — do not write a new standalone flex rule that duplicates it.
- **Every mobile breakpoint list that mentions `.brief-heading`/`.brief-heading button`/`.brief-panel` must be updated**, not just the base rules — three separate `@media (max-width: 520px)` selector lists are involved (flex-column, button-padding, border-radius), and each needs its own edit (see Task 1).
- **Three CSS custom properties become orphaned by this change** (`--color-border-on-dark`, `--color-text-on-dark-muted`, `--color-text-on-dark-faint`) and must be deleted from `:root`, not left dead.
- **Commit trailer:** every commit message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- **Work only in the worktree** `/Users/luckyjain/Projects/ecc-worktree-today-rebuild` (branch `today-rebuild`), commands from its `frontend/` directory unless a step says otherwise. Confirm `git branch --show-current` prints `today-rebuild` before every commit. Stage explicit paths only (never `git add -A` or `.`). Do not push and do not open a PR. Another Claude session may be using `/Users/luckyjain/Projects/executive-command-center` on a different branch: never work there.
- **Snapshot root** (session scratchpad, outside the repo): `/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/today-rebuild-snaps`, referred to below as `$SNAPS`.
- The shell is zsh: never `echo` a bare string of `=` characters.

## File Structure

- Modify `frontend/src/dashboard/MorningBrief.tsx`: remove `.brief-stats`, rename classes (Task 1).
- Modify `frontend/src/dashboard/MorningBrief.test.tsx`: replace the stats test, keep the rest (Task 1).
- Modify `frontend/src/styles.css`: delete old rules, add new ones, fix three mobile selector lists, delete three orphaned tokens (Task 1).
- Modify `frontend/e2e/scenarios/dashboard-brief.mjs`: remove the now-dead stats assertions, add a negative check (Task 2).
- Modify `frontend/e2e/scenarios/layout-integrity.mjs`: add a standalone 320px check for `/today` (Task 2).
- Modify `DESIGN.md` (Task 3).

---

### Task 1: Restructure `MorningBrief.tsx` and its CSS

**Files:**
- Modify: `frontend/src/dashboard/MorningBrief.tsx`
- Modify: `frontend/src/dashboard/MorningBrief.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: nothing new.
- Produces (used by Task 2): the component renders `<section className="brief-status" aria-labelledby="morning-brief-title">` with a `<div className="brief-status-heading">` inside it (no `.brief-stats` anywhere), so Task 2's e2e edits can rely on `.brief-stats` genuinely no longer existing.

- [ ] **Step 1: Write the failing test**

In `frontend/src/dashboard/MorningBrief.test.tsx`, replace the entire `'renders a compact count per brief category instead of duplicating full item lists'` test (and remove the `statValue` helper function above `describe`, which only that test used) with:

```tsx
  it('does not render a stats strip -- the live dashboard already shows these counts, and can show a different one', async () => {
    const populated = {
      ...baseBrief,
      sections: {
        today_schedule: [
          { id: 'evt-1', title: 'Board sync', starts_at: '2026-07-16T14:00:00Z' },
          { id: 'evt-2', title: 'Standup', starts_at: '2026-07-16T15:00:00Z' },
        ],
        top_priorities: [{ entity_id: 't1', title: 'Ship report', score: 90, status: 'in_progress' }],
        overdue_commitments: [],
        risks: [],
      },
    }
    vi.stubGlobal('fetch', vi.fn(() => response(populated)))
    renderBrief()

    await screen.findByText(/Generation 1/)
    expect(screen.queryByText('Schedule')).toBeNull()
    expect(screen.queryByText('Priorities')).toBeNull()
    expect(screen.queryByText('Overdue')).toBeNull()
    expect(screen.queryByText('Risks')).toBeNull()
    expect(screen.queryByText('Board sync')).toBeNull()
  })
```

- [ ] **Step 2: Run the test suite to confirm the new test fails for the expected reason**

Run: `pnpm vitest run src/dashboard/MorningBrief.test.tsx`
Expected: FAIL. The new test fails because `screen.queryByText('Schedule')` currently finds the `.brief-stats` `<dt>Schedule</dt>` element (not `null`). The other 5 tests in the file still pass. Record this output as RED evidence.

- [ ] **Step 3: Restructure `MorningBrief.tsx`**

Replace the component's `return` statement:

```tsx
  return (
    <section className="brief-panel" aria-labelledby="morning-brief-title">
      <div className="brief-heading">
        <div>
          <p className="eyebrow">PERSISTED DAILY BRIEF</p>
          <h2 id="morning-brief-title">Morning Brief</h2>
          <p>{brief.data ? `Generation ${brief.data.generation_version} · ${brief.data.ai_status.replaceAll('_', ' ')}` : 'A deterministic briefing of today’s attention.'}</p>
        </div>
        <button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending || brief.isLoading}>
          {refresh.isPending ? 'Refreshing…' : 'Refresh brief'}
        </button>
      </div>

      {brief.isLoading ? <div className="inline-status" role="status">Preparing your morning brief…</div> : null}
      {brief.isError ? <div className="inline-status error-panel" role="alert">{brief.error.message}</div> : null}
      {refresh.isError ? <div className="inline-status error-panel" role="alert">{refresh.error.message}</div> : null}
      {brief.data?.ai_status === 'disabled' ? (
        <div className="inline-status" role="status">AI-assisted sections are disabled; showing deterministic results only.</div>
      ) : null}
      {brief.data?.stale ? (
        <div className="inline-status degraded-panel" role="status">
          This brief is stale{brief.data.stale_reason ? `: ${brief.data.stale_reason.replaceAll('_', ' ')}` : ''}. Refresh to regenerate it.
        </div>
      ) : null}

      {brief.data ? (
        <dl className="brief-stats">
          <div>
            <dt>Schedule</dt>
            <dd>{brief.data.sections.today_schedule?.length ?? 0}</dd>
          </div>
          <div>
            <dt>Priorities</dt>
            <dd>{brief.data.sections.top_priorities?.length ?? 0}</dd>
          </div>
          <div>
            <dt>Overdue</dt>
            <dd>{brief.data.sections.overdue_commitments?.length ?? 0}</dd>
          </div>
          <div>
            <dt>Risks</dt>
            <dd>{brief.data.sections.risks?.length ?? 0}</dd>
          </div>
        </dl>
      ) : null}
    </section>
  )
```

with:

```tsx
  return (
    <section className="brief-status" aria-labelledby="morning-brief-title">
      <div className="brief-status-heading">
        <div>
          <p className="eyebrow">PERSISTED DAILY BRIEF</p>
          <h2 id="morning-brief-title">Morning Brief</h2>
          <p>{brief.data ? `Generation ${brief.data.generation_version} · ${brief.data.ai_status.replaceAll('_', ' ')}` : 'A deterministic briefing of today’s attention.'}</p>
        </div>
        <button type="button" onClick={() => refresh.mutate()} disabled={refresh.isPending || brief.isLoading}>
          {refresh.isPending ? 'Refreshing…' : 'Refresh brief'}
        </button>
      </div>

      {brief.isLoading ? <div className="inline-status" role="status">Preparing your morning brief…</div> : null}
      {brief.isError ? <div className="inline-status error-panel" role="alert">{brief.error.message}</div> : null}
      {refresh.isError ? <div className="inline-status error-panel" role="alert">{refresh.error.message}</div> : null}
      {brief.data?.ai_status === 'disabled' ? (
        <div className="inline-status" role="status">AI-assisted sections are disabled; showing deterministic results only.</div>
      ) : null}
      {brief.data?.stale ? (
        <div className="inline-status degraded-panel" role="status">
          This brief is stale{brief.data.stale_reason ? `: ${brief.data.stale_reason.replaceAll('_', ' ')}` : ''}. Refresh to regenerate it.
        </div>
      ) : null}
    </section>
  )
```

(The `<dl className="brief-stats">` block is deleted entirely; nothing else in the component — imports, the `MorningBriefResponse` type, the two `useQuery`/`useMutation` calls, `fetchMorningBrief`/`refreshMorningBrief` — changes.)

- [ ] **Step 4: Run the test suite to confirm GREEN**

Run: `pnpm vitest run src/dashboard/MorningBrief.test.tsx`
Expected: PASS, 6 tests (5 unchanged + the 1 replaced). Record this as GREEN evidence.

- [ ] **Step 5: Edit `styles.css` — delete the old rules**

Find and delete this entire block (currently at approximately lines 293-309):

```css
.brief-panel {
  margin-top: 36px;
  border-radius: var(--radius-panel);
  background: var(--color-ink);
  color: var(--color-text-on-ink);
  padding: clamp(24px, 4vw, 42px);
}
.brief-heading { margin-bottom: 22px; }
.brief-heading h2 { margin: 0; font-size: var(--text-display-sm); letter-spacing: -.035em; }
.brief-heading p:not(.eyebrow) { margin: 10px 0 0; color: var(--color-text-on-dark-muted); }
.brief-heading .eyebrow { color: var(--color-text-on-dark-faint); }
.brief-panel button { background: transparent; color: var(--color-text-on-ink); border-color: var(--color-border-on-dark); }
.brief-panel .inline-status { color: var(--color-ink); }

.brief-stats { display: flex; flex-wrap: wrap; gap: 28px; margin: 22px 0 0; }
.brief-stats dt { margin: 0; color: var(--color-text-on-dark-faint); font-size: var(--text-xs); font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
.brief-stats dd { margin: 4px 0 0; font-size: var(--text-2xl); font-weight: 650; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
```

- [ ] **Step 6: Edit `styles.css` — rename `.brief-heading` to `.brief-status-heading` in the shared base selector list**

Find:

```css
.topbar,
.brief-heading,
.recommendation-heading,
.explore-heading,
.work-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}
```

Replace with:

```css
.topbar,
.brief-status-heading,
.recommendation-heading,
.explore-heading,
.work-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
}
```

- [ ] **Step 7: Edit `styles.css` — add the two new rules, in the same place the old `.brief-panel` block was**

Insert, where the block deleted in Step 5 used to be:

```css
.brief-status { margin-top: 36px; margin-bottom: 20px; }
/* h2 size/tracking matches .section-heading h2 exactly (var(--text-lg),
 * -.01em) -- the same "quiet section heading" precedent already used
 * elsewhere (e.g. AttentionQueue's grouped headings), not a new size. */
.brief-status-heading h2 { margin: 0; font-size: var(--text-lg); letter-spacing: -.01em; }
.brief-status-heading p:not(.eyebrow) { margin: 6px 0 0; color: var(--color-text-secondary); }
```

- [ ] **Step 8: Edit `styles.css` — fix the three mobile `@media (max-width: 520px)` selector lists**

Find:

```css
  .topbar,
  .brief-heading,
  .recommendation-heading,
  .explore-heading,
  .work-heading { align-items: flex-start; flex-direction: column; }
  .topbar button,
  .brief-heading button,
  .recommendation-heading button { padding: 9px 14px; }
```

Replace with:

```css
  .topbar,
  .brief-status-heading,
  .recommendation-heading,
  .explore-heading,
  .work-heading { align-items: flex-start; flex-direction: column; }
  .topbar button,
  .brief-status-heading button,
  .recommendation-heading button { padding: 9px 14px; }
```

Find:

```css
  .brief-panel,
  .recommendation-panel,
  .explore-panel,
  .work-panel { border-radius: 12px; }
```

Replace with:

```css
  .recommendation-panel,
  .explore-panel,
  .work-panel { border-radius: 12px; }
```

(`.brief-status` gets no entry here — it has no `border-radius` rule at all now.)

- [ ] **Step 9: Edit `styles.css` — delete the three orphaned tokens**

In the `:root` block, find and delete these three lines (they are consecutive or near-consecutive; delete each exactly once):

```css
  --color-border-on-dark: #566173;
  --color-text-on-dark-muted: #b7c0ce;
  --color-text-on-dark-faint: #9eabba;
```

- [ ] **Step 10: Verify no stray reference remains**

Run from `frontend/`: `grep -n 'brief-panel\|brief-heading\|color-border-on-dark\|color-text-on-dark-muted\|color-text-on-dark-faint' src/styles.css src/dashboard/MorningBrief.tsx`
Expected: no output. If anything prints, you missed an edit above — go back and fix it before continuing.

- [ ] **Step 11: Full checks**

Run: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build`
Expected: all pass. `check:tokens` confirms no raw color/font-size was introduced and every remaining `var()` resolves (this is what would catch a missed token deletion leaving a dangling reference, or a typo in the new rules).

- [ ] **Step 12: Visual check**

Run from `frontend/`:

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/today-rebuild-snaps
VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build
pnpm visual:snapshots capture "$SNAPS/task1"
```

Open `$SNAPS/task1/desktop-today.png` with the Read tool. Expected: "Top priorities" is the first, largest, most visually prominent element on the page (white card, still the same as before). Directly below it, "Morning Brief" is a short text row with no border and no fill — the eyebrow, "Morning Brief" heading (noticeably smaller than before, matching the size of other section headings elsewhere in the app), the generation/status line, and the "Refresh brief" button, with the default (loaded, not-stale, AI-enabled in this fixture — check what the fixture actually sets) inline-status messages, if any, rendering below it as their own small bordered boxes. The dashboard grid follows with visible, reasonable spacing (not flush, not excessively far) below the status row. If the spacing looks wrong (too tight or too loose against the grid's own 18px internal gap), adjust `.brief-status`'s `margin-bottom` value from Step 7 and re-capture before committing — 20px is a starting point, not a fixed requirement.

- [ ] **Step 13: Commit**

```bash
git add frontend/src/dashboard/MorningBrief.tsx frontend/src/dashboard/MorningBrief.test.tsx frontend/src/styles.css
git commit -m "$(cat <<'EOF'
feat(dashboard): demote Morning Brief to a borderless status row

.brief-stats is removed: its counts could actively disagree with the live
dashboard's own badges (the backend inserts empty-placeholder rows a live
Section filters out via visibleItems(), but the brief counted the raw
array length), not just repeat them. The dark full-width .brief-panel is
replaced with borderless .brief-status, reusing the app's existing shared
heading-flex selector instead of duplicating it -- giving the wrapper its
own border/fill would have nested an .inline-status banner inside an
identical box, which DESIGN.md's Hierarchy rules forbid. Top priorities is
now the page's one actual dominant anchor.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Fix the e2e scenarios and run full verification

**Files:**
- Modify: `frontend/e2e/scenarios/dashboard-brief.mjs`
- Modify: `frontend/e2e/scenarios/layout-integrity.mjs`

**Interfaces:**
- Consumes: Task 1's `.brief-status`/`.brief-status-heading` markup and the removal of `.brief-stats`.

- [ ] **Step 1: Fix `dashboard-brief.mjs`**

Find:

```js
  // The brief's item lists duplicated the live dashboard grid one-for-one
  // (same categories, same items, styled as identical cards) with nothing
  // distinguishing "live" from "persisted snapshot" -- replaced with a
  // compact count strip; full item detail already lives in the dashboard
  // sections above.
  const briefStats = briefPanel.locator('.brief-stats')
  await briefStats.getByText('Schedule').waitFor()
  await briefStats.getByText('Priorities').waitFor()
  await briefStats.getByText('Overdue').waitFor()
  await briefStats.getByText('Risks').waitFor()
```

Replace with:

```js
  // The brief's item lists used to duplicate the live dashboard grid
  // one-for-one (same categories, same items, styled as identical cards)
  // with nothing distinguishing "live" from "persisted snapshot" -- fixed
  // first with a compact count strip, then that strip was removed entirely
  // once it turned out the counts could actively disagree with the live
  // dashboard's own badges (the backend's empty-placeholder rows are
  // filtered out of the live count but not the brief's). Assert there is
  // no stats block left to regress back to a stale one.
  assert.equal(await briefPanel.locator('dl').count(), 0, 'the brief should not render a stats dl')
```

- [ ] **Step 2: Run the fixed scenario in isolation**

Run: `VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build && node e2e/run.mjs` (this runs the full e2e suite; there is no single-scenario runner in this project — see Step 5 for the full run, this step is a first check before touching the second file)
Expected: `dashboard-brief` passes. If it fails, read the assertion output and fix the edit above before continuing (do not touch `layout-integrity.mjs` yet, so a failure is unambiguous about which file caused it).

- [ ] **Step 3: Add the standalone `/today` 320px check to `layout-integrity.mjs`**

Find:

```js
  await page.goto(`${baseURL}/automation`)
  for (const tab of ['Workflows', 'Policies']) {
    await page.getByRole('tab', { name: tab }).click()
    await page.locator('.wizard-stepper').first().waitFor()
    assert.equal(await horizontalOverflow(page), 0, `/automation (${tab}) must not scroll horizontally at 320px`)
  }
```

Replace with:

```js
  await page.goto(`${baseURL}/automation`)
  for (const tab of ['Workflows', 'Policies']) {
    await page.getByRole('tab', { name: tab }).click()
    await page.locator('.wizard-stepper').first().waitFor()
    assert.equal(await horizontalOverflow(page), 0, `/automation (${tab}) must not scroll horizontally at 320px`)
  }

  // /today has no .wizard-stepper, so it can't join the loop above -- a
  // standalone check instead. .brief-status-heading is the row most likely
  // to overflow at 320px (a heading plus a button, the same shape the
  // loop above exists to guard for the wizard-stepper case).
  await page.goto(`${baseURL}/today`)
  await page.locator('.brief-status-heading').waitFor()
  assert.equal(await horizontalOverflow(page), 0, '/today must not scroll horizontally at 320px')
```

- [ ] **Step 4: Run the full e2e suite**

Run: `node e2e/run.mjs` (the build from Step 2 is still current unless Task 1's files changed since — if in doubt, rebuild first: `VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build && node e2e/run.mjs`)
Expected: all scenarios pass, including `dashboard-brief` and `layout-integrity`, each with its own axe accessibility scan.

- [ ] **Step 5: Full checks**

Run from `frontend/`: `pnpm typecheck && pnpm vitest run && pnpm check:tokens && pnpm build`
Expected: all pass (no source files changed since Task 1's Step 11 run, but confirm nothing regressed).

- [ ] **Step 6: Commit**

```bash
git add frontend/e2e/scenarios/dashboard-brief.mjs frontend/e2e/scenarios/layout-integrity.mjs
git commit -m "$(cat <<'EOF'
test(e2e): fix dashboard-brief for the removed stats, cover /today at 320px

dashboard-brief.mjs asserted directly on .brief-stats, which no longer
exists; replaced with a negative check that no stats dl comes back.
layout-integrity.mjs's existing 320px loop waits for .wizard-stepper,
which /today doesn't have, so /today gets its own standalone check rather
than joining that loop.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Update DESIGN.md

**Files:**
- Modify: `DESIGN.md`

**Interfaces:**
- Consumes: Tasks 1-2 (documents what they built).

- [ ] **Step 1: Confirm every anchor before editing**

Run from the worktree root: `grep -n 'brief-panel\|brief-heading\|brief-stats' DESIGN.md`
Expected: hits at approximately lines 65, 82, 100, 104, 120, 248, 269, 333, 365 (nine lines total). If the count or line numbers differ substantially from this, the file has changed since this plan was written — read the actual current lines before editing rather than assuming the quotes below still match verbatim, and report any mismatch instead of improvising.

- [ ] **Step 2: Rewrite the `## Morning Brief` section**

Find the paragraph starting `MorningBrief.tsx renders .brief-stats` (the section's only paragraph, immediately after the `## Morning Brief` heading). Replace the entire paragraph with:

```markdown
`MorningBrief.tsx` renders `.brief-status`, a borderless status row — eyebrow, heading, generation/AI-status line, and the "Refresh brief" action — with no `.brief-stats` count strip and no dark `.brief-panel` hero treatment. It used to render both, in two stages. First, four full `Section`s reading the brief's own persisted `sections` payload, which carries the same shape (and, for most fields, the same underlying data) as the live `/api/v1/dashboard/today` response the `.dashboard-grid` below it already renders — so Schedule/Priorities/Overdue/Risks each appeared twice on the page, styled identically, with nothing to tell a reader "this one's live" from "this one's yesterday's persisted snapshot." That was fixed with `.brief-stats`, a compact count strip — but the strip's own counts turned out to be a second, subtler version of the same problem: the backend inserts `{"empty": true, ...}` placeholder rows for an empty category, and the live dashboard's `Section` component filters those out before counting (`visibleItems()` in `dashboard/Sections.tsx`), while `.brief-stats` counted the raw array length. On a day with nothing scheduled, the live Schedule badge could read 0 while the brief's own Schedule stat read 1 — the two numbers didn't just repeat each other, one of them was wrong. Second, `.brief-panel` gave Morning Brief a solid `--color-ink` fill and a `--text-display-sm` heading, visually louder than "Top priorities" — the page's own deliberately promoted dominant anchor — directly above it, the exact failure Hierarchy rules names ("if a new page has two `.work-panel`s of equal size fighting for attention above the fold, that's the tell something needs to be demoted"). `MorningBriefResponse` has no narrative/summary text field today, so a real differentiated brief (prose, not another list or count) is backend work, not something this fix invents. `.brief-status` is the honest no-backend-change version: a borderless row for the panel's one genuinely unique contribution (generation number, AI status, staleness, the Refresh action), with no border or fill of its own so that its `.inline-status`/`.error-panel`/`.degraded-panel` status messages — which already carry that exact box treatment — never render as a card nested inside a card.
```

- [ ] **Step 3: Layout primitives table**

Find:

```markdown
| `.brief-stats` | Compact `dt`/`dd` count strip inside `.brief-panel` — see Morning Brief below |
```

Replace with:

```markdown
| `.brief-status` | Borderless status row (eyebrow, heading, generation/status line, Refresh action) — see Morning Brief below |
```

- [ ] **Step 4: Composition's "Not affected" list**

Find (inside the Composition section):

```
Not affected by the setting: `.dashboard-card`, `.connector-card`, `.simulation-panel`, `.brief-panel`, list rows, and every token. Cards nested inside a canvas page keep their own boundaries.
```

Replace `.brief-panel` with `.brief-status` in that sentence (keep everything else identical):

```
Not affected by the setting: `.dashboard-card`, `.connector-card`, `.simulation-panel`, `.brief-status`, list rows, and every token. Cards nested inside a canvas page keep their own boundaries.
```

- [ ] **Step 5: Spacing section's top-margin example**

Find the phrase `the \`.app-shell\`/\`.brief-panel\`/\`.work-panel\` top margins` (inside the Spacing section's long paragraph). Replace `.brief-panel` with `.brief-status` in that phrase only — do not alter any other part of the sentence.

- [ ] **Step 6: Geometry's radius consumer list**

Find the phrase `\`--radius-panel: 16px\` (was \`28px\` — \`.brief-panel\`, \`.recommendation-panel\`/\`.explore-panel\`/\`.work-panel\`)` (inside the Geometry section). Replace with `` `--radius-panel: 16px` (was `28px` — `.recommendation-panel`/`.explore-panel`/`.work-panel`) `` — remove `.brief-panel` from the list entirely (`.brief-status` has no radius at all, so it is not a consumer of either radius token).

- [ ] **Step 7: Responsive behavior table**

Find the phrase `Panel heading rows (\`.topbar\`, \`.brief-heading\`, etc.)` (inside the Responsive behavior section's `max-width: 520px` table row). Replace `.brief-heading` with `.brief-status-heading` in that phrase only.

- [ ] **Step 8: Page anatomy's heading-zone bullet**

Find the phrase `\`.work-heading\`/\`.brief-heading\`/\`.recommendation-heading\`/etc.` (inside Page anatomy's numbered list, item 2 "Heading zone"). Replace `.brief-heading` with `.brief-status-heading` in that phrase only.

- [ ] **Step 9: Known follow-ups — correct the "done" claim**

Find the bullet beginning `**No single dominant anchor on the Today dashboard** — done.` (the full bullet is one paragraph). Append this sentence to the end of that same bullet, after its existing last sentence:

```
 Promoting Top priorities out of the grid turned out not to be sufficient on its own: Morning Brief's own dark, full-width `.brief-panel` treatment still outweighed it visually, the identical failure mode this rule describes, one level of abstraction later. Sub-project 5 of the Calm Executive Workspace direction (below) finished the job by demoting Morning Brief to a borderless status row.
```

- [ ] **Step 10: Add the Provenance entry**

Append this paragraph at the very end of the file:

```markdown
Today page rebuild (sub-project 5 of the "Calm Executive Workspace" direction) landed 2026-09-24. Investigated before designing, the same discipline sub-project 3 used: one part of a hypothetical full rebuild (the page's most obvious defect, Morning Brief duplicating live dashboard items) was already fixed on 2026-09-04, and two further, verified problems remained. First, that 2026-09-04 fix's own "no single dominant anchor" follow-up wasn't fully closed — promoting Top priorities into its own `.work-panel` didn't prevent Morning Brief's separate dark `.brief-panel` treatment from outweighing it visually, the identical two-competing-panels failure Hierarchy rules names, one level later (see Known follow-ups above). Second, `.brief-stats`'s counts weren't just redundant with the live dashboard's own badges — they could actively disagree with them, because the backend's empty-placeholder rows are filtered out of the live count (`visibleItems()`) but not the brief's raw length count. Fixed by demoting Morning Brief to `.brief-status`, a borderless row carrying only its genuinely unique content (generation, AI status, staleness, Refresh), reusing the app's existing shared heading-flex selector rather than duplicating it, and removing `.brief-stats` entirely rather than trying to make its counts trustworthy. Verified with before/after screenshots, the unit and e2e suites (including a new negative check that the stats block doesn't return, and a new standalone 320px overflow check for `/today`, since the existing 320px loop is keyed on an element `/today` doesn't have), `tsc`, the token check, and a production build. See `docs/superpowers/specs/2026-09-24-today-rebuild-design.md` and `docs/superpowers/plans/2026-09-24-today-rebuild.md`.
```

- [ ] **Step 11: Check and commit**

Run from the worktree root: `python3 scripts/check_docs.py; echo "docs exit: $?"` (must print `docs exit: 0`). The two `docs/superpowers/...` paths above are plain backtick text, not markdown links: keep them that way. Then from `frontend/`: `pnpm check:tokens` (must stay OK — no color/size literals were touched).

```bash
git add DESIGN.md
git commit -m "$(cat <<'EOF'
docs: document the Morning Brief status-row demotion

Rewrites the Morning Brief section around the real reason the stats had to
go (they could disagree with the live dashboard, not just repeat it), fixes
seven other stale .brief-panel/.brief-heading/.brief-stats references,
corrects the "no single dominant anchor -- done" follow-up to record that
it wasn't fully closed until this sub-project, and adds the provenance
entry.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
