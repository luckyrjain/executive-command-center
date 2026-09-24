#!/usr/bin/env node
// Design-token conformance gate for styles.css (DESIGN.md's Color section:
// "style new work against a token ... never a raw hex value").
//
// Checks:
//   1. No raw hex/rgb()/rgba() color outside the :root token block.
//   2. Every var(--name) resolves to a custom property declared in the file.
//      This is what makes a token rename safe: a stale reference to a deleted
//      token fails here instead of silently rendering as "unset".
//   3. No raw px/clamp() font-size outside :root (use a var(--text-*) token).
//   4. No raw numeric font-weight outside :root (use a var(--font-weight-*) token).
//   5. No raw em letter-spacing outside :root (use a var(--tracking-*) token).
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
const RAW_FONT_SIZE_PATTERN = /font-size\s*:\s*(?:[0-9.]+px|clamp\()/
const RAW_FONT_WEIGHT_PATTERN = /font-weight\s*:\s*[0-9]{3}\b/
const RAW_LETTER_SPACING_PATTERN = /letter-spacing\s*:\s*-?\.[0-9]+em\b/
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
  const inRoot = rootLineFlags(stripped)
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
    for (const match of stripped[index].matchAll(VAR_PATTERN)) {
      if (!defined.has(match[1])) violations.push({ line: index + 1, text: line.trim(), name: match[1] })
    }
  })
  return violations
}

export function findRawFontSizes(source) {
  const lines = source.split('\n')
  const stripped = stripComments(source).split('\n')
  const inRoot = rootLineFlags(stripped)
  const violations = []
  lines.forEach((line, index) => {
    if (inRoot[index]) return
    if (line.includes(ALLOW_MARKER)) return
    if (RAW_FONT_SIZE_PATTERN.test(stripped[index])) violations.push({ line: index + 1, text: line.trim() })
  })
  return violations
}

export function findRawFontWeights(source) {
  const lines = source.split('\n')
  const stripped = stripComments(source).split('\n')
  const inRoot = rootLineFlags(stripped)
  const violations = []
  lines.forEach((line, index) => {
    if (inRoot[index]) return
    if (line.includes(ALLOW_MARKER)) return
    if (RAW_FONT_WEIGHT_PATTERN.test(stripped[index])) violations.push({ line: index + 1, text: line.trim() })
  })
  return violations
}

export function findRawLetterSpacing(source) {
  const lines = source.split('\n')
  const stripped = stripComments(source).split('\n')
  const inRoot = rootLineFlags(stripped)
  const violations = []
  lines.forEach((line, index) => {
    if (inRoot[index]) return
    if (line.includes(ALLOW_MARKER)) return
    if (RAW_LETTER_SPACING_PATTERN.test(stripped[index])) violations.push({ line: index + 1, text: line.trim() })
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
  const rawFontSizes = findRawFontSizes(source)
  const rawFontWeights = findRawFontWeights(source)
  const rawLetterSpacing = findRawLetterSpacing(source)

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
    report(
      'raw font-size value(s) found outside the :root token block.',
      'Use a var(--text-*) token from the type scale, or add an inline `/* design-tokens-allow: <reason> */` comment for a deliberate one-off.',
      rawFontSizes,
      (v) => v.text,
    ),
    report(
      'raw font-weight value(s) found outside the :root token block.',
      'Use a var(--font-weight-*) token, or add an inline `/* design-tokens-allow: <reason> */` comment for a deliberate one-off.',
      rawFontWeights,
      (v) => v.text,
    ),
    report(
      'raw letter-spacing value(s) found outside the :root token block.',
      'Use a var(--tracking-*) token, or add an inline `/* design-tokens-allow: <reason> */` comment for a deliberate one-off.',
      rawLetterSpacing,
      (v) => v.text,
    ),
  ].some(Boolean)

  if (failed) {
    process.exitCode = 1
    return
  }
  console.log('check-design-tokens: no raw colors, font sizes, font weights, or letter-spacing outside :root; every var() resolves. OK.')
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main()
