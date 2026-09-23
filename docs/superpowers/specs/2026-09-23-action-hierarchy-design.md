# Action Hierarchy and Icons: Design

**Sub-project 4 of the "Calm Executive Workspace" redesign.** Foundation tokens (1), Navigation (2) and Composition philosophy (3) are merged. The Today page rebuild (5) is a separate, later sub-project and is not in scope here.

## Why this exists

The original brief for this sub-project was "icon system (first real one), refined button variants." Checked against the app before designing, both halves turned out to have real, unaddressed work — unlike sub-project 3, where two of its three parts were already satisfied.

**Icons have a named surface already waiting for them.** The navigation redesign (sub-project 2) explicitly deferred them: "Icons are not part of this sub-project — sidebar items are plain text. Icons are sub-project 4's job; introducing them here would pre-empt that sub-project's own icon-vocabulary decision" (`docs/superpowers/specs/2026-09-11-navigation-redesign-design.md`). `SidebarNavigation.tsx`'s 15 workspace links are still plain text today.

**Button hierarchy is genuinely inconsistent, not just undocumented.** DESIGN.md's own Buttons section (`## Buttons: action hierarchy`) already states the rule: `.btn-primary` marks "the one dominant forward action in a row," and the Hierarchy rules section already says "a row shouldn't present two equally loud actions." But a full survey of `frontend/src/**/*.tsx` (231 `<button>` elements, 43 non-test files) found `.btn-primary` used in only 2 files, 4 times. Every Schedule/Risk/Automation wizard's Back+Continue or Back+submit row, and roughly 20 other rows with a clear forward action beside a clear back/cancel/reject action, are completely unstyled — violating the app's own stated rule. `.btn-quiet` (added in Visual Foundation v2) has zero consumers anywhere in the app.

## Scope decisions (from brainstorming)

- **Icons: sidebar navigation items only.** Not connector-health status badges, not wizard step indicators — those are separate surfaces with their own vocabulary decisions, out of scope here.
- **Icon source: inline SVG components, no new dependency.** This repo has added exactly one runtime dependency since its inception (`react-router-dom`, for real routing), and only because a real need existed. 15 glyphs don't justify a package; they're hand-copied from an open, permissively-licensed set (Lucide, MIT) and inlined as local React components.
- **Button hierarchy: a full audit, applying the existing rule wherever it calls for it** — not scoped down to a narrow subset. The survey (below) grounds every category the rule needs to handle.
- **`.btn-destructive` assignments are explicitly not re-litigated.** The survey found a real inconsistency (`DelegationsPanel.tsx`'s "Reject" is already `.btn-destructive`; `ApprovalInbox.tsx`'s structurally identical "Reject" isn't), but deciding what counts as "irreversible or high-risk" is a separate judgment call from assigning primary/quiet emphasis. This spec flags it as a documented follow-up in DESIGN.md's Known follow-ups, not a change this sub-project makes.

## Survey: the current state of every action row

A full read of `frontend/src/features/**/*.tsx` (excluding tests) found 231 buttons. Existing usage: `.btn-primary` 4 uses in 2 files (`RecommendationPanel.tsx`, `ConnectorHealthPanel.tsx`); `.btn-destructive` 22 uses in 15 files; `.btn-quiet` 0 uses anywhere.

Action rows fall into four shapes:

1. **A single unambiguous forward/confirm action beside one or more clearly secondary actions** (back, cancel, discard, reject, defer, dismiss, "keep reviewing"). About 20 rows: every Back+Continue and Back+submit wizard step in `ScheduleWorkspace.tsx`, `RiskWorkspace.tsx`, `PolicyPanel.tsx`, `WorkflowList.tsx`, `GmailPanel.tsx`'s connect step; Save+Discard/Cancel pairs in `EntityDetail.tsx`, `RiskReviewQueue.tsx`, `Planner.tsx`'s inline edit; Accept+"keep reviewing" in `Planner.tsx`'s replan diff; Regenerate+Discard in `AttentionExplanation.tsx`; Confirm+Dismiss in `TeamSuggestionsPanel.tsx`; Fulfil+Cancel in `WaitingView.tsx`; Approve+Reject in `ApprovalInbox.tsx`; Confirm match beside Reject+Defer (3-button) in `ResolutionInbox.tsx`; Complete beside Edit/Cancel/Archive (3-4 button) in `TaskWorkspace.tsx`. The 2 already-correct precedents (`ConnectorHealthPanel.tsx`'s 3 `.btn-primary` uses, `RecommendationPanel.tsx`'s "Confirm and execute" beside 3 unstyled siblings) are this shape — the rule below generalizes from them, it doesn't invent a new one.
2. **A dismiss/cancel action beside a `.btn-destructive` action in an elevated confirm sub-panel.** Exactly one exact match: `MembersPanel.tsx`'s "Cancel" beside "Confirm removal."
3. **Peer rows with no real hierarchy.** ~12 rows: status-filter toggles (`IncidentsPanel.tsx`, `DecisionsPanel.tsx`), the provider-picker tiles (`ConnectorHealthPanel.tsx`), the Search/Audit `role="tablist"`, Edit+Archive row actions (repeated across several list files), mutually-exclusive Enable/Disable (`DomainsPanel.tsx`), equal navigation links (`EngineeringOverview.tsx`, `WorkflowList.tsx`).
4. **Lone terminal submits with no sibling and no wizard context.** ~25 rows: "Save record," "Create task," "Invite," "Propose delegation," and similar single-button forms.

## Design

### 1. Icons (`frontend/src/navigation/icons.tsx`, `frontend/src/navigation/SidebarNavigation.tsx`, `frontend/src/styles.css`)

**New file `frontend/src/navigation/icons.tsx`** exports 15 small function components, one per workspace, each rendering a 24×24 viewBox stroke SVG (`stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"`), simplified by hand from the matching Lucide glyph (MIT-licensed; no attribution file required, but the top of the new file carries a one-line comment naming the source). Each component accepts no props beyond passthrough `className`; size is set by the consumer via CSS, not a prop, matching how every other primitive in this codebase is sized by its caller's class rather than an inline prop.

Glyph mapping (approved):

| Workspace | Component | Glyph |
|---|---|---|
| Today | `TodayIcon` | sun |
| Attention | `AttentionIcon` | bell |
| Recommendations | `RecommendationsIcon` | sparkles |
| Work | `WorkIcon` | briefcase |
| Notes | `NotesIcon` | file-text |
| Schedule | `ScheduleIcon` | calendar |
| Planner | `PlannerIcon` | list-checks |
| Meeting prep | `MeetingPrepIcon` | users-round |
| Risks | `RisksIcon` | shield-alert |
| Knowledge | `KnowledgeIcon` | book-open |
| Search & audit | `SearchAuditIcon` | search |
| Automation | `AutomationIcon` | zap |
| Engineering | `EngineeringIcon` | wrench |
| Personal | `PersonalIcon` | user |
| Team | `TeamIcon` | users |

**`workspaces.ts`** gains an `icon: ComponentType` field (or equivalent) on `WorkspaceEntry`, pointing at the matching component — required, the same discipline `composition` used, so a new workspace can't ship without picking an icon.

**`SidebarNavigation.tsx`** renders the icon before the label inside a new wrapping `<span className="sidebar-nav-link-content">`, so the existing `justify-content: space-between` (icon+label on the left, badge on the right) keeps working:

```tsx
<NavLink to={entry.path} end>
  <span className="sidebar-nav-link-content">
    <entry.icon aria-hidden="true" className="sidebar-nav-icon" />
    <span>{entry.label}</span>
  </span>
  {counts[entry.view] ? <span className="sidebar-nav-badge" ...>...</span> : null}
</NavLink>
```

`aria-hidden="true"` on the icon: the link's own text is still the sole accessible name, nothing is duplicated for assistive tech (the same reasoning already documented for `.status-badge`'s decorative `::before` dot).

**CSS:** `.sidebar-nav-link-content { display: flex; align-items: center; gap: var(--space-2); }` and `.sidebar-nav-icon { width: 18px; height: 18px; flex-shrink: 0; }`. No new color rule — the icon's `stroke="currentColor"` inherits `.sidebar-nav a`'s existing `color` (secondary at weight 500 unselected, ink at weight 650 selected via the existing `[aria-current="page"]` rule), so the current selected/unselected treatment applies to the icon automatically.

### 2. Button hierarchy (`frontend/src/**/*.tsx`, no new CSS — `.btn-primary` and `.btn-quiet` already exist)

**Rule for `.btn-primary`:** in an action row, if exactly one button is the unambiguous forward/confirm action and every other button in the row is a back, cancel, discard, reject, defer, or dismiss action, the forward action gets `className="btn-primary"`. This holds regardless of how many other buttons are in the row (2 or more) — `RecommendationPanel.tsx`'s existing 4-button row is the precedent, not an exception. Applied to every row the survey placed in shape 1 above (about 20 rows, decided per-row in the implementation plan against the survey's exact file:line list).

**Rule for `.btn-quiet`:** a dismiss/cancel button gets `className="btn-quiet"` only when it sits directly beside a `.btn-destructive` button inside an elevated confirm sub-panel — the one exact match found is `MembersPanel.tsx`'s "Cancel" beside "Confirm removal." If the implementation plan's task-by-task pass finds no other exact match, `.btn-quiet` ships with exactly one consumer; that is a correct, honest result, not a gap to fill speculatively.

**Left unstyled, deliberately:**
- Shape 3 (peer/toggle rows) — no change.
- Shape 4 (lone terminal submits with no sibling and no wizard context) — nothing in the row is visually louder for `.btn-primary` to differentiate from, so staying default is the correct reading of "the one dominant forward action in a **row**," not an oversight.
- `MergeReview.tsx`'s two "Merge into X" buttons — both are equally destructive choices with no neutral option in the row; inventing one is out of scope.

**Not touched:** every existing `.btn-destructive` assignment. No row gets a *new* `.btn-destructive` class as part of this sub-project.

### 3. Documentation (`DESIGN.md`)

- The Buttons section gains a short paragraph naming `.btn-quiet`'s first real consumer (replacing the current "Not yet consumed by any component" line) and stating the row-shape rule above in the same terms this spec uses.
- The `## Not yet part of the system` section's "no icon library" clause is rewritten: the app now has a small, hand-picked, inline SVG set scoped to the sidebar; a general-purpose icon library for arbitrary future surfaces is still an open decision, made only when one is needed.
- A new Known follow-ups bullet records the `.btn-destructive` inconsistency this sub-project found and deliberately did not fix (`ApprovalInbox.tsx`'s "Reject" vs `DelegationsPanel.tsx`'s "Reject").
- A Provenance entry for this sub-project, including that it found real work on both halves (unlike sub-project 3).

## Verification

- **Screenshots.** Before/after full-page captures of every workspace route (`pnpm visual:snapshots`) plus a close-up of the sidebar showing all 15 icons at once, and a close-up of `MembersPanel.tsx`'s confirm-removal panel showing the new `.btn-quiet` Cancel beside the existing `.btn-destructive` Confirm removal.
- **A contrast check is not needed:** icons use `currentColor` against the same text colors already verified in Foundation tokens; no new color is introduced.
- **Unit and e2e suites, `tsc`, `check:tokens`, and a production build**, all green. No new checks are added to `check:tokens` — icons and button classes are not colors or font sizes.
- **A manual accessibility pass:** confirm every sidebar icon is `aria-hidden`, confirm no link's accessible name changed (the icon isn't inside the label's own text node), and re-run the axe scans the e2e suite already includes on every scenario.

## Out of scope

- Icons anywhere outside the sidebar (connector-health badges, wizard steps, empty states).
- A general-purpose icon library decision for future surfaces.
- Any new `.btn-destructive` assignment, including the flagged `ApprovalInbox`/`DelegationsPanel` "Reject" inconsistency.
- Any restyle of `MergeReview.tsx`'s two-destructive-button row.
- The Today page rebuild (sub-project 5).
