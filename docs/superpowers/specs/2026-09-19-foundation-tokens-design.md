# Foundation Tokens: Design

**Sub-project 1 of the "Calm Executive Workspace" redesign.** It was deferred when navigation (sub-project 2) was pulled forward, and shipped after that. Composition philosophy, action hierarchy and icons, and the Today page rebuild are separate, later sub-projects and are not in scope here.

## Why this exists

The original definition of this sub-project was "typography scale, surface/elevation hierarchy, motion timings." Visual Foundation v2 (`docs/superpowers/specs/2026-09-04-visual-foundation-v2-design.md`) already shipped the motion tokens (`--motion-fast/standard/slow`, `--ease-standard`) and the two elevation tokens (`--shadow-card`, `--shadow-panel`). Those are done and untouched here.

What is not done:

- **Typography has no tokens.** `styles.css` declares about 45 raw `font-size` values across 13 distinct sizes (12px x14, 13px x9, 11px x6, then singles including 15px and 22px, plus three `clamp()` display sizes).
- **Colors are named by value or history, not role.** About 35 `--color-*` tokens include 7 near-identical border greys (`#cfd6df` to `#edf0f3`, three of them within 5 RGB steps of each other), 5 text greys, and `--color-white`, which is used 19 times as both a surface fill and a text color on dark fills. `DESIGN.md` records consolidating these as "a separate design decision nobody's made yet"; this spec makes that decision.
- **A latent accessibility defect.** `--color-text-muted` (`#667085`) measures 4.39:1 on `--color-surface-recessed`, below the 4.5:1 AA minimum for small text. Consolidation is the natural place to fix it.

## Scope decisions (from brainstorming)

- **Visual change is allowed within a bound.** Consolidation may shift a grey by a few RGB steps. The bound is: every merge moves a color by at most 7 RGB steps per channel, except the two explicitly listed exceptions below (`mono` into `ink`, `error` into `danger`). Nothing becomes a different color family, and no text color gets lighter.
- **Approach: single tier, rename in place.** Old value- and history-named tokens are replaced by role-named tokens and deleted. There is no alias layer, no raw-palette-plus-semantic tier, and no deprecated names left behind.
- **`styles.css` only.** No `.tsx` file references `var(--...)`, so the migration is contained to one stylesheet.
- **Out of scope:** font weights (six values in use: 400, 500, 600, 650, 700, 800), letter-spacing, line-height, spacing scale, radius, and dark mode. Weights and tracking are a natural follow-up once the size scale exists, but nothing here needs them.

## Token changes

### Text

| New token | Value | Replaces | Shift |
| --- | --- | --- | --- |
| `--color-ink` | `#18212f` | `--color-ink`, `--color-text-mono` (`#293344`) | mono darkens by 17-20 steps (exception, 3 uses on `dd` values) |
| `--color-text-body` | `#4f5b6d` | `--color-text-copy` | none (rename) |
| `--color-text-secondary` | `#596579` | `--color-text-secondary`, `--color-text-tertiary` (`#5a6472`) | tertiary shifts 1-7 steps (blue channel) |
| `--color-text-muted` | `#636d82` | `--color-text-muted` (`#667085`) | 3 steps darker; fixes AA on recessed (4.59:1) |

`--color-text-on-ink` (`#ffffff`) is new: the text color for content on `--color-ink` and `--color-accent` fills (primary button, selected tab, done wizard step, dark brief panel). It replaces the text-on-dark uses of `--color-white`.

Unchanged: `--color-accent`, `--color-accent-simulation`, `--color-text-ai-accent`, `--color-text-success`, `--color-text-success-strong`, `--color-text-danger-strong`, `--color-text-on-dark-muted`, `--color-text-on-dark-faint`.

### Borders

| New token | Value | Replaces | Shift |
| --- | --- | --- | --- |
| `--color-border-strong` | `#cfd6df` | `--color-border-default` | none (rename) |
| `--color-border-base` | `#d8dee7` | `--color-border-panel`, `--color-border-panel-alt` (`#dde3ea`), `--color-border-panel-2` (`#dce2ea`) | 1-5 steps |
| `--color-border-subtle` | `#e3e8ef` | `--color-border-subtle`, `--color-border-hairline` (`#e7ebf0`) | hairline shifts 4 steps |
| `--color-border-faint` | `#edf0f3` | `--color-border-faint` | none |

`--color-border-default` is deliberately **deleted**, not repurposed. Its value moves to `--color-border-strong` and a new tier takes the "default" middle position under a different name. Reusing the old name for a new value would let a missed reference silently change color instead of failing the undefined-variable check below.

Unchanged: `--color-border-danger`, `--color-border-success`, `--color-border-degraded`, `--color-border-ai`, `--color-border-on-dark`.

### Surfaces

| New token | Value | Replaces |
| --- | --- | --- |
| `--color-surface-page` | `#f3f5f7` | `--color-page-bg` |
| `--color-surface-panel` | `#ffffff` | `--color-white` (surface uses) |
| `--color-surface-recessed` | `#eef1f5` | unchanged |

### Status tints

The success, danger, degraded, ai, simulation, and pinned families stay as they are, with one merge: `--color-border-error` (`#e7b8b8`) and `--color-bg-error` (`#fff7f7`) fold into `--color-border-danger` (`#e3c8c8`) and `--color-bg-danger` (`#fdf1f1`). They are the same role at slightly different values, used by a single rule (`.error-panel`). This is the largest visible shift in the set (about 16 steps), so the before/after review below shows it explicitly; if it is not acceptable, the two tokens stay separate and this merge is dropped, with nothing else affected.

### Typography scale

| Token | Value | Replaces |
| --- | --- | --- |
| `--text-2xs` | `11px` | 11px (uppercase labels) |
| `--text-xs` | `12px` | 12px |
| `--text-sm` | `13px` | 13px |
| `--text-md` | `14px` | 14px, 15px |
| `--text-base` | `16px` | 16px (body) |
| `--text-lg` | `18px` | 18px |
| `--text-xl` | `20px` | 20px, 22px |
| `--text-2xl` | `28px` | 28px (KPI value) |
| `--text-display-sm` | `clamp(28px, 4vw, 44px)` | brief and work `h2` |
| `--text-display-md` | `clamp(32px, 5vw, 56px)` | topbar `h1` |
| `--text-display-lg` | `clamp(42px, 7vw, 78px)` | hero `h1` |

Two rules move by one or two pixels: the single 15px declaration goes to 14px, and the single 22px declaration goes to 20px.

## Enforcement

`frontend/scripts/check-design-tokens.mjs` (stdlib only, already wired into CI as `check:tokens`) gains two checks:

1. **Every `var(--name)` in `styles.css` must be defined in `:root`.** This catches a stale reference to a deleted token, which is the main risk of a rename migration.
2. **No raw `font-size: <n>px` outside `:root`.** The existing `/* design-tokens-allow: <reason> */` escape hatch applies. `clamp()` display sizes live in `:root` as tokens, so they are covered by the same rule.

Existing color checks are unchanged. No blanket check is added for spacing or radius, for the same reason `DESIGN.md` already gives: those have documented, sanctioned raw values.

## Verification

- **Before/after screenshots** from the existing e2e harness (`frontend/e2e/server.mjs` plus fixtures) of: Today, a work panel with lists, the connector wizard, Risks, an error-state panel, a degraded-state panel, and the sidebar with a selected item. The "before" set is captured from `main` before any change. The review reports the largest per-channel delta and shows the `.error-panel` and `mono` cases side by side.
- **Contrast check** of every text token against every surface it is used on (page, panel, recessed), with all pairs at 4.5:1 or above. The measured ratios are recorded in `DESIGN.md`.
- **Unit suite, e2e suite (including its axe accessibility scans), `tsc`, `check:tokens`, and a production build** all green.

## Documentation

`DESIGN.md` is updated in the same change: the Typography table gains the scale, the Color section's token groups and its "kept as separate tokens because the migration was a pure refactor" note are rewritten to describe the consolidated set, the recorded contrast ratios are added, and a Provenance entry is added. The Known follow-ups line about near-duplicate tokens is closed. Font weights and letter-spacing are added as a new follow-up.

## Out of scope

- Weights, letter-spacing, and line-height tokens.
- Spacing, radius, and shadow changes (all shipped in Visual Foundation v2).
- Any component restyle. Where a rule's value shifts, that is a consequence of consolidation, not a redesign of that component.
- Dark mode, which the single-tier approach does not preclude but does not build toward either.
- Sub-projects 3 (composition philosophy), 4 (action hierarchy and icons) and 5 (Today rebuild).
