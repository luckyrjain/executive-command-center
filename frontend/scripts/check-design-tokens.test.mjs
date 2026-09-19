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
