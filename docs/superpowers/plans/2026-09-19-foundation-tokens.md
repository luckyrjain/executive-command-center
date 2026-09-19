# Foundation Tokens Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the value- and history-named color tokens in `frontend/src/styles.css` with a smaller role-based set, add a typography scale, and enforce both in CI.

**Architecture:** Single-tier rename in place. Old tokens are deleted and every `var()` reference in `styles.css` is migrated by a scripted, mapping-driven pass, so a missed reference fails the new undefined-variable check instead of silently changing color. `frontend/scripts/check-design-tokens.mjs` is refactored into testable functions and gains two checks. Before/after screenshots and a contrast measurement verify the intended small visual shifts.

**Tech Stack:** Plain CSS custom properties, Node stdlib scripts, Vitest, Playwright (already a devDependency; used by the e2e harness), Python 3 for one-off migration and contrast scripts.

**Spec:** `docs/superpowers/specs/2026-09-19-foundation-tokens-design.md`

## Global Constraints

- **`styles.css` only.** No `.tsx` file references `var(--...)`; do not touch component files.
- **No compatibility shims.** Old token names are deleted, not aliased. `--color-border-default` in particular is deleted, never repurposed for the new middle tier (the new tier is named `--color-border-base`).
- **Shift bound:** every color merge moves a color by at most 7 RGB steps per channel, except the two named exceptions: `--color-text-mono` into `--color-ink`, and `--color-border-error`/`--color-bg-error` into the danger tokens.
- **No text color gets lighter.** `--color-text-muted` moves from `#667085` to `#636d82` (darker).
- **Every text token must be at least 4.5:1** against every surface it is used on (page `#f3f5f7`, panel `#ffffff`, recessed `#eef1f5`).
- **Out of scope:** font weights, letter-spacing, line-height, spacing, radius, shadow, motion, dark mode, any component restyle, any `.tsx` change.
- **Commit trailer:** every commit message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- **Commands run from `frontend/`** unless a step says otherwise. The working branch is `foundation-tokens`; do not push and do not open a PR (the controller does that when asked).
- **Snapshot output root** (session scratchpad, not in the repo): `/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/foundation-tokens-snaps`. Referred to below as `$SNAPS`.

## File Structure

- Create `frontend/e2e/visual-snapshots.mjs`: capture and compare tool for full-page screenshots (Task 1). Reused by the later redesign sub-projects, which also change visuals.
- Modify `frontend/scripts/check-design-tokens.mjs`: split into exported functions, add undefined-variable check (Task 2), add raw font-size check (Task 4).
- Create `frontend/scripts/check-design-tokens.test.mjs`: Vitest unit tests for those functions (Tasks 2 and 4).
- Modify `frontend/src/styles.css`: the token migration (Tasks 3 and 4).
- Modify `DESIGN.md`: document the consolidated set and scale (Task 6).

---

### Task 1: Visual snapshot tool and "before" baseline

**Files:**
- Create: `frontend/e2e/visual-snapshots.mjs`

**Interfaces:**
- Produces: CLI `node e2e/visual-snapshots.mjs capture <outDir>` (writes PNGs) and `node e2e/visual-snapshots.mjs compare <dirA> <dirB>` (prints a table, exits 0). Later tasks call both.

- [ ] **Step 1: Write the tool**

Create `frontend/e2e/visual-snapshots.mjs`:

```js
import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'

import { chromium } from 'playwright'

import { createFixtureApi } from './fixtures.mjs'
import { startPreviewServer } from './server.mjs'

const WORKSPACE_PATHS = [
  '/today', '/attention', '/recommendations', '/work', '/notes', '/schedule', '/planner', '/meeting-prep',
  '/risks', '/knowledge', '/search-audit', '/automation', '/engineering', '/personal', '/team',
]

// Markup exercising the token-bearing classes that default fixtures do not
// render: error/degraded panels, all four status badges, hint/error text,
// and a definition list (the monospace `dd` color).
const SWATCH_HTML = `
<section class="work-panel" id="token-swatch" aria-label="Token swatch">
  <div class="inline-status error-panel" role="alert">Error panel sample text</div>
  <div class="inline-status degraded-panel" role="status">Degraded panel sample text</div>
  <p>
    <span class="status-badge is-active">Healthy</span>
    <span class="status-badge is-degraded">Degraded</span>
    <span class="status-badge is-error">Error</span>
    <span class="status-badge is-neutral">Normal</span>
  </p>
  <p class="field-hint">Field hint sample text</p>
  <p class="field-error">Field error sample text</p>
  <p class="empty-state">Empty state sample text</p>
  <dl class="detail-fields"><dt>Owner</dt><dd>sample-owner-id</dd><dt>Score rationale</dt><dd>sample rationale</dd></dl>
  <ul class="item-list"><li><strong>Item title</strong><div class="item-meta"><span>meta one</span><span>meta two</span></div></li></ul>
</section>`

async function capture(outDir) {
  await mkdir(outDir, { recursive: true })
  const server = await startPreviewServer()
  const browser = await chromium.launch()
  try {
    const shoot = async (name, viewport, route, { swatch = false } = {}) => {
      const context = await browser.newContext({ viewport, reducedMotion: 'reduce' })
      const page = await context.newPage()
      await createFixtureApi(page)
      await page.goto(`${server.baseURL}${route}`)
      await page.waitForLoadState('networkidle')
      if (swatch) {
        await page.evaluate((html) => {
          document.querySelector('#workspace-main')?.insertAdjacentHTML('beforeend', html)
        }, SWATCH_HTML)
      }
      await page.screenshot({ path: path.join(outDir, `${name}.png`), fullPage: true })
      await context.close()
    }

    for (const route of WORKSPACE_PATHS) {
      await shoot(`desktop${route.replace(/\//g, '-')}`, { width: 1280, height: 900 }, route)
    }
    await shoot('desktop-swatch', { width: 1280, height: 900 }, '/today', { swatch: true })
    await shoot('mobile-today', { width: 390, height: 844 }, '/today')
    console.log(`captured ${WORKSPACE_PATHS.length + 2} screenshots to ${outDir}`)
  } finally {
    await browser.close()
    server.stop()
  }
}

function dataUrl(buffer) {
  return `data:image/png;base64,${buffer.toString('base64')}`
}

async function compare(dirA, dirB) {
  const names = (await readdir(dirA)).filter((name) => name.endsWith('.png')).sort()
  const browser = await chromium.launch()
  const page = await browser.newPage()
  await page.goto('about:blank')
  const rows = []
  for (const name of names) {
    let b
    try {
      b = await readFile(path.join(dirB, name))
    } catch {
      rows.push({ name, note: 'missing in second dir' })
      continue
    }
    const a = await readFile(path.join(dirA, name))
    const result = await page.evaluate(async ([aData, bData]) => {
      const load = (src) => new Promise((resolve, reject) => {
        const image = new Image()
        image.onload = () => resolve(image)
        image.onerror = reject
        image.src = src
      })
      const [ia, ib] = await Promise.all([load(aData), load(bData)])
      const w = Math.min(ia.width, ib.width)
      const h = Math.min(ia.height, ib.height)
      const pixels = (image) => {
        const canvas = document.createElement('canvas')
        canvas.width = w
        canvas.height = h
        const ctx = canvas.getContext('2d')
        ctx.drawImage(image, 0, 0)
        return ctx.getImageData(0, 0, w, h).data
      }
      const da = pixels(ia)
      const db = pixels(ib)
      let maxDelta = 0
      let differing = 0
      for (let i = 0; i < da.length; i += 4) {
        const d = Math.max(Math.abs(da[i] - db[i]), Math.abs(da[i + 1] - db[i + 1]), Math.abs(da[i + 2] - db[i + 2]))
        if (d > 0) differing += 1
        if (d > maxDelta) maxDelta = d
      }
      return { maxDelta, differing, total: w * h, sizeA: [ia.width, ia.height], sizeB: [ib.width, ib.height] }
    }, [dataUrl(a), dataUrl(b)])
    rows.push({ name, ...result })
  }
  await browser.close()

  console.log('name'.padEnd(34), 'maxDelta', 'differing%', 'sizeA -> sizeB')
  for (const row of rows) {
    if (row.note) {
      console.log(row.name.padEnd(34), row.note)
      continue
    }
    const pct = ((row.differing / row.total) * 100).toFixed(2)
    const sizes = `${row.sizeA.join('x')} -> ${row.sizeB.join('x')}${row.sizeA[1] === row.sizeB[1] ? '' : '  (height changed: compared overlap only)'}`
    console.log(row.name.padEnd(34), String(row.maxDelta).padStart(8), `${pct}%`.padStart(10), sizes)
  }
}

const [mode, first, second] = process.argv.slice(2)
if (mode === 'capture' && first) {
  await capture(path.resolve(first))
} else if (mode === 'compare' && first && second) {
  await compare(path.resolve(first), path.resolve(second))
} else {
  console.error('usage: node e2e/visual-snapshots.mjs capture <outDir> | compare <dirA> <dirB>')
  process.exitCode = 1
}
```

- [ ] **Step 2: Build the app the way the e2e suite does**

Run: `VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build`
Expected: build succeeds (the chunk-size warning is normal).

- [ ] **Step 3: Capture the baseline from unmodified CSS**

Confirm `git diff --stat main -- src/styles.css` prints nothing (CSS is still main's), then run:

```bash
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/foundation-tokens-snaps
node e2e/visual-snapshots.mjs capture "$SNAPS/before"
```

Expected: `captured 17 screenshots to .../before`, and `ls "$SNAPS/before" | wc -l` prints `17`.

- [ ] **Step 4: Self-test the compare mode against itself**

Run: `node e2e/visual-snapshots.mjs compare "$SNAPS/before" "$SNAPS/before"`
Expected: 17 rows, every `maxDelta` is `0` and every `differing%` is `0.00%`. If any row is non-zero, the capture is non-deterministic: stop and report it rather than continuing (a flaky baseline makes the later comparison meaningless).

- [ ] **Step 5: Open one screenshot and confirm it is not a blank or error page**

Read `$SNAPS/before/desktop-swatch.png` with the Read tool. Expected: the app shell with the sidebar, the Today page, and a "Token swatch" panel at the bottom containing the error panel, degraded panel, four badges, and sample text. If the swatch is missing, the `#workspace-main` selector did not match: check `frontend/src/App.tsx` for the actual main-element id and fix the selector.

- [ ] **Step 6: Commit**

```bash
git add frontend/e2e/visual-snapshots.mjs
git commit -m "$(cat <<'EOF'
test(e2e): add a visual snapshot capture/compare tool

Full-page screenshots of every workspace route plus a token swatch, and a
canvas-based per-channel diff, so visual redesign sub-projects can report the
real size of any color shift instead of asserting it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Refactor the token check and add the undefined-variable check

**Files:**
- Modify: `frontend/scripts/check-design-tokens.mjs`
- Create: `frontend/scripts/check-design-tokens.test.mjs`

**Interfaces:**
- Produces (used by Task 4): from `check-design-tokens.mjs`, `export function stripComments(source)`, `export function findRawColors(source)`, `export function findUndefinedVars(source)`. Each `find*` returns an array of `{ line: number, text: string, ... }` (1-based `line`). `main()` still reads `../src/styles.css`, prints, and sets `process.exitCode = 1` on any violation. The file must still be runnable as `node scripts/check-design-tokens.mjs` and must not run `main()` when imported by a test.

- [ ] **Step 1: Write the failing tests**

Create `frontend/scripts/check-design-tokens.test.mjs`:

```js
import { describe, expect, it } from 'vitest'

import { findRawColors, findUndefinedVars, stripComments } from './check-design-tokens.mjs'

const ROOT = `:root {
  --color-ink: #18212f;
  --space-1: 4px;
}
`

describe('stripComments', () => {
  it('blanks block comments but keeps line numbering intact', () => {
    const source = 'a {\n  /* var(--nope)\n  still comment */\n  color: red;\n}'
    const stripped = stripComments(source)
    expect(stripped.split('\n')).toHaveLength(source.split('\n').length)
    expect(stripped).not.toContain('--nope')
    expect(stripped).toContain('color: red;')
  })
})

describe('findRawColors', () => {
  it('flags a raw hex outside :root and ignores the :root block', () => {
    const source = `${ROOT}.a { color: #fff; }\n`
    const found = findRawColors(source)
    expect(found).toHaveLength(1)
    expect(found[0].line).toBe(5)
  })

  it('honors the design-tokens-allow marker', () => {
    const source = `${ROOT}.a { background: rgba(0, 0, 0, .1); } /* design-tokens-allow: tint */\n`
    expect(findRawColors(source)).toHaveLength(0)
  })
})

describe('findUndefinedVars', () => {
  it('passes when every var() is defined in :root', () => {
    const source = `${ROOT}.a { color: var(--color-ink); margin: var(--space-1); }\n`
    expect(findUndefinedVars(source)).toEqual([])
  })

  it('flags a var() whose token is not defined anywhere', () => {
    const source = `${ROOT}.a { color: var(--color-gone); }\n`
    const found = findUndefinedVars(source)
    expect(found).toHaveLength(1)
    expect(found[0]).toMatchObject({ line: 5, name: '--color-gone' })
  })

  it('handles a var() with a fallback', () => {
    const source = `${ROOT}.a { color: var(--color-gone, red); }\n`
    expect(findUndefinedVars(source)).toHaveLength(1)
  })

  it('treats a custom property declared outside :root as defined', () => {
    const source = `${ROOT}.a { --local: 1px; margin: var(--local); }\n`
    expect(findUndefinedVars(source)).toEqual([])
  })

  it('ignores var() mentions inside comments', () => {
    const source = `${ROOT}/* mentions var(--color-gone) in prose */\n.a { color: var(--color-ink); }\n`
    expect(findUndefinedVars(source)).toEqual([])
  })
})
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `pnpm test -- --run scripts/check-design-tokens.test.mjs`
Expected: FAIL. The imports do not exist yet (`findRawColors is not a function` / "does not provide an export").

- [ ] **Step 3: Rewrite the script**

Replace the entire contents of `frontend/scripts/check-design-tokens.mjs` with:

```js
#!/usr/bin/env node
// Design-token conformance gate for styles.css (DESIGN.md's Color section:
// "style new work against a token ... never a raw hex value").
//
// Checks:
//   1. No raw hex/rgb()/rgba() color outside the :root token block.
//   2. Every var(--name) resolves to a custom property declared in the file.
//      This is what makes a token rename safe: a stale reference to a deleted
//      token fails here instead of silently rendering as "unset".
//
// Spacing/radius/shadow are deliberately NOT checked: DESIGN.md documents
// sanctioned raw numbers for those, so a blanket check would fail on existing,
// intentional code. No new dependency (no stylelint): a small stdlib-only
// script in the same spirit as scripts/check_docs.py in the repo root.
//
// A rule allowed to use a raw value despite a check (a one-off tint with no
// real token) gets an inline `/* design-tokens-allow: <reason> */` comment on
// the same line -- a deliberate, visible exception, not silently skipped.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const STYLES_PATH = fileURLToPath(new URL('../src/styles.css', import.meta.url))
const COLOR_PATTERN = /#[0-9a-fA-F]{3,8}\b|\brgba?\(/g
const VAR_PATTERN = /var\(\s*(--[\w-]+)/g
const DECLARATION_PATTERN = /(--[\w-]+)\s*:/g
const ALLOW_MARKER = 'design-tokens-allow'

// Blank out /* ... */ comments while preserving newlines, so line numbers in
// the stripped text still match the original file.
export function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, ' '))
}

function rootLineFlags(lines) {
  let inRoot = false
  return lines.map((line) => {
    const trimmed = line.trim()
    if (trimmed.startsWith(':root')) inRoot = true
    if (inRoot && trimmed === '}') {
      inRoot = false
      return true
    }
    return inRoot
  })
}

export function findRawColors(source) {
  const lines = source.split('\n')
  const stripped = stripComments(source).split('\n')
  const inRoot = rootLineFlags(lines)
  const violations = []
  lines.forEach((line, index) => {
    if (inRoot[index]) return
    if (line.includes(ALLOW_MARKER)) return
    const matches = stripped[index].match(COLOR_PATTERN)
    if (matches) violations.push({ line: index + 1, text: line.trim(), matches })
  })
  return violations
}

export function findUndefinedVars(source) {
  const lines = source.split('\n')
  const stripped = stripComments(source).split('\n')
  const defined = new Set()
  for (const line of stripped) {
    for (const match of line.matchAll(DECLARATION_PATTERN)) defined.add(match[1])
  }
  const violations = []
  lines.forEach((line, index) => {
    if (line.includes(ALLOW_MARKER)) return
    for (const match of stripped[index].matchAll(VAR_PATTERN)) {
      if (!defined.has(match[1])) violations.push({ line: index + 1, text: line.trim(), name: match[1] })
    }
  })
  return violations
}

function report(title, hint, violations, describe) {
  if (violations.length === 0) return false
  console.error(`check-design-tokens: ${violations.length} ${title}`)
  console.error(`${hint}\n`)
  for (const violation of violations) console.error(`  styles.css:${violation.line}: ${describe(violation)}`)
  console.error('')
  return true
}

function main() {
  const source = readFileSync(STYLES_PATH, 'utf8')
  const colors = findRawColors(source)
  const undefinedVars = findUndefinedVars(source)

  const failed = [
    report(
      'raw color value(s) found outside the :root token block.',
      'Use a var(--color-*) token instead, or add an inline `/* design-tokens-allow: <reason> */` comment for a deliberate one-off.',
      colors,
      (v) => v.text,
    ),
    report(
      'var() reference(s) to a token that is not defined.',
      'Define the token in :root, or fix the name (a renamed/deleted token leaves stale references behind).',
      undefinedVars,
      (v) => `${v.name} -- ${v.text}`,
    ),
  ].some(Boolean)

  if (failed) {
    process.exitCode = 1
    return
  }
  console.log('check-design-tokens: no raw color values outside :root; every var() resolves. OK.')
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main()
```

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `pnpm test -- --run scripts/check-design-tokens.test.mjs`
Expected: PASS, 7 tests.

- [ ] **Step 5: Run the script against the real stylesheet**

Run: `pnpm check:tokens`
Expected: `check-design-tokens: no raw color values outside :root; every var() resolves. OK.` and exit 0. If it reports undefined variables, the current stylesheet has a pre-existing stale reference: report it verbatim and do not fix it in this task (the controller decides).

- [ ] **Step 6: Commit**

```bash
git add frontend/scripts/check-design-tokens.mjs frontend/scripts/check-design-tokens.test.mjs
git commit -m "$(cat <<'EOF'
test(tokens): check that every var() resolves to a defined token

Refactors the token check into unit-tested functions and adds an
undefined-variable check, the safety net for deleting and renaming tokens.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Migrate to the role-based color token set

**Files:**
- Modify: `frontend/src/styles.css` (the `:root` block and every `var(--color-...)` reference)

**Interfaces:**
- Consumes: the `check:tokens` undefined-variable check from Task 2.
- Produces (used by Tasks 5 and 6): these tokens exist in `:root` after this task, and the deleted ones do not.

  Added or changed: `--color-text-body` (`#4f5b6d`), `--color-text-muted` (now `#636d82`), `--color-text-on-ink` (`#ffffff`), `--color-border-strong` (`#cfd6df`), `--color-border-base` (`#d8dee7`), `--color-surface-page` (`#f3f5f7`), `--color-surface-panel` (`#ffffff`).

  Deleted: `--color-text-mono`, `--color-text-copy`, `--color-text-tertiary`, `--color-border-default`, `--color-border-panel`, `--color-border-panel-alt`, `--color-border-panel-2`, `--color-border-hairline`, `--color-border-error`, `--color-bg-error`, `--color-page-bg`, `--color-white`.

  Unchanged: `--color-ink`, `--color-text-secondary`, `--color-border-subtle`, `--color-border-faint`, `--color-surface-recessed`, and every status/accent/on-dark token.

- [ ] **Step 1: Record the pre-migration reference counts**

Run:

```bash
cd frontend
for t in text-mono text-copy text-tertiary border-default border-panel border-panel-alt border-panel-2 border-hairline border-error bg-error page-bg white; do
  printf '%-20s %s\n' "--color-$t" "$(grep -c "var(--color-$t)" src/styles.css)"
done
```

Expected (matches the survey taken at planning time; a mismatch means `main` has moved, so report it): mono 3, copy 7, tertiary 10, border-default 3, border-panel 3, panel-alt 1, panel-2 1, hairline 6, border-error 1, bg-error 1, page-bg 0, white 19.

- [ ] **Step 2: Run the migration script**

Write this to the session scratchpad as `migrate_colors.py`, or run it inline from `frontend/`:

```python
import re, sys
path = 'src/styles.css'
css = open(path).read()

# 1. Straight renames / merges of var() references.
mapping = {
    '--color-text-mono': '--color-ink',
    '--color-text-copy': '--color-text-body',
    '--color-text-tertiary': '--color-text-secondary',
    '--color-border-default': '--color-border-strong',
    '--color-border-panel': '--color-border-base',
    '--color-border-panel-alt': '--color-border-base',
    '--color-border-panel-2': '--color-border-base',
    '--color-border-hairline': '--color-border-subtle',
    '--color-border-error': '--color-border-danger',
    '--color-bg-error': '--color-bg-danger',
    '--color-page-bg': '--color-surface-page',
}
for old, new in mapping.items():
    css = re.sub(r'var\(' + re.escape(old) + r'\)', 'var(' + new + ')', css)

# 2. --color-white splits by role: a `color:` property is text-on-ink; every
#    other use (background, color-mix input, border, ...) is the panel surface.
css = re.sub(r'(?<![-\w])(color\s*:\s*)var\(--color-white\)', r'\1var(--color-text-on-ink)', css)
css = css.replace('var(--color-white)', 'var(--color-surface-panel)')

# 3. :root definitions: drop deleted tokens, add/change the new ones.
root_match = re.search(r':root\s*\{.*?\n\}', css, re.S)
root = root_match.group(0)
for deleted in ['--color-text-mono', '--color-text-copy', '--color-text-tertiary', '--color-border-default',
                '--color-border-panel', '--color-border-panel-alt', '--color-border-panel-2',
                '--color-border-hairline', '--color-border-error', '--color-bg-error']:
    root, n = re.subn(r'\n  ' + re.escape(deleted) + r':[^;]*;', '', root)
    assert n == 1, f'expected to delete exactly one definition of {deleted}, deleted {n}'

def rename_def(root, old, new, value):
    root, n = re.subn(r'(\n  )' + re.escape(old) + r':[^;]*;', r'\1' + new + ': ' + value + ';', root)
    assert n == 1, f'expected one definition of {old}, found {n}'
    return root

root = rename_def(root, '--color-page-bg', '--color-surface-page', '#f3f5f7')
root = rename_def(root, '--color-white', '--color-surface-panel', '#ffffff')
root = rename_def(root, '--color-text-muted', '--color-text-muted', '#636d82')

# New tokens, inserted next to their siblings.
root = root.replace('  --color-text-secondary: #596579;',
                    '  --color-text-secondary: #596579;\n  --color-text-body: #4f5b6d;\n  --color-text-on-ink: #ffffff;', 1)
root = root.replace('  --color-border-subtle: #e3e8ef;',
                    '  --color-border-strong: #cfd6df;\n  --color-border-base: #d8dee7;\n  --color-border-subtle: #e3e8ef;', 1)
css = css[:root_match.start()] + root + css[root_match.end():]
open(path, 'w').write(css)
print('migrated')
```

Expected output: `migrated` (any assertion error means the `:root` block differs from what the plan assumed: stop and report it).

- [ ] **Step 3: Verify no old names remain and the new ones resolve**

Run:

```bash
cd frontend
grep -nE -- '--color-(text-mono|text-copy|text-tertiary|border-default|border-panel|border-panel-alt|border-panel-2|border-hairline|border-error|bg-error|page-bg|white)\b' src/styles.css
echo "grep exit: $?"
pnpm check:tokens
```

Expected: the grep prints nothing and `grep exit: 1`; `check:tokens` prints its OK line. Any hit or any undefined-variable report is a missed reference: fix it by hand using the mapping above, and re-run.

- [ ] **Step 4: Spot-check the `--color-white` split**

Run: `grep -n 'color-text-on-ink' src/styles.css | cut -c1-140`
Expected: only uses that are the CSS property `color:` on a dark or accent fill: `.brief-panel`, `.brief-panel button`, `.wizard-step-circle.done`, `.provider-glyph` (aria-pressed), `.tab-list button[aria-selected]`, `button[aria-pressed]`, `.btn-primary`, `.workspace-nav button[aria-selected]`, `.simulation-banner`. (About 9 lines.) Then run `grep -n 'color-surface-panel' src/styles.css | cut -c1-140` and confirm every hit is a `background`, a `color-mix(...)` input, or `border-*` and none is a text `color:`.

- [ ] **Step 5: Run the automated checks**

Run: `pnpm typecheck && pnpm test -- --run && pnpm build`
Expected: all pass (555+ tests, tsc clean, build succeeds).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/styles.css
git commit -m "$(cat <<'EOF'
refactor(styles): consolidate color tokens into a role-based set

Merges near-duplicate text/border greys, splits --color-white into
surface-panel and text-on-ink, renames page-bg to surface-page, folds the
error tints into danger, and darkens --color-text-muted slightly so it clears
4.5:1 on the recessed surface. Old names are deleted, not aliased.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Add the typography scale and enforce it

**Files:**
- Modify: `frontend/src/styles.css`
- Modify: `frontend/scripts/check-design-tokens.mjs`
- Modify: `frontend/scripts/check-design-tokens.test.mjs`

**Interfaces:**
- Consumes: `stripComments`, `rootLineFlags` behavior, `report()`, and `main()` from Task 2. `rootLineFlags` is module-private; `findRawFontSizes` lives in the same file.
- Produces: `export function findRawFontSizes(source)` returning `[{ line, text }]`; `main()` also reports it. Tokens in `:root`: `--text-2xs` 11px, `--text-xs` 12px, `--text-sm` 13px, `--text-md` 14px, `--text-base` 16px, `--text-lg` 18px, `--text-xl` 20px, `--text-2xl` 28px, `--text-display-sm` `clamp(28px, 4vw, 44px)`, `--text-display-md` `clamp(32px, 5vw, 56px)`, `--text-display-lg` `clamp(42px, 7vw, 78px)`.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/scripts/check-design-tokens.test.mjs` (add `findRawFontSizes` to the existing import list from `./check-design-tokens.mjs`):

```js
describe('findRawFontSizes', () => {
  it('flags a raw px font-size outside :root', () => {
    const source = `${ROOT}.a { font-size: 12px; }\n`
    const found = findRawFontSizes(source)
    expect(found).toHaveLength(1)
    expect(found[0].line).toBe(5)
  })

  it('flags a raw clamp() font-size outside :root', () => {
    const source = `${ROOT}.a { font-size: clamp(28px, 4vw, 44px); }\n`
    expect(findRawFontSizes(source)).toHaveLength(1)
  })

  it('accepts a token reference and a keyword/inherit value', () => {
    const source = `${ROOT}.a { font-size: var(--text-sm); }\n.b { font-size: inherit; }\n`
    expect(findRawFontSizes(source)).toEqual([])
  })

  it('ignores the :root block itself', () => {
    const source = ':root {\n  --text-sm: 13px;\n  --text-x: clamp(1px, 2vw, 3px);\n}\n.a { font-size: var(--text-sm); }\n'
    expect(findRawFontSizes(source)).toEqual([])
  })

  it('honors the design-tokens-allow marker', () => {
    const source = `${ROOT}.a { font-size: 9px; } /* design-tokens-allow: icon glyph */\n`
    expect(findRawFontSizes(source)).toEqual([])
  })

  it('ignores a font-size mentioned inside a comment', () => {
    const source = `${ROOT}/* was font-size: 12px */\n.a { color: var(--color-ink); }\n`
    expect(findRawFontSizes(source)).toEqual([])
  })
})
```

- [ ] **Step 2: Run to confirm they fail**

Run: `pnpm test -- --run scripts/check-design-tokens.test.mjs`
Expected: the new `findRawFontSizes` tests FAIL ("findRawFontSizes is not a function" / missing export); the earlier 7 still pass.

- [ ] **Step 3: Implement `findRawFontSizes` and wire it into `main()`**

In `frontend/scripts/check-design-tokens.mjs`, add this constant next to the other patterns:

```js
const RAW_FONT_SIZE_PATTERN = /font-size\s*:\s*(?:[0-9.]+px|clamp\()/
```

Add this function after `findUndefinedVars`:

```js
export function findRawFontSizes(source) {
  const lines = source.split('\n')
  const stripped = stripComments(source).split('\n')
  const inRoot = rootLineFlags(lines)
  const violations = []
  lines.forEach((line, index) => {
    if (inRoot[index]) return
    if (line.includes(ALLOW_MARKER)) return
    if (RAW_FONT_SIZE_PATTERN.test(stripped[index])) violations.push({ line: index + 1, text: line.trim() })
  })
  return violations
}
```

In `main()`, add `const rawFontSizes = findRawFontSizes(source)` beside the other two, add this entry to the `failed` array after the undefined-vars report:

```js
    report(
      'raw font-size value(s) found outside the :root token block.',
      'Use a var(--text-*) token from the type scale, or add an inline `/* design-tokens-allow: <reason> */` comment for a deliberate one-off.',
      rawFontSizes,
      (v) => v.text,
    ),
```

and update the success message to: `check-design-tokens: no raw colors or font sizes outside :root; every var() resolves. OK.`

Also update the header comment's check list to add: `3. No raw px/clamp() font-size outside :root (use a var(--text-*) token).`

- [ ] **Step 4: Run the unit tests, then confirm the real stylesheet now fails the new check**

Run: `pnpm test -- --run scripts/check-design-tokens.test.mjs`
Expected: PASS, 13 tests.

Run: `pnpm check:tokens`
Expected: exit 1, reporting roughly 45 raw font-size lines. This is the failing state the migration below fixes.

- [ ] **Step 5: Add the scale tokens to `:root`**

In `frontend/src/styles.css`, add this block inside `:root`, immediately after the `--space-24: 96px;` line and before the `/* Container/grid` comment:

```css

  /* Type scale -- font-size in new CSS should reference one of these rather
   * than a raw px value (enforced by check:tokens). The three display sizes
   * are fluid; the rest are fixed. */
  --text-2xs: 11px;
  --text-xs: 12px;
  --text-sm: 13px;
  --text-md: 14px;
  --text-base: 16px;
  --text-lg: 18px;
  --text-xl: 20px;
  --text-2xl: 28px;
  --text-display-sm: clamp(28px, 4vw, 44px);
  --text-display-md: clamp(32px, 5vw, 56px);
  --text-display-lg: clamp(42px, 7vw, 78px);
```

- [ ] **Step 6: Migrate every raw font-size**

Run from `frontend/`:

```python
import re
path = 'src/styles.css'
css = open(path).read()
root_match = re.search(r':root\s*\{.*?\n\}', css, re.S)
head, root, tail = css[:root_match.start()], root_match.group(0), css[root_match.end():]

mapping = {
    '11px': 'var(--text-2xs)',
    '12px': 'var(--text-xs)',
    '13px': 'var(--text-sm)',
    '14px': 'var(--text-md)',
    '15px': 'var(--text-md)',
    '16px': 'var(--text-base)',
    '18px': 'var(--text-lg)',
    '20px': 'var(--text-xl)',
    '22px': 'var(--text-xl)',
    '28px': 'var(--text-2xl)',
    'clamp(28px, 4vw, 44px)': 'var(--text-display-sm)',
    'clamp(32px, 5vw, 56px)': 'var(--text-display-md)',
    'clamp(42px, 7vw, 78px)': 'var(--text-display-lg)',
}

def sub(match):
    value = match.group(2)
    assert value in mapping, f'unmapped font-size value: {value}'
    return match.group(1) + mapping[value]

pattern = re.compile(r'(font-size\s*:\s*)([0-9.]+px|clamp\([^)]*\))')
tail = pattern.sub(sub, tail)
head = pattern.sub(sub, head)
open(path, 'w').write(head + root + tail)
print('migrated')
```

Expected: `migrated`. An `unmapped font-size value` assertion means a size exists that the spec did not account for: stop and report it (do not invent a mapping).

- [ ] **Step 7: Verify**

Run: `pnpm check:tokens`
Expected: `check-design-tokens: no raw colors or font sizes outside :root; every var() resolves. OK.`

Run: `grep -cE 'font-size: *[0-9.]+px|font-size: *clamp' src/styles.css`
Expected: `0`. The `--text-*` definitions inside `:root` do not match the `font-size:` pattern, so any non-zero count is a rule the migration missed.

Run: `pnpm typecheck && pnpm test -- --run && pnpm build`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/styles.css frontend/scripts/check-design-tokens.mjs frontend/scripts/check-design-tokens.test.mjs
git commit -m "$(cat <<'EOF'
feat(styles): add a typography scale and enforce it in check:tokens

Adds --text-* size tokens (eight fixed steps, three fluid display sizes),
migrates every raw font-size to them (15px folds to 14px, 22px to 20px), and
fails CI on a new raw px/clamp font-size outside :root.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Verify: screenshots, contrast, full suites

**Files:**
- No source changes expected. This task produces evidence; fix only what a check proves is broken, in `frontend/src/styles.css`.

**Interfaces:**
- Consumes: `visual-snapshots.mjs` (Task 1), the migrated tokens (Tasks 3 and 4), `$SNAPS/before` from Task 1.
- Produces (used by Task 6): the measured contrast ratios and the per-page max delta figures, reported in the task's final message.

- [ ] **Step 1: Capture the "after" set**

Run:

```bash
cd frontend
SNAPS=/private/tmp/claude-502/-Users-luckyjain-Projects-executive-command-center/749a0f20-1e95-4e41-a14d-ab9d3a265093/scratchpad/foundation-tokens-snaps
VITE_API_BASE_URL=http://127.0.0.1:4173 pnpm build
node e2e/visual-snapshots.mjs capture "$SNAPS/after"
node e2e/visual-snapshots.mjs compare "$SNAPS/before" "$SNAPS/after"
```

Expected: 17 rows. Every `maxDelta` should be at most 20 (the largest planned shifts are the `.error-panel` merge, about 16 per channel, and `mono` into `ink`, about 20). The only planned layout change is the 15px-to-14px fold (`.connector-identity strong`) and the 22px-to-20px fold (`.recommendation-copy h3`); a page that renders one of those (likely `/engineering`, `/recommendations`) may show a changed height and a large localized delta, so inspect those two images visually rather than trusting the number. A `maxDelta` above 20, or a height change, on any other row is a bug in the migration: find it and fix it.

- [ ] **Step 2: Look at the swatch, before and after**

Read `$SNAPS/before/desktop-swatch.png` and `$SNAPS/after/desktop-swatch.png`. Describe, in one or two sentences, how the error panel, degraded panel, badges, and `dd` values differ. If the error panel changed noticeably (it should be slightly paler and less pink-red), say so plainly.

- [ ] **Step 3: Measure contrast for every text token against every surface it sits on**

Run from `frontend/`:

```python
import re
css = open('src/styles.css').read()
root = re.search(r':root\s*\{.*?\n\}', css, re.S).group(0)
tok = dict(re.findall(r'(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;', root))

def lum(h):
    h = h.lstrip('#'); c = [int(h[i:i+2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in c]
    return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
def ratio(a, b):
    hi, lo = sorted([lum(a), lum(b)], reverse=True)
    return (hi + .05) / (lo + .05)

surfaces = ['--color-surface-page', '--color-surface-panel', '--color-surface-recessed']
core = ['--color-ink', '--color-text-body', '--color-text-secondary', '--color-text-muted']
failed = False
print('%-26s' % 'token', *['%-22s' % s.replace('--color-surface-', 'on ') for s in surfaces])
for t in core:
    cells = []
    for s in surfaces:
        r = ratio(tok[t], tok[s]); cells.append('%.2f' % r)
        if r < 4.5: failed = True
    print('%-26s' % t, *['%-22s' % c for c in cells])
status = [('--color-text-success-strong', '--color-bg-success'), ('--color-text-danger-strong', '--color-bg-danger'),
          ('--color-text-ai-accent', '--color-bg-ai'), ('--color-accent-simulation', '--color-bg-simulation-a'),
          ('--color-text-danger-strong', '--color-surface-panel'), ('--color-accent', '--color-surface-panel')]
print()
for fg, bg in status:
    r = ratio(tok[fg], tok[bg]); print('%-30s on %-28s %.2f%s' % (fg, bg, r, '' if r >= 4.5 else '  <-- BELOW 4.5'))
print('\nCORE TEXT TOKENS PASS 4.5:1' if not failed else '\nCORE TEXT TOKEN BELOW 4.5:1')
```

Expected: `--color-text-muted` at least 4.59 on recessed, all four core tokens at 4.5 or above on all three surfaces, and the line `CORE TEXT TOKENS PASS 4.5:1`. Status-token pairs are reported for the record; a status pair below 4.5 that existed before this change is not this task's to fix (say so if it appears).

- [ ] **Step 4: Run every automated suite**

Run: `pnpm typecheck && pnpm test -- --run && pnpm check:tokens && pnpm test:e2e`
Expected: typecheck clean, all unit tests pass, token check OK, and the e2e run finishes with every scenario passing (26 scenarios, each with its axe scan). If `pnpm test:e2e` fails on a browser-install error rather than a scenario, run `pnpm exec playwright install chromium` once and retry.

- [ ] **Step 5: Report**

Final message must contain: the compare table (all 17 rows), the contrast table, the swatch description from Step 2, and the pass/fail of each suite. No commit unless a fix was needed; if one was, commit it as `fix(styles): <what the check caught>` with the trailer.

---

### Task 6: Update DESIGN.md

**Files:**
- Modify: `DESIGN.md`

**Interfaces:**
- Consumes: the migrated tokens from Tasks 3 and 4. The contrast figures written into `DESIGN.md` below were precomputed from the planned token values; Step 4 re-verifies them against the real stylesheet.

- [ ] **Step 1: Find every reference to a deleted token name**

Run from the repo root:

```bash
grep -nE -- '--color-(text-mono|text-copy|text-tertiary|border-default|border-panel|border-panel-alt|border-panel-2|border-hairline|border-error|bg-error|page-bg|white)\b' DESIGN.md docs/*.md 2>/dev/null | cut -c1-160
```

Expected: hits in `DESIGN.md` (the Color section's examples and groups, and possibly the Layout primitives and Interaction states sections). Note each one; the steps below rewrite the Color section, and any remaining hit elsewhere in `DESIGN.md` gets updated to the replacement name from the mapping below. Historical Provenance paragraphs that describe past states are left as written.

Mapping for stray references: `--color-text-mono` becomes `--color-ink`; `--color-text-copy` becomes `--color-text-body`; `--color-text-tertiary` becomes `--color-text-secondary`; `--color-border-default` becomes `--color-border-strong`; `--color-border-panel`, `-panel-alt`, `-panel-2` become `--color-border-base`; `--color-border-hairline` becomes `--color-border-subtle`; `--color-border-error` and `--color-bg-error` become the `-danger` equivalents; `--color-page-bg` becomes `--color-surface-page`; `--color-white` becomes `--color-surface-panel` (surface) or `--color-text-on-ink` (text on a dark fill).

- [ ] **Step 2: Add the scale to the Typography section**

In the Typography table, replace the two rows for Panel title and Section heading with:

```markdown
| Panel title (`.work-heading h2`) | `--text-display-sm` (`clamp(28px, 4vw, 44px)`) | `letter-spacing: -.035em` |
| Section heading | `--text-lg` (`18px`) | weight `700` |
```

and change the Wizard review value row's size cell from `` `13px` `` to `` `--text-sm` (`13px`) ``. Then, directly below the table, add:

```markdown
**Type scale.** Every `font-size` in `styles.css` references one of these tokens; `check:tokens` fails on a raw `px` or `clamp()` value outside `:root` (an inline `/* design-tokens-allow: <reason> */` comment marks a deliberate one-off). New CSS reaches for a step, not a new number.

| Token | Value | Used for |
|---|---|---|
| `--text-2xs` | `11px` | uppercase labels (`dt` in review/detail lists, wizard step numbers) |
| `--text-xs` | `12px` | meta lines, chips, badges, captions |
| `--text-sm` | `13px` | field hints and errors, dense body copy |
| `--text-md` | `14px` | list item titles, card titles |
| `--text-base` | `16px` | body |
| `--text-lg` | `18px` | section headings |
| `--text-xl` | `20px` | sub-panel and card headings |
| `--text-2xl` | `28px` | KPI values |
| `--text-display-sm` | `clamp(28px, 4vw, 44px)` | panel titles |
| `--text-display-md` | `clamp(32px, 5vw, 56px)` | top bar heading |
| `--text-display-lg` | `clamp(42px, 7vw, 78px)` | page hero |

Two one-off sizes were folded into their neighbors when the scale was introduced (15px to `--text-md`, 22px to `--text-xl`). Font weights and letter-spacing are not tokenized yet; see Known follow-ups.
```

- [ ] **Step 3: Rewrite the Color section's token groups**

In the Color section: change the example in the paragraph starting "Every color is a CSS custom property" from `` `var(--color-border-default)` `` to `` `var(--color-border-base)` ``, and change the sentence ending "...fails on a raw hex/`rgb()`/`rgba()` value anywhere in `styles.css` outside the `:root` block itself." to also state the other two checks: "It also fails on any `var(--x)` that resolves to no defined custom property (which is what makes renaming or deleting a token safe) and on a raw `px`/`clamp()` `font-size` outside `:root` (see Typography above)." Change "Scoped to colors only, deliberately — spacing/radius/shadow" to "Spacing, radius and shadow are deliberately not checked —".

Replace the bulleted group list (from `- **Ink:**` through `- **Accent:**`) with:

```markdown
- **Ink:** `--color-ink` (`#18212f`) — headings, primary text, and the fill of `.btn-primary`
- **Text:** `--color-text-body` (`#4f5b6d`, running copy), `--color-text-secondary` (`#596579`, secondary labels and meta), `--color-text-muted` (`#636d82`, hints and de-emphasized text), and `--color-text-on-ink` (`#ffffff`, text on an ink or accent fill)
- **Borders:** `--color-border-strong` (`#cfd6df`, inputs and controls), `--color-border-base` (`#d8dee7`, panels and cards), `--color-border-subtle` (`#e3e8ef`, dividers inside a panel), `--color-border-faint` (`#edf0f3`, the lightest separator)
- **Surfaces:** `--color-surface-panel` (`#ffffff`, cards and panels), `--color-surface-page` (`#f3f5f7`, the page), `--color-surface-recessed` (`#eef1f5`, recessed or upcoming areas and the sidebar)
- **Error:** the error panel (`.error-panel`) uses the danger tokens (`--color-border-danger` / `--color-bg-danger`); there is no separate error color
- **Elevation:** `--shadow-card` (`.dashboard-card`) and `--shadow-panel` (`.recommendation-panel`/`.explore-panel`/`.work-panel`) — the app's only two shadow values, see Geometry below for when each applies
- **Accent:** `--color-accent` (`#2C4A87`) — its first real consumer is the top-level workspace nav's selected state (Navigation below); not yet used for anything beyond that one job
```

Replace the paragraph starting "Token names describe role, not shade" with:

```markdown
Token names describe role, not shade. The near-duplicate greys the original token migration kept separate were consolidated in the Foundation tokens sub-project (`docs/superpowers/specs/2026-09-19-foundation-tokens-design.md`): five text greys became four, seven borders became four, and `--color-white` was split into `--color-surface-panel` and `--color-text-on-ink` because it was being used both as a fill and as text on dark fills. Every merge moved a color by at most 7 RGB steps per channel, with two named exceptions (`--color-text-mono` folded into `--color-ink`, and the error tints folded into the danger tokens). **Contrast:** every text token clears 4.5:1 on every surface it sits on. Measured 2026-09-19, on page / panel / recessed: `--color-ink` 14.81 / 16.19 / 14.29; `--color-text-body` 6.30 / 6.89 / 6.08; `--color-text-secondary` 5.39 / 5.90 / 5.20; `--color-text-muted` 4.76 / 5.20 / 4.59. `--color-text-muted` was darkened from `#667085` to `#636d82` to fix a 4.39:1 failure on the recessed surface.
```

- [ ] **Step 4: Confirm the figures still hold**

Re-run the contrast script from Task 5 Step 3. Its four core rows must match the figures written above to two decimals. If they differ (a token value moved after this plan was written), replace the figures in `DESIGN.md` with the measured ones.
- [ ] **Step 5: Update Known follow-ups and Provenance**

In Known follow-ups, add these two bullets at the very end of that section, immediately before the `## Provenance` heading:

```markdown
- **Near-duplicate color tokens** — done (Foundation tokens, 2026-09-19); see Color above. `--color-border-panel`/`-panel-alt`/`-panel-2`, the text greys, and the error-versus-danger tints are consolidated.
- **Font weights and letter-spacing are not tokenized.** Six weights are in use (`400`, `500`, `600`, `650`, `700`, `800`, with `800` the most common, on uppercase labels) and several letter-spacing values (`-.045em` through `.08em`). An uppercase-label pattern (`11px`, weight `800`, `.08em` tracking) repeats in three rules. Left for a later pass now that the size scale exists; not urgent because nothing currently drifts.
```

In Provenance, add this paragraph at the end of the file:

```markdown
Foundation tokens (sub-project 1 of the "Calm Executive Workspace" direction, deferred while navigation was pulled forward) landed 2026-09-19. Motion and elevation tokens already existed from Visual Foundation v2, so the real remaining work was two things: a typography scale (about 45 raw `font-size` values across 13 sizes became eight fixed steps plus three fluid display sizes, with 15px and 22px folded into their neighbors) and a role-based color set (about 35 value- and history-named tokens down to a smaller role-named set; see Color above for the mapping and the contrast measurements). It also fixed a latent accessibility defect, `--color-text-muted` at 4.39:1 on the recessed surface. `check:tokens` gained an undefined-`var()` check (the safety net for the rename) and a raw-`font-size` check. Verified with before/after full-page screenshots of every workspace route, the unit and e2e suites including axe scans, `tsc`, the token check, and a production build. See `docs/superpowers/specs/2026-09-19-foundation-tokens-design.md` and `docs/superpowers/plans/2026-09-19-foundation-tokens.md`.
```

- [ ] **Step 6: Run the docs check and the token check, then commit**

Run from the repo root: `python3 scripts/check_docs.py; echo "docs exit: $?"` then `cd frontend && pnpm check:tokens`
Expected: `docs exit: 0` and the token check OK. If the docs check reports a broken link, the two `docs/superpowers/...` paths above are written as plain backtick text, not markdown links: keep them that way.

```bash
git add DESIGN.md
git commit -m "$(cat <<'EOF'
docs: document the consolidated color tokens and the type scale

Rewrites the Color token groups for the role-based set, adds the type scale
and measured contrast figures, closes the near-duplicate-token follow-up, and
adds the Foundation tokens provenance entry.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
