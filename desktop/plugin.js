// ── BUILT ARTIFACT — the single-file build of api.js + page.js + working-tree.js ──
// The Desktop runtime loader evaluates a disk plugin from a BLOB: URL
// (apps/desktop/src/contrib/runtime-loader.ts), so a relative import cannot
// resolve against the module's base, and its import allowlist refuses every
// specifier outside @hermes/plugin-sdk / react. One file is the only shape
// the loader accepts, so the four modules are merged here.
//
// EDIT: api.js (REST layer), page.js (list page) and working-tree.js (per-file
// git panel) stay on disk as the readable originals — THIS file is what runs.
// Change the split file AND the matching section below.
//
// One deliberate deviation from the split sources: the list's query key is
// rooted at PM_QUERY_ROOT (see projectsKey), so a panel mutation invalidates
// the list too — the split page.js keys the list under 'project-manager' and
// the split panel under 'pm', which leaves the list's dirty/ahead badges
// stale after a stage/discard.
import { createElement, useCallback, useEffect, useState } from 'react'
import {
  Badge,
  Button,
  ConfirmDialog,
  Codicon,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  cn,
  host,
  relativeTime,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'

// ── [1] DATA LAYER (from api.js) ───────────────────────────────────────────
// Data layer for the Desktop Projects surface. Every call rides ctx.rest,
// which is namespace-bound to /api/plugins/project-manager — the same
// router the web dashboard and the mobile app already speak.
//
// Note the paths are RELATIVE: pluginRest() prefixes /api/plugins/
// project-manager and rejects any `..` segment, so a project name that
// looks like a path traversal can't escape the namespace.
let rest = null

/**
 * Bind the doors at register time; the disposer is what unload runs. BOTH
 * doors are released by the one disposer: the REST handle and the native
 * shell handle, so an unloaded plugin can reach neither.
 */
function bindDoors(r, open) {
  rest = r
  openExternal = open
  return () => {
    rest = null
    openExternal = null
  }
}

function door() {
  if (!rest) throw new Error('project-manager-desktop: rest door not bound')
  return rest
}

// ── Native app doors ───────────────────────────────────────────────────────
// ctx.openExternal hands a URL to the Electron shell, which opens it in the
// system browser — the desktop equivalent of the web card's window.open().
// Bound at register time so an unloaded plugin cannot reach it.
let openExternal = null

/**
 * The web card's remote normalization, ported: an SSH remote becomes an
 * HTTPS URL, only http(s) survives, and the .git suffix goes. A remote that
 * is neither is not a link — the card shows it as plain text.
 */
function normalizeRemoteUrl(url) {
  if (!url) return null
  let out = url
  if (out.indexOf('git@') === 0) {
    out = out.replace(/^git@([^:]+):/, 'https://$1/')
  }
  if (out.indexOf('http://') !== 0 && out.indexOf('https://') !== 0) {
    return null
  }
  return out.replace(/\.git$/, '')
}

/** The host-specific tree URL for a branch — the web card's openBranch(). */
function branchUrl(remote, branch) {
  const url = normalizeRemoteUrl(remote)
  if (!url || !branch) return null
  if (url.indexOf('github.com') !== -1) return `${url}/tree/${encodeURIComponent(branch)}`
  if (url.indexOf('gitlab.com') !== -1) return `${url}/-/tree/${encodeURIComponent(branch)}`
  if (url.indexOf('bitbucket.org') !== -1) return `${url}/src/${encodeURIComponent(branch)}`
  return `${url}/tree/${encodeURIComponent(branch)}`
}

/**
 * Open a URL in the system browser. The door RESOLVES false when the shell
 * cannot open it — it never throws, because the desktop degrades to false on
 * a missing bridge — so the resolved value is what says success or failure.
 * A missing door is a no-op, not a throw: an older desktop without the
 * capability must not break the card.
 */
async function openInBrowser(url) {
  if (!url || typeof openExternal !== 'function') return false
  try {
    return (await openExternal(url)) !== false
  } catch {
    return false
  }
}

const proj = name => `/projects/${encodeURIComponent(name)}`

const fetchProjects = (page = 1, pageSize = 21, query = '') =>
  door()(`/projects?page=${page}&page_size=${pageSize}${query ? `&q=${encodeURIComponent(query)}` : ''}`)

const refreshProjects = () => door()('/projects/refresh', { method: 'POST' })

const fetchGitStatus = name => door()(`${proj(name)}/git-status`)

const fetchDiff = (name, file) =>
  door()(`${proj(name)}/git/diff?file=${encodeURIComponent(file)}`)

/** stage | unstage | discard — all three take {filepath} and return FileOpResponseDTO. */
const fileOp = (name, action, file) =>
  door()(`${proj(name)}/git/${action}`, { method: 'POST', body: { filepath: file } })

const gitFetch = (name, remote = 'origin') =>
  door()(`${proj(name)}/git/fetch`, { method: 'POST', body: { remote } })

// The card's Pull/Commit buttons. Pull takes no body — the helper resolves
// the upstream branch itself.
const gitPull = name =>
  door()(`${proj(name)}/git-pull`, { method: 'POST', body: {} })

const gitCommit = (name, message) =>
  door()(`${proj(name)}/git-commit`, {
    method: 'POST',
    body: { message, commit_and_push: false }
  })

const gitPush = (name, remote = 'origin', branch = null) =>
  door()(`${proj(name)}/git-push`, { method: 'POST', body: { remote, branch } })

// No body — the route takes none and stashes untracked files too, exactly
// what the web's "Stash all" button sends.
const gitStash = name => door()(`${proj(name)}/git-stash`, { method: 'POST', body: {} })

// ── Watch paths + bulk pull ────────────────────────────────────────────────
// The Desktop surface had no way to add a watch folder, which is where the
// project list comes from in the first place. These are the same routes the
// web dashboard uses.
const fetchWatchPaths = () => door()('/watch-paths')

const addWatchPath = (path, type, label) =>
  door()('/watch-paths', { method: 'POST', body: label ? { path, type, label } : { path, type } })

const deleteWatchPath = id => door()(`/watch-paths/${encodeURIComponent(id)}`, { method: 'DELETE' })

/** Body is {label} — RenameRequest has exactly that one field. An empty label
 *  is a 400, so an emptied input must not be sent. */
const renameWatchPath = (id, label) =>
  door()(`/watch-paths/${encodeURIComponent(id)}`, { method: 'PATCH', body: { label } })

/** Body is {ids: number[]} — the FULL new order; sort_order is the row index. */
const reorderWatchPaths = ids => door()('/watch-paths/reorder', { method: 'POST', body: { ids } })

/** Server refuses a prefix under 2 chars (400), so the caller filters first. */
const suggestWatchPaths = prefix =>
  door()(`/watch-paths/suggest?prefix=${encodeURIComponent(prefix)}`)

const pullAll = () => door()('/projects/pull-all', { method: 'POST', body: {} })

// ── Branches, commits, worktrees ───────────────────────────────────────────
// Read-only project views the web card shows and Desktop never fetched.
// /branches answers a bare BranchItem[] ({name, is_current}); /commits
// answers {commits: CommitItem[]} ({hash, message, date, author}).
const fetchBranches = name => door()(`${proj(name)}/branches`)

const fetchCommits = name => door()(`${proj(name)}/commits`)

// The worktree routes are a DIFFERENT contract from the git/* ones: success is
// HTTP 200 with {worktrees, current_worktree_id}, and every refusal is a 4xx/5xx
// {error, code} — there is no success:false to inspect. `code` is the part the
// UI acts on (WORKTREE_DIRTY / WORKTREE_LOCKED / WORKTREE_PROTECTED / …).
const wtPath = name => `${proj(name)}/worktrees`

const fetchWorktrees = name => door()(wtPath(name))

/** {branches: [{name, is_current, is_remote, upstream}], current_branch} —
 *  the branches of the ACTIVE worktree, which is what the picker checks out in. */
const fetchWorktreeBranches = name => door()(`${wtPath(name)}/branches`)

const selectWorktree = (name, worktreeId) =>
  door()(`${wtPath(name)}/${encodeURIComponent(worktreeId)}/select`, { method: 'POST', body: {} })

/** BranchCheckoutRequest models exactly {branch, stash, force}; the flags are
 *  only sent when chosen so a plain checkout stays a plain checkout. */
const checkoutBranch = (name, branch, { stash = false, force = false } = {}) =>
  door()(`${wtPath(name)}/checkout`, {
    method: 'POST',
    body: stash ? { branch, stash: true } : force ? { branch, force: true } : { branch }
  })

/** WorktreeDeleteRequest is {force} and force ONLY overrides WORKTREE_DIRTY —
 *  current / locked / protected are refused either way. */
const deleteWorktree = (name, worktreeId, force) =>
  door()(`${wtPath(name)}/${encodeURIComponent(worktreeId)}`, {
    method: 'DELETE',
    body: { force: !!force }
  })

const deleteAllWorktrees = (name, force) =>
  door()(wtPath(name), { method: 'DELETE', body: { force: !!force } })

/** The worktree routes answer `{error, code}` and the REST door re-throws them
 *  as `404: {json}` — the code is the actionable half (which protection fired),
 *  so both are surfaced instead of the bare message. */
function wtError(err) {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  const brace = raw.indexOf('{')

  if (brace !== -1) {
    try {
      const body = JSON.parse(raw.slice(brace))

      return { code: body.code || null, text: body.error || body.detail || body.message || raw }
    } catch {
      // Not JSON — fall through to the raw message.
    }
  }

  return { code: null, text: raw }
}

/** `${code}: ${text}` when the server named a code, else the message alone. */
const wtErrorText = err => {
  const { code, text } = wtError(err)

  return code ? `${code}: ${text}` : text
}


// ── [2] WORKING-TREE PANEL (from working-tree.js) ──────────────────────────
// Per-file git surface: the stage / unstage / discard / diff feature set the
// web dashboard already ships (dashboard/dist/index.js, WorkingTreeSection),
// ported onto the Desktop plugin runtime.
//
// The endpoint's contract is the reason this file is shaped the way it is:
// /git/stage, /git/unstage, /git/discard and /git/fetch answer HTTP 200 with
// success:false when the git call fails. A resolved promise is NOT a success,
// so every mutating path below checks result.success BEFORE it refreshes
// anything — otherwise a rejected-to-the-server op repaints the file list from
// a status that never changed and the user reads it as "it worked".

// One key root for the whole surface: a mutating op invalidates this prefix so
// the git-status list AND the parent page's projects list (its dirty/ahead
// badges read the same /projects payload) refetch together.
const PM_QUERY_ROOT = ['pm']

const gitStatusKey = projectName => ['pm', 'git-status', projectName]

/** The REST door throws `409: {"detail":"…"}` or `{"error":…,"code":…}` —
 *  pull the human-readable half out instead of showing raw JSON. */
function errText(err) {
  const raw = err instanceof Error ? err.message : String(err ?? '')
  const brace = raw.indexOf('{')

  if (brace !== -1) {
    try {
      const body = JSON.parse(raw.slice(brace))

      return body.detail || body.error || body.message || raw
    } catch {
      // Not JSON — fall through to the raw message.
    }
  }

  return raw
}

// /git-status ships two file shapes (modified_files entries are
// {path, index_status, worktree_status}; untracked_files entries are {path}).
// Normalising once here keeps the rows and the action handlers agreeing on
// path / badge / index state.
const filePath = file => (typeof file === 'string' ? file : file?.path || file?.file || '')

const fileStatus = file => {
  if (typeof file === 'string') return 'M'
  const code = file?.status || (file?.index_status && file.index_status !== ' ' ? file.index_status : file?.worktree_status) || 'M'

  return String(code).charAt(0).toUpperCase()
}

// ' ' is git's "no change on this side" — an unstage on such a file is a no-op
// the server would just reject, so the button says so instead of firing.
const hasIndexChange = file =>
  typeof file !== 'string' && !!(file?.index_status && file.index_status !== ' ')

// Discard is irreversible and behaves DIFFERENTLY per case, so the confirm has
// to name which one the user is about to do.
// Which list a row came from is NOT enough: git reports a staged-new file
// (`A `) under modified_files, but it exists in no commit, so there is no HEAD
// version to restore it from and the backend un-stages it, leaving the bytes
// on disk untracked. Both "removed from the commit" cases are unrecoverable,
// so both must warn as such — claiming a HEAD restore for an `A ` file would
// be the opposite of what happens.
const discardCopy = (path, untracked, indexStatus) => {
  if (untracked || indexStatus === 'A' || indexStatus === '?') {
    return (
      `'${path}' is in no commit. Discarding removes it from the commit and ` +
      'leaves it untracked on disk — nothing here can restore a version of it.'
    )
  }
  return `'${path}' is tracked. Discarding restores it from HEAD — uncommitted local edits to it are lost forever.`
}

// Split a diff into coloured lines. Not a parser: the git preamble
// (diff --git / index / --- / +++) only repeats the filename this view is
// already titled with, so it goes, and +/- get the cheapest possible emphasis.
function diffLines(text) {
  if (!text) return null

  const rows = String(text).split('\n')
  const out = []

  for (const [i, line] of rows.entries()) {
    if (line.startsWith('diff --git ') || line.startsWith('index ') || line.startsWith('--- ') || line.startsWith('+++ ')) {
      continue
    }

    const cls = line.startsWith('+')
      ? 'text-emerald-600 dark:text-emerald-400'
      : line.startsWith('-')
        ? 'text-destructive'
        : line.startsWith('@')
          ? 'text-muted-foreground'
          : ''

    out.push(createElement('span', { key: `l${i}`, className: cls }, `${line}\n`))
  }

  return out.length > 0 ? out : null
}

function DiffSection({ title, text }) {
  const body = diffLines(text)

  return createElement(
    'div',
    { className: 'space-y-1' },
    createElement('div', { className: 'text-xs font-semibold text-muted-foreground' }, title),
    body
      ? createElement(
          'pre',
          {
            className:
              'max-h-48 overflow-auto whitespace-pre-wrap rounded border border-border bg-muted p-2 font-mono text-[11px]'
          },
          body
        )
      : createElement('div', { className: 'text-[11px] text-muted-foreground' }, 'No changes.')
  )
}

/** The diff is a PEEK, not a navigation — it floats over the list so the file
 *  rows stay mounted and the user can walk straight to the next file. */
function DiffOverlay({ diff, onClose }) {
  const payload = diff.data
  let body

  if (diff.loading) {
    body = createElement('p', { className: 'text-sm text-muted-foreground' }, 'Loading diff…')
  } else if (diff.error) {
    body = createElement('p', { className: 'text-sm text-destructive', role: 'alert' }, diff.error)
  } else if (!payload?.success) {
    body = createElement(
      'p',
      { className: 'text-sm text-destructive', role: 'alert' },
      payload?.message || 'Failed to load diff.'
    )
  } else if (payload.binary) {
    // binary:true means BOTH diff strings are empty BY DESIGN — an empty <pre>
    // would read as "no changes", which is a lie about a changed binary file.
    body = createElement(
      'p',
      { className: 'text-sm text-muted-foreground' },
      'Binary file — git produced no textual diff.'
    )
  } else {
    body = createElement(
      'div',
      { className: 'space-y-3' },
      payload.truncated
        ? createElement(
            'p',
            { className: 'text-xs text-destructive', role: 'alert' },
            'Diff truncated — this file is larger than the server’s diff limit.'
          )
        : null,
      DiffSection({ title: 'Staged', text: payload.staged_diff }),
      DiffSection({ title: 'Working tree', text: payload.worktree_diff })
    )
  }

  return createElement(
    'div',
    { className: 'fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4', onClick: onClose },
    createElement(
      'div',
      {
        className: 'w-full max-w-2xl rounded-md border border-border bg-background p-5 shadow-lg',
        role: 'dialog',
        'aria-modal': 'true',
        'aria-labelledby': 'pm-diff-title',
        onClick: e => e.stopPropagation()
      },
      createElement(
        'div',
        { className: 'mb-3 flex items-center justify-between gap-2' },
        createElement('h3', { id: 'pm-diff-title', className: 'truncate text-base font-semibold', title: diff.path }, diff.path),
        createElement(
          'button',
          {
            type: 'button',
            className: 'text-xl leading-none text-muted-foreground hover:text-foreground',
            onClick: onClose,
            'aria-label': 'Close diff'
          },
          '✕'
        )
      ),
      body
    )
  )
}

/** The web's ProjectCard folds its extra blocks behind a header row. One
 *  disclosure shape for all three so they behave identically. */
function Disclosure({ label, count, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)

  return createElement(
    'section',
    { className: 'border-t border-border pt-2' },
    createElement(
      'button',
      {
        type: 'button',
        'aria-expanded': open,
        className: 'flex w-full items-center justify-between gap-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground',
        onClick: () => setOpen(prev => !prev)
      },
      label,
      createElement(
        'span',
        { className: 'flex items-center gap-1 text-[11px]' },
        count === undefined || count === null ? null : createElement(Badge, { size: 'xs', variant: 'muted' }, String(count)),
        open ? '▾' : '▸'
      )
    ),
    open ? children : null
  )
}

/** Read-only /branches — the web card shows this list next to the commits.
 *  No checkout here: the branch switch lives in the worktree block below,
 *  which owns the dirty/stash decision. */
function ProjectBranches({ projectName }) {
  const { data, error, isPending } = useQuery({
    enabled: !!projectName,
    queryFn: () => fetchBranches(projectName),
    queryKey: [...PM_QUERY_ROOT, 'branches', projectName],
    staleTime: 30_000
  })

  // The route answers a BARE list, not {branches: …}.
  const branches = Array.isArray(data) ? data : []

  return createElement(
    Disclosure,
    { count: branches.length, label: 'Branches' },
    error
      ? createElement('p', { className: 'text-xs text-destructive' }, errorText(error))
      : isPending
        ? createElement('p', { className: 'text-xs text-muted-foreground' }, 'Reading branches…')
        : branches.length === 0
          ? createElement('p', { className: 'text-xs text-muted-foreground' }, 'No local branches.')
          : createElement(
              'ul',
              { className: 'max-h-40 space-y-0.5 overflow-y-auto text-xs' },
              branches.map(branch =>
                createElement(
                  'li',
                  { key: branch.name, className: 'flex items-center gap-1.5' },
                  createElement(Codicon, { name: 'source-control', size: '0.8em' }),
                  createElement('span', { className: 'truncate' }, branch.name),
                  branch.is_current ? createElement(Badge, { size: 'xs', variant: 'muted' }, 'current') : null
                )
              )
            )
  )
}

/** Read-only /commits — CommitListResponse is {commits: CommitItem[]}. */
function ProjectCommits({ projectName }) {
  const { data, error, isPending } = useQuery({
    enabled: !!projectName,
    queryFn: () => fetchCommits(projectName),
    queryKey: [...PM_QUERY_ROOT, 'commits', projectName],
    staleTime: 30_000
  })

  const commits = data?.commits ?? []

  return createElement(
    Disclosure,
    { count: commits.length, label: 'Recent commits' },
    error
      ? createElement('p', { className: 'text-xs text-destructive' }, errorText(error))
      : isPending
        ? createElement('p', { className: 'text-xs text-muted-foreground' }, 'Reading commits…')
        : commits.length === 0
          ? createElement('p', { className: 'text-xs text-muted-foreground' }, 'No commits yet.')
          : createElement(
              'ul',
              { className: 'max-h-48 space-y-0.5 overflow-y-auto text-xs' },
              commits.map(commit => {
                const age = relativeAge(commit.date)

                return createElement(
                  'li',
                  { key: commit.hash, className: 'flex items-baseline gap-1.5', title: commit.message },
                  createElement('code', { className: 'shrink-0 text-[10px] text-muted-foreground' }, String(commit.hash).slice(0, 7)),
                  createElement('span', { className: 'min-w-0 flex-1 truncate' }, commit.message),
                  createElement('span', { className: 'shrink-0 text-[10px] text-muted-foreground/70' }, `${commit.author}${age ? ` · ${age}` : ''}`)
                )
              })
            )
  )
}

/** The web's WorktreesSection, ported. Contract notes that shape this file:
 *
 *  - every route here REFUSES with 4xx/5xx `{error, code}` instead of the
 *    `success:false` the git/* routes use, so refusals arrive as a throw and
 *    `code` is what says WHICH protection fired;
 *  - `is_current` / `is_locked` / `is_protected` are unremovable by ANY body,
 *    and `is_dirty` is removable only with `force: true` — so the button says
 *    so up front instead of firing a request the server will refuse;
 *  - checkout refuses a dirty tree with WORKTREE_DIRTY, and the two ways past
 *    it (stash / force) are a USER decision, so they are explicit buttons in
 *    this file — never an automatic stash, and never window.confirm. */
function WorktreesSection({ projectName, onChanged }) {
  const client = useQueryClient()
  const list = useQuery({
    enabled: !!projectName,
    queryFn: () => fetchWorktrees(projectName),
    queryKey: [...PM_QUERY_ROOT, 'worktrees', projectName],
    staleTime: 5_000
  })
  const branchesQuery = useQuery({
    enabled: !!projectName,
    queryFn: () => fetchWorktreeBranches(projectName),
    queryKey: [...PM_QUERY_ROOT, 'worktree-branches', projectName],
    staleTime: 5_000
  })

  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  // Which row an op is in flight for — one row locks, not the whole list.
  const [busyId, setBusyId] = useState(null)
  const [armedDelete, setArmedDelete] = useState(null)
  const [armedBulk, setArmedBulk] = useState(false)
  const [bulkForce, setBulkForce] = useState(false)
  const [target, setTarget] = useState('')
  const [checkingOut, setCheckingOut] = useState(false)
  // Set when the server answered WORKTREE_DIRTY: the branch is parked until the
  // user picks stash / force / cancel. Nothing retries on its own.
  const [dirtyChoice, setDirtyChoice] = useState(null)

  const worktrees = list.data?.worktrees ?? []
  const currentId = list.data?.current_worktree_id ?? null
  const branches = branchesQuery.data?.branches ?? []
  const currentBranch = branchesQuery.data?.current_branch ?? ''

  // Everything a worktree op can move: the list, the active worktree's
  // branches, and the project's own git state (the checkout switches branches).
  const settle = useCallback(async () => {
    try {
      await refreshProjects()
    } catch (err) {
      setError(wtErrorText(err))

      return
    }
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'worktrees'] })
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'worktree-branches'] })
    await client.invalidateQueries({ queryKey: gitStatusKey(projectName) })
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'branches'] })
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'commits'] })
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'projects'] })
  }, [client, projectName])

  const onSelect = useCallback(
    async id => {
      if (!id || busyId) return

      setBusyId(id)
      setError(null)
      setNotice(null)

      try {
        // Empty body: WorktreeSelectRequest has no fields.
        await selectWorktree(projectName, id)
        setNotice('Active worktree switched.')
        await settle()
      } catch (err) {
        setError(wtErrorText(err))
      } finally {
        setBusyId(null)
      }
    },
    [busyId, projectName, settle]
  )

  const runDelete = useCallback(
    async (worktree, force) => {
      setBusyId(worktree.id)
      setError(null)
      setNotice(null)

      try {
        // {force: true} is the ONLY thing that overrides WORKTREE_DIRTY — the
        // other three protections refuse either way, which is why the button
        // is disabled for them instead of being fired and caught.
        await deleteWorktree(projectName, worktree.id, force)
        setNotice(`Removed ${worktree.branch || worktree.path}.`)
        await settle()
      } catch (err) {
        setError(wtErrorText(err))
      } finally {
        setBusyId(null)
      }
    },
    [projectName, settle]
  )

  const runBulkDelete = useCallback(
    async force => {
      setArmedBulk(false)
      setError(null)
      setNotice(null)
      setBusyId('__bulk__')

      try {
        const result = await deleteAllWorktrees(projectName, force)
        // The bulk route answers per-outcome, so all three parts are reported:
        // a silent skip reads as "it worked" when a worktree is still there.
        const skippedItems = Array.isArray(result?.skipped) ? result.skipped : []
        const errorItems = Array.isArray(result?.errors) ? result.errors : []
        const removed = result?.removed_ids?.length ?? 0

        setNotice(
          `Removed ${removed} worktree${removed === 1 ? '' : 's'}` +
            (skippedItems.length
              ? `, skipped ${skippedItems.length} (${skippedItems.map(item => item.reason).filter(Boolean).join(', ')})`
              : '') +
            (errorItems.length ? `, ${errorItems.length} failed` : '')
        )
        await settle()
      } catch (err) {
        setError(wtErrorText(err))
      } finally {
        setBusyId(null)
      }
    },
    [projectName, settle]
  )

  const doCheckout = useCallback(
    async (branch, opts) => {
      setCheckingOut(true)
      setError(null)
      setNotice(null)

      try {
        const result = await checkoutBranch(projectName, branch, opts)

        setDirtyChoice(null)
        setTarget('')
        setNotice(
          `Checked out ${result?.branch || branch}${result?.stashed ? ' (changes stashed)' : ''}.`
        )
        await settle()
      } catch (err) {
        const parsed = wtError(err)

        // WORKTREE_DIRTY is a QUESTION, not a failure: hold the branch and let
        // the user decide. Anything else is a real error and is shown as-is.
        if (parsed.code === 'WORKTREE_DIRTY') {
          setDirtyChoice({ branch, message: parsed.text })
        } else {
          setError(wtErrorText(err))
        }
      } finally {
        setCheckingOut(false)
      }
    },
    [projectName, settle]
  )

  const badges = worktree => {
    const out = []

    if (worktree.is_current) out.push(createElement(Badge, { key: 'cur', size: 'xs', variant: 'muted' }, 'current'))
    if (worktree.is_locked) out.push(createElement(Badge, { key: 'lock', size: 'xs', variant: 'warn' }, worktree.lock_reason ? `locked: ${worktree.lock_reason}` : 'locked'))
    if (worktree.is_dirty) out.push(createElement(Badge, { key: 'dirty', size: 'xs', variant: 'warn' }, 'dirty'))
    if (worktree.is_protected) out.push(createElement(Badge, { key: 'prot', size: 'xs', variant: 'outline' }, 'protected'))
    if (!worktree.branch) out.push(createElement(Badge, { key: 'det', size: 'xs', variant: 'outline' }, 'detached'))

    return out
  }

  // Exactly the backend's order (current → locked → protected → dirty), so the
  // title names the protection that would actually fire.
  const removeRefusal = worktree => {
    if (worktree.is_current) return "Cannot remove: this is the project's own worktree"
    if (worktree.is_locked) return 'Cannot remove: worktree is locked'
    if (worktree.is_protected) return `Cannot remove: protected branch '${worktree.branch || 'unknown'}'`
    if (worktree.is_dirty) return 'Removing needs force: it has uncommitted changes'

    return 'Remove this worktree'
  }

  const removable = worktree => !worktree.is_current && !worktree.is_locked && !worktree.is_protected

  const rows = worktrees.map(worktree => {
    const busy = busyId === worktree.id
    const isCurrent = worktree.id === currentId

    return createElement(
      'li',
      {
        className: cn('flex items-center gap-1.5 rounded px-1 py-0.5', isCurrent && 'bg-accent/5'),
        key: worktree.id
      },
      createElement(
        'button',
        {
          type: 'button',
          'aria-label': `Select worktree ${worktree.branch || worktree.path}`,
          'aria-pressed': isCurrent,
          className: cn('shrink-0 text-[11px] underline-offset-2', isCurrent ? 'text-muted-foreground/50' : 'hover:underline'),
          disabled: isCurrent || !!busyId,
          onClick: () => onSelect(worktree.id),
          title: isCurrent ? 'Already the active worktree' : 'Make this the active worktree'
        },
        isCurrent ? '●' : '○'
      ),
      createElement('code', { className: 'min-w-0 flex-1 truncate', title: worktree.path }, worktree.branch || worktree.path),
      createElement('span', { className: 'flex shrink-0 items-center gap-1' }, badges(worktree)),
      createElement(
        'button',
        {
          type: 'button',
          className: 'shrink-0 rounded border border-destructive/40 px-1.5 py-0.5 text-[11px] text-destructive hover:bg-destructive/10',
          disabled: !removable(worktree) || !!busyId,
          onClick: () => setArmedDelete(worktree),
          title: removeRefusal(worktree),
          'aria-label': `Remove worktree ${worktree.branch || worktree.path}`
        },
        busy ? '…' : 'Remove'
      )
    )
  })

  const checkoutButton = (label, onClick, disabled) =>
    createElement(
      'button',
      {
        type: 'button',
        className: 'shrink-0 rounded border border-[var(--ui-stroke-secondary)] px-2 py-0.5 text-xs hover:bg-accent/10 disabled:opacity-40',
        disabled,
        onClick
      },
      label
    )

  const dirtyWorktrees = worktrees.filter(worktree => removable(worktree) && worktree.is_dirty).length
  const cleanRemovable = worktrees.filter(removable).length

  return createElement(
    Disclosure,
    { count: worktrees.length, label: 'Worktrees & branches' },
    createElement(
      'div',
      { className: 'mt-2 space-y-2' },

      list.error
        ? createElement('p', { className: 'text-xs text-destructive', role: 'alert' }, wtErrorText(list.error))
        : null,
      error ? createElement('p', { className: 'text-xs text-destructive', role: 'alert' }, error) : null,
      notice ? createElement('p', { className: 'text-xs text-muted-foreground', role: 'status' }, notice) : null,

      createElement(
        'div',
        { className: 'flex flex-wrap items-center gap-1.5' },
        createElement('span', { className: 'text-xs font-medium' }, 'Worktrees'),
        createElement(
          'button',
          {
            type: 'button',
            className: 'rounded border border-[var(--ui-stroke-secondary)] px-2 py-0.5 text-xs hover:bg-accent/10',
            onClick: () => list.refetch?.(),
            title: 'Re-read the worktree list',
            disabled: list.isFetching
          },
          list.isFetching ? 'Loading…' : 'Refresh'
        ),
        createElement(
          'button',
          {
            type: 'button',
            className: 'rounded border border-destructive/40 px-2 py-0.5 text-xs text-destructive hover:bg-destructive/10 disabled:opacity-40',
            disabled: cleanRemovable === 0 && dirtyWorktrees === 0,
            onClick: () => {
              setBulkForce(false)
              setArmedBulk(true)
            },
            title: 'Remove every non-current, non-locked, non-protected worktree'
          },
          busyId === '__bulk__' ? 'Removing…' : 'Delete all'
        )
      ),

      list.isPending && !worktrees.length
        ? createElement('p', { className: 'text-xs text-muted-foreground' }, 'Reading worktrees…')
        : worktrees.length === 0
          ? createElement('p', { className: 'text-xs text-muted-foreground' }, 'No worktrees found.')
          : createElement('ul', { className: 'max-h-56 space-y-0.5 overflow-y-auto text-xs' }, rows),

      // ── branch checkout ────────────────────────────────────────────────
      createElement(
        'div',
        { className: 'space-y-1.5 border-t border-border pt-2' },
        createElement('div', { className: 'flex flex-wrap items-center gap-1.5' },
          createElement('span', { className: 'text-xs font-medium' }, 'Branch'),
          createElement(
            'button',
            {
              type: 'button',
              className: 'rounded border border-[var(--ui-stroke-secondary)] px-2 py-0.5 text-xs hover:bg-accent/10',
              disabled: branchesQuery.isFetching,
              onClick: () => branchesQuery.refetch?.()
            },
            branchesQuery.isFetching ? 'Loading…' : 'Refresh'
          ),
          currentBranch ? createElement(Badge, { size: 'xs', variant: 'muted' }, currentBranch) : null
        ),

        branchesQuery.error
          ? createElement('p', { className: 'text-xs text-destructive' }, wtErrorText(branchesQuery.error))
          : null,

        createElement(
          'div',
          { className: 'flex flex-wrap items-center gap-1.5' },
          createElement(
            'select',
            {
              'aria-label': `Branch to check out in ${projectName}`,
              className: 'min-w-0 flex-1 rounded border border-[var(--ui-stroke-secondary)] bg-background px-2 py-1 text-xs outline-none focus:border-[var(--ui-stroke-focus)]',
              disabled: checkingOut,
              onChange: event => setTarget(event.target.value),
              value: target
            },
            createElement('option', { value: '' }, branches.length ? 'Pick a local branch…' : 'No branches'),
            branches.map(branch =>
              createElement(
                'option',
                {
                  // A remote-only branch is refused by the backend
                  // (BRANCH_IS_REMOTE_ONLY), so it is not offered as a target.
                  disabled: branch.is_remote,
                  key: `${branch.is_remote ? 'r' : 'l'}-${branch.name}`,
                  value: branch.name
                },
                `${branch.name}${branch.is_current ? ' (current)' : ''}${branch.is_remote ? ' (remote)' : ''}`
              )
            )
          ),
          checkoutButton(
            checkingOut ? 'Checking out…' : 'Checkout',
            () => target && doCheckout(target, {}),
            checkingOut || !target
          )
        ),

        // The dirty-tree question, answered by the user — never auto-stashed.
        dirtyChoice
          ? createElement(
              'div',
              { className: 'rounded border border-[var(--ui-stroke-focus)] p-2 text-xs', 'data-dirty-choice': dirtyChoice.branch },
              createElement('p', { className: 'mb-1.5' }, `${dirtyChoice.message} Pick what happens to those changes:`),
              createElement(
                'div',
                { className: 'flex flex-wrap gap-1.5' },
                checkoutButton('Stash & checkout', () => doCheckout(dirtyChoice.branch, { stash: true }), false),
                checkoutButton('Force checkout', () => doCheckout(dirtyChoice.branch, { force: true }), false),
                checkoutButton('Cancel', () => setDirtyChoice(null), false)
              )
            )
          : null
      ),

      // ── confirms ───────────────────────────────────────────────────────
      // Delete-with-force: force is the confirm's own action, so the label
      // names the loss rather than hiding it behind a generic "Remove".
      createElement(ConfirmDialog, {
        cancelLabel: 'Keep',
        confirmLabel: armedDelete?.is_dirty ? 'Remove anyway' : 'Remove',
        description: armedDelete
          ? `${armedDelete.path}${armedDelete.branch ? ` (${armedDelete.branch})` : ''}` +
            (armedDelete.is_dirty
              ? '\n\nIt has uncommitted changes. Removing discards them — this cannot be undone.'
              : '')
          : '',
        destructive: true,
        onClose: () => setArmedDelete(null),
        onConfirm: async () => {
          const target_ = armedDelete

          setArmedDelete(null)
          if (target_) await runDelete(target_, target_.is_dirty)
        },
        open: !!armedDelete,
        title: 'Remove worktree?'
      }),

      createElement(ConfirmDialog, {
        cancelLabel: 'Cancel',
        confirmLabel: bulkForce ? 'Force delete all' : `Delete ${cleanRemovable}`,
        description:
          `Removes ${cleanRemovable} worktree${cleanRemovable === 1 ? '' : 's'} permanently.` +
          (dirtyWorktrees
            ? `\n\n${dirtyWorktrees} dirty worktree${dirtyWorktrees === 1 ? '' : 's'} will be ${bulkForce ? 'FORCE-REMOVED with their uncommitted changes discarded' : 'skipped'}.`
            : ''),
        destructive: true,
        // The force choice is a real checkbox, not a second dialog stack.
        children: dirtyWorktrees
          ? createElement(
              'label',
              { className: 'mb-2 flex items-start gap-1.5 text-xs' },
              createElement('input', {
                type: 'checkbox',
                checked: bulkForce,
                onChange: event => setBulkForce(event.target.checked),
                style: { marginTop: 2 }
              }),
              createElement('span', null, `Also force-remove the ${dirtyWorktrees} dirty one${dirtyWorktrees === 1 ? '' : 's'} (discards uncommitted changes)`)
            )
          : null,
        onClose: () => setArmedBulk(false),
        onConfirm: async () => {
          const force = bulkForce

          setBulkForce(false)
          await runBulkDelete(force)
        },
        open: armedBulk,
        title: 'Delete removable worktrees?'
      })
    )
  )
}

function WorkingTreePanel({ projectName }) {
  const client = useQueryClient()
  const status = useQuery({
    enabled: !!projectName,
    queryFn: () => fetchGitStatus(projectName),
    queryKey: gitStatusKey(projectName),
    // The tree moves under the panel; a short window keeps a remount from
    // re-running git without letting a stale list sit on screen.
    staleTime: 5_000
  })

  const [error, setError] = useState(null)
  const [busyPath, setBusyPath] = useState(null)
  const [fetching, setFetching] = useState(false)
  const [stashing, setStashing] = useState(false)
  const [confirm, setConfirm] = useState(null)
  const [stashConfirm, setStashConfirm] = useState(false)
  const [diff, setDiff] = useState(null)

  // After a mutation the SERVER caches /projects, and the parent's badges read
  // that payload — so bust the backend cache FIRST, then refetch our queries.
  // (Same order as the dashboard's refreshStatus → refreshProjects.)
  const settle = useCallback(async () => {
    // Bust the backend cache FIRST (the badges read that payload), then
    // refetch ours. The catch is NOT swallowed: if the bust fails the server
    // cache is still warm and an invalidate would repaint pre-mutation badges
    // with no error — the exact failure this order exists to prevent.
    try {
      await refreshProjects()
    } catch (err) {
      setError(errText(err) || 'Could not refresh projects; badges may be stale')
      return
    }
    await client.invalidateQueries({ queryKey: PM_QUERY_ROOT })
  }, [client])

  const runFileOp = useCallback(
    async (action, path) => {
      if (!path || busyPath) return

      setBusyPath(path)
      setError(null)

      try {
        const result = await fileOp(projectName, action, path)

        setBusyPath(null)

        // A path that escapes the repo, or any other git failure, is a RESOLVED
        // 200 with success:false. Checking the flag is the whole ballgame.
        if (!result?.success) {
          setError(result?.message || `Git ${action} failed.`)

          return
        }

        await settle()
      } catch (err) {
        setBusyPath(null)
        setError(errText(err))
      }
    },
    [busyPath, projectName, settle]
  )

  const runFetch = useCallback(async () => {
    if (fetching) return

    setFetching(true)
    setError(null)

    try {
      const result = await gitFetch(projectName)

      setFetching(false)

      if (!result?.success) {
        setError(result?.message || 'Fetch failed.')

        return
      }

      // Fetch moves the remote refs, so behind/ahead can change — refresh the
      // same way every other mutating op does.
      await settle()
    } catch (err) {
      setFetching(false)
      setError(errText(err))
    }
  }, [fetching, projectName, settle])

  // `git stash push -u` sweeps every change (untracked included) off the tree
  // and leaves it on the stack. Same shape as runFetch: the stash route raises
  // a 400 {detail} on failure and answers {success, message, stderr} otherwise,
  // so BOTH shapes have to be checked.
  const runStash = useCallback(async () => {
    if (stashing) return

    setStashing(true)
    setError(null)

    try {
      const result = await gitStash(projectName)

      setStashing(false)

      if (!result?.success) {
        setError(result?.message || result?.stderr || 'Stash failed.')

        return
      }

      // The whole point of the op: the tree is clean now, so re-read status.
      await settle()
    } catch (err) {
      setStashing(false)
      setError(errText(err))
    }
  }, [projectName, settle, stashing])

  const openDiff = useCallback(async path => {
    if (!path) return

    setDiff({ path, loading: true, data: null, error: null })

    try {
      const data = await fetchDiff(projectName, path)

      // Drop a late response whose viewer was closed or moved on meanwhile.
      setDiff(prev => (prev?.path === path ? { path, loading: false, data, error: null } : prev))
    } catch (err) {
      setDiff(prev => (prev?.path === path ? { path, loading: false, data: null, error: errText(err) } : prev))
    }
  }, [projectName])

  const closeDiff = useCallback(() => setDiff(null), [])

  // Escape closes the viewer. The listener only exists while it is on screen,
  // so it can't swallow keys anywhere else on the page.
  useEffect(() => {
    if (!diff) return

    const onKeyDown = e => {
      if (e.key === 'Escape') closeDiff()
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [closeDiff, diff])

  const files = status.data
  const modified = files?.modified_files || []
  const untracked = files?.untracked_files || []
  const locked = busyPath !== null || fetching || stashing

  const fileRow = (file, key, isUntracked) => {
    const path = filePath(file)
    const busy = busyPath === path

    const action = (label, hint, disabled, onClick, destructive) =>
      createElement(
        Button,
        {
          'aria-label': hint,
          className: cn(
            'shrink-0 px-1.5 text-[11px]',
            destructive && 'border-destructive/40 text-destructive hover:bg-destructive/10'
          ),
          disabled: locked || disabled,
          key: label,
          onClick,
          size: 'sm',
          title: hint,
          variant: 'outline'
        },
        busy && label === 'Stage' ? '…' : label
      )

    return createElement(
      'li',
      { key, className: 'flex items-center gap-1.5' },
      createElement(
        Badge,
        { className: 'w-6 shrink-0 justify-center text-[10px]', variant: isUntracked ? 'muted' : 'outline' },
        isUntracked ? '?' : fileStatus(file)
      ),
      createElement('code', { className: 'min-w-0 flex-1 truncate', title: path }, path),
      action('Stage', `Stage ${path}`, false, () => runFileOp('stage', path)),
      action(
        'Unstage',
        hasIndexChange(file) ? `Unstage ${path}` : `${path} has nothing staged`,
        !hasIndexChange(file),
        () => runFileOp('unstage', path)
      ),
      action('Diff', `Show diff for ${path}`, false, () => openDiff(path)),
      // Discard only ARMS the confirm here — nothing is requested until the
      // dialog's onConfirm runs.
      action(
        'Discard',
        `Discard changes in ${path}`,
        false,
        () => setConfirm({ path, untracked: isUntracked, indexStatus: fileStatus(file) }),
        true
      )
    )
  }

  const list = createElement(
    'ul',
    { className: 'max-h-72 space-y-1 overflow-y-auto text-xs' },
    modified.map((file, i) => fileRow(file, `m${i}`, false)),
    untracked.map((file, i) => fileRow(file, `u${i}`, true))
  )

  const body = !projectName
    ? createElement('p', { className: 'text-sm text-muted-foreground' }, 'Pick a project to see its working tree.')
    : status.error
      ? createElement('p', { className: 'text-sm text-destructive', role: 'alert' }, errText(status.error))
      : status.isPending
        ? createElement('p', { className: 'text-sm text-muted-foreground' }, 'Reading working tree…')
        : !modified.length && !untracked.length
          ? createElement('p', { className: 'text-sm text-muted-foreground' }, 'Working tree clean.')
          : list

  return createElement(
    'section',
    { 'aria-label': 'Working tree', className: 'space-y-2 border-t border-border pt-2' },
    createElement(
      'div',
      { className: 'flex flex-wrap items-center gap-1.5' },
      files?.branch
        ? createElement(
            'span',
            { className: 'truncate text-xs text-muted-foreground', title: files.branch },
            `${files.branch}${files.ahead || files.behind ? ` ↑${files.ahead || 0} ↓${files.behind || 0}` : ''}`
          )
        : null,
      createElement(
        Button,
        {
          disabled: fetching,
          onClick: runFetch,
          size: 'sm',
          title: 'Fetch from origin without changing the working tree',
          variant: 'outline'
        },
        fetching ? 'Fetching…' : 'Fetch'
      ),
      // Stash-all sits beside the commit field on the web. It only arms the
      // confirm here — nothing is requested until the dialog confirms.
      createElement(
        Button,
        {
          className: 'border-destructive/40 text-destructive hover:bg-destructive/10',
          disabled: locked,
          onClick: () => setStashConfirm(true),
          size: 'sm',
          title: 'Move all changes (including untracked) to the stash',
          variant: 'outline'
        },
        stashing ? 'Stashing…' : 'Stash all'
      )
    ),
    // The op's own failure message — a rejected promise must never look like a
    // no-op, and it must not be the only place the reason shows up.
    error ? createElement('p', { className: 'text-sm text-destructive', role: 'alert' }, error) : null,
    body,
    createElement(ConfirmDialog, {
      cancelLabel: 'Cancel',
      confirmLabel: 'Stash all',
      description:
        'This runs `git stash push -u` — every change, tracked and untracked, leaves the working tree. ' +
        'The entry stays on the stack (`stash@{0}`) so `git stash pop` brings it back. This cannot be undone from here.',
      destructive: true,
      onClose: () => setStashConfirm(false),
      onConfirm: async () => {
        setStashConfirm(false)
        await runStash()
      },
      open: stashConfirm,
      title: 'Stash all changes?'
    }),
    createElement(ConfirmDialog, {
      cancelLabel: 'Cancel',
      confirmLabel: 'Discard',
      description: confirm ? discardCopy(confirm.path, confirm.untracked, confirm.indexStatus) : '',
      destructive: true,
      // The op runs HERE, not in the button — an unconfirmed discard must never
      // reach the network.
      onClose: () => setConfirm(null),
      onConfirm: async () => {
        const target = confirm

        setConfirm(null)

        if (target) await runFileOp('discard', target.path)
      },
      open: !!confirm,
      title: 'Discard changes?'
    }),
    diff ? createElement(DiffOverlay, { diff, onClose: closeDiff }) : null,
    // Under the working tree, same order as the web card: branches, commits,
    // then the worktree block that owns the checkout.
    createElement(ProjectBranches, { projectName }),
    createElement(ProjectCommits, { projectName }),
    createElement(WorktreesSection, { projectName })
  )
}

// ── [3] PROJECTS PAGE (from page.js) ───────────────────────────────────────
// Projects list page for the Desktop surface. Reads exclusively through the
// data layer above — never build a path here, the REST door owns encoding.

// The backend only accepts page_size 9 / 21 / 27 — 21 is the middle grid.
const PAGE_SIZE = 21

// Cache key for one page of results. The search term and page MUST be in
// here: omit either and switching search paints the previous rows.
//
// The list is keyed UNDER the panel's PM_QUERY_ROOT on purpose: the panel
// invalidates that root after every mutation, and the list's dirty/ahead
// badges are read from the same /projects payload the panel busts. A list
// keyed outside that root keeps painting pre-mutation badges.
const projectsKey = (query, page) => [...PM_QUERY_ROOT, 'projects', query, page]

/** Debounce the search box so a keystroke storm is one request, not twelve. */
function useDebounced(value, delay = 300) {
  const [settled, setSettled] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])

  return settled
}

const truncate = (text, max) => (text.length <= max ? text : `${text.slice(0, max - 1)}…`)

/** Local git remote paths (git@host:owner/repo) are long and share a prefix. */
const shortRemote = url => {
  const tail = url.slice(url.lastIndexOf('/') + 1).replace(/\.git$/, '')
  return tail.length > 0 && tail.length <= 32 ? tail : truncate(url, 34)
}

const relativeAge = iso => {
  const ms = Date.parse(iso)
  if (!Number.isFinite(ms)) return null
  const rel = relativeTime(ms)
  // Intl signs the past with '-'; this reads better as "3 days ago".
  return rel.startsWith('-') ? `${rel.slice(1)} ago` : 'just now'
}

/**
 * Watched folders — the source of the project list, so without it the page is
 * read-only on a fresh install. Same routes the web dashboard uses: list,
 * add, rename, reorder, remove, and the path autocomplete.
 */
function WatchFolders({ client }) {
  const [path, setPath] = useState('')
  const [type, setType] = useState('scan_root')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  // Inline label edit: {id, value, saving}. The web double-clicks a label;
  // an always-present input is keyboard-reachable with no hidden gesture.
  const [editing, setEditing] = useState(null)
  const [reordering, setReordering] = useState(false)
  const [suggestions, setSuggestions] = useState([])
  // The value the user just picked from the dropdown. Without it the debounce
  // re-queries for the picked path and the listbox springs back open on the
  // item that was just clicked.
  const [picked, setPicked] = useState(null)

  const { data, isPending } = useQuery({
    queryKey: [...PM_QUERY_ROOT, 'watch-paths'],
    queryFn: fetchWatchPaths
  })

  const folders = data?.watch_paths ?? []

  // A rename or a reorder changes what the projects list is ordered by, so
  // both bust the SERVER cache first — same order submit()/remove() use.
  const refetchAll = async () => {
    await refreshProjects()
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'watch-paths'] })
    await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'projects'] })
  }

  const submit = async () => {
    const trimmed = path.trim()
    if (!trimmed || saving) return
    setSaving(true)
    setError(null)
    try {
      await addWatchPath(trimmed, type)
      setPath('')
      setSuggestions([])
      // A new folder means a new scan: bust the server cache, then re-read.
      await refetchAll()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setSaving(false)
    }
  }

  const remove = async id => {
    setError(null)
    try {
      await deleteWatchPath(id)
      await refetchAll()
    } catch (err) {
      setError(errorText(err))
    }
  }

  const saveLabel = async (entry, value) => {
    const label = value.trim()
    // An empty label is a 400 (label must not be empty) and the row already
    // falls back to its path, so just close the editor untouched.
    if (!label || label === (entry.label ?? '')) {
      setEditing(null)

      return
    }
    setEditing({ ...editing, saving: true })
    setError(null)
    try {
      // Body is exactly {label} — RenameRequest has one field.
      await renameWatchPath(entry.id, label)
      setEditing(null)
      await refetchAll()
    } catch (err) {
      setEditing(prev => (prev ? { ...prev, saving: false } : prev))
      setError(errorText(err))
    }
  }

  // ▲ / ▼ instead of drag: keyboard-reachable and no drag library. The server
  // assigns sort_order from the array index, so the FULL id list goes up.
  const move = async (index, delta) => {
    if (reordering) return
    const target = index + delta
    if (target < 0 || target >= folders.length) return
    const ids = folders.map(entry => entry.id)
    const [moved] = ids.splice(index, 1)
    ids.splice(target, 0, moved)
    setReordering(true)
    setError(null)
    try {
      await reorderWatchPaths(ids)
      await refetchAll()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setReordering(false)
    }
  }

  // Debounced path autocomplete. The route 400s on a prefix under 2 chars, so
  // the request is only made once the input is long enough to be useful.
  const debouncedPath = useDebounced(path, 200)

  useEffect(() => {
    const prefix = debouncedPath.trim()
    // A pick is the user's answer, not a new question: hold the listbox shut
    // until they actually type again.
    if (picked !== null && prefix === picked) {
      setSuggestions([])

      return
    }
    if (prefix.length < 2) {
      setSuggestions([])

      return
    }
    let live = true
    suggestWatchPaths(prefix).then(
      payload => {
        if (!live) return
        setSuggestions(Array.isArray(payload?.suggestions) ? payload.suggestions : [])
      },
      // A bad prefix just means no dropdown — never an error the user must clear.
      () => {
        if (live) setSuggestions([])
      }
    )
    return () => {
      live = false
    }
  }, [debouncedPath, picked])

  const field = 'w-full rounded border border-[var(--ui-stroke-secondary)] bg-transparent px-2 py-1 text-sm outline-none focus:border-[var(--ui-stroke-focus)]'

  return createElement(
    'section',
    { className: 'rounded-[6px] border border-[var(--ui-stroke-secondary)] p-3', 'aria-label': 'Watched folders' },
    createElement('h2', { className: 'mb-2 text-sm font-medium' }, 'Watched folders'),
    isPending
      ? createElement('div', { className: 'text-sm text-muted-foreground' }, 'Loading…')
      : folders.length === 0
        ? createElement(
            'p',
            { className: 'mb-2 text-xs text-muted-foreground' },
            'No folders watched yet. Add one and the projects inside it appear below.'
          )
        : createElement(
            'ul',
            { className: 'mb-2 space-y-1' },
            folders.map((entry, index) => {
              const isEditing = editing?.id === entry.id
              const pathCell = createElement('span', { className: 'truncate', title: entry.path }, entry.path)

              return createElement(
                'li',
                { key: String(entry.id), className: 'flex items-center gap-2 text-xs' },
                // Reorder handles. Disabled at the ends so the row they move
                // past always exists.
                createElement(
                  'span',
                  { className: 'flex shrink-0 flex-col' },
                  createElement(
                    'button',
                    {
                      type: 'button',
                      'aria-label': `Move ${entry.path} up`,
                      className: 'h-3 leading-none text-[9px] text-muted-foreground hover:text-foreground disabled:opacity-30',
                      disabled: reordering || index === 0,
                      onClick: () => move(index, -1),
                      title: 'Move up'
                    },
                    '▲'
                  ),
                  createElement(
                    'button',
                    {
                      type: 'button',
                      'aria-label': `Move ${entry.path} down`,
                      className: 'h-3 leading-none text-[9px] text-muted-foreground hover:text-foreground disabled:opacity-30',
                      disabled: reordering || index === folders.length - 1,
                      onClick: () => move(index, 1),
                      title: 'Move down'
                    },
                    '▼'
                  )
                ),
                createElement('span', { className: 'min-w-0 flex-1 truncate' }, pathCell),
                createElement(Badge, { variant: 'muted', size: 'xs' }, entry.type === 'single_repo' ? 'single repo' : 'scan root'),
                isEditing
                  ? createElement(
                      'span',
                      { className: 'flex shrink-0 items-center gap-1' },
                      createElement('input', {
                        type: 'text',
                        autoFocus: true,
                        disabled: editing.saving,
                        value: editing.value,
                        'aria-label': `Label for ${entry.path}`,
                        placeholder: 'Label',
                        className: 'w-32 rounded border border-[var(--ui-stroke-secondary)] bg-background px-1.5 py-0.5 text-xs outline-none focus:border-[var(--ui-stroke-focus)]',
                        onChange: event => setEditing({ ...editing, value: event.target.value }),
                        // Enter saves, Escape abandons — a blur also saves, but
                        // Escape must not be undone by the blur that follows it.
                        onKeyDown: event => {
                          if (event.key === 'Enter') {
                            event.preventDefault()
                            saveLabel(entry, editing.value)
                          }
                          if (event.key === 'Escape') {
                            event.preventDefault()
                            setEditing(null)
                          }
                        },
                        onBlur: () => saveLabel(entry, editing.value)
                      }),
                      createElement(
                        'button',
                        {
                          type: 'button',
                          className: 'shrink-0 rounded border border-[var(--ui-stroke-secondary)] px-2 py-0.5 hover:bg-accent/10 disabled:opacity-50',
                          disabled: editing.saving,
                          onMouseDown: event => event.preventDefault(),
                          onClick: () => saveLabel(entry, editing.value)
                        },
                        editing.saving ? 'Saving…' : 'Save'
                      )
                    )
                  : createElement(
                      'button',
                      {
                        type: 'button',
                        className: 'max-w-40 shrink-0 truncate rounded border border-transparent px-1.5 py-0.5 text-xs text-muted-foreground hover:border-[var(--ui-stroke-secondary)] hover:bg-accent/10',
                        onClick: () => setEditing({ id: entry.id, value: entry.label ?? '', saving: false }),
                        title: entry.label ? `${entry.label} — edit label` : 'Add a label'
                      },
                      entry.label || '—'
                    ),
                createElement('button', {
                  type: 'button',
                  className: 'shrink-0 rounded border border-[var(--ui-stroke-secondary)] px-2 py-0.5 hover:bg-accent/10',
                  onClick: () => remove(entry.id)
                }, 'Remove')
              )
            })
          ),
    error ? createElement('p', { role: 'alert', className: 'mb-2 text-xs text-destructive' }, error) : null,
    createElement(
      'div',
      { className: 'relative flex flex-wrap items-center gap-1.5' },
      createElement('input', {
        type: 'text',
        className: `${field} min-w-0 flex-1`,
        placeholder: '/home/user/projects',
        'aria-label': 'Folder path',
        value: path,
        // Typing after a pick re-opens the question; a programmatic set does not.
        onChange: event => {
          setPicked(null)
          setPath(event.target.value)
        },
        onKeyDown: event => {
          if (event.key === 'Enter') {
            event.preventDefault()
            submit()
          }
        }
      }),
      createElement(
        'select',
        { className: `${field} w-auto`, value: type, onChange: event => setType(event.target.value) },
        createElement('option', { value: 'scan_root' }, 'Scan subfolders'),
        createElement('option', { value: 'single_repo' }, 'This folder is one repo')
      ),
      createElement(
        'button',
        {
          type: 'button',
          className: 'shrink-0 rounded border border-[var(--ui-stroke-secondary)] px-2 py-1 text-sm hover:bg-accent/10 disabled:opacity-50',
          disabled: saving || !path.trim(),
          onClick: submit
        },
        saving ? 'Adding…' : 'Add folder'
      ),
      // Autocomplete dropdown. Rendered inside the same relative wrapper as the
      // input so it sits under it without a portal.
      suggestions.length
        ? createElement(
            'ul',
            {
              className:
                'absolute left-0 right-0 top-full z-20 mt-0.5 max-h-48 overflow-y-auto rounded border border-[var(--ui-stroke-secondary)] bg-background py-0.5 text-xs shadow-md',
              role: 'listbox',
              'aria-label': 'Path suggestions',
              'data-suggestions': suggestions.length
            },
            suggestions.map(item =>
              createElement(
                'li',
                { key: item, role: 'option', 'aria-selected': false },
                createElement(
                  'button',
                  {
                    type: 'button',
                    className: 'block w-full truncate px-2 py-1 text-left hover:bg-accent/10',
                    onClick: () => {
                      setPicked(item)
                      setPath(item)
                      setSuggestions([])
                    }
                  },
                  item
                )
              )
            )
          )
        : null
    )
  )
}

/**
 * One project row.
 *
 * `panel` is the working-tree detail for THIS project (null when another one
 * is selected). It renders inside the card so the detail sits under the project
 * it belongs to — at the bottom of a long list it was off-screen, which made
 * the page unusable.
 */
function ProjectCard({
  project,
  selected,
  onSelect,
  panel,
  panelError,
  cardError,
  busyPath,
  onCardAction,
  onOpenRemote,
  onOpenBranch
}) {
  const branch = project.current_branch || 'DETACHED'
  const age = project.last_commit_date ? relativeAge(project.last_commit_date) : null
  const isBusy = busyPath === project.name
  // A session can only be aimed at a folder where the SDK exposes the
  // gateway door; an older desktop without it shows the button disabled
  // rather than a button that always fails.
  const canOpenSession =
    typeof host?.request === 'function' && typeof host?.openSession === 'function' && !!project.path

  // Per-card actions, matching the web dashboard's rules: Pull is refused by
  // the backend on a dirty tree, and Commit only makes sense when there is
  // something to commit.
  const action = (label, title, disabled, onClick) =>
    createElement(
      'button',
      {
        type: 'button',
        className: cn(
          'shrink-0 rounded border px-2 py-0.5 text-xs transition-colors',
          disabled
            ? 'cursor-not-allowed border-[var(--ui-stroke-secondary)] text-muted-foreground/50'
            : 'border-[var(--ui-stroke-secondary)] hover:bg-accent/10'
        ),
        title,
        disabled: disabled || isBusy,
        'aria-label': `${label} ${project.name}`,
        onClick: event => {
          // The card itself is a button; without this the action click would
          // bubble and toggle the selection as well.
          event.stopPropagation()
          onClick()
        }
      },
      label
    )

  const head = createElement(
    'div',
    {
      className: cn(
        'flex w-full items-center gap-3 rounded-[6px] px-3 py-2 text-left transition-colors',
        selected ? 'bg-accent/5' : 'hover:bg-accent/5'
      ),
      role: 'button',
      tabIndex: 0,
      'aria-expanded': !!selected,
      'data-project': project.name,
      onClick: () => onSelect?.(project.name),
      onKeyDown: event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect?.(project.name)
        }
      }
    },
    createElement(
      'div',
      { className: 'flex min-w-0 w-40 shrink-0 items-center gap-1.5' },
      createElement(
        'span',
        {
          className: cn(
            'truncate text-sm font-medium',
            // The web card turns the project NAME into the link to its
            // repository. Same here — the remote decides whether the name is
            // a link or plain text.
            project.remote_url ? 'cursor-pointer text-accent hover:underline' : 'text-foreground'
          ),
          title: project.remote_url ? 'Open the Git repository' : 'No remote URL',
          'data-remote-link': project.name,
          onClick: event => {
            event.stopPropagation()
            onOpenRemote?.(project)
          }
        },
        project.name
      ),
      project.branch_count > 0
        ? createElement(Badge, { variant: 'muted', size: 'xs', title: 'local branches' }, `${project.branch_count} br`)
        : null
    ),

    createElement(
      'div',
      { className: 'flex min-w-0 w-32 shrink-0 items-center gap-1 text-xs text-muted-foreground' },
      createElement(Codicon, { name: 'source-control', size: '0.85em' }),
      createElement(
        'span',
        {
          className: cn('truncate', project.remote_url ? 'cursor-pointer hover:underline' : ''),
          title: project.remote_url ? `Open ${branch} on the remote` : branch,
          'data-branch-link': project.name,
          onClick: event => {
            event.stopPropagation()
            onOpenBranch?.(project)
          }
        },
        branch
      )
    ),

    createElement(
      'div',
      { className: 'flex shrink-0 items-center gap-1' },
      project.is_dirty ? createElement(Badge, { variant: 'warn', size: 'xs' }, 'dirty') : null,
      project.ahead > 0 ? createElement(Badge, { variant: 'success', size: 'xs' }, `↑${project.ahead}`) : null,
      project.behind > 0 ? createElement(Badge, { variant: 'destructive', size: 'xs' }, `↓${project.behind}`) : null,
      !project.is_dirty && !project.ahead && !project.behind
        ? createElement(Badge, { variant: 'outline', size: 'xs' }, 'clean')
        : null,
      project.remote_url
        ? createElement('span', { className: 'truncate text-xs text-muted-foreground', title: project.remote_url }, shortRemote(project.remote_url))
        : createElement(Badge, { variant: 'muted', size: 'xs' }, 'no remote')
    ),

    createElement(
      'div',
      { className: 'min-w-0 flex-1' },
      project.last_commit_message
        ? createElement(
            'div',
            { className: 'truncate text-xs text-muted-foreground', title: project.last_commit_message },
            project.last_commit_message
          )
        : null
    ),

    age ? createElement('div', { className: 'shrink-0 text-xs text-muted-foreground/70' }, age) : null,

    createElement(
      'div',
      { className: 'flex shrink-0 items-center gap-1' },
      action('Pull', 'Fast-forward from the upstream branch', !!project.is_dirty, () => onCardAction('pull', project.name)),
      action('Fetch', 'Fetch from the remote without touching the working tree', false, () => onCardAction('fetch', project.name)),
      // Same rule as the web card: a Push button with nothing to push is just
      // a way to get a rejection, so it only exists when ahead > 0.
      project.ahead > 0
        ? action('Push', `Push ${project.ahead} commit${project.ahead === 1 ? '' : 's'} to the remote branch`, false, () => onCardAction('push', project.name))
        : null,
      project.is_dirty
        ? action('Commit', 'Commit the staged changes', false, () => onCardAction('commit', project.name))
        : null,
      // A fresh session in the project's own folder. The web dashboard's
      // "open in workspace" is the same idea; the desktop can do it
      // directly, because the gateway takes the folder on session.create.
      action(
        'Chat',
        project.path
          ? `Open a new session in ${project.path}`
          : 'This project has no known folder on the server',
        !canOpenSession,
        () => onCardAction('session', project.name)
      )
    )
  )

  return createElement(
    'div',
    {
      className: cn(
        'w-full rounded-[6px] border',
        selected ? 'border-[var(--ui-stroke-focus)]' : 'border-[var(--ui-stroke-secondary)]'
      ),
      'data-card': project.name
    },
    head,
    // A card-action failure renders on ITS OWN card, unselected, so a
    // rejected fetch is visible where the user clicked it.
    cardError
      ? createElement(
          'div',
          { role: 'alert', 'data-card-error': project.name, className: 'px-3 pb-2 text-xs text-destructive' },
          cardError
        )
      : null,
    panel || panelError
      ? createElement(
          'div',
          { className: 'border-t border-[var(--ui-stroke-secondary)] px-3 py-2', 'data-detail': project.name },
          panelError
            ? createElement('div', { role: 'alert', className: 'text-sm text-destructive' }, panelError)
            : null,
          !panel && !panelError
            ? createElement('div', { className: 'text-sm text-muted-foreground' }, 'Loading working tree…')
            : null,
          panel ?? null
        )
      : null
  )
}

/**
 * Pull a readable message out of either error shape the bridge can throw:
 * the plugin's contract error {error, code} or FastAPI's {detail}. A failed
 * list must say WHY, never render blank.
 */
function errorText(err) {
  if (!err) return 'Unknown error'
  const parsed = (() => {
    const raw = err.body ?? err.data ?? null
    if (raw && typeof raw === 'object') return raw
    const match = /\{.*\}/.exec(String(err.message ?? ''))
    if (match) {
      try {
        return JSON.parse(match[0])
      } catch {
        return null
      }
    }
    return null
  })()

  if (err.code === 'PROJECT_NOT_FOUND' || parsed?.code === 'PROJECT_NOT_FOUND') {
    return parsed?.error ?? err.detail ?? err.message ?? 'Project not found'
  }
  return err.detail ?? parsed?.detail ?? parsed?.error ?? err.message ?? 'Request failed'
}

// The detail panel used to be a SIBLING module loaded on demand, so a
// missing or broken sibling could not take the whole list down with it. There
// is no sibling module any more — the panel is inlined above — so the panel
// is the static happy path and this loader only has to hand it over. The
// failure branch is kept: if the inlined panel is ever removed or renamed,
// the page says so instead of rendering a broken tree.
const loadWorkingTreePanel = () =>
  Promise.resolve().then(() => {
    if (typeof WorkingTreePanel !== 'function') {
      throw new Error('working tree panel is unavailable')
    }

    return { WorkingTreePanel }
  })

function ProjectsPage() {
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState(null)
  const [panel, setPanel] = useState(null)
  const [panelError, setPanelError] = useState(null)
  // Card-action failures belong to the card that failed. Paging them into the
  // page-level panelError painted them on whichever card happened to be open —
  // or nowhere at all, when none was.
  const [cardError, setCardError] = useState(null)
  const [busy, setBusy] = useState(false)
  // Which project's card action is in flight. Per-card, so clicking one row
  // never disables every other row's buttons.
  const [busyPath, setBusyPath] = useState(null)
  // Commit asks for a message in a real inline form: there is no
  // window.prompt in the Electron renderer, and a modal is what the web
  // dashboard does too.
  const [commitFor, setCommitFor] = useState(null)
  const [commitMessage, setCommitMessage] = useState('')
  // Watch folders + the bulk pull.
  const [watchOpen, setWatchOpen] = useState(false)
  const [pullingAll, setPullingAll] = useState(false)
  const [notice, setNotice] = useState(null)

  const query = useDebounced(search)
  const client = useQueryClient()

  // A new search invalidates the current page number — page 4 of a term
  // with two hits is an empty list, not an error.
  useEffect(() => {
    setPage(1)
  }, [query])

  const { data, isPending, error } = useQuery({
    queryKey: projectsKey(query, page),
    queryFn: () => fetchProjects(page, PAGE_SIZE, query),
    refetchInterval: 30_000
  })

  // The detail panel is inlined in this module, so there is nothing to
  // fetch; the load stays async so the panel still arrives after the
  // selection renders, and a panel that cannot load shows its error
  // instead of taking the list down with it.
  useEffect(() => {
    if (!selected) {
      setPanel(null)
      setPanelError(null)
      return
    }
    let live = true
    loadWorkingTreePanel().then(
      mod => {
        if (live) {
          setPanel(() => mod)
          setPanelError(null)
        }
      },
      err => {
        if (live) setPanelError(errorText(err))
      }
    )
    return () => {
      live = false
    }
  }, [selected])

  // A card's Pull / Fetch / Commit. `message` is supplied by the commit form;
  // there is no prompt to answer, because the Electron renderer has none.
  /**
   * Open a fresh session whose working directory is the project's own
   * folder. The gateway's session.create takes a cwd, and that RPC is the
   * only way a plugin can aim a session at a directory: ctx.newChat() takes
   * a profile, and the project scope is the app's own state.
   */
  const openSessionFor = async name => {
    const project = (data?.projects ?? []).find(p => p.name === name)
    if (!project) {
      setCardError({ name, text: 'Project not found' })
      return
    }
    if (!project.path) {
      setCardError({ name, text: 'This project has no known folder on the server' })
      return
    }
    if (typeof host?.request !== 'function' || typeof host?.openSession !== 'function') {
      setCardError({ name, text: 'This desktop version cannot open a session from a plugin' })
      return
    }
    setBusyPath(name)
    try {
      const created = await host.request('session.create', {
        cwd: project.path,
        // Without this flag the gateway may fall back to the profile's own
        // workspace folder instead of the folder the client named.
        cwd_explicit: true,
        title: project.name
      })
      const storedId = created?.stored_session_id ?? created?.session_id ?? created?.id
      if (!storedId) {
        throw new Error('The gateway returned no session id')
      }
      await host.openSession(storedId)
      setCardError(null)
    } catch (err) {
      setCardError({ name, text: errorText(err) })
    } finally {
      setBusyPath(null)
    }
  }

  // The web card's name click opens the repository, and its branch chip
  // opens that branch on the remote. The desktop does it through the
  // Electron shell instead of window.open().
  const onOpenRemote = async project => {
    const url = normalizeRemoteUrl(project.remote_url)
    if (!url) return
    if (!(await openInBrowser(url))) {
      setCardError({ name: project.name, text: 'Could not open the repository' })
    }
  }

  const onOpenBranch = async project => {
    const url = branchUrl(project.remote_url, project.current_branch)
    if (!url) return
    if (!(await openInBrowser(url))) {
      setCardError({ name: project.name, text: 'Could not open the branch' })
    }
  }

  const onCardAction = async (action, name, message) => {
    if (busyPath) return

    if (action === 'commit' && !message) {
      setCommitFor(name)
      setCommitMessage('')
      return
    }

    // A session is not a git operation: it creates a chat in the project's
    // folder and navigates the app to it. Nothing about the working tree
    // moves, so the list and the panel are left alone.
    if (action === 'session') {
      await openSessionFor(name)
      return
    }

    setBusyPath(name)
    try {
      const result =
        action === 'pull' ? await gitPull(name)
          : action === 'push' ? await gitPush(name)
            : action === 'fetch' ? await gitFetch(name)
              : await gitCommit(name, message)

      // The contract is HTTP 200 + success:false — a resolved promise is NOT
      // a success, and pretending otherwise repaints stale badges.
      if (result && result.success === false) {
        setCardError({ name, text: result.message || result.stderr || `${action} failed` })
        return
      }
      // git push writes its progress ("To github.com…", object counts) to
      // stderr even on a clean run, so a stderr alone is only a failure when
      // the route did not report success.
      if (result && result.stderr && result.success !== true) {
        setCardError({ name, text: result.stderr })
        return
      }

      setCardError(null)
      // A push/pull/commit moves ahead-behind and the working tree, so the
      // open panel's status is invalidated too — not just the list badges.
      await refreshProjects()
      await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'projects'] })
      if (selected) {
        await client.invalidateQueries({ queryKey: gitStatusKey(selected) })
      }
    } catch (err) {
      setCardError({ name, text: errorText(err) })
    } finally {
      setBusyPath(null)
      setCommitFor(null)
      setCommitMessage('')
    }
  }

  // Pull every tracked project in one call. The backend skips dirty ones and
  // reports per-project outcomes, so the summary is the useful part.
  const onPullAll = async () => {
    if (pullingAll) return
    setPullingAll(true)
    try {
      const result = await pullAll()
      const summary = result
        ? `${result.pulled} pulled, ${result.already_up_to_date} up to date, ${result.dirty_skipped} dirty skipped, ${result.failed} failed`
        : 'Pull all finished'
      // The summary is a page-level fact, so it goes to the page-level
      // channel — on the panel it was invisible unless a card happened to be
      // open. Failures read as errors; a clean run reads as a status.
      setNotice(summary)
      setCardError(result && result.failed > 0 ? { name: null, text: summary } : null)
      await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'projects'] })
    } catch (err) {
      setPanelError(errorText(err))
    } finally {
      setPullingAll(false)
    }
  }

  const onRefresh = async () => {
    setBusy(true)
    try {
      await refreshProjects()
      // Every list page is a cache miss after a re-scan, so invalidate the
      // whole prefix rather than the current key alone.
      await client.invalidateQueries({ queryKey: [...PM_QUERY_ROOT, 'projects'] })
      // The open panel's header shows the same branch/ahead/behind the card
      // shows, but from the git-status payload — a re-scan can move those, so
      // the panel's status has to be refetched too or the two disagree.
      if (selected) {
        await client.invalidateQueries({ queryKey: gitStatusKey(selected) })
      }
    } catch (err) {
      setPanelError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const projects = data?.projects ?? []
  const pageCount = data?.page_count ?? 1
  const total = query ? data?.total_filtered ?? 0 : data?.total ?? 0

  return createElement(
    'div',
    { className: 'flex h-full flex-col gap-3 p-4' },
    createElement(
      'div',
      { className: 'flex items-center gap-2' },
      createElement(
        'div',
        { className: 'relative flex-1' },
        createElement(Codicon, {
          name: 'search',
          size: '0.9em',
          className: 'pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground'
        }),
        createElement('input', {
          type: 'search',
          value: search,
          placeholder: 'Search projects…',
          'aria-label': 'Search projects',
          onChange: event => setSearch(event.target.value),
          className:
            'h-8 w-full rounded-[6px] border border-[var(--ui-stroke-secondary)] bg-background pl-7 pr-2 text-sm outline-none focus:border-[var(--ui-stroke-focus)]'
        })
      ),
      createElement(
        'button',
        {
          type: 'button',
          onClick: onRefresh,
          disabled: busy,
          className: cn(
            'h-8 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-3 text-sm',
            busy && 'opacity-60'
          )
        },
        busy ? 'Refreshing…' : 'Refresh'
      ),
      createElement(
        'button',
        {
          type: 'button',
          onClick: () => setWatchOpen(open => !open),
          'aria-expanded': watchOpen,
          className:
            'h-8 shrink-0 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-3 text-sm hover:bg-accent/10'
        },
        'Folders…'
      ),
      createElement(
        'button',
        {
          type: 'button',
          onClick: onPullAll,
          disabled: pullingAll,
          className: cn(
            'h-8 shrink-0 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-3 text-sm hover:bg-accent/10',
            pullingAll && 'opacity-60'
          )
        },
        pullingAll ? 'Pulling all…' : 'Pull all'
      )
    ),

    watchOpen ? createElement(WatchFolders, { client }) : null,

    notice
      ? createElement(
          'div',
          { role: 'status', 'data-notice': notice, className: 'rounded-[6px] border border-[var(--ui-stroke-secondary)] p-2 text-xs text-muted-foreground' },
          notice
        )
      : null,

    commitFor
      ? createElement(
          'div',
          {
            className:
              'flex flex-wrap items-center gap-1.5 rounded-[6px] border border-[var(--ui-stroke-focus)] p-2',
            'data-commit-for': commitFor
          },
          createElement('span', { className: 'text-xs text-muted-foreground' }, `Commit message — ${commitFor}`),
          createElement('input', {
            type: 'text',
            autoFocus: true,
            value: commitMessage,
            placeholder: 'fix: …',
            'aria-label': 'Commit message',
            onChange: event => setCommitMessage(event.target.value),
            onKeyDown: event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                onCardAction('commit', commitFor, commitMessage.trim())
              }
              if (event.key === 'Escape') {
                setCommitFor(null)
                setCommitMessage('')
              }
            },
            className:
              'h-8 min-w-0 flex-1 rounded-[6px] border border-[var(--ui-stroke-secondary)] bg-background px-2 text-sm outline-none focus:border-[var(--ui-stroke-focus)]'
          }),
          createElement(
            'button',
            {
              type: 'button',
              disabled: !commitMessage.trim(),
              onClick: () => onCardAction('commit', commitFor, commitMessage.trim()),
              className:
                'h-8 shrink-0 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-3 text-sm hover:bg-accent/10 disabled:opacity-50'
            },
            'Commit'
          ),
          createElement(
            'button',
            {
              type: 'button',
              onClick: () => {
                setCommitFor(null)
                setCommitMessage('')
              },
              className: 'h-8 shrink-0 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-3 text-sm hover:bg-accent/10'
            },
            'Cancel'
          )
        )
      : null,

    error
      ? createElement(
          'div',
          {
            role: 'alert',
            'data-error': errorText(error),
            className: 'rounded-[6px] border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive'
          },
          errorText(error)
        )
      : null,

    isPending && projects.length === 0
      ? createElement('div', { className: 'p-4 text-sm text-muted-foreground' }, 'Loading projects…')
      : null,

    !isPending && !error && projects.length === 0
      ? createElement('div', { className: 'p-4 text-sm text-muted-foreground' }, 'No projects match this search.')
      : null,

    projects.length
      ? createElement(
          'div',
          { className: 'flex flex-col gap-1.5' },
          projects.map(project =>
            createElement(ProjectCard, {
              key: project.name,
              project,
              selected: selected === project.name,
              onSelect: name => setSelected(prev => (prev === name ? null : name)),
              panel:
                selected === project.name && panel
                  ? // key: React reuses the instance across a project switch, so
                    // without it the previous project's error message, armed
                    // confirm and busyPath stay painted over the new project's
                    // file list — and an armed discard can fire at the wrong repo.
                    createElement(panel.WorkingTreePanel, {
                      key: selected,
                      projectName: selected
                    })
                  : null,
              panelError: selected === project.name ? panelError : null,
              cardError: cardError && cardError.name === project.name ? cardError.text : null,
              busyPath,
              onCardAction,
              onOpenRemote,
              onOpenBranch
            })
          )
        )
      : null,

    createElement(
      'div',
      { className: 'mt-auto flex items-center justify-between pt-2 text-xs text-muted-foreground' },
      createElement('span', null, `${total} project${total === 1 ? '' : 's'}${query ? ` matching “${query}”` : ''}`),
      createElement(
        'div',
        { className: 'flex items-center gap-1' },
        createElement(
          'button',
          {
            type: 'button',
            'aria-label': 'Previous page',
            disabled: page <= 1,
            onClick: () => setPage(prev => Math.max(1, prev - 1)),
            className: 'h-7 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-2 disabled:opacity-40'
          },
          'Previous'
        ),
        createElement('span', { className: 'px-1 tabular-nums' }, `${page} / ${pageCount}`),
        createElement(
          'button',
          {
            type: 'button',
            'aria-label': 'Next page',
            // page_count is the server's own answer — never derived from
            // total/page_size, which disagrees on the empty final page.
            disabled: page >= pageCount,
            onClick: () => setPage(prev => Math.min(pageCount, prev + 1)),
            className: 'h-7 rounded-[6px] border border-[var(--ui-stroke-secondary)] px-2 disabled:opacity-40'
          },
          'Next'
        )
      )
    ),

    // The working-tree panel renders inside ProjectCard, under the row you
    // clicked. It used to live here at the bottom of the whole list, which put
    // it several screens below the project you opened.
  )
}

// ── [4] PLUGIN SHELL (from plugin.js) ──────────────────────────────────────
// project-manager — native Desktop surface for the existing
// plugins/project-manager FastAPI router. No new backend: ctx.rest is
// namespace-bound to /api/plugins/project-manager, which is the same
// router the web dashboard and the mobile app already speak.
//
// Ships OFF like kanban — a git-mutating surface (discard!) should never
// come up enabled unasked.
const plugin = {
  // MUST equal the backend router's namespace. ctx.rest builds every URL as
  // /api/plugins/<id><path> (src/api/plugins.ts), so a mismatch here 404s
  // every call while the loader still reports the plugin as loaded — kanban
  // has the same id as its router for exactly this reason.
  id: 'project-manager',
  name: 'Projects',
  description: 'Git projects with a per-file stage/discard/diff working tree.',
  defaultEnabled: false,
  register(ctx) {
    // The disposer runs on unload/disable, so the REST handle never leaks
    // into a re-enabled copy.
    ctx.onDispose(bindDoors(ctx.rest, ctx.os?.openExternal))

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/projects' },
        // Runtime plugins are served raw (no JSX transform), so the element
        // is built explicitly rather than as <ProjectsPage />.
        render: () => createElement(ProjectsPage)
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 55,
        data: { codicon: 'source-control', label: 'Projects', path: '/projects' }
      }
    ])
  }
}

export default plugin
