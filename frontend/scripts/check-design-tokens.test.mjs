import { describe, expect, it } from 'vitest'

import {
  findRawColors,
  findRawFontSizes,
  findRawFontWeights,
  findRawLetterSpacing,
  findUndefinedVars,
  stripComments,
} from './check-design-tokens.mjs'

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

  it('is not fooled by a :root-looking line inside a comment', () => {
    const source = `${ROOT}/* prose\n:root {\n*/\n.a { color: #fff; }\n`
    expect(findRawColors(source)).toHaveLength(1)
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

  it('still reports an undefined var() on a line carrying the allow marker', () => {
    const source = `${ROOT}.a { color: var(--color-gone); } /* design-tokens-allow: tint */\n`
    const found = findUndefinedVars(source)
    expect(found).toHaveLength(1)
    expect(found[0]).toMatchObject({ name: '--color-gone' })
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
    const source = ':root {\n  font-size: 16px;\n  --text-sm: 13px;\n}\n.a { font-size: var(--text-sm); }\n'
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

describe('findRawFontWeights', () => {
  it('flags a raw font-weight outside :root', () => {
    const source = `${ROOT}.a { font-weight: 700; }\n`
    const found = findRawFontWeights(source)
    expect(found).toHaveLength(1)
    expect(found[0].line).toBe(5)
  })

  it('accepts a token reference', () => {
    const source = `${ROOT}.a { font-weight: var(--font-weight-700); }\n`
    expect(findRawFontWeights(source)).toEqual([])
  })

  it('ignores the :root block itself', () => {
    const source = ':root {\n  --font-weight-700: 700;\n}\n.a { font-weight: var(--font-weight-700); }\n'
    expect(findRawFontWeights(source)).toEqual([])
  })

  it('honors the design-tokens-allow marker', () => {
    const source = `${ROOT}.a { font-weight: 700; } /* design-tokens-allow: one-off */\n`
    expect(findRawFontWeights(source)).toEqual([])
  })

  it('ignores a font-weight mentioned inside a comment', () => {
    const source = `${ROOT}/* was font-weight: 700 */\n.a { color: var(--color-ink); }\n`
    expect(findRawFontWeights(source)).toEqual([])
  })
})

describe('findRawLetterSpacing', () => {
  it('flags a raw letter-spacing outside :root', () => {
    const source = `${ROOT}.a { letter-spacing: -.02em; }\n`
    const found = findRawLetterSpacing(source)
    expect(found).toHaveLength(1)
    expect(found[0].line).toBe(5)
  })

  it('accepts a token reference', () => {
    const source = `${ROOT}.a { letter-spacing: var(--tracking-neg-02); }\n`
    expect(findRawLetterSpacing(source)).toEqual([])
  })

  it('ignores the :root block itself', () => {
    const source = ':root {\n  --tracking-neg-02: -.02em;\n}\n.a { letter-spacing: var(--tracking-neg-02); }\n'
    expect(findRawLetterSpacing(source)).toEqual([])
  })

  it('honors the design-tokens-allow marker', () => {
    const source = `${ROOT}.a { letter-spacing: -.02em; } /* design-tokens-allow: one-off */\n`
    expect(findRawLetterSpacing(source)).toEqual([])
  })

  it('ignores a letter-spacing mentioned inside a comment', () => {
    const source = `${ROOT}/* was letter-spacing: -.02em */\n.a { color: var(--color-ink); }\n`
    expect(findRawLetterSpacing(source)).toEqual([])
  })
})
