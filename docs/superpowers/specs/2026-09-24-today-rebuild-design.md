# Today Page Rebuild: Design

**Sub-project 5 of the "Calm Executive Workspace" redesign.** Foundation tokens (1), Navigation (2), Composition philosophy (3), and Action hierarchy and icons (4) are merged. This is the last named sub-project in that direction.

Note on numbering: DESIGN.md's Provenance section also uses "sub-project 3"/"sub-project 5"/"sub-project 6" for items in the earlier, separate Visual Foundation v2 six-part decomposition (Foundation, Navigation, Today's IA, card system, inspector/drawer, mobile pass) — a different list than this one. This document is sub-project 5 of the Calm Executive Workspace direction specifically; it is not the "sub-project 5 (inspector/detail drawer)" already closed under the older numbering.

## Why this exists

The original brief for this sub-project was "Today page rebuild," with no further detail. Checked against the live page before designing, one part of a hypothetical full rebuild was already done and two real, unaddressed problems were found — the same investigate-first discipline the last two sub-projects used.

**Already done, not reopened here.** A 2026-09-04 fix (recorded in DESIGN.md's Morning Brief section) already removed the page's most obvious defect: `MorningBrief.tsx` used to re-render the same four `Section`/`.dashboard-card` panels the live dashboard grid already showed, so Schedule/Priorities/Overdue/Risks each appeared twice, item-for-item, with no visual cue distinguishing "live" from "persisted." That fix replaced the duplicated sections with `.brief-stats`, a compact `<dl>` of counts.

**Two real problems remain, verified against the running page (screenshot captured with the app's own e2e fixtures), not assumed from reading the source:**

1. **Two panels compete for the page's one dominant anchor, and the wrong one currently wins.** "Top priorities" (a `.work-panel`, deliberately promoted per `Sections.tsx`'s own comment: "'panel' promotes this section to the page's single dominant anchor") sits first. Directly below it, "Morning Brief" renders as a second full-width panel — solid `--color-ink` fill, a `--text-display-sm` heading — visually louder than Top priorities' white card. DESIGN.md's own Hierarchy rules state "one dominant anchor per page" and "if a new page has two `.work-panel`s of equal size fighting for attention above the fold, that's the tell something needs to be demoted." Morning Brief is semantically a data-freshness status artifact (is this stale, is AI on, when was it generated) — Page anatomy's zone 4 ("secondary zones... visually quieter"), not zone 3. It is currently styled as if it outranks zone 3.
2. **`.brief-stats`'s four counts are exact duplicates of counts already visible elsewhere on the same page, in the same viewport.** Verified with live fixture data: Morning Brief's Schedule/Priorities/Overdue/Risks counts (1/1/1/1 in the fixture) are the identical numbers already shown as badges on the Schedule card, Overdue commitments card, Open risks card, and the Top priorities panel — all visible without scrolling. The 2026-09-04 fix removed item-level duplication; the aggregate-count duplication it left behind is the same defect at one level of abstraction up. DESIGN.md's own reasoning for that fix already establishes the conclusion this spec acts on: "`.brief-panel`'s own heading block (generation number, AI status, staleness) is unchanged and remains its only source of information the live dashboard doesn't also show" — the stats were never that unique information.

## Scope decisions (from brainstorming)

- **Investigate and fix the verified problems, not a ground-up redesign.** Matches the discipline sub-project 3 (Composition) established: don't rebuild what already works.
- **Approach: demote Morning Brief to a compact, quiet status strip and remove `.brief-stats` entirely.** Its only remaining unique content — generation number, AI status, staleness, and the Refresh action — already has a home in the app's existing "here's the health of this data" idiom (`.status-panel`/`.inline-status`, the exact pattern `TodayPage.tsx` already uses for its own dashboard loading/error/stale states, directly above where Morning Brief renders). Reusing that idiom, rather than inventing a new visual treatment, was chosen over two alternatives considered and rejected:
  - Keep the dark hero panel but move it below the dashboard grid — rejected: still visually jarring wherever it lands, and doesn't address that the panel's remaining content doesn't need hero treatment at any size.
  - Fold Morning Brief into the page's topbar caption, removing it as a separate element entirely — rejected: a stale-brief "Refresh" action deserves its own visible control, not a buried caption line.
- **`Sections.tsx`, `dashboard-grid`, and `Top priorities`'s own styling are untouched.** The fix is Morning Brief's presentation only.
- **No backend or data-shape change.** `MorningBriefResponse` is unchanged; this is a frontend rendering decision on data already fetched.

## Design

### 1. `MorningBrief.tsx` — restructure, no new data dependency

Remove the `<dl className="brief-stats">` block entirely (four `<dt>`/`<dd>` pairs and their surrounding markup). Nothing else in the component's data flow changes: the same `useQuery`/`useMutation` calls, the same loading/error/AI-disabled/stale conditionals, the same "Refresh brief" button.

Change the wrapping element's class from `brief-panel` to `brief-status`, and the heading block's class from `brief-heading` to `brief-status-heading` (new names, since the visual contract changes completely — reusing `.brief-panel`'s old name for a fundamentally different look would misdescribe it to a future reader, the same reasoning DESIGN.md already gives for renaming `.primary-action` to `.btn-primary` when its role changed). The heading's `<h2 id="morning-brief-title">Morning Brief</h2>` and its description `<p>` stay exactly as they are — same text, same conditional generation/AI-status string — only their surrounding class and resulting size change.

### 2. CSS — replace `.brief-panel`/`.brief-heading`/`.brief-stats` with `.brief-status`/`.brief-status-heading`

Delete: `.brief-panel`, `.brief-heading`, `.brief-heading h2`, `.brief-heading p:not(.eyebrow)`, `.brief-heading .eyebrow`, `.brief-panel button`, `.brief-panel .inline-status`, `.brief-stats`, `.brief-stats dt`, `.brief-stats dd`, and their mobile-breakpoint entries (`.brief-heading` in the `@media (max-width: 520px)` flex-column rule, and `.brief-panel` in the same breakpoint's radius rule).

Add:

```css
/* Values intentionally match .status-panel/.inline-status's own border,
 * radius, background and padding (the same "quiet data-health box" look),
 * but this is a standalone rule rather than a shared class: .status-panel
 * carries its own `margin-bottom: 20px`, tuned for stacking above a page's
 * content. .dashboard-grid (which follows this element) has no margin of
 * its own -- today the gap between Morning Brief and the grid is zero
 * external margin (the old .brief-panel's bottom edge sits flush against
 * the grid). Sharing .status-panel's class would add an unwanted 20px gap
 * that doesn't exist today; a standalone rule with matching values keeps
 * today's spacing rhythm intact. */
.brief-status {
  margin-top: 36px;
  border: 1px solid var(--color-border-base);
  border-radius: var(--radius-control);
  background: var(--color-surface-panel);
  padding: 16px 18px;
}
/* h2 size/tracking matches .section-heading h2 exactly (var(--text-lg),
 * -.01em) -- the same "quiet section heading" precedent already used
 * elsewhere (e.g. AttentionQueue's grouped headings), not a new size. */
.brief-status-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.brief-status-heading h2 { margin: 0; font-size: var(--text-lg); letter-spacing: -.01em; }
.brief-status-heading p:not(.eyebrow) { margin: 6px 0 0; color: var(--color-text-secondary); }
```

`.eyebrow` needs no new rule — the existing shared `.eyebrow` rule (`--color-text-secondary`, not an on-dark variant) already applies once the dark background is gone. `.brief-status .inline-status` needs no override either, for the same reason: `.inline-status`'s default colors were only wrong against `--color-ink`; on `--color-surface-panel` they're already correct. The plain `button` rule now applies to "Refresh brief" unmodified, matching every other page's default button.

**Mobile breakpoint.** `.brief-heading`/`.brief-heading button`/`.brief-panel` are each one member of a larger shared selector list (`.topbar, .brief-heading, .recommendation-heading, .explore-heading, .work-heading { ... }` at `@media (max-width: 520px)`, similarly for the button-padding and border-radius rules). Remove only `.brief-heading` and `.brief-heading button` from those two selector lists — the rules themselves stay, serving `.topbar`/`.recommendation-heading`/`.explore-heading`/`.work-heading`. Remove `.brief-panel` from the border-radius selector list the same way (`.recommendation-panel, .explore-panel, .work-panel` keep their `border-radius: 12px` at this breakpoint). Add `.brief-status-heading` as a new member of the flex-column selector list (alongside `.topbar`, etc.) — its heading-plus-button row is the same shape as the others this rule already protects from a cramped 320px layout, and this repo just landed a dedicated fix (`fix(frontend): wizard stepper no longer overflows the page at 320px`, #274) for exactly this class of narrow-viewport overflow, so a new heading-row element gets the same defensive treatment from the start rather than needing its own follow-up fix.

**Token cleanup.** `--color-border-on-dark`, `--color-text-on-dark-muted`, and `--color-text-on-dark-faint` (defined in `styles.css`'s `:root`) have no consumer anywhere else in the file — verified by a full-file grep before writing this spec. Delete all three token definitions along with the rules above; leaving them defined-but-unused would pass `check:tokens` (which only checks that referenced tokens resolve, not that defined tokens are used) but would be dead weight.

### 3. Placement — unchanged order, demoted weight

`TodayPage.tsx`'s JSX order stays exactly as it is: Top priorities, then Morning Brief, then the dashboard grid. Reordering was considered and rejected: Morning Brief's new compact treatment is quiet enough that it no longer competes with Top priorities for "first dominant thing on the page" regardless of position, so moving it adds a diff for no verified benefit. Top priorities remains the true first thing rendered when present; Morning Brief now reads as a thin status line between it and the grid, consistent with Page anatomy's zone 4.

### 4. Tests

`MorningBrief.test.tsx`'s `'renders a compact count per brief category instead of duplicating full item lists'` test and its `statValue()` helper are removed — the behavior they assert (stats exist and show the right counts) no longer exists by design. Replace with a test asserting the stats are gone: render the same populated fixture (which has non-empty `today_schedule`/`top_priorities` and empty `overdue_commitments`/`risks`) and assert `screen.queryByText('Schedule')`, `screen.queryByText('Priorities')`, `screen.queryByText('Overdue')`, and `screen.queryByText('Risks')` all resolve to `null` — this file renders only `<MorningBrief />` in isolation (no sidebar, no other page chrome), so a `null` result means the stat labels are genuinely gone from the component's own output, not merely absent from view. Every other existing `MorningBrief.test.tsx` test (refresh headers, stale-clearing, AI-disabled notice, refresh-failure alert) needs no test-logic change — they all assert against text and roles the restructure preserves (`Generation N`, `role="alert"`, the "Refresh brief" button's accessible name) — but each must be re-run to confirm, since none of the four original tests were touched to prove it.

**`frontend/e2e/scenarios/dashboard-brief.mjs` needs a real edit, not just a re-run — checked directly against its current source rather than assumed.** Its `briefPanel` locator is `section[aria-labelledby="morning-brief-title"]`, which survives unchanged (that id stays on the same `<h2>`), but it also does `briefPanel.locator('.brief-stats')` and four `getByText` calls on that now-deleted element (`Schedule`/`Priorities`/`Overdue`/`Risks`) — as written today, this scenario would fail after the CSS/markup change, not merely need re-running. Remove those five lines and their preceding comment block (which documents the exact duplication problem this spec now finishes fixing — restate that comment, don't just delete it, since the reasoning is still relevant context for a future reader of this file). The scenario's other assertions (`Generation 3 · disabled`, the AI-disabled and stale banner text, the post-refresh `Generation 4` check) are all text-content assertions unaffected by the class/size change and need no edit.

### 5. Documentation (`DESIGN.md`)

The `## Morning Brief` section currently describes `.brief-stats` as the fix and cites the exact reasoning this spec now supersedes ("`.brief-panel`'s own heading block ... remains its only source of information the live dashboard doesn't also show"). Rewrite that section's second half — everything from "`.brief-stats` is the honest no-backend-change version" onward — to describe the new state: `.brief-stats` is gone (not merely unduplicated), replaced by `.brief-status`, a compact bordered strip reusing the same visual language as `.status-panel`/`.inline-status`; the panel's remaining content (generation number, AI status, staleness, the Refresh action) is exactly what DESIGN.md already identified as its one genuinely unique contribution. Also update the Hierarchy rules' "two `.work-panel`s of equal size" example if it still reads as hypothetical — it no longer is; note that this was a real instance, now fixed, rather than leaving the rule as a purely abstract warning. Add a Provenance entry for this sub-project, including the two verified-not-assumed problems (competing anchor weight, redundant aggregate counts) and that this is the last named sub-project of the "Calm Executive Workspace" direction.

## Verification

- **Before/after screenshot** of `/today` from unmodified `main`, then after this change, using the app's own `pnpm visual:snapshots` tool.
- **Visual confirmation** that Top priorities is now unambiguously the first, loudest element on the page, and Morning Brief reads as a quiet status line, not a competing panel.
- **Unit and e2e suites** (the e2e `dashboard-brief` scenario specifically exercises Morning Brief's refresh flow and must still pass), `tsc`, `check:tokens`, and a production build, all green.
- **A contrast check is not needed:** the new rule reuses `--color-text-secondary` and `--color-surface-panel`, both already verified in Foundation tokens; no new color is introduced.
- **A 320px mobile check specifically:** confirm the heading, description, and "Refresh brief" button in `.brief-status-heading` stack cleanly with no overflow, and that removing `.brief-heading`/`.brief-panel` from their shared mobile selector lists left `.topbar`/`.recommendation-heading`/`.explore-heading`/`.work-heading`/`.recommendation-panel`/`.explore-panel`/`.work-panel`'s own mobile treatment unchanged (a scoped diff on those rules, not a re-verification of their behavior from scratch).
- **Confirm the three now-orphaned tokens are truly unused** after the CSS deletions (a fresh grep, not a re-assertion of the pre-change count from this spec).

## Out of scope

- Any change to `Sections.tsx`, `.dashboard-card`, `.dashboard-grid`, or the Top priorities panel's own styling.
- Any backend or `MorningBriefResponse` change.
- A narrative/prose brief (DESIGN.md already notes this needs a backend summary field that doesn't exist; still true, still not this sub-project's job).
- Reordering Morning Brief relative to Top priorities or the dashboard grid.
- Any other workspace page.
