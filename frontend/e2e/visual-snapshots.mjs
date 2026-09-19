import { mkdir, readdir, readFile } from 'node:fs/promises'
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
          const main = document.querySelector('#workspace-main')
          if (!main) throw new Error('#workspace-main not found; cannot inject the token swatch')
          main.insertAdjacentHTML('beforeend', html)
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
