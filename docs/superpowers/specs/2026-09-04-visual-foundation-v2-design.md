# Visual Foundation v2 — design token system

**Status:** approved design, spec written; implementation not yet planned or built.
**Sub-project 1 of a larger redesign** (see decomposition below). This spec covers only the token/primitive layer — color, typography, geometry, spacing, motion, and action hierarchy. Navigation architecture, Today's information architecture, the KPI/component vocabulary, the inspector/drawer pattern, and the responsive/mobile pass are separate, later sub-projects that build on this foundation; none of that is in scope here.

## Why this exists

The current `DESIGN.md` documents a deliberately restrained, near-monochrome system (grayscale + one dark-navy ink anchor, no accent color, `.field-form`/wizard component patterns, a page-composition philosophy). That system is well-documented and internally consistent, and multiple recent PRs have deliberately protected its restraint (refusing a table component, an icon library, a generic info color, page-level "this is dangerous" chrome — see `DESIGN.md`'s "Not yet part of the system" and the design-critique conversation that preceded this spec).

The user has now explicitly asked for a different overall visual identity for this app — not incremental fixes to the current system, a deliberate redesign, briefed as **"Linear × Stripe Dashboard × high-end executive operating system."** This spec is the foundation that redesign builds on.

This is treated as **Architectural** work (new token system, changes several components depend on) per the project's own brainstorming discipline, decomposed into ordered sub-projects rather than one giant spec:

1. **Foundation** (this spec) — token layer: color, typography, geometry, spacing, motion, action hierarchy.
2. Navigation architecture — pill row → left rail (or whatever shape is chosen), global search, the confirmed top-nav selected-state bug.
3. Today's information architecture — narrative redesign of the dashboard.
4. Card/component system — density modes, Metric/Delta/Trend/Sparkline/Table vocabulary.
5. Inspector/detail drawer — new interaction pattern.
6. Mobile/responsive pass.

Each later sub-project gets its own spec → plan → implementation cycle once reached.

## Direction

Three concrete approaches were considered and presented to the user:

- **A — Linear-leaning**: literal Linear palette (violet/indigo accent `#5B5FEF`-ish), tight 8px spacing, small radii, Inter, hairline borders over shadows.
- **B — Stripe-leaning**: literal Stripe palette (indigo `#635BFF`-ish), layered gray surfaces, more generous whitespace, real soft shadows for elevation.
- **C — Hybrid (chosen)**: both products' *restraint and precision* — tight spacing, small radii, one considered accent, shadows reserved for genuine elevation — but an accent color that is not literally either product's actual brand hue, so the result reads as "considered, in that tradition" rather than a literal skin of either product.

The user chose **C**.

## Color

Named palette — kept values are unchanged from the current system (already considered, no reason to replace something that's already right); new values are additions for this redesign.

| Role | Value | Status | Usage |
|---|---|---|---|
| Ink | `#18212f` | kept | Body text anchor, primary text color |
| Accent | `#2C4A87` | **new** | Links, primary actions, focus ring, selected nav state |
| Critical | text `#8A2C2C` / border `#E3C8C8` / bg `#FDF1F1` | kept, renamed | Was "danger" — see Priority language below |
| Needs attention | existing amber/degraded family | kept, renamed | Was "degraded" — see Priority language below |
| Healthy | `#1C6B3A` | kept, renamed | Was "success" — see Priority language below |
| Normal | no color — default ink/border | n/a | The fourth priority tier is "nothing to report," not a hue |

**Priority language.** The existing `success`/`danger`/`degraded` semantic families are re-labeled as one deliberate 4-tier priority vocabulary — **Critical / Needs attention / Normal / Healthy** — rather than left as ad hoc per-component naming. The underlying hex values are unchanged (they're already muted and considered); what's added is a consistent name and a documented rule for when each applies, so a new surface reaches for "Critical" or "Needs attention" as a named concept instead of reinventing the choice per component the way `EngineeringOverview.tsx`'s recent `.degraded-panel` usage already does informally.

No new saturated colors are introduced beyond the one accent. This is a hard constraint carried over from the current system's own anti-patterns list: no gradients, no purple/pink, no decorative color.

## Typography

Inter remains the sole typeface — a considered choice for this redesign specifically (Linear uses Inter too), not the default-because-no-decision the current system's own "one font, everywhere" line already documents itself as being deliberate about.

**New: a numeric/KPI type role.** `font-variant-numeric: tabular-nums`, weight ~650, tighter letter-spacing (`-.02em` at typical metric sizes), for large standalone numbers (headline metrics, KPI tiles — the actual components come in sub-project 4, but the type role belongs to the foundation). This is Inter's own tabular figures, not a second family — matches the "utility face for data" role from the studio method without abandoning the one-font system.

Existing type scale (panel titles, section headings, eyebrows, body) carries over unchanged from `DESIGN.md`'s Typography table; this spec doesn't revisit those roles.

## Geometry

Tightened toward Linear/Stripe's crisper, less-rounded feel:

| Token | Current | New |
|---|---|---|
| `--radius-control` | `14px` | `8px` |
| `--radius-panel` | `28px` | `16px` |
| Pills (`999px`) | unchanged | unchanged — still "fully round," not a size choice |

Elevation stays two-tier (`--shadow-card` / `--shadow-panel`), values reduced in blur/opacity slightly for a crisper, less-diffuse feel consistent with the tighter radii — exact new shadow values are an implementation-time tuning detail, not fixed here, since they need to be checked against real surfaces to avoid looking flat or looking too heavy at the new radius.

## Spacing

The existing `--space-1` (4px) through `--space-24` (96px) scale carries over unchanged. It's already a clean 8px-rooted system consistent with how both reference products space their own UIs; there's no reason to redesign something that already fits the new direction.

## Motion

Existing tokens carry over with one change: `--motion-fast` tightens from `120ms` to `100ms` for a snappier feel consistent with the reference products' own micro-interactions. `--motion-standard` (180ms), `--motion-slow` (280ms), and `--ease-standard` are unchanged. The `prefers-reduced-motion` query added in an earlier pass is unaffected and still applies.

## Action hierarchy

Adds a **quiet/ghost** variant: no fill, no border by default, subtle hover state (a faint background tint or underline, not the existing bordered-button hover). For low-emphasis actions where even the default bordered-pill button reads as too heavy — both reference products lean on this treatment often (e.g. a "Cancel" next to a filled primary button, or a row of inline text-actions in a dense list).

Existing hierarchy (`.btn-destructive`, `.recommendation-actions .primary-action`, default/secondary) is unchanged by this spec; ghost is a genuine fourth tier, not a replacement for any of the three that exist today.

## Out of scope for this spec

Explicitly deferred to later sub-projects, not decided here:

- Whether pill-shaped buttons stay pill-shaped once radii tighten elsewhere (sub-project 4, once real components are being redesigned against the new geometry).
- Exact shadow blur/opacity values (implementation-time tuning against real surfaces).
- Any component that consumes these tokens (nav, dashboard, KPI tiles, drawer) — those are sub-projects 2–5.
- Whether `--color-page-bg`/surface grays need adjustment to sit well against the new accent — worth checking during implementation, not a decision this spec makes.

## Open questions for implementation time

- Whether `--radius-control: 8px` reads as too tight for existing dense forms (many stacked inputs) should be checked visually before committing app-wide.
