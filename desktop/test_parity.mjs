// Behavioural parity harness for the NEW Desktop UI in
// plugins/project-manager/desktop/plugin.js — the ~940 lines that added
// Push, Stash-all, the commit form, the watch-folder manager (add / remove /
// rename / reorder / autocomplete), Pull-all, the Branches + Recent-commits
// sections and the whole Worktrees & branches block.
//
// plugin.single.mjs proves the LOADER contract and the list; test_working_tree
// .mjs proves the per-file stage/unstage/discard/diff panel. Between them they
// never click a Push, never render a worktree row, never type a commit
// message and never add a watch folder — so a green suite said nothing about
// any of the new code. This file is the one that does.
//
// Same seam as plugin.single.mjs, verbatim: the shipped plugin.js is loaded
// through a `data:text/javascript;base64,` URL (the Desktop runtime loader
// evaluates a disk plugin from a BLOB: URL — a relative import cannot resolve
// against a module base), `@hermes/plugin-sdk` is mapped to a stub by
// registerHooks, `react` is mapped to the app's own singleton, and the page is
// rendered through the ROUTES_AREA contribution register() hands back. Nothing
// here reaches past the shipped surface: every request goes through the REST
// door the plugin bound itself.
//
// Real: react (the app's singleton copy), react-dom/client under jsdom, the
// whole ProjectsPage → ProjectCard → WorkingTreePanel → WorktreesSection tree.
// Stubbed: the SDK components/hooks, because the real ones are TSX behind `@/`
// aliases — and the backend, which is answered by a recorded responder.
//
// Every assertion is on an OBSERVED request or an OBSERVED piece of rendered
// text. A source grep proves nothing about what runs.
//
// Run from the desktop directory:
//     node test_parity.mjs
// Point it at another build of the plugin (used for mutation testing):
//     PM_PLUGIN_JS=/path/to/plugin.js node test_parity.mjs
//     node test_parity.mjs /path/to/plugin.js
import assert from 'node:assert/strict'
import { createRequire, registerHooks } from 'node:module'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const HERE = new URL('.', import.meta.url).pathname
const NM = '/usr/local/lib/hermes-agent/node_modules'
const PLUGIN_JS = process.env.PM_PLUGIN_JS || process.argv[2] || join(HERE, 'plugin.js')

// ── react singleton + jsdom, installed BEFORE react-dom loads ───────────────
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

// The Electron renderer has NO window.prompt. A plugin that used it would
// read a null message and silently do nothing, so a prompt call is a failure.
let promptCalls = 0

dom.window.prompt = () => {
  promptCalls += 1

  return null
}

const REACT_URL = new URL(`file://${NM}/react/index.js`).href
const { createElement, useEffect, useState, act } = await import(REACT_URL)

globalThis.__REACT__ = { createElement, useEffect, useState }

// ── the SDK stub, served to the plugin under test ──────────────────────────
// The real SDK is TSX behind `@/` aliases, so the primitives are stubbed. The
// useQuery stub is a MINI query client: it calls the plugin's own queryFn
// (so the REST door really is hit) and re-renders with the answer, which is
// what react-query does. Without that, a section could never SHOW the data
// its route returned.
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
  const { name, size, spinning, style, ...rest } = props

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
    createElement('button', { 'data-testid': 'confirm-cancel', onClick: onClose }, cancelLabel)
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

// A mini query client: fire the plugin's queryFn, hold the answer, re-render.
// globalThis.__SDK__ is read lazily — the stub module is evaluated while the
// plugin module is imported, which is before the harness finishes wiring up.
export const useQuery = config => {
  const log = globalThis.__SDK__.queries
  // Bounded: the stub records every render's config, so a render loop
  // would otherwise grow this array until the process is OOM-killed --
  // which hides the assertion that should have failed.
  log.push(config)
  if (log.length > 200) log.shift()

  // A render loop shows up here as the same query asked for over and over.
  // Fail loudly with the key instead of letting the process eat 50 GB.
  const loopKey = JSON.stringify(config.queryKey)
  const counts = (globalThis.__SDK__.queryCounts = globalThis.__SDK__.queryCounts || {})
  counts[loopKey] = (counts[loopKey] || 0) + 1
  if (counts[loopKey] > 40) {
    throw new Error('RENDER LOOP: query ' + loopKey + ' asked for ' + counts[loopKey] + ' times')
  }
  const key = JSON.stringify(config.queryKey)
  const [result, setResult] = useState({ data: undefined, error: null, isPending: true, isFetching: false })

  useEffect(() => {
    let live = true

    setResult(prev => ({ ...prev, isPending: true, isFetching: true }))
    Promise.resolve()
      .then(() => config.queryFn())
      .then(
        data => {
          if (live) setResult({ data, error: null, isPending: false, isFetching: false })
        },
        error => {
          if (live) setResult({ data: undefined, error, isPending: false, isFetching: false })
        }
      )

    return () => {
      live = false
    }
  }, [key])

  return { ...result, refetch: () => config.queryFn() }
}

export const useQueryClient = () => G.client

// The gateway door the plugin uses to start a session in a project's folder.
// The harness records every call so a test can assert the cwd that was sent.
export const host = {
  request: async (method, params) => {
    const log = (G.hostCalls = G.hostCalls || [])
    log.push({ method: method, params: params })
    if (method === 'session.create') {
      // A scenario can make the gateway refuse, to prove a failed session
      // create lands on the card that asked for it.
      if (G.hostReject) throw new Error(G.hostReject)
      return { stored_session_id: 'stored-test' }
    }
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

// ── fixtures (pydantic model_dump output) ───────────────────────────────────
const PROJECTS = {
  page_count: 1,
  projects: [
    {
      ahead: 0,
      behind: 0,
      branch_count: 2,
      current_branch: 'main',
      is_dirty: false,
      last_commit_date: '2026-10-20T09:12:00Z',
      last_commit_message: 'chore: alpha housekeeping',
      name: 'alpha',
      // The backend sends the folder the project lives in; the Chat button
      // aims the new session at it.
      path: '/repos/alpha',
      remote_url: 'git@github.com:tiboo00/alpha.git'
    },
    {
      ahead: 3,
      behind: 1,
      branch_count: 4,
      current_branch: 'feat/parity',
      is_dirty: true,
      last_commit_date: '2026-10-20T10:12:00Z',
      last_commit_message: 'feat: beta feature work',
      name: 'beta',
      path: '/repos/beta',
      remote_url: 'git@github.com:tiboo00/beta.git'
    }
  ],
  total: 2,
  total_filtered: 2
}

const STATUS = {
  ahead: 3,
  behind: 1,
  branch: 'feat/parity',
  detached: false,
  has_upstream: true,
  last_commit_hash: 'abc1234',
  last_commit_message: 'feat: beta feature work',
  modified_files: [
    { index_status: ' ', path: 'src/app.js', worktree_status: 'M' },
    { index_status: 'M', path: 'src/lib.js', worktree_status: 'M' }
  ],
  untracked_files: [{ path: 'src/new.js' }]
}

// GET /projects/{name}/branches — a BARE list.
const PROJECT_BRANCHES = [
  { name: 'main', is_current: false },
  { name: 'release/2026', is_current: true }
]

// GET /projects/{name}/commits — {commits: CommitItem[]}.
const COMMITS = {
  commits: [
    {
      author: 'Ada',
      date: '2026-10-20T10:12:00Z',
      hash: 'abc1234def5678',
      message: 'parity: cover the worktree section'
    }
  ]
}

// GET /projects/{name}/worktrees — {worktrees, current_worktree_id}.
const WORKTREES = {
  current_worktree_id: 'wt_aaa11111',
  worktrees: [
    {
      branch: 'main',
      head: 'abc1234',
      id: 'wt_aaa11111',
      is_current: true,
      is_dirty: false,
      is_locked: false,
      is_protected: true,
      lock_reason: '',
      path: '/repos/alpha'
    },
    {
      branch: 'feature/locked',
      head: 'def5678',
      id: 'wt_bbb22222',
      is_current: false,
      is_dirty: true,
      is_locked: true,
      is_protected: false,
      lock_reason: 'in progress',
      path: '/repos/alpha-locked'
    },
    {
      branch: 'release/clean',
      head: 'aaa1111',
      id: 'wt_ccc33333',
      is_current: false,
      is_dirty: false,
      is_locked: false,
      is_protected: false,
      lock_reason: '',
      path: '/repos/alpha-clean'
    },
    {
      branch: 'hotfix/dirty',
      head: 'bbb2222',
      id: 'wt_ddd44444',
      is_current: false,
      is_dirty: true,
      is_locked: false,
      is_protected: false,
      lock_reason: '',
      path: '/repos/alpha-dirty'
    }
  ]
}

// GET /projects/{name}/worktrees/branches — the ACTIVE worktree's branches.
const WORKTREE_BRANCHES = {
  branches: [
    { is_current: true, is_remote: false, name: 'wt-main', upstream: null },
    { is_current: false, is_remote: false, name: 'wt-feature', upstream: null },
    { is_current: false, is_remote: false, name: 'wt-other', upstream: null }
  ],
  current_branch: 'wt-main'
}

// GET /watch-paths — WatchPathListResponse.
const WATCH_PATHS = {
  total: 2,
  watch_paths: [
    {
      created_at: '2026-10-01T08:00:00Z',
      enabled: true,
      id: 1,
      label: 'Alpha repos',
      path: '/repos/alpha',
      type: 'scan_root'
    },
    {
      created_at: '2026-10-02T08:00:00Z',
      enabled: true,
      id: 2,
      label: null,
      path: '/repos/beta',
      type: 'single_repo'
    }
  ]
}

// The contract error the worktree routes answer with — the REST door re-throws
// it as `400: {json}`, and the code is the actionable half.
const WORKTREE_DIRTY_ERROR =
  '400: {"error":"Cannot checkout: uncommitted changes on worktree \'/repos/alpha\'.","code":"WORKTREE_DIRTY"}'

// ── the REST door ──────────────────────────────────────────────────────────
const client = {
  invalidations: [],
  invalidateQueries: async filter => {
    client.invalidations.push(filter?.queryKey ?? null)
  }
}

const restLog = []
const queries = []
// Per-scenario overrides: key is "METHOD /path", value is a payload or a
// function (path, opts) => payload. A function may throw to model a refusal.
const stubs = new Map()

function defaultResponder(path, opts) {
  const method = opts?.method ?? 'GET'

  if (method === 'GET') {
    if (path.startsWith('/projects?')) return PROJECTS
    if (path.endsWith('/git-status')) return STATUS
    if (path.includes('/worktrees/branches')) return WORKTREE_BRANCHES
    if (path.endsWith('/worktrees')) return WORKTREES
    if (path.endsWith('/branches')) return PROJECT_BRANCHES
    if (path.endsWith('/commits')) return COMMITS
    if (path === '/watch-paths') return WATCH_PATHS
    if (path.startsWith('/watch-paths/suggest')) return { suggestions: [] }

    return {}
  }

  if (path.endsWith('/git/fetch')) return { message: 'Fetched from origin', stderr: '', success: true }
  if (path.endsWith('/git-push')) return { message: 'Pushed to origin', stderr: '', success: true }
  if (path.endsWith('/git-pull')) return { added_commits: 0, message: 'up to date', stderr: '', success: true }
  if (path.endsWith('/git-commit')) return { message: 'committed', push: null, stderr: '', success: true }
  if (path.endsWith('/git-stash')) return { message: 'stashed', stderr: '', success: true }
  if (path.endsWith('/git/discard')) return { deleted: false, message: 'discarded', stderr: '', success: true }
  if (path.endsWith('/git/stage')) return { deleted: false, message: 'staged', stderr: '', success: true }
  if (path === '/projects/pull-all') {
    return {
      already_up_to_date: 2,
      dirty_skipped: 0,
      diverged: 0,
      failed: 0,
      pulled: 1,
      results: [],
      success: true,
      total: 3
    }
  }
  if (path.startsWith('/watch-paths')) return { deleted: true, id: 1, label: 'x', renamed: true, reordered: true }
  if (path === '/projects/refresh') return { success: true }

  return { message: 'ok', stderr: '', success: true }
}

function responder(path, opts) {
  const method = opts?.method ?? 'GET'
  const key = `${method} ${path}`

  if (stubs.has(key)) {
    const stub = stubs.get(key)

    return typeof stub === 'function' ? stub(path, opts) : stub
  }

  return defaultResponder(path, opts)
}

// ── the shipped plugin.js, loaded through the loader's own seam ────────────
// globalThis.__SDK__ is wired BEFORE the import: the plugin imports
// '@hermes/plugin-sdk' while it is evaluated, and the stub captures the
// globals at ITS evaluation time.
globalThis.__SDK__ = { client, queries }
// The same object the SDK stub closes over, so a scenario can read the calls
// the plugin made to the gateway door.
const G = globalThis.__SDK__

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
      // The real door RESOLVES false when the shell cannot open the URL —
      // it never throws. A scenario flips this to prove the refusal is
      // reported on the card instead of passing for a success.
      return Promise.resolve(!G.openExternalFails)
    }
  },
  onDispose: () => {},
  registerMany: items => contributions.push(...items),
  rest: (path, opts) => {
    restLog.push({ path, method: opts?.method ?? 'GET', body: opts?.body ?? null })

    try {
      return Promise.resolve(responder(path, opts))
    } catch (error) {
      return Promise.reject(error)
    }
  }
})

const route = contributions.find(c => c.area === 'routes')

if (!route) {
  console.error(`FATAL: register() contributed no ROUTES_AREA page (areas: ${contributions.map(c => c.area).join(', ')})`)
  process.exit(1)
}

// ── render plumbing ─────────────────────────────────────────────────────────
const { createRoot } = await import(`file://${NM}/react-dom/client.js`)

let container = null
let root = null

const flush = async (ms = 0) => {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, ms))
  })
}

const click = async element => {
  assert.ok(element, 'click() got no element')
  await act(async () => {
    element.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }))
  })
  await flush()
}

const type = async (input, value) => {
  assert.ok(input, 'type() got no input')
  // React tracks the DOM value on the element instance, so the native setter
  // has to be used to make the change visible to onChange.
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set

  await act(async () => {
    setter.call(input, value)
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  })
  await flush()
}

const choose = async (select, value) => {
  assert.ok(select, 'choose() got no select')
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, 'value').set

  await act(async () => {
    setter.call(select, value)
    select.dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  })
  await flush()
}

const html = () => container.innerHTML
const text = () => container.textContent
const byLabel = label => container.querySelector(`[aria-label="${label}"]`)
const byText = (label, root = container) =>
  [...root.querySelectorAll('button')].find(button => button.textContent.trim() === label)
// The repository link (the project NAME) and the branch link (the branch chip)
// of a card. Both are spans, not buttons, so they are found by their
// attribute — which carries the project they belong to.
const remoteLink = name => container.querySelector(`[data-remote-link="${name}"]`)
const branchLink = name => container.querySelector(`[data-branch-link="${name}"]`)
const cardOf = name => container.querySelector(`[data-card="${name}"]`)

// The Disclosure blocks (Branches / Recent commits / Worktrees & branches)
// render their children only when open, so a section's contents are asserted
// after its header was clicked open.
const openDisclosure = async label => {
  const section = [...container.querySelectorAll('section')].find(
    node => node.firstElementChild && node.firstElementChild.tagName === 'BUTTON' && node.textContent.includes(label)
  )

  assert.ok(section, `no disclosure labelled "${label}"`)
  await click(section.firstElementChild)

  return section
}

async function mount() {
  if (root) {
    await act(async () => root.unmount())
    container.remove()
  }

  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  await act(async () => root.render(route.render()))
  await flush()
}

const selectProject = async name => {
  const card = container.querySelector(`[data-project="${name}"]`)

  assert.ok(card, `no card for project ${name}`)
  await click(card)
  assert.ok(container.querySelector(`[data-detail="${name}"]`), `selecting ${name} did not mount its panel`)
}

const callsTo = (method, path) => restLog.filter(call => call.method === method && call.path === path)

// ── the checks ──────────────────────────────────────────────────────────────
const results = []

async function scenario(name, fn) {
  restLog.length = 0
  // The render-loop guard counts per query key; the keys repeat across
  // scenarios, so the counter is per scenario.
  if (globalThis.__SDK__) globalThis.__SDK__.queryCounts = {}
  client.invalidations.length = 0
  queries.length = 0
  stubs.clear()
  promptCalls = 0
  // The gateway door and the shell door are per-scenario too, so a scenario
  // only ever sees the calls IT made.
  externalCalls.length = 0
  G.hostCalls = []
  G.hostReject = null
  G.openExternalFails = false

  try {
    await fn()
    results.push(`ok   ${name}`)
  } catch (error) {
    results.push(`FAIL ${name}\n       ${error.stack.split('\n').slice(0, 5).join('\n       ')}`)
    process.exitCode = 1
  }
}

// A. Push only when ahead.
await scenario('A: the Push button exists only on a card that is ahead', async () => {
  await mount()

  const ahead = byLabel('Push beta')

  assert.ok(ahead, 'the ahead card has no Push button')
  assert.equal(ahead.disabled, false, 'the Push button is disabled on an ahead card')
  assert.equal(byLabel('Push alpha'), null, 'a card with ahead:0 still offers Push')
  assert.ok(text().includes('↑3'), 'the ahead badge is missing')
})

// A2. The Push button sends the push request (and only that one).
await scenario('A: the Push button posts /projects/<name>/git-push', async () => {
  await mount()

  await click(byLabel('Push beta'))

  const pushes = callsTo('POST', '/projects/beta/git-push')

  assert.equal(pushes.length, 1, `expected one push, got ${JSON.stringify(pushes)}`)
  assert.deepEqual(pushes[0].body, { branch: null, remote: 'origin' })
})

// B. A card action sends the right request.
await scenario('B: the card Fetch button posts /projects/<name>/git/fetch', async () => {
  await mount()

  await click(byLabel('Fetch alpha'))

  const fetches = callsTo('POST', '/projects/alpha/git/fetch')

  assert.equal(fetches.length, 1, `expected exactly one fetch, got ${JSON.stringify(fetches)}`)
  assert.deepEqual(fetches[0].body, { remote: 'origin' })
})

// B2. A card action must not toggle the card selection (event.stopPropagation).
await scenario('B: a card action does not toggle the card selection', async () => {
  await mount()

  assert.equal(container.querySelector('[data-detail]'), null, 'a card is selected before the click')
  await click(byLabel('Fetch alpha'))

  assert.equal(container.querySelector('[data-detail]'), null, 'the action click selected the card')
  assert.equal(container.querySelector('[aria-expanded="true"]'), null, 'the action click expanded a card')
})

// C. success:false is surfaced; a clean run that wrote to stderr is not.
await scenario('C: a clean fetch that wrote progress to stderr shows no error', async () => {
  stubs.set('POST /projects/beta/git/fetch', {
    message: 'Fetched from origin',
    stderr: 'To github.com:tiboo00/beta.git\n   abc1234..def5678  main -> main',
    success: true
  })

  await mount()
  await selectProject('beta')
  await click(byLabel('Fetch beta'))

  assert.equal(
    container.querySelector('[role="alert"]'),
    null,
    `a clean run was reported as an error: ${text().slice(0, 300)}`
  )
  assert.ok(!text().includes('To github.com'), 'git progress leaked into the UI as an error')
})

await scenario('C: a fetch refused with success:false surfaces the server message', async () => {
  stubs.set('POST /projects/beta/git/fetch', { message: 'Fetch rejected', stderr: '', success: false })

  await mount()
  await selectProject('beta')
  await click(byLabel('Fetch beta'))

  assert.ok(text().includes('Fetch rejected'), `the refusal was not surfaced: ${text().slice(0, 300)}`)
  const alert = container.querySelector('[role="alert"]')

  assert.ok(alert, 'no role=alert node; markup=' + html().slice(0, 400))
})

// D. Commit opens a form, and the form posts the message.
await scenario('D: Commit opens an inline form and posts the typed message', async () => {
  await mount()

  await click(byLabel('Commit beta'))

  assert.equal(promptCalls, 0, 'the commit used window.prompt (the Electron renderer has none)')
  const form = container.querySelector('[data-commit-for="beta"]')

  assert.ok(form, 'no commit form; markup=' + html().slice(0, 400))
  const field = form.querySelector('input[aria-label="Commit message"]')

  assert.ok(field, 'the commit form has no message field')
  assert.equal(
    callsTo('POST', '/projects/beta/git-commit').length,
    0,
    'the commit was sent before the message was typed'
  )

  await type(field, 'fix: parity for the worktree section')
  await click(byText('Commit', form))

  const commits = callsTo('POST', '/projects/beta/git-commit')

  assert.equal(commits.length, 1, `expected one commit, got ${JSON.stringify(commits)}`)
  assert.equal(commits[0].body.message, 'fix: parity for the worktree section')
  assert.equal(commits[0].body.commit_and_push, false)
})

// E. Discard still gates its confirm.
await scenario('E: Discard requests NOTHING until the confirm is accepted', async () => {
  await mount()
  await selectProject('beta')

  const discard = byLabel('Discard changes in src/app.js')

  assert.ok(discard, 'the discard button is missing')

  await click(discard)

  assert.equal(
    restLog.filter(call => call.method === 'POST').length,
    0,
    `a request was sent before the confirm: ${JSON.stringify(restLog)}`
  )
  assert.ok(container.querySelector('[data-testid="confirm"]'), 'the confirm dialog did not open')

  await click(container.querySelector('[data-testid="confirm-accept"]'))

  const discards = callsTo('POST', '/projects/beta/git/discard')

  assert.equal(discards.length, 1, `expected one discard, got ${JSON.stringify(discards)}`)
  assert.deepEqual(discards[0].body, { filepath: 'src/app.js' })
})

// E2. Stash-all is the other irreversible op — it gates its confirm too.
await scenario('E: Stash all requests NOTHING until the confirm is accepted', async () => {
  await mount()
  await selectProject('beta')

  const stash = byText('Stash all')

  assert.ok(stash, 'the stash-all button is missing')

  await click(stash)

  assert.equal(
    restLog.filter(call => call.method === 'POST').length,
    0,
    `a request was sent before the confirm: ${JSON.stringify(restLog)}`
  )
  assert.ok(container.querySelector('[data-testid="confirm"]'), 'the confirm dialog did not open')

  await click(container.querySelector('[data-testid="confirm-accept"]'))

  const stashes = callsTo('POST', '/projects/beta/git-stash')

  assert.equal(stashes.length, 1, `expected one stash, got ${JSON.stringify(stashes)}`)
  assert.deepEqual(stashes[0].body, {})
})

// F. Watch folder add/remove.
await scenario('F: a watch folder can be added and removed', async () => {
  await mount()

  await click(byText('Folders…'))
  const section = container.querySelector('section[aria-label="Watched folders"]')

  assert.ok(section, 'the watch-folder section did not open')

  const input = section.querySelector('input[aria-label="Folder path"]')

  assert.ok(input, 'the watch-folder path field is missing')

  await type(input, '/repos/gamma')
  await click(byText('Add folder', section))

  const adds = callsTo('POST', '/watch-paths')

  assert.equal(adds.length, 1, `expected one add, got ${JSON.stringify(adds)}`)
  assert.deepEqual(adds[0].body, { path: '/repos/gamma', type: 'scan_root' })

  const row = [...section.querySelectorAll('li')].find(li => li.textContent.includes('/repos/alpha'))

  assert.ok(row, 'the existing watch folder row is missing')
  await click(byText('Remove', row))

  const deletes = callsTo('DELETE', '/watch-paths/1')

  assert.equal(deletes.length, 1, `expected one delete, got ${JSON.stringify(deletes)}`)
})

// G. Watch folder rename and reorder.
await scenario('G: a watch folder can be renamed', async () => {
  await mount()

  await click(byText('Folders…'))
  const section = container.querySelector('section[aria-label="Watched folders"]')
  const row = [...section.querySelectorAll('li')].find(li => li.textContent.includes('/repos/alpha'))

  assert.ok(row, 'the watch folder row is missing')
  await click(byText('Alpha repos', row))

  const field = section.querySelector('input[aria-label="Label for /repos/alpha"]')

  assert.ok(field, 'no label editor appeared; markup=' + section.innerHTML.slice(0, 400))

  await type(field, 'Renamed repos')
  await click(byText('Save', section))

  const renames = callsTo('PATCH', '/watch-paths/1')

  assert.equal(renames.length, 1, `expected one rename, got ${JSON.stringify(renames)}`)
  assert.deepEqual(renames[0].body, { label: 'Renamed repos' })
})

await scenario('G: a watch folder can be reordered', async () => {
  await mount()

  await click(byText('Folders…'))
  const section = container.querySelector('section[aria-label="Watched folders"]')

  assert.ok(section, 'the watch-folder section did not open')
  await click(byLabel('Move /repos/beta up'))

  const reorders = callsTo('POST', '/watch-paths/reorder')

  assert.equal(reorders.length, 1, `expected one reorder, got ${JSON.stringify(reorders)}`)
  // The route takes the FULL new order; sort_order is the array index.
  assert.deepEqual(reorders[0].body, { ids: [2, 1] })
})

// H. Pull all.
await scenario('H: Pull all posts /projects/pull-all', async () => {
  await mount()

  await click(byText('Pull all'))

  const pulls = callsTo('POST', '/projects/pull-all')

  assert.equal(pulls.length, 1, `expected one pull-all, got ${JSON.stringify(pulls)}`)
})

await scenario('H: a Pull all with failures surfaces the summary', async () => {
  stubs.set('POST /projects/pull-all', {
    already_up_to_date: 2,
    dirty_skipped: 0,
    diverged: 0,
    failed: 1,
    pulled: 1,
    results: [],
    success: true,
    total: 4
  })

  await mount()
  await selectProject('beta')
  await click(byText('Pull all'))

  const summary = '1 pulled, 2 up to date, 0 dirty skipped, 1 failed'

  assert.ok(text().includes(summary), `the failure summary was not surfaced: ${text().slice(0, 400)}`)
})

// I. Branches and Commits sections.
await scenario('I: the Branches section reads /branches and renders the names', async () => {
  await mount()
  await selectProject('beta')

  const reads = callsTo('GET', '/projects/beta/branches')

  assert.equal(reads.length, 1, `expected one /branches read, got ${JSON.stringify(reads)}`)

  await openDisclosure('Branches')

  assert.ok(text().includes('release/2026'), 'the branch names are not rendered')
})

await scenario('I: the Recent commits section reads /commits and renders the commits', async () => {
  await mount()
  await selectProject('beta')

  const reads = callsTo('GET', '/projects/beta/commits')

  assert.equal(reads.length, 1, `expected one /commits read, got ${JSON.stringify(reads)}`)

  await openDisclosure('Recent commits')

  assert.ok(text().includes('parity: cover the worktree section'), 'the commit message is not rendered')
  assert.ok(text().includes('abc1234'), 'the commit hash is not rendered')
})

// J. Worktrees: list, select, delete with force, and the dirty checkout.
await scenario('J: the worktree list renders the rows with their badges', async () => {
  await mount()
  await selectProject('beta')

  const reads = callsTo('GET', '/projects/beta/worktrees')

  assert.equal(reads.length, 1, `expected one worktree read, got ${JSON.stringify(reads)}`)

  const section = await openDisclosure('Worktrees & branches')

  for (const branch of ['main', 'feature/locked', 'release/clean', 'hotfix/dirty']) {
    assert.ok(section.textContent.includes(branch), `the worktree row "${branch}" is missing`)
  }
  // The badges are read INSIDE the worktree section: the project card has a
  // badge called "dirty" too, so a page-wide search would prove nothing.
  const rowBadges = [...section.querySelectorAll('li span')].map(span => span.textContent)

  assert.ok(rowBadges.includes('current'), `the current badge is missing: ${JSON.stringify(rowBadges)}`)
  assert.ok(rowBadges.includes('locked: in progress'), `the locked badge is missing: ${JSON.stringify(rowBadges)}`)
  assert.ok(rowBadges.includes('dirty'), `the dirty badge is missing: ${JSON.stringify(rowBadges)}`)
})

await scenario('J: selecting a worktree posts to /worktrees/<id>/select', async () => {
  await mount()
  await selectProject('beta')
  await openDisclosure('Worktrees & branches')

  await click(byLabel('Select worktree release/clean'))

  const selects = callsTo('POST', '/projects/beta/worktrees/wt_ccc33333/select')

  assert.equal(selects.length, 1, `expected one select, got ${JSON.stringify(selects)}`)
})

await scenario('J: deleting a clean worktree sends force:false', async () => {
  await mount()
  await selectProject('beta')
  await openDisclosure('Worktrees & branches')

  await click(byLabel('Remove worktree release/clean'))
  assert.ok(container.querySelector('[data-testid="confirm"]'), 'the delete confirm did not open')
  await click(container.querySelector('[data-testid="confirm-accept"]'))

  const deletes = callsTo('DELETE', '/projects/beta/worktrees/wt_ccc33333')

  assert.equal(deletes.length, 1, `expected one delete, got ${JSON.stringify(deletes)}`)
  assert.deepEqual(deletes[0].body, { force: false })
})

await scenario('J: deleting a DIRTY worktree sends force:true', async () => {
  await mount()
  await selectProject('beta')
  await openDisclosure('Worktrees & branches')

  await click(byLabel('Remove worktree hotfix/dirty'))
  assert.ok(container.querySelector('[data-testid="confirm"]'), 'the delete confirm did not open')
  await click(container.querySelector('[data-testid="confirm-accept"]'))

  const deletes = callsTo('DELETE', '/projects/beta/worktrees/wt_ddd44444')

  assert.equal(deletes.length, 1, `expected one delete, got ${JSON.stringify(deletes)}`)
  assert.deepEqual(deletes[0].body, { force: true })
})

await scenario('J: a current or locked worktree cannot be removed at all', async () => {
  await mount()
  await selectProject('beta')
  await openDisclosure('Worktrees & branches')

  const current = byLabel('Remove worktree main')
  const locked = byLabel('Remove worktree feature/locked')

  assert.ok(current, 'the current worktree has no remove button')
  assert.ok(locked, 'the locked worktree has no remove button')
  assert.equal(current.disabled, true, 'the current worktree is removable')
  assert.equal(locked.disabled, true, 'the locked worktree is removable')

  await click(current)
  await click(locked)

  assert.equal(
    restLog.filter(call => call.method === 'DELETE').length,
    0,
    `a protected worktree was deleted: ${JSON.stringify(restLog.filter(c => c.method === 'DELETE'))}`
  )
})

await scenario('J: a dirty checkout asks first, then stash and force are explicit', async () => {
  // The plain checkout is REFUSED with WORKTREE_DIRTY; only a stash/force
  // checkout is answered.
  stubs.set('POST /projects/beta/worktrees/checkout', (path, opts) => {
    if (opts.body && (opts.body.stash || opts.body.force)) {
      return { branch: opts.body.branch, previous_branch: 'wt-main', stashed: !!opts.body.stash, worktrees: [] }
    }

    throw new Error(WORKTREE_DIRTY_ERROR)
  })

  await mount()
  await selectProject('beta')
  await openDisclosure('Worktrees & branches')

  const select = container.querySelector('select[aria-label="Branch to check out in beta"]')

  assert.ok(select, 'the branch picker is missing')

  await choose(select, 'wt-feature')
  assert.equal(container.querySelector('[data-dirty-choice]'), null, 'the choice appeared before the refusal')

  await click(byText('Checkout'))

  const plain = callsTo('POST', '/projects/beta/worktrees/checkout')

  assert.equal(plain.length, 1, `expected one checkout attempt, got ${JSON.stringify(plain)}`)
  assert.deepEqual(plain[0].body, { branch: 'wt-feature' })

  const choice = container.querySelector('[data-dirty-choice="wt-feature"]')

  assert.ok(choice, `the stash/force choice did not appear: ${text().slice(0, 400)}`)

  await click(byText('Stash & checkout', choice))

  const stashed = callsTo('POST', '/projects/beta/worktrees/checkout')

  assert.equal(stashed.length, 2, `expected two checkout calls, got ${JSON.stringify(stashed)}`)
  assert.deepEqual(stashed[1].body, { branch: 'wt-feature', stash: true })

  // The force path is a separate, explicit choice on a fresh refusal.
  await choose(container.querySelector('select[aria-label="Branch to check out in beta"]'), 'wt-other')
  await click(byText('Checkout'))

  const refused = callsTo('POST', '/projects/beta/worktrees/checkout')

  assert.equal(refused.length, 3, `expected three checkout calls, got ${JSON.stringify(refused)}`)

  const forceChoice = container.querySelector('[data-dirty-choice="wt-other"]')

  assert.ok(forceChoice, 'the second refusal did not offer the choice')
  await click(byText('Force checkout', forceChoice))

  const forced = callsTo('POST', '/projects/beta/worktrees/checkout')

  assert.equal(forced.length, 4, `expected four checkout calls, got ${JSON.stringify(forced)}`)
  assert.deepEqual(forced[3].body, { branch: 'wt-other', force: true })
})

// ── regressions: an error must appear where the user acted ───────────────────
//
// Both of these were real bugs the new UI shipped with, and both are now
// pinned so they cannot come back:
//
//  1. A card action's failure went to the page-level `panelError`, which
//     ProjectCard renders only for the SELECTED card. A failed Fetch on `alpha`
//     while `beta` was open painted the message on beta — or, with nothing
//     selected, nowhere at all. It now goes to a per-card channel and renders
//     on alpha, unselected.
//
//  2. The Pull-all summary used that same channel, so a bulk pull that failed
//     some projects said nothing unless a card happened to be open. It is a
//     page-level fact and now goes to the page-level `notice`.
await scenario('a card action failure lands on the card that failed, not the selected one', async () => {
  stubs.set('POST /projects/alpha/git/fetch', { message: 'Fetch rejected', stderr: '', success: false })

  await mount()
  await selectProject('beta')
  await click(byLabel('Fetch alpha'))

  const alpha = container.querySelector('[data-card="alpha"]')
  const beta = container.querySelector('[data-card="beta"]')

  assert.ok(alpha, 'no alpha card rendered')
  assert.ok(beta, 'no beta card rendered')
  assert.ok(
    alpha.textContent.includes('Fetch rejected'),
    "the failure did not appear on the card that failed"
  )
  assert.ok(
    !beta.textContent.includes('Fetch rejected'),
    'the failure leaked onto the selected card'
  )
})

await scenario('a Pull all failure summary is visible with no card selected', async () => {
  stubs.set('POST /projects/pull-all', {
    already_up_to_date: 2,
    dirty_skipped: 0,
    diverged: 0,
    failed: 1,
    pulled: 1,
    results: [],
    success: true,
    total: 4
  })

  await mount()
  await click(byText('Pull all'))

  assert.ok(text().includes('1 failed'), 'the pull-all summary was not shown')
  assert.ok(
    container.querySelector('[data-notice]'),
    'the summary must reach the page-level notice channel'
  )
})

// ── K: the remote links (web-dashboard parity) ──────────────────────────────
//
// The web card turns the project NAME into the link to the repository and
// the branch chip into the link to that branch on the remote. The desktop
// does the same, through the Electron shell instead of window.open() — so the
// URL the browser is handed is the NORMALIZED one: an SSH remote becomes
// https, and the .git suffix goes.
await scenario('K: the project name opens the repository URL', async () => {
  await mount()

  const link = remoteLink('alpha')

  assert.ok(link, 'the project name is not a repository link')
  assert.equal(link.textContent, 'alpha', 'the link does not show the project name')
  assert.equal(
    link.getAttribute('data-remote-link'),
    'alpha',
    'the link does not carry the project it belongs to'
  )

  await click(link)

  assert.deepEqual(externalCalls, ['https://github.com/tiboo00/alpha'])
})

await scenario('K: the branch chip opens that branch on the remote', async () => {
  await mount()

  const chip = branchLink('alpha')

  assert.ok(chip, 'the branch chip is not a link')
  assert.equal(
    chip.getAttribute('data-branch-link'),
    'alpha',
    'the branch link does not carry the project it belongs to'
  )

  await click(chip)

  assert.deepEqual(externalCalls, ['https://github.com/tiboo00/alpha/tree/main'])

  // A branch with a slash is url-encoded, so the remote URL stays a URL.
  await click(branchLink('beta'))

  assert.deepEqual(externalCalls[1], 'https://github.com/tiboo00/beta/tree/feat%2Fparity')
})

await scenario('K: clicking the project name does not select the card', async () => {
  await mount()

  assert.equal(container.querySelector('[data-detail]'), null, 'a card is selected before the click')
  await click(remoteLink('alpha'))

  // The card row is a role=button that selects the project. The name is a
  // link INSIDE it, so the click must not bubble into a selection.
  assert.equal(container.querySelector('[aria-expanded="true"]'), null, 'the name click expanded a card')
  assert.equal(container.querySelector('[data-detail]'), null, 'the name click selected the card')
  assert.deepEqual(externalCalls, ['https://github.com/tiboo00/alpha'], 'the name did not open the repository')
})

await scenario('K: a remote that is not http(s) is not a link', async () => {
  stubs.set('GET /projects?page=1&page_size=21', {
    ...PROJECTS,
    projects: PROJECTS.projects.map(project =>
      project.name === 'beta' ? { ...project, remote_url: 'file:///srv/git/beta' } : project
    )
  })

  await mount()

  await click(remoteLink('beta'))

  assert.deepEqual(externalCalls, [], 'a remote that is not http(s) was opened in the browser')
})

await scenario('K: a shell that cannot open the URL says so on the card', async () => {
  // The real door RESOLVES false when the Electron shell has no bridge for
  // the URL; it never throws. A false that is read as a success leaves the
  // user with a click that did nothing, so the refusal must be reported.
  G.openExternalFails = true

  await mount()

  await click(remoteLink('alpha'))

  const error = cardOf('alpha').querySelector('[data-card-error="alpha"]')

  assert.ok(error, 'the failed open was silent; the card says nothing')
  assert.ok(
    error.textContent.includes('Could not open the repository'),
    `the card does not say what failed: ${error.textContent}`
  )
  assert.equal(
    cardOf('beta').querySelector('[data-card-error]'),
    null,
    'the failure leaked onto another card'
  )
})

await scenario('K: a shell that cannot open the branch URL says so on the card', async () => {
  G.openExternalFails = true

  await mount()

  await click(branchLink('alpha'))

  const error = cardOf('alpha').querySelector('[data-card-error="alpha"]')

  assert.ok(error, 'the failed open was silent; the card says nothing')
  assert.ok(
    error.textContent.includes('Could not open the branch'),
    `the card does not say what failed: ${error.textContent}`
  )
})

// ── L: the Chat button opens a session in the project's own folder ─────────
//
// The gateway's session.create is the only door a plugin has to aim a session
// at a directory, so the card's Chat button has to name the folder (cwd),
// say that the folder was named EXPLICITLY (cwd_explicit, or the gateway
// falls back to the profile's workspace), and then open the session it got.
await scenario('L: the Chat button opens a session in the project folder', async () => {
  await mount()

  const chat = byText('Chat', cardOf('alpha'))

  assert.ok(chat, 'the card has no Chat button')
  assert.equal(chat.textContent.trim(), 'Chat', 'the button label is not Chat')
  assert.equal(chat.disabled, false, 'the Chat button is disabled on a project with a folder')

  await click(chat)

  const creates = G.hostCalls.filter(call => call.method === 'session.create')
  const opens = G.hostCalls.filter(call => call.method === 'openSession')

  assert.equal(creates.length, 1, `expected one session.create, got ${JSON.stringify(G.hostCalls)}`)
  assert.deepEqual(
    creates[0].params,
    { cwd: '/repos/alpha', cwd_explicit: true, title: 'alpha' },
    'the session was not created in the project folder'
  )
  assert.equal(opens.length, 1, `expected one openSession, got ${JSON.stringify(G.hostCalls)}`)
  assert.equal(opens[0].id, 'stored-test', 'the created session was not the one opened')
})

await scenario('L: a refused session create lands on the card that asked for it', async () => {
  G.hostReject = 'the gateway refused the session'

  await mount()

  await click(byText('Chat', cardOf('beta')))

  const error = cardOf('beta').querySelector('[data-card-error="beta"]')

  assert.ok(error, 'the refusal did not land on the card that asked for the session')
  assert.ok(
    error.textContent.includes('the gateway refused the session'),
    `the refusal message is missing: ${error.textContent}`
  )
  assert.equal(
    cardOf('alpha').querySelector('[data-card-error]'),
    null,
    'the refusal leaked onto another card'
  )
  assert.equal(
    G.hostCalls.filter(call => call.method === 'openSession').length,
    0,
    'a refused session was opened anyway'
  )
})

await scenario('L: a project with no known folder cannot start a session', async () => {
  stubs.set('GET /projects?page=1&page_size=21', {
    ...PROJECTS,
    projects: PROJECTS.projects.map(project => {
      if (project.name !== 'beta') return project
      const { path, ...withoutPath } = project

      return withoutPath
    })
  })

  await mount()

  const chat = byText('Chat', cardOf('beta'))

  assert.ok(chat, 'the card has no Chat button')
  assert.equal(chat.disabled, true, 'the Chat button is enabled for a project with no folder')
  assert.match(
    chat.getAttribute('title'),
    /no known folder/,
    'the button does not say why it is disabled'
  )

  await click(chat)

  assert.deepEqual(G.hostCalls, [], 'a session was started for a project with no folder')
})

// ── report ──────────────────────────────────────────────────────────────────
if (root) {
  await act(async () => root.unmount())
}

console.log(`plugin under test: ${PLUGIN_JS}`)
console.log('')
console.log(results.join('\n'))
const failed = results.filter(r => r.startsWith('FAIL')).length

console.log(`\n${results.length - failed}/${results.length} scenarios passed`)
console.log(`window.prompt fallbacks: ${promptCalls} (must be 0)`)
