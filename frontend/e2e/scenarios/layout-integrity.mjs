import assert from 'node:assert/strict'

import { createCollaborationStore, createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

// Two-up `.work-grid` titles use --text-2xl (28px); a workspace title that
// falls through to the page-hero `h1` rule reaches 78px, and one on the plain
// panel-title scale (--text-display-sm) reaches 44px.
const TWO_UP_TITLE_PX = 28

function horizontalOverflow(page) {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
}

/**
 * Computed-layout regressions that no markup-level test can see (pure CSS):
 * a page rendered two-up in `.work-grid` must not scroll the whole page
 * sideways and its titles must fit on one line, a workspace with many tabs
 * wraps them instead of overflowing off-screen, the global WorkspaceSwitcher
 * stays a compact control, `.recommendation-heading` titles don't break
 * mid-word, and `.work-subsection` stays a heading + spacing primitive rather
 * than a second bordered/shadowed card inside `.work-panel`.
 */
export async function run({ page, baseURL }) {
  // Two workspaces so the global WorkspaceSwitcher renders on every page below
  // (it renders nothing for a single-workspace account).
  const stamp = '2026-01-01T00:00:00Z'
  const self = { accountId: 'account-layout', usersId: 'users-layout', email: 'layout@example.test', displayName: 'Layout', workspaceId: 'workspace-a' }
  const collaborationStore = createCollaborationStore({
    workspaces: [
      { id: 'workspace-a', name: 'Alpha Co', timezone: 'UTC', created_at: stamp },
      { id: 'workspace-b', name: 'Beta Co', timezone: 'UTC', created_at: stamp },
    ],
    memberships: ['workspace-a', 'workspace-b'].map((workspace_id) => ({
      workspace_id, account_id: self.accountId, users_id: self.usersId, role: 'owner', status: 'active', created_at: stamp,
    })),
  })
  await createFixtureApi(page, { collaborationStore, collaborationSelf: self })

  // 820-1100px is where the page-hero h1 scale overflowed a half-width
  // `.work-grid` column (Tasks next to Commitments); 790 is one column, 1440 a
  // wide desktop -- the two-up title size is the same at every one of them.
  for (const width of [790, 820, 900, 965, 1000, 1100, 1440]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`${baseURL}/work`)
    const titles = page.locator('.work-heading h1')
    await titles.first().waitFor()
    assert.equal(await titles.count(), 2, `expected the Tasks and Commitments titles two-up at ${width}px`)
    // Not below 900px: with a wide fallback font (Verdana) the Commitments *form*
    // column overflows by ~5px at 820px regardless of the headings.
    if (width >= 900) assert.equal(await horizontalOverflow(page), 0, `/work must not scroll horizontally at ${width}px`)
    const metrics = await titles.evaluateAll((els) => els.map((el) => {
      const style = getComputedStyle(el)
      return {
        fontSize: parseFloat(style.fontSize),
        // Rendered lines, and the h1's line-height as a multiple of its font size.
        lines: Math.round(el.getBoundingClientRect().height / parseFloat(style.lineHeight)),
        leading: parseFloat(style.lineHeight) / parseFloat(style.fontSize),
      }
    }))
    for (const { fontSize, lines, leading } of metrics) {
      assert.equal(fontSize, TWO_UP_TITLE_PX, `two-up workspace <h1> is ${fontSize}px at ${width}px; expected ${TWO_UP_TITLE_PX}px (--text-2xl)`)
      // `overflow-wrap: anywhere` keeps a too-wide title from overflowing, but by
      // breaking it mid-word; at these widths the two-up scale must fit on one line.
      // Only from 1000px: in the narrower two-up columns a wide fallback font (Verdana,
      // Georgia, or DejaVu on a Linux runner) breaks "Commitments" at 28px whatever the CSS.
      if (width >= 1000) assert.equal(lines, 1, `workspace <h1> wraps onto ${lines} lines at ${width}px (a title broken mid-word)`)
      // The bare `h1` rule's .94 leading crowds a wrapped title.
      assert.ok(leading >= 1, `workspace <h1> line-height is ${leading.toFixed(2)}x its font size; expected >= 1`)
    }
  }

  // A single-panel page's `<h1>` takes the panel-title scale, clamp(28px, 4vw, 44px) --
  // not the page-hero `h1` rule (up to 78px) it fell through to before `.work-heading h1`
  // joined that rule. Not two-up, so the two-up 28px override does not apply here.
  for (const width of [900, 1280]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`${baseURL}/notes`)
    const h1 = page.locator('.work-heading h1').first()
    await h1.waitFor()
    const size = await h1.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))
    const expected = Math.min(44, Math.max(28, 0.04 * width))
    assert.ok(Math.abs(size - expected) < 0.5, `single-panel workspace <h1> is ${size}px at ${width}px; expected the panel-title scale (${expected}px)`)
  }

  // `.work-heading h2` titles ("Resolution review", two-up on /knowledge) take the same
  // tight leading as the h1s (without it they inherit body's 1.55) and the same two-up size.
  await page.goto(`${baseURL}/knowledge`)
  await page.locator('.work-heading h2').first().waitFor()
  const h2Metrics = await page.locator('.work-heading h2').evaluateAll((els) => els.map((el) => {
    const style = getComputedStyle(el)
    return { leading: parseFloat(style.lineHeight) / parseFloat(style.fontSize), fontSize: parseFloat(style.fontSize) }
  }))
  assert.ok(h2Metrics.length > 0, 'expected .work-heading h2 titles on /knowledge')
  for (const { leading, fontSize } of h2Metrics) {
    assert.ok(leading < 1.2, `workspace <h2> line-height is ${leading.toFixed(2)}x its font size; expected the tight 1.05`)
    assert.equal(fontSize, TWO_UP_TITLE_PX, `two-up workspace <h2> is ${fontSize}px; expected ${TWO_UP_TITLE_PX}px (--text-2xl)`)
  }

  // Backstop: a long unbroken entity name must wrap inside its column, not push
  // the page sideways.
  await page.setViewportSize({ width: 1000, height: 900 })
  await page.goto(`${baseURL}/work`)
  await page.locator('.work-heading h1').first().waitFor()
  await page.locator('.work-heading h1').first().evaluate((el) => { el.textContent = 'Unbrokenentitynamethatkeepsgoing'.repeat(3) })
  assert.equal(await horizontalOverflow(page), 0, 'a long unbroken <h1> must wrap within its column, not overflow the page')

  // `overflow-wrap: anywhere` is scoped to `.work-heading`: beside its button,
  // `.recommendation-heading`'s title broke as "Recommendation" / "s" at 802-818px.
  await page.setViewportSize({ width: 810, height: 900 })
  await page.goto(`${baseURL}/recommendations`)
  const recommendationsTitle = page.locator('.recommendation-heading h2').first()
  await recommendationsTitle.waitFor()
  const wrapped = await recommendationsTitle.evaluate((el) => Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)))
  assert.equal(wrapped, 1, '810px: the Recommendations title must not break mid-word')
  await page.setViewportSize({ width: 1280, height: 900 })

  // The global WorkspaceSwitcher is a compact control, not a 680px form field
  // with an extra 28px of top margin stacked on its own padding.
  await page.goto(`${baseURL}/today`)
  const switcher = page.locator('div.workspace-switcher')
  await switcher.getByRole('combobox', { name: 'Workspace', exact: true }).waitFor()
  const switcherGeometry = await switcher.evaluate((el) => ({
    marginTop: getComputedStyle(el.querySelector('.field-form')).marginTop,
    selectWidth: el.querySelector('select').getBoundingClientRect().width,
    maxWidth: parseFloat(getComputedStyle(el).getPropertyValue('--content-select')),
  }))
  assert.equal(switcherGeometry.marginTop, '0px', 'the switcher must not inherit .field-form margin-top')
  assert.ok(switcherGeometry.selectWidth <= switcherGeometry.maxWidth, `the switcher select is ${switcherGeometry.selectWidth}px wide; expected <= ${switcherGeometry.maxWidth} (--content-select)`)

  // Search & Audit's two tabs sit beside the title in `.explore-heading`; wrapping
  // must not let that flex item shrink and stack them (521-1300px).
  for (const width of [600, 900, 1100]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`${baseURL}/search-audit`)
    const tabs = page.locator('.tab-list [role="tab"]')
    await tabs.first().waitFor()
    const tops = await tabs.evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().top)))
    assert.equal(new Set(tops).size, 1, `${width}px: Search & Audit tabs must share one row, got tops ${tops}`)
  }

  // Engineering has 10 tabs -- more than fit one row at any of these widths.
  for (const width of [900, 1280]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto(`${baseURL}/engineering`)
    const tabs = page.locator('.tab-list [role="tab"]')
    await tabs.first().waitFor()
    assert.ok(await tabs.count() > 5, 'expected the Engineering workspace to render its full tab set')
    const geometry = await tabs.evaluateAll((els) => {
      const boxes = els.map((el) => el.getBoundingClientRect())
      return {
        rows: new Set(boxes.map((box) => Math.round(box.top))).size,
        maxRight: Math.max(...boxes.map((box) => box.right)),
        viewport: document.documentElement.clientWidth,
      }
    })
    assert.ok(geometry.rows > 1, `${width}px: the tab list must wrap onto a second row, got ${geometry.rows}`)
    assert.ok(geometry.maxRight <= geometry.viewport, `${width}px: a tab sits past the viewport edge (${geometry.maxRight} > ${geometry.viewport})`)
    assert.equal(await horizontalOverflow(page), 0, `/engineering must not scroll horizontally at ${width}px`)
  }

  // .work-subsection: no card chrome, but real vertical separation.
  await page.setViewportSize({ width: 1280, height: 900 })
  await page.goto(`${baseURL}/attention`)
  const panel = page.locator('section[aria-labelledby="attention-title"]')
  await panel.getByRole('heading', { name: 'Attention queue', level: 1 }).waitFor()
  const subsections = panel.locator('.work-subsection')
  await subsections.first().waitFor()
  assert.ok(await subsections.count() >= 5, 'expected one .work-subsection per attention group')
  assert.equal(await panel.locator('.dashboard-card').count(), 0, 'a .work-panel must not nest a .dashboard-card')
  const chrome = await subsections.evaluateAll((els) => els.map((el) => {
    const style = getComputedStyle(el)
    return { border: style.borderTopWidth, shadow: style.boxShadow, marginTop: parseFloat(style.marginTop) }
  }))
  for (const { border, shadow, marginTop } of chrome) {
    assert.equal(border, '0px', '.work-subsection must not draw a border')
    assert.equal(shadow, 'none', '.work-subsection must not draw a shadow')
    assert.ok(marginTop > 0, '.work-subsection must separate itself from the block above with spacing')
  }
  // The default fixture has no attention items, so every group renders its
  // empty state -- assert it exists rather than skipping the check without one.
  const emptyState = panel.locator('.work-subsection .empty-state').first()
  await emptyState.waitFor()
  const align = await emptyState.evaluate((el) => getComputedStyle(el).textAlign)
  assert.notEqual(align, 'center', '.work-subsection .empty-state is left-aligned under its heading')

  // A bare direct-child <h2> in a `.work-subsection` (Meeting prep, Planner) has its
  // UA margins reset and stays larger than the <h3> sub-headings under it (Meeting
  // prep's Facts). Injected markup, as visual-snapshots.mjs does for classes the
  // default fixtures never render, into <main> (`.work-grid h3` resizes h3s in Attention).
  const bare = await page.evaluate(() => {
    const section = document.createElement('section')
    section.className = 'work-subsection'
    section.innerHTML = '<h2 id="probe-h2">Probe</h2><h3 id="probe-h3">Sub-heading</h3>'
    document.querySelector('#workspace-main').append(section)
    const h2 = getComputedStyle(document.getElementById('probe-h2'))
    return { fontSize: parseFloat(h2.fontSize), marginTop: h2.marginTop, h3FontSize: parseFloat(getComputedStyle(document.getElementById('probe-h3')).fontSize) }
  })
  assert.ok(bare.fontSize > bare.h3FontSize, `.work-subsection h2 (${bare.fontSize}px) must be larger than its h3 sub-headings (${bare.h3FontSize}px)`)
  assert.equal(bare.marginTop, '0px', 'a bare .work-subsection > h2 has no default top margin')
  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="attention-title"]' })
}
