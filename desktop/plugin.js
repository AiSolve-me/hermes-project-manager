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

/** Bind the REST door at register time; the disposer is what unload runs. */
function bindApi(r) {
  rest = r
  return () => {
    rest = null
  }
}

function door() {
  if (!rest) throw new Error('project-manager-desktop: rest door not bound')
  return rest
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
  const [confirm, setConfirm] = useState(null)
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
  const locked = busyPath !== null || fetching

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
      )
    ),
    // The op's own failure message — a rejected promise must never look like a
    // no-op, and it must not be the only place the reason shows up.
    error ? createElement('p', { className: 'text-sm text-destructive', role: 'alert' }, error) : null,
    body,
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
    diff ? createElement(DiffOverlay, { diff, onClose: closeDiff }) : null
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

/** One project row. Pure — takes a ProjectItem, renders no fetching. */
function ProjectCard({ project, selected, onSelect }) {
  const branch = project.current_branch || 'DETACHED'
  const age = project.last_commit_date ? relativeAge(project.last_commit_date) : null

  return createElement(
    'div',
    {
      className: cn(
        'w-full rounded-[6px] border p-3 text-left transition-colors',
        selected ? 'border-[var(--ui-stroke-focus)] bg-accent/5' : 'border-[var(--ui-stroke-secondary)] hover:bg-accent/5'
      ),
      role: 'button',
      tabIndex: 0,
      'aria-pressed': selected,
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
      { className: 'flex items-start justify-between gap-2' },
      createElement(
        'div',
        { className: 'min-w-0' },
        createElement('div', { className: 'truncate text-sm font-medium text-foreground' }, project.name),
        createElement(
          'div',
          { className: 'mt-0.5 flex items-center gap-1 text-xs text-muted-foreground' },
          createElement(Codicon, { name: 'source-control', size: '0.85em' }),
          createElement('span', { className: 'truncate' }, branch)
        )
      ),
      // Ahead/behind only mean something against an upstream; a repo with
      // no remote would otherwise always read "0/0" and look falsely synced.
      project.branch_count > 0
        ? createElement(Badge, { variant: 'muted', size: 'xs', title: 'local branches' }, `${project.branch_count} br`)
        : null
    ),

    createElement(
      'div',
      { className: 'mt-2 flex flex-wrap items-center gap-1.5' },
      project.is_dirty ? createElement(Badge, { variant: 'warn', size: 'xs' }, 'dirty') : null,
      project.ahead > 0 ? createElement(Badge, { variant: 'success', size: 'xs' }, `↑${project.ahead}`) : null,
      project.behind > 0 ? createElement(Badge, { variant: 'destructive', size: 'xs' }, `↓${project.behind}`) : null,
      !project.is_dirty && !project.ahead && !project.behind
        ? createElement(Badge, { variant: 'outline', size: 'xs' }, 'clean')
        : null,
      project.remote_url
        ? createElement(
            'span',
            { className: 'truncate text-xs text-muted-foreground', title: project.remote_url },
            shortRemote(project.remote_url)
          )
        : createElement(Badge, { variant: 'muted', size: 'xs' }, 'no remote')
    ),

    project.last_commit_message
      ? createElement(
          'div',
          { className: 'mt-2 truncate text-xs text-muted-foreground', title: project.last_commit_message },
          project.last_commit_message
        )
      : null,
    age
      ? createElement('div', { className: 'mt-0.5 text-xs text-muted-foreground/70' }, `last commit ${age}`)
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
  const [busy, setBusy] = useState(false)

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
      )
    ),

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
              onSelect: name => setSelected(prev => (prev === name ? null : name))
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

    selected
      ? createElement(
          'section',
          { className: 'mt-2 border-t border-[var(--ui-stroke-secondary)] pt-3', 'data-detail': selected },
          createElement('h2', { className: 'mb-2 text-sm font-medium' }, `Working tree — ${selected}`),
          panelError
            ? createElement('div', { role: 'alert', className: 'text-sm text-destructive' }, panelError)
            : null,
          !panel && !panelError
            ? createElement('div', { className: 'text-sm text-muted-foreground' }, 'Loading working tree…')
            : null,
          panel
            ? createElement(
                'div',
                null,
                // key: React reuses the instance across a project switch, so
                // without it the previous project's error message, armed
                // confirm and busyPath stay painted over the new project's
                // file list — and an armed discard can fire at the wrong repo.
                createElement(panel.WorkingTreePanel, { key: selected, projectName: selected })
              )
            : null
        )
      : null
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
    ctx.onDispose(bindApi(ctx.rest))

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
