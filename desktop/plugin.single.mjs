// Harness for the SINGLE-FILE project-manager Desktop plugin.
//
// The Desktop runtime loader evaluates a disk plugin.js from a BLOB: URL, so
// a relative import cannot resolve against the module base — and the loader
// refuses every specifier outside its four-key allowlist. This harness
// proves the built plugin.js survives that pipeline:
//
//   1. the loader's OWN import check runs against the file (not a copy of
//      it — the functions are extracted from the installed runtime-loader.ts
//      and executed, so the check cannot drift from the real one);
//   2. no dead relative specifier ('./api.js', './page.js', './working-tree')
//      survives anywhere in the text, comments included;
//   3. the module really EVALUATES as ESM through a data: URL, with 'react'
//      mapped to the app's real react singleton and '@hermes/plugin-sdk'
//      mapped to a stub (the real SDK is TSX behind '@/' aliases — the same
//      seam test_working_tree.mjs uses);
//   4. the default export is a valid HermesPlugin and register() contributes
//      a ROUTES_AREA page at '/projects';
//   5. that page RENDERS — the merged module's components actually mount and
//      paint, not just evaluate.
//
// Run from the desktop directory:
//     node plugin.single.mjs
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { join } from 'node:path'

const HERE = new URL('.', import.meta.url).pathname
const NM = '/usr/local/lib/hermes-agent/node_modules'
const DESKTOP_SRC = '/usr/local/lib/hermes-agent/apps/desktop/src'
const LOADER_TS = join(DESKTOP_SRC, 'contrib/runtime-loader.ts')
const RUNTIME_TS = join(DESKTOP_SRC, 'sdk/runtime.ts')
const PLUGIN_JS = join(HERE, 'plugin.js')

const results = []
let failed = 0

function check(name, fn) {
  try {
    fn()
    results.push(`  ok   ${name}`)
  } catch (error) {
    results.push(`  FAIL ${name}\n       ${error.message}`)
    failed++
    process.exitCode = 1
  }
}

async function checkAsync(name, fn) {
  try {
    await fn()
    results.push(`  ok   ${name}`)
  } catch (error) {
    results.push(`  FAIL ${name}\n       ${error.stack.split('\n').slice(0, 4).join('\n       ')}`)
    failed++
    process.exitCode = 1
  }
}

// ── the loader's own allowlist, read out of the installed sdk/runtime.ts ────
// sdkImportMap() is the ONLY thing that decides which specifiers a runtime
// plugin may import. It is not hardcoded here: the keys are parsed from the
// installed source, so a change upstream changes this harness.
function allowedSpecifiers() {
  const src = readFileSync(RUNTIME_TS, 'utf8')
  const start = src.indexOf('cached ??= {')
  const block = src.slice(start, src.indexOf('return cached'))
  const keys = []

  for (const m of block.matchAll(/^\s*(?:'([^']+)'|([A-Za-z_$][\w$]*))\s*:/gm)) keys.push(m[1] ?? m[2])

  assert.ok(keys.length > 0, 'could not parse sdkImportMap() out of sdk/runtime.ts')

  return keys
}

// ── the loader's own import check, executed (not re-implemented) ───────────
// unsupportedImports() / codeRanges() / importSpecifierRe() / inCode() are
// module-private in runtime-loader.ts, so the source between the specifier
// regex and the first exported function is lifted verbatim and transpiled
// with the app's own esbuild. The only change is the sdkImportMap() stub —
// the real one hands out live blob URLs, this one the same KEYS with inert
// values, which is all the check reads.
async function loadLoaderCheck(allowed) {
  const esbuild = await import('file:///usr/local/lib/hermes-agent/node_modules/esbuild/lib/main.js')
  const src = readFileSync(LOADER_TS, 'utf8')
  const start = src.indexOf('const importSpecifierRe = () =>')
  const end = src.indexOf('export function unloadRuntimePlugin')

  assert.ok(start > 0 && end > start, 'could not locate the import check in runtime-loader.ts')

  const slice = src.slice(start, end)

  assert.ok(!/^\s*import\s/m.test(slice), 'the lifted slice is not self-contained')

  const module = `
const sdkImportMap = () => (${JSON.stringify(Object.fromEntries(allowed.map(k => [k, 'blob:' + k])))})
${slice}
export { unsupportedImports, codeRanges, importSpecifierRe, inCode }
`

  const { code } = await esbuild.transform(module, { loader: 'ts', format: 'esm' })
  const url = 'data:text/javascript;base64,' + Buffer.from(code).toString('base64')

  return import(url)
}

// ── the SDK stub, served to the plugin under test ──────────────────────────
// Mirrors test_working_tree.mjs: the real SDK is TSX behind '@/' aliases, so
// the primitives are stubbed. The AREA constants are the REAL values, read
// from the installed app/routes.ts — a wrong area id must fail here.
function sdkStub() {
  const routes = readFileSync(join(DESKTOP_SRC, 'app/routes.ts'), 'utf8')
  const area = name => {
    const m = new RegExp(`export const ${name} = '([^']+)'`).exec(routes)

    return m ? m[1] : name
  }

  return `
const { createElement, useEffect, useState } = globalThis.__REACT__
const G = globalThis.__SDK__

export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const ROUTES_AREA = ${JSON.stringify(area('ROUTES_AREA'))}
export const SIDEBAR_NAV_AREA = ${JSON.stringify(area('SIDEBAR_NAV_AREA'))}

export const Badge = props => {
  const { variant, size, ...rest } = props

  return createElement('span', rest)
}

export const Button = props => {
  const { variant, size, ...rest } = props

  return createElement('button', { type: 'button', ...rest })
}

export const Codicon = props => {
  const { name, size, spinning, style, ...rest } = props

  return createElement('i', { ...rest, 'data-codicon': name })
}

export const ConfirmDialog = ({ open, onClose, onConfirm, title, description, confirmLabel, cancelLabel }) => {
  if (!open) return null

  return createElement(
    'div',
    { 'data-testid': 'confirm', role: 'dialog' },
    createElement('h2', null, title),
    createElement('p', { 'data-testid': 'confirm-description' }, description),
    createElement('button', { 'data-testid': 'confirm-accept', onClick: () => onConfirm() }, confirmLabel),
    createElement('button', { 'data-testid': 'confirm-cancel', onClick: onClose }, cancelLabel)
  )
}

// The real relativeTime() is Intl-based; the page's own "ago" formatting
// depends on its exact shape, so the stub is Intl too.
const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' })

export const relativeTime = (targetMs, nowMs = Date.now()) => {
  const diff = targetMs - nowMs
  const abs = Math.abs(diff)
  const sign = diff < 0 ? -1 : 1

  if (abs < 60000) return rtf.format(sign * Math.round(abs / 1000), 'second')
  if (abs < 3600000) return rtf.format(sign * Math.round(abs / 60000), 'minute')
  if (abs < 86400000) return rtf.format(sign * Math.round(abs / 3600000), 'hour')

  return rtf.format(sign * Math.round(abs / 86400000), 'day')
}

// The page queries ['pm','projects',…] and the panel ['pm','git-status',…];
// the key tells the two apart.
export const useQuery = config => {
  G.queries.push(config)

  return G.resultFor(config.queryKey)
}

export const useQueryClient = () => G.client
`
}

// ── fixtures ───────────────────────────────────────────────────────────────
const PROJECTS = {
  page_count: 1,
  projects: [
    {
      ahead: 2,
      behind: 0,
      branch_count: 3,
      current_branch: 'feat/project-manager-desktop-plugin',
      is_dirty: true,
      last_commit_date: '2026-10-18T09:12:00Z',
      last_commit_message: 'feat(project-manager): desktop per-file working-tree panel',
      name: 'tiboo00-hermes',
      remote_url: 'git@github.com:tiboo00/hermes.git'
    }
  ],
  total: 1,
  total_filtered: 1
}

const STATUS = {
  ahead: 2,
  behind: 0,
  branch: 'feat/project-manager-desktop-plugin',
  modified_files: [
    { index_status: ' ', path: 'plugins/project-manager/desktop/working-tree.js', worktree_status: 'M' },
    { index_status: 'M', path: 'plugins/project-manager/desktop/api.js', worktree_status: 'M' }
  ],
  untracked_files: [{ path: 'plugins/project-manager/desktop/plugin.single.mjs' }]
}

// ── jsdom + react, installed BEFORE react-dom loads ───────────────────────
const require = createRequire(join(NM, 'noop.js'))
const { JSDOM } = require('jsdom')

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' })

globalThis.window = dom.window
globalThis.document = dom.window.document
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: dom.window.navigator,
  writable: true
})
globalThis.HTMLElement = dom.window.HTMLElement
globalThis.Element = dom.window.Element
globalThis.Node = dom.window.Node
globalThis.Event = dom.window.Event
globalThis.MouseEvent = dom.window.MouseEvent
globalThis.KeyboardEvent = dom.window.KeyboardEvent
globalThis.requestAnimationFrame = cb => setTimeout(cb, 0)
globalThis.cancelAnimationFrame = id => clearTimeout(id)
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const REACT_URL = new URL(`file://${NM}/react/index.js`).href
const { createElement, useEffect, useState, act } = await import(REACT_URL)

globalThis.__REACT__ = { createElement, useEffect, useState }

const SDK_URL = 'test:hermes-plugin-sdk-stub'
const reactUrl = new URL(REACT_URL).href

registerHooks({
  load(url, context, nextLoad) {
    if (url === SDK_URL) {
      return { format: 'module', source: sdkStub(), shortCircuit: true }
    }

    return nextLoad(url, context)
  },
  resolve(specifier, context, nextResolve) {
    if (specifier === '@hermes/plugin-sdk') {
      return { shortCircuit: true, url: SDK_URL }
    }

    // The runtime loader maps react to the app's singleton; same seam here.
    if (specifier === 'react') {
      return { shortCircuit: true, url: reactUrl }
    }

    return nextResolve(specifier, context)
  }
})

const client = {
  invalidateQueries: async filter => {
    client.invalidations.push(filter?.queryKey ?? null)
  },
  invalidations: []
}

globalThis.__SDK__ = {
  client,
  queries: [],
  resultFor(key) {
    if (Array.isArray(key) && key[1] === 'git-status') {
      return { data: STATUS, error: null, isPending: false }
    }

    return { data: PROJECTS, error: null, isPending: false }
  }
}

// ── the plugin source ─────────────────────────────────────────────────────
const pluginSrc = readFileSync(PLUGIN_JS, 'utf8')
const allowed = allowedSpecifiers()
const loader = await loadLoaderCheck(allowed)

// ── 1. the loader's own import check ───────────────────────────────────────
const unsupported = loader.unsupportedImports(pluginSrc)
const ranges = loader.codeRanges(pluginSrc)
const found = []

for (const m of pluginSrc.matchAll(loader.importSpecifierRe())) {
  if (m[3] && loader.inCode(ranges, m.index ?? 0) && !found.includes(m[3])) found.push(m[3])
}

check('the loader\'s own import check accepts every specifier', () => {
  assert.deepEqual(unsupported, [], `unsupported import(s): ${unsupported.join(', ')}`)
  assert.ok(found.length > 0, 'no import specifier found at all — the check never ran')
})

check('every specifier is in the loader allowlist', () => {
  for (const spec of found) {
    assert.ok(allowed.includes(spec), `specifier not in the allowlist: ${spec}`)
  }
})

// ── 2. no dead relative specifier survives ─────────────────────────────────
check('no dead relative specifier survives in the file', () => {
  for (const dead of ['./api.js', './page.js', './working-tree']) {
    assert.ok(!pluginSrc.includes(dead), `dead relative specifier still in the file: ${dead}`)
  }
})

// ── 3. the module really evaluates ────────────────────────────────────────
const pluginUrl = 'data:text/javascript;base64,' + Buffer.from(pluginSrc).toString('base64')
const mod = await import(pluginUrl)

check('the module evaluates and default-exports a plugin object', () => {
  assert.ok(mod.default, 'no default export')
  assert.equal(typeof mod.default, 'object')
})

check('the default export is a valid HermesPlugin', () => {
  assert.equal(mod.default.defaultEnabled, false)
  assert.equal(typeof mod.default.register, 'function')
  assert.equal(typeof mod.default.name, 'string')
  assert.equal(typeof mod.default.description, 'string')
})

// ── 4. register() contributes the route + the nav row ──────────────────────
const contributions = []
const disposers = []
const restCalls = []

mod.default.register({
  onDispose: fn => disposers.push(fn),
  registerMany: items => contributions.push(...items),
  rest: (path, opts) => {
    restCalls.push({ path, method: opts?.method ?? 'GET', body: opts?.body ?? null })

    return Promise.resolve({ success: true, message: '', stderr: '' })
  }
})

check('register() contributes a ROUTES_AREA page at /projects', () => {
  const page = contributions.find(c => c.area === 'routes')

  assert.ok(page, `no ROUTES_AREA contribution (areas: ${contributions.map(c => c.area).join(', ')})`)
  assert.equal(page.data.path, '/projects')
  assert.equal(page.id, 'page')
  assert.equal(typeof page.render, 'function')
})

check('register() contributes the sidebar nav row', () => {
  const nav = contributions.find(c => c.area === 'sidebar.nav')

  assert.ok(nav, 'no SIDEBAR_NAV_AREA contribution')
  assert.equal(nav.data.path, '/projects')
  assert.equal(nav.data.label, 'Projects')
})

check('register() installs a disposer', () => {
  assert.equal(disposers.length, 1, 'expected exactly one disposer')
})

// ── 5. the page renders ────────────────────────────────────────────────────
const { createRoot } = await import(`file://${NM}/react-dom/client.js`)

const container = document.createElement('div')
document.body.appendChild(container)
const root = createRoot(container)
const route = contributions.find(c => c.area === 'routes')

await act(async () => {
  root.render(route.render())
})

check('the merged page renders the project list', () => {
  const text = container.textContent

  assert.ok(text.includes('tiboo00-hermes'), `project name missing from the render: ${text.slice(0, 200)}`)
  assert.ok(text.includes('feat/project-manager-desktop-plugin'), 'branch missing')
  assert.ok(text.includes('dirty'), 'dirty badge missing')
  assert.ok(text.includes('1 project'), 'project count missing')
})

check('the page queried the projects endpoint through the REST door', () => {
  const list = globalThis.__SDK__.queries.find(q => Array.isArray(q.queryKey) && q.queryKey[1] === 'projects')

  assert.ok(list, 'the page never issued its projects query')
  assert.deepEqual(list.queryKey, ['pm', 'projects', '', 1])
  assert.equal(list.queryFn instanceof Function, true)
})

// Click the card → the working-tree panel (inlined from working-tree.js)
// mounts and paints the file rows.
const card = container.querySelector('[data-project="tiboo00-hermes"]')

await act(async () => {
  card.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }))
})

await act(async () => {
  await new Promise(resolve => setTimeout(resolve, 0))
})

check('selecting a project renders the inlined working-tree panel', () => {
  const text = container.textContent

  assert.ok(text.includes('Working tree — tiboo00-hermes'), 'working tree section missing')
  assert.ok(text.includes('plugins/project-manager/desktop/working-tree.js'), 'modified file row missing')
  assert.ok(text.includes('plugins/project-manager/desktop/plugin.single.mjs'), 'untracked file row missing')
})

check('the panel queried git-status through the REST door', () => {
  const status = globalThis.__SDK__.queries.find(q => Array.isArray(q.queryKey) && q.queryKey[1] === 'git-status')

  assert.ok(status, 'the panel never issued its git-status query')
  assert.deepEqual(status.queryKey, ['pm', 'git-status', 'tiboo00-hermes'])
})

// ── the badge invariant: a panel mutation must reach the LIST's query ──────
// The list's dirty/ahead/behind badges are read from the /projects payload
// the panel's mutation also invalidates. If the list's query key is not
// under the root the panel invalidates, the badges keep showing pre-mutation
// state. The split sources key the list under 'project-manager' and the
// panel under 'pm' — this is the check that catches that split.
const stage = container.querySelector('[aria-label="Stage plugins/project-manager/desktop/api.js"]')

assert.ok(stage, 'stage button missing from the panel')

await act(async () => {
  stage.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }))
})

await act(async () => {
  await new Promise(resolve => setTimeout(resolve, 0))
})

check('a panel mutation invalidates the list query (badges stay fresh)', () => {
  const list = globalThis.__SDK__.queries.find(q => Array.isArray(q.queryKey) && q.queryKey[1] === 'projects')
  const mutation = client.invalidations.find(key => Array.isArray(key) && key[0] === 'pm')

  assert.ok(mutation, `the mutation invalidated nothing under the shared root: ${JSON.stringify(client.invalidations)}`)
  assert.deepEqual(mutation, ['pm'])
  assert.equal(
    list.queryKey.slice(0, mutation.length).join('|'),
    mutation.join('|'),
    `the list query ${JSON.stringify(list.queryKey)} is not under the invalidated root ${JSON.stringify(mutation)}`
  )
})

// ── control: the seam would catch a relative import ────────────────────────
// A data: URL module has no base to resolve './x.js' against — the same
// failure the loader's blob: base causes. If this control ever resolves,
// the import check above has stopped proving anything.
await checkAsync('CONTROL: a relative import cannot resolve against this base', async () => {
  const relative = "import './api.js'\nexport default 1"

  await assert.rejects(
    () => import('data:text/javascript;base64,' + Buffer.from(relative).toString('base64')),
    /Cannot find module|ERR_MODULE_NOT_FOUND|Invalid relative URL|does not provide an export/i
  )
})

await act(async () => {
  root.unmount()
})

// ── report ─────────────────────────────────────────────────────────────────
console.log('allowed specifiers (from sdk/runtime.ts sdkImportMap()):')
for (const spec of allowed) console.log(`  - ${spec}`)
console.log(`specifiers found in plugin.js (loader check): ${found.join(', ')}`)
console.log('')
console.log(results.join('\n'))
console.log(`\n${results.length - failed}/${results.length} checks passed`)
