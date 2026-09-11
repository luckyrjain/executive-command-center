#!/usr/bin/env node
// Design-token conformance gate for styles.css (DESIGN.md's Color section:
// "style new work against a token ... never a raw hex value"). Deliberately
// narrow: it checks colors only, the one category DESIGN.md states as an
// unconditional rule with no grandfathered exceptions. Spacing/radius/shadow
// are NOT checked here -- DESIGN.md itself documents raw numbers still
// throughout styles.css for those (Spacing, Geometry sections), so a blanket
// check would fail on existing, sanctioned code rather than catching new
// violations. No new dependency (no stylelint): this is a small, stdlib-only
// script in the same spirit as scripts/check_docs.py and
// scripts/check_phase3_prohibited_signals.py in the repo root.
//
// A rule allowed to use a raw color value despite this check (a one-off
// tint with no real token, e.g. a translucency effect) gets an inline
// `/* design-tokens-allow: <reason> */` comment on the same line -- logged
// as a deliberate, visible exception, not silently skipped.

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const STYLES_PATH = fileURLToPath(new URL('../src/styles.css', import.meta.url))
const COLOR_PATTERN = /#[0-9a-fA-F]{3,8}\b|\brgba?\(/g
const ALLOW_MARKER = 'design-tokens-allow'

function main() {
  const source = readFileSync(STYLES_PATH, 'utf8')
  const lines = source.split('\n')

  let inRoot = false
  const violations = []

  lines.forEach((line, index) => {
    const trimmed = line.trim()
    if (trimmed.startsWith(':root')) { inRoot = true }
    if (inRoot && trimmed === '}') { inRoot = false; return }
    if (inRoot) return // :root is the token definition block itself

    if (line.includes(ALLOW_MARKER)) return // explicit, logged exception

    const matches = line.match(COLOR_PATTERN)
    if (matches) violations.push({ line: index + 1, text: line.trim(), matches })
  })

  if (violations.length === 0) {
    console.log('check-design-tokens: no raw color values outside :root. OK.')
    return
  }

  console.error(`check-design-tokens: ${violations.length} raw color value(s) found outside the :root token block.`)
  console.error('Use a var(--color-*) token instead, or add an inline `/* design-tokens-allow: <reason> */` comment for a deliberate one-off.\n')
  for (const v of violations) {
    console.error(`  styles.css:${v.line}: ${v.text}`)
  }
  process.exitCode = 1
}

main()
