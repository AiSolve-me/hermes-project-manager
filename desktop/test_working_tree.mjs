// Behavioural harness for the SHIPPED desktop plugin: plugins/project-manager/
// desktop/plugin.js — the one file the Desktop runtime loader actually
// evaluates. It is NOT a copy and not a split original; it is loaded through
// the SAME seam the loader uses (a data: URL module with `@hermes/plugin-sdk`
// and `react` resolved by module hooks), driven through the real
// `plugin.register(ctx)` entry point, and rendered through the ROUTES_AREA
// contribution register() hands back. Every assertion below therefore
// exercises the code that ships.
//
// What is real: react (the app's own singleton copy), react-dom/client under
// jsdom, the whole ProjectsPage → WorkingTreePanel tree, and the REST door
// bound by register(). What is stubbed: the SDK components/hooks, because the
// real ones are TSX with `@/` aliases.
//
// Fixtures below are verbatim `model_dump()` output of the pydantic models in
// dashboard/plugin_api.py (GitStatusResponse / GitDiffResponseDTO /
// FileOpResponseDTO / GitFetchResponseDTO).
import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const HERE = new URL('.', import.meta.url).pathname
const PLUGIN_JS = join(HERE, 'plugin.js')

// ── react singleton + jsdom, installed BEFORE react-dom loads ───────────────
const NM = '/usr/local/lib/hermes-agent/node_modules'
const { createRequire } = await import('node:module')
const require = createRequire(join(NM, 'noop.js'))
const { JSDOM } = require('jsdom')

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' })

globalThis.window = dom.window
globalThis.document = dom.window.document
// Node 22 ships a getter-only global navigator; define over it rather than assign.
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

// window.confirm is a jsdom no-op that returns false; the panel must never
// reach it (the SDK ConfirmDialog is the door), so make a silent call a failure.
let confirmCalls = 0

dom.window.confirm = msg => {
  confirmCalls += 1

  return false
}

const REACT_URL = new URL(`file://${NM}/react/index.js`).href
const { createElement, useEffect, useState, act } = await import(REACT_URL)

globalThis.__REACT__ = { createElement, useEffect, useState }

// ── the SDK stub, served to the plugin under test ──────────────────────────
const calls = []
let queryResult = { data: undefined, error: null, isPending: true }
let responder = () => ({})
let restLog = []
const queries = []

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

const SDK_STUB = `
const { createElement, useEffect, useState } = globalThis.__REACT__
const G = globalThis.__SDK__

export const cn = (...parts) => parts.filter(Boolean).join(' ')
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'

export const Button = props => {
  const { variant, size, ...rest } = props

  return createElement('button', { type: 'button', ...rest })
}

export const Badge = props => {
  const { variant, size, ...rest } = props

  return createElement('span', rest)
}

export const Codicon = props => {
  const { name, size, spinning, ...rest } = props

  return createElement('i', { ...rest, 'data-codicon': name })
}

// Enough of the real ConfirmDialog to prove the GATE: the op must be wired to
// the confirm button, never to the opening button.
export const ConfirmDialog = ({ open, onClose, onConfirm, title, description, confirmLabel, cancelLabel }) => {
  if (!open) return null

  return createElement(
    'div',
    { 'data-testid': 'confirm', role: 'dialog' },
    createElement('h2', null, title),
    createElement('p', { 'data-testid': 'confirm-description' }, description),
    createElement('button', { 'data-testid': 'confirm-accept', onClick: () => onConfirm() }, confirmLabel),
    createElement('button', { 'data-testid': 'confirm-cancel', onClick: () => onClose() }, cancelLabel)
  )
}

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
// the second key element tells the two apart. Only the panel's answer is
// scenario-controlled — the list always renders the same card so a project
// can be selected and the panel mounted.
export const useQuery = config => {
  G.queries.push(config)

  return Array.isArray(config.queryKey) && config.queryKey[1] === 'git-status'
    ? G.queryResult
    : { data: G.projects, error: null, isPending: false }
}

export const useQueryClient = () => G.client

// The gateway door the plugin uses to start a session in a project's folder.
// The harness records every call so a test can assert the cwd that was sent.
export const host = {
  request: async (method, params) => {
    const log = (G.hostCalls = G.hostCalls || [])
    log.push({ method: method, params: params })
    if (method === 'session.create') return { stored_session_id: 'stored-test' }
    return {}
  },
  openSession: async id => {
    const log = (G.hostCalls = G.hostCalls || [])
    log.push({ method: 'openSession', id: id })
  }
}
`

const SDK_URL = 'test:hermes-plugin-sdk-stub'
const reactUrl = new URL(REACT_URL).href

registerHooks({
  load(url, context, nextLoad) {
    if (url === SDK_URL) {
      return { format: 'module', source: SDK_STUB, shortCircuit: true }
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
    calls.push({ invalidateQueries: filter?.queryKey ?? null })
  }
}

globalThis.__SDK__ = {
  client,
  projects: PROJECTS,
  queries,
  get queryResult() {
    return queryResult
  },
  set queryResult(v) {
    queryResult = v
  }
}

// ── the shipped plugin.js, loaded through the loader's own seam ────────────
// A disk plugin is evaluated from a blob:/data: URL with no module base, so
// this is the only way it can run at all — and it is the same shape the
// Desktop runtime loader hands it.
const pluginSrc = readFileSync(PLUGIN_JS, 'utf8')
const pluginUrl = 'data:text/javascript;base64,' + Buffer.from(pluginSrc).toString('base64')
const mod = await import(pluginUrl)

// ── drive the REAL entry point: register(ctx) ──────────────────────────────
// The REST door and every contribution come from the plugin's own register(),
// so nothing below reaches past the shipped surface.
const contributions = []

const externalCalls = []

mod.default.register({
  os: {
    openExternal: url => {
      externalCalls.push(url)
      return Promise.resolve(true)
    }
  },
  onDispose: () => {},
  registerMany: items => contributions.push(...items),
  rest: (path, opts) => {
    restLog.push({ path, method: opts?.method ?? 'GET', body: opts?.body ?? null })

    return Promise.resolve(responder(path, opts))
  }
})

const route = contributions.find(c => c.area === 'routes')

if (!route) {
  console.error(`FATAL: register() contributed no ROUTES_AREA page (areas: ${contributions.map(c => c.area).join(', ')})`)
  process.exit(1)
}

// ── fixtures (pydantic model_dump output) ───────────────────────────────────
const STATUS_MIXED = {
  ahead: 2,
  behind: 0,
  branch: 'feat/project-manager-desktop-plugin',
  detached: false,
  has_upstream: true,
  last_commit_hash: '1cd80ec0b',
  last_commit_message: 'wip: working tree panel',
  modified_files: [
    {
      index_status: ' ',
      path: 'plugins/project-manager/desktop/working-tree.js',
      worktree_status: 'M'
    },
    { index_status: 'M', path: 'plugins/project-manager/desktop/api.js', worktree_status: 'M' },
    { index_status: 'D', path: 'README.md', worktree_status: ' ' }
  ],
  untracked_files: [{ path: 'plugins/project-manager/desktop/test_working_tree.mjs' }]
}

const DIFF_BINARY = {
  binary: true,
  filepath: 'assets/logo.png',
  message: '',
  staged_diff: '',
  success: true,
  truncated: false,
  worktree_diff: ''
}

const DIFF_TRUNCATED = {
  binary: false,
  filepath: 'dist/bundle.js',
  message: '',
  staged_diff:
    'diff --git a/dist/bundle.js b/dist/bundle.js\nindex 1111111..2222222 100644\n--- a/dist/bundle.js\n+++ b/dist/bundle.js\n@@ -1,3 +1,3 @@\n-old line\n+new line',
  success: true,
  truncated: true,
  worktree_diff: ''
}

const FILEOP_FAILURE = {
  deleted: false,
  message: 'discard failed: path escapes repo',
  stderr: 'fatal: ...',
  success: false
}

const FETCH_FAILURE = {
  message: 'fetch failed: no upstream configured',
  stderr: 'fatal',
  success: false
}

// ── render plumbing ─────────────────────────────────────────────────────────
const { createRoot } = await import(`file://${NM}/react-dom/client.js`)

let container = null
let root = null

// Render what register() contributed and select the project, so the panel
// under test is mounted through the shipped page — not imported by hand.
async function mount() {
  if (root) {
    await act(async () => root.unmount())
    container.remove()
  }

  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => root.render(route.render()))

  const card = container.querySelector('[data-project="tiboo00-hermes"]')

  assert.ok(card, 'the shipped page rendered no project card')
  await click(card)

  assert.ok(
    container.querySelector('section[aria-label="Working tree"]'),
    'selecting the project did not mount the working-tree panel'
  )
}

const flush = async () => {
  await act(async () => {
    await Promise.resolve()
    await new Promise(resolve => setTimeout(resolve, 0))
  })
}

const click = async element => {
  await act(async () => {
    element.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }))
  })
  await flush()
}

const byLabel = label => container.querySelector(`[aria-label="${label}"]`)
const html = () => container.innerHTML
const text = () => container.textContent

// The panel's own useQuery config — the last one keyed for git-status.
const panelQuery = () => [...queries].reverse().find(c => Array.isArray(c.queryKey) && c.queryKey[1] === 'git-status')

// plugin.js exports only `default`; these are the values the assertions below
// expect the shipped code to use, written out so a drift in plugin.js fails.
const GIT_STATUS_KEY = ['pm', 'git-status', 'tiboo00-hermes']
const PM_QUERY_ROOT = ['pm']

// ── the checks ──────────────────────────────────────────────────────────────
const results = []

async function scenario(name, fn) {
  calls.length = 0
  restLog = []
  confirmCalls = 0
  queries.length = 0
  globalThis.__SDK__.queryResult = { data: undefined, error: null, isPending: true }
  responder = () => ({})

  try {
    await fn()
    results.push(`ok  ${name}`)
  } catch (error) {
    results.push(`FAIL ${name}\n       ${error.stack.split('\n').slice(0, 4).join('\n       ')}`)
    process.exitCode = 1
  }
}

await scenario('mixed status renders BOTH groups and the real fetch wiring', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  await mount()

  const markup = html()
  for (const f of [...STATUS_MIXED.modified_files, ...STATUS_MIXED.untracked_files]) {
    assert.ok(markup.includes(f.path), `missing file row: ${f.path}`)
  }
  // The untracked group is its own row with a '?' badge, not folded in.
  assert.ok(markup.includes('test_working_tree.mjs'), 'untracked row missing')
  assert.ok(text().includes('feat/project-manager-desktop-plugin'), 'branch missing')
  assert.ok(text().includes('↑2'), 'ahead badge missing')

  // useQuery is handed the panel's REST call and the project's own key.
  const cfg = panelQuery()
  assert.ok(cfg, 'the panel never issued a git-status query')
  assert.deepEqual(cfg.queryKey, GIT_STATUS_KEY)
  responder = () => STATUS_MIXED
  await cfg.queryFn()
  assert.equal(restLog.at(-1).path, '/projects/tiboo00-hermes/git-status')
})

await scenario('Discard on a TRACKED file requests NOTHING before the confirm', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = () => ({ success: true, message: 'ok', stderr: '', deleted: false })
  await mount()

  const discard = byLabel('Discard changes in plugins/project-manager/desktop/api.js')
  assert.ok(discard, 'discard button missing for tracked file')

  await click(discard)

  assert.equal(restLog.length, 0, `the REST door was called before the confirm: ${JSON.stringify(restLog)}`)
  assert.equal(calls.length, 0, 'query cache was invalidated before the confirm')
  assert.equal(confirmCalls, 0, 'fell back to window.confirm instead of the SDK dialog')
  assert.ok(container.querySelector('[data-testid="confirm"]'), 'confirm dialog did not open')

  const copy = container.querySelector('[data-testid="confirm-description"]').textContent
  assert.ok(copy.includes('HEAD'), `confirm copy must name the HEAD restore: ${copy}`)
  assert.ok(copy.includes('lost forever'), `confirm copy must state the loss: ${copy}`)

  // Only the confirm button reaches the network.
  await click(container.querySelector('[data-testid="confirm-accept"]'))
  const post = restLog.find(c => c.path.includes('/git/discard'))
  assert.ok(post, 'confirm did not issue the discard POST')
  assert.equal(post.method, 'POST')
  assert.deepEqual(post.body, { filepath: 'plugins/project-manager/desktop/api.js' })
  // …and only THEN does the refresh happen, in the dashboard's order.
  assert.ok(restLog.some(c => c.path === '/projects/refresh'), 'backend /projects cache not busted')
  assert.ok(calls.some(c => Array.isArray(c.invalidateQueries)), 'queries not invalidated')
  assert.deepEqual(calls.at(-1).invalidateQueries, PM_QUERY_ROOT)
})

await scenario('Discard on an UNTRACKED file warns about DELETION', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  await mount()

  await click(byLabel('Discard changes in plugins/project-manager/desktop/test_working_tree.mjs'))

  assert.equal(restLog.length, 0, 'the REST door was called before the confirm')
  const copy = container.querySelector('[data-testid="confirm-description"]').textContent
  // A file in no commit cannot be restored from one, so the copy must say so
  // AND must not promise a HEAD restore — that would be the opposite of what
  // the backend does to a staged-new file.
  assert.ok(/in no commit/.test(copy), `untracked copy must say it is in no commit: ${copy}`)
  assert.ok(
    !/restores it from HEAD/.test(copy),
    `untracked copy must NOT promise a HEAD restore: ${copy}`
  )
})

await scenario('cancelling the confirm sends nothing', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  await mount()

  await click(byLabel('Discard changes in README.md'))
  await click(container.querySelector('[data-testid="confirm-cancel"]'))

  assert.equal(restLog.length, 0, 'cancel still hit the network')
  assert.ok(!container.querySelector('[data-testid="confirm"]'), 'dialog stayed open after cancel')
})

await scenario('Unstage is disabled when the file has no index change', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  await mount()

  const unstageable = byLabel('Unstage plugins/project-manager/desktop/api.js')
  assert.ok(unstageable, 'staged file must offer Unstage')
  assert.equal(unstageable.disabled, false)

  const notStaged = byLabel('plugins/project-manager/desktop/working-tree.js has nothing staged')
  assert.ok(notStaged, 'unstaged file must explain why Unstage is unavailable')
  assert.equal(notStaged.disabled, true)
})

await scenario('success:false renders the server message and does NOT refresh', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = () => FILEOP_FAILURE
  await mount()

  const before = text()
  await click(byLabel('Stage plugins/project-manager/desktop/api.js'))

  assert.ok(text().includes(FILEOP_FAILURE.message), 'server message not surfaced')
  const alert = container.querySelector('[role="alert"]')
  assert.ok(alert, 'no role=alert node; markup=' + html())
  assert.equal(alert.textContent, FILEOP_FAILURE.message)

  // The list must not be optimistically rewritten on a failed op.
  assert.ok(text().includes('plugins/project-manager/desktop/api.js'), 'row vanished on failure')
  assert.equal(text().replace(/\s+/g, ' ').length > 0, true)
  assert.equal(restLog.filter(c => c.path === '/projects/refresh').length, 0, 'refreshed after a failure')
  assert.equal(calls.length, 0, 'invalidated the cache after a failure')
  assert.ok(before.length > 0)
})

await scenario('gitFetch success:false surfaces its message', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = () => FETCH_FAILURE
  await mount()

  await click(container.querySelector('button[title^="Fetch from origin"]'))

  assert.ok(text().includes(FETCH_FAILURE.message), 'fetch message not surfaced')
  assert.equal(restLog.filter(c => c.path === '/projects/refresh').length, 0, 'refreshed after a failed fetch')
})

await scenario('a contract error body {"detail":…} / {"error":…} renders readably', async () => {
  // errText is not exported by plugin.js, so each shape is driven through the
  // panel's own error branch — the same rendering the user sees.
  for (const [thrown, expected] of [
    [new Error('409: {"detail":"project escapes namespace"}'), 'project escapes namespace'],
    [new Error('404: {"error":"no such project","code":404}'), 'no such project'],
    [new Error('plain failure'), 'plain failure']
  ]) {
    globalThis.__SDK__.queryResult = { data: undefined, error: thrown, isPending: false }
    await mount()

    assert.ok(text().includes(expected), `${expected} not rendered for ${thrown.message}`)
    assert.ok(!text().includes('{"'), `raw JSON leaked to the user for ${thrown.message}: ${text()}`)
  }
})

await scenario('binary diff says "binary file" and renders NO empty <pre>', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = path => (path.includes('/git/diff') ? DIFF_BINARY : { success: true, message: '', stderr: '' })
  await mount()

  await click(byLabel('Show diff for plugins/project-manager/desktop/api.js'))

  assert.ok(text().includes('Binary file'), 'binary notice missing')
  assert.equal(container.querySelectorAll('pre').length, 0, 'an empty <pre> was rendered for a binary file')
})

await scenario('truncated diff warns AND still shows the diff text', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = path => (path.includes('/git/diff') ? DIFF_TRUNCATED : { success: true, message: '', stderr: '' })
  await mount()

  await click(byLabel('Show diff for plugins/project-manager/desktop/api.js'))

  assert.ok(text().includes('Diff truncated'), 'truncation warning missing')
  assert.ok(text().includes('+new line'), 'the diff we did receive was dropped')
  assert.ok(text().includes('Staged'), 'staged section missing')
  assert.ok(text().includes('Working tree'), 'worktree section missing')
  // The git preamble is noise on a view already titled with the path.
  assert.ok(!text().includes('diff --git'), 'git preamble not stripped')
  assert.ok(!text().includes('index 1111111'), 'git index line not stripped')
  // Worktree section is present and honestly empty.
  assert.ok(text().includes('No changes.'), 'empty worktree section not labelled')
  const diffCall = restLog.find(c => c.path.includes('/git/diff'))
  assert.equal(diffCall.path, '/projects/tiboo00-hermes/git/diff?file=plugins%2Fproject-manager%2Fdesktop%2Fapi.js')
})

await scenario('Escape and the close button both dismiss the diff', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = path => (path.includes('/git/diff') ? DIFF_TRUNCATED : { success: true, message: '' })
  await mount()

  await click(byLabel('Show diff for plugins/project-manager/desktop/api.js'))
  assert.ok(container.querySelector('[role="dialog"]'), 'diff overlay did not open')

  await act(async () => {
    window.dispatchEvent(new dom.window.KeyboardEvent('keydown', { bubbles: true, key: 'Escape' }))
  })
  await flush()
  assert.ok(!container.querySelector('[role="dialog"]'), 'Escape did not dismiss the diff')

  await click(byLabel('Show diff for plugins/project-manager/desktop/api.js'))
  assert.ok(container.querySelector('[role="dialog"]'))
  await click(container.querySelector('[aria-label="Close diff"]'))
  assert.ok(!container.querySelector('[role="dialog"]'), 'close button did not dismiss the diff')
})

await scenario('a fetch rejecting mid-op shows the message and unblocks the row', async () => {
  globalThis.__SDK__.queryResult = { data: STATUS_MIXED, error: null, isPending: false }
  responder = path => {
    if (path.includes('/git/stage')) return Promise.reject(new Error('500: {"error":"git exploded","code":500}'))
    return { success: true, message: '', stderr: '' }
  }
  await mount()

  await click(byLabel('Stage plugins/project-manager/desktop/api.js'))

  assert.ok(text().includes('git exploded'), 'thrown error message not rendered')
  assert.equal(byLabel('Discard changes in README.md').disabled, false, 'rows stayed locked after a throw')
})

// ── report ──────────────────────────────────────────────────────────────────
if (root) {
  await act(async () => root.unmount())
}

console.log(results.join('\n'))
const failed = results.filter(r => r.startsWith('FAIL')).length

console.log(`\n${results.length - failed}/${results.length} scenarios passed`)
console.log(`window.confirm fallbacks: ${confirmCalls} (must be 0)`)
