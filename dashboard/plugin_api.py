"""
Project Manager FastAPI Plugin API
==================================

Router initialization for the project-manager plugin endpoints.

Prefix: /project-manager (mount: /api/plugins/project-manager/)
"""

from __future__ import annotations

import os
import subprocess
import sys
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Fix for running outside package context — always add our directory to sys.path
# so the dashboard server can import this module via importlib.
_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)

# Remove stale modules from cache so we always load OUR modules, not another plugin's.
sys.modules.pop("db", None)
sys.modules.pop("models", None)
sys.modules.pop("git_scanner", None)
sys.modules.pop("cache", None)
sys.modules.pop("docker_status", None)

from cache import cache
from docker_status import has_docker, list_running_stacks, match_project
from models import (
    ProjectItem,
    ProjectListResponse,
    CommitItem,
    CommitListResponse,
    BranchItem,
    WatchPathItem,
    WatchPathListResponse,
    AddWatchPathRequest,
)
from git_scanner import (
    scan_all_projects,
    scan_project,
    get_branches,
    get_current_branch,
    get_git_status,
    git_commit,
    git_push,
    git_stash,
    get_last_commit,
    _cleanup_stale_lock,
)
from db import (
    ALLOWED_PAGE_SIZES,
    DEFAULT_PAGE_SIZE,
    record_scan,
    list_all_latest_scans,
    get_latest_scan,
    get_db_pool,
    add_watch_path,
    list_watch_paths,
    get_enabled_watch_paths,
    delete_watch_path,
    reorder_watch_paths,
    rename_watch_path,
    list_latest_scans_paginated,
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(tags=["project-manager"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_git_error(exc: Exception) -> bool:
    """Return True if *exc* represents a git command failure."""
    return isinstance(
        exc,
        (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError),
    )


def _handle_project_lookup(name: str) -> dict:
    """Fetch the latest scan row for *name*; raise 404 if missing."""
    row = get_latest_scan(project_name=name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    return row


def _scan_to_project_item(scan: dict) -> ProjectItem:
    """Convert a raw scan dict (from git_scanner) into a ProjectItem."""
    last_commit = scan.get("last_commit") or {}
    last_commit_date = None
    if last_commit.get("date"):
        try:
            last_commit_date = datetime.strptime(
                last_commit["date"], "%Y-%m-%d %H:%M:%S %z"
            )
        except ValueError:
            pass

    last_push_date = None
    if scan.get("last_push"):
        try:
            last_push_date = datetime.strptime(
                scan["last_push"], "%Y-%m-%d %H:%M:%S %z"
            )
        except ValueError:
            pass

    return ProjectItem(
        name=scan["project"],
        path=scan["path"],
        remote_url=scan.get("remote_url"),
        current_branch=scan.get("branch", ""),
        last_commit_hash=last_commit.get("hash"),
        last_commit_message=last_commit.get("message"),
        last_commit_date=last_commit_date,
        last_push_date=last_push_date,
        is_dirty=scan.get("dirty", False),
        branch_count=len(scan.get("branches", [])),
        branches=scan.get("branches", []),
        watch_path_sort_order=scan.get("watch_path_sort_order", 0),
    )


def _db_row_to_project_item(row: dict) -> ProjectItem:
    """Convert a DB row dict into a ProjectItem."""

    def _parse_dt(val: Optional[str]) -> Optional[datetime]:
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None

    return ProjectItem(
        name=row["project_name"],
        path=row["path"],
        remote_url=row.get("remote_url"),
        current_branch=row.get("current_branch", ""),
        last_commit_hash=row.get("last_commit_hash"),
        last_commit_message=row.get("last_commit_message"),
        last_commit_date=_parse_dt(row.get("last_commit_date")),
        last_push_date=_parse_dt(row.get("last_push_date")),
        is_dirty=bool(row.get("is_dirty", 0)),
        branch_count=row.get("branch_count", 0),
        branches=[],
        watch_path_sort_order=int(row.get("watch_path_sort_order", 0) or 0),
        ahead=int(row.get("ahead", 0) or 0),
        behind=int(row.get("behind", 0) or 0),
    )


async def _annotate_docker(projects: list[ProjectItem]) -> None:
    """Stamp live compose flags on the page slice. Not stored in the git-list cache."""
    stacks = await asyncio.to_thread(list_running_stacks)
    for item in projects:
        running, ports = match_project(item.path, stacks)
        item.has_docker = running or has_docker(item.path)
        item.docker_running = running
        item.docker_ports = ports


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class RefreshResponse(BaseModel):
    """Response payload for the force-refresh endpoint."""

    refreshed: bool = Field(
        True, description="Whether the refresh completed successfully"
    )
    count: int = Field(0, description="Number of projects scanned")
    scanned_at: Optional[datetime] = Field(
        None, description="When the scan was completed"
    )


@router.post("/projects/refresh", response_model=RefreshResponse)
async def refresh_projects() -> RefreshResponse:
    """Force a full re-scan of all projects.

    Clears all paginated project-list cache entries, runs a fresh git
    scan, persists the results to the local SQLite DB, and returns the
    scan count.
    """
    try:
        # 1. Clear cache and old DB records --------------------------------------
        # The paginated list_projects caches under namespaced keys
        # like "project_manager:projects:q=<q>:page=<p>:size=<s>".
        # A wildcard delete keeps every permutation consistent.
        deleted = cache.delete_pattern("project_manager:projects")
        log.debug("refresh_projects: cleared %d project list cache key(s)", deleted)

        # Wipe all old scan records so stale data (e.g. deleted branches) disappears
        with get_db_pool().connection() as conn:
            cur = conn.execute("DELETE FROM project_scans")
            conn.commit()
            log.debug("refresh_projects: wiped %d old scan row(s)", cur.rowcount)

        # 2. Scan all projects -----------------------------------------------------
        raw_scans = scan_all_projects(paths=get_enabled_watch_paths())
        scanned_at = datetime.now(timezone.utc)

        # 3. Persist to DB ---------------------------------------------------------
        for scan in raw_scans:
            last_commit = scan.get("last_commit") or {}
            record_scan(
                project_name=scan["project"],
                path=scan["path"],
                remote_url=scan.get("remote_url"),
                current_branch=scan.get("branch"),
                last_commit_hash=last_commit.get("hash"),
                last_commit_message=last_commit.get("message"),
                last_commit_date=last_commit.get("date"),
                last_push_date=scan.get("last_push"),
                is_dirty=1 if scan.get("dirty") else 0,
                branch_count=len(scan.get("branches", [])),
                watch_path_sort_order=int(scan.get("watch_path_sort_order", 0) or 0),
                ahead=int(scan.get("ahead", 0) or 0),
                behind=int(scan.get("behind", 0) or 0),
            )

        return RefreshResponse(
            refreshed=True,
            count=len(raw_scans),
            scanned_at=scanned_at,
        )
    except Exception as exc:
        log.exception("refresh_projects: unexpected error")
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    q: str = "",
    page: int = 1,
    page_size: int = 21,
) -> ProjectListResponse:
    """Return a paginated, searchable list of all discovered git projects.

    The list is read from the most recent ``project_scans`` row per
    project (no git scan is performed on this endpoint — git scans are
    triggered explicitly by ``POST /projects/refresh``).

    Args:
        q: free-text search query. Empty string returns every project.
            Case-insensitive substring match against ``project_name``,
            ``path``, ``remote_url`` and ``current_branch``.
        page: 1-based page number. Values < 1 are clamped to 1.
        page_size: number of projects per page. Must be one of
            ``9``, ``21`` or ``27``; any other value is rejected with
            HTTP 400.

    Returns:
        ``ProjectListResponse`` with the page slice, total / filtered
        counts, page metadata and the timestamp of the most recent
        successful refresh.

    Caching:
        Results are cached in-memory under a namespaced key derived
        from the (q, page, page_size) tuple with a 60s TTL. The cache
        is invalidated by ``POST /projects/refresh`` (wildcard delete
        on ``project_manager:projects*``).
    """
    # ALLOWED_PAGE_SIZES, DEFAULT_PAGE_SIZE, list_latest_scans_paginated
    # are imported at module load time (see top of file). Importing them
    # here would re-trigger the `from db import` inside the handler,
    # which is vulnerable to sys.path pollution from other plugins
    # (e.g. youtube-dashboard also defines a `dashboard/db.py` and the
    # late `from db import` resolves to the wrong module, producing
    # "cannot import name 'ALLOWED_PAGE_SIZES' from 'db'").

    CACHE_KEY_TMPL = "project_manager:projects:q={q}:page={page}:size={size}"
    CACHE_TTL = 60

    q = (q or "").strip()
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"page_size must be one of {list(ALLOWED_PAGE_SIZES)}, "
                f"got {page_size}"
            ),
        )
    if page < 1:
        page = 1

    cache_key = CACHE_KEY_TMPL.format(q=q, page=page, size=page_size)

    try:
        # 1. Try cache -----------------------------------------------------------
        cached = cache.get(cache_key)
        if cached is not None:
            response = ProjectListResponse(**cached)
            await _annotate_docker(response.projects)
            return response

        # 2. Read from DB (latest scan per project) ------------------------------
        rows, total_filtered = list_latest_scans_paginated(
            q=q, page=page, page_size=page_size
        )
        projects = [_db_row_to_project_item(row) for row in rows]

        # Determine the timestamp of the most recent scan across all
        # projects so the UI can show "last refreshed at ...".
        with get_db_pool().connection() as conn:
            ts_row = conn.execute(
                "SELECT MAX(scanned_at) AS max_scanned_at FROM project_scans"
            ).fetchone()
        scanned_at = None
        if ts_row and ts_row["max_scanned_at"]:
            try:
                scanned_at = datetime.fromisoformat(ts_row["max_scanned_at"])
            except ValueError:
                scanned_at = None

        total = total_filtered
        page_count = max(1, (total + page_size - 1) // page_size) if total else 1

        response = ProjectListResponse(
            projects=projects,
            total=total,
            total_filtered=total_filtered,
            page=page,
            page_size=page_size,
            page_count=page_count,
            scanned_at=scanned_at,
        )

        # 3. Warm cache (git fields only — docker flags are live, annotated below)
        cache.set(cache_key, response.model_dump(mode="json"), ttl=CACHE_TTL)

        await _annotate_docker(response.projects)
        return response
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("list_projects: unexpected error")
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


@router.get("/projects/{name}", response_model=ProjectItem)
async def get_project(name: str) -> ProjectItem:
    """Return details for a single project by name.

    Reads the most recent scan from the DB.  If the project has never
    been scanned, returns HTTP 404.
    """
    try:
        row = _handle_project_lookup(name)
        item = _db_row_to_project_item(row)
        await _annotate_docker([item])
        return item
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("get_project: unexpected error for name=%s", name)
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


@router.get("/projects/{name}/branches", response_model=list[BranchItem])
async def list_project_branches(name: str) -> list[BranchItem]:
    """Return all local branches for a project, marking the current one.

    Looks up the project path from the latest DB scan, then runs
    ``git branch --format="%(refname:short)"`` in the repository.
    Returns HTTP 404 if the project has never been scanned.
    Returns HTTP 400 if the git command fails.
    """
    try:
        row = _handle_project_lookup(name)
        project_path = row["path"]
        if not project_path:
            raise HTTPException(
                status_code=500, detail=f"Project '{name}' has no recorded path"
            )

        try:
            branches = get_branches(project_path)
            current = get_current_branch(project_path)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ) as exc:
            log.warning("list_project_branches: git error for %s: %s", name, exc)
            raise HTTPException(
                status_code=400, detail=f"Git command failed for project '{name}'"
            )

        return [BranchItem(name=b, is_current=(b == current)) for b in branches]
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("list_project_branches: unexpected error for name=%s", name)
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


# ---------------------------------------------------------------------------
# Commit history
# ---------------------------------------------------------------------------


def _run_git_log(path: str, limit: int = 10) -> list[CommitItem]:
    """Run ``git log`` and return parsed CommitItem objects."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                path,
                "log",
                f"-{limit}",
                "--format=%H|%ci|%s|%an",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        raise HTTPException(
            status_code=400, detail=f"Git command failed: {exc}"
        ) from exc

    commits: list[CommitItem] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        hash_val, date_str, message, author = parts
        try:
            dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            dt = datetime.now(timezone.utc)
        commits.append(
            CommitItem(
                hash=hash_val.strip(),
                message=message.strip(),
                date=dt,
                author=author.strip(),
            )
        )
    return commits


@router.get("/projects/{name}/commits", response_model=CommitListResponse)
async def get_project_commits(name: str) -> CommitListResponse:
    """Return the last 10 commits for a project.

    The project path is resolved from the most recent DB scan record.
    If the project has never been scanned, returns HTTP 404.
    If the git command fails, returns HTTP 400.
    """
    try:
        row = _handle_project_lookup(name)
        project_path = row.get("path")
        if not project_path:
            raise HTTPException(
                status_code=500, detail=f"Project '{name}' has no recorded path"
            )

        commits = _run_git_log(project_path, limit=10)
        return CommitListResponse(commits=commits)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("get_project_commits: unexpected error for name=%s", name)
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


# ---------------------------------------------------------------------------
# Git pull (PULL-API)
# ---------------------------------------------------------------------------


def _run_git(
    path: str,
    argv: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess:
    """Run ``git -C <path> <argv...>`` with its own process group.

    ``start_new_session=True`` makes the child the leader of a new
    process group so a caller that needs to abort (on timeout, on
    detected divergence) can ``os.killpg`` the entire group and take
    down any ``git fetch``/``git merge`` grandchildren that the parent
    wrapper spawned. The vanilla ``subprocess.run(..., timeout=N)``
    only kills the immediate child, leaving the grandchildren holding
    the ``.git/index.lock`` and the working tree dirty on the next
    invocation.
    """
    import os  # local import: only this helper needs the POSIX kill API

    proc = subprocess.Popen(
        ["git", "-C", path, *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 15)  # SIGTERM the whole group
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)  # SIGKILL fallback
            except ProcessLookupError:
                pass
        # Best-effort stale-lock recovery; the helper swallows its own
        # errors so callers don't have to.
        _cleanup_stale_lock(path)
        raise
    return subprocess.CompletedProcess(
        args=proc.args, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


def _git_rev_parse(path: str) -> str:
    """Return the current HEAD SHA for *path* (empty string on failure)."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return result.stdout.strip()


def _count_commits_in_range(path: str, old_sha: str, new_sha: str) -> int:
    """Count commits in ``old_sha..new_sha`` (the fast-forward range)."""
    if not old_sha or not new_sha:
        return 0
    result = subprocess.run(
        ["git", "-C", path, "rev-list", "--count", f"{old_sha}..{new_sha}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or 0)
    except ValueError:
        return 0


def _git_upstream_branch(path: str) -> str | None:
    """Return the upstream tracking *branch name* (not full ref) for the
    repo at *path*, or ``None`` if the current branch has no upstream.

    Uses ``git rev-parse --abbrev-ref --abbrev-ref=strict @{upstream}``
    which yields e.g. ``origin/main``.  We then strip the ``<remote>/``
    prefix to get just the branch name (``main``).
    """
    result = subprocess.run(
        ["git", "-C", path, "rev-parse",
         "--abbrev-ref", "--abbrev-ref=strict", "@{upstream}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    if not ref or ref.startswith("refs/remotes/"):
        return None
    # ref is like "origin/main" → return "main"
    parts = ref.split("/", 1)
    return parts[1] if len(parts) == 2 else ref


def _git_pull(path: str, remote: str = "origin", branch: str | None = None) -> tuple[bool, str, int]:
    """Fetch *remote*/*branch* and fast-forward the local branch.

    Splitting ``git pull`` into ``git fetch`` + ``git merge --ff-only``
    avoids two recurring failure modes:

    1. ``git pull`` produces a merge commit when the local branch has
       diverged from upstream. With ``--ff-only`` we fail fast and the
       dashboard UI surfaces "Local branch diverged; rebase or reset
       first" instead of silently creating a merge commit the user
       then has to clean up.
    2. The wrapper subprocess of ``git pull`` spawns grandchildren
       (``git fetch`` + ``git merge``). Killing the wrapper alone
       leaves them running and holding ``.git/index.lock``, breaking
       every subsequent git call until manual cleanup. ``_run_git``
       runs each step in its own process group and kills the group on
       timeout.

    ``fetch_timeout`` is 600s — matches the terminal tool ceiling and
    is plenty for any realistic fetch on the dashboard's tracked
    repos (the largest we observed, hermes-agent at ~120 commits,
    finishes in seconds). ``merge_timeout`` is short because the
    fast-forward is local-only.

    Returns ``(success, message, added_commits)`` where *message* is a
    concise human-readable summary — **never** raw git stdout, which
    can span hundreds of lines for large fetches.
    """
    # When no branch was specified, resolve the upstream tracking branch
    # so we can fetch *only* that ref. ``git fetch origin`` (no refspec)
    # fetches every branch and writes FETCH_HEAD pointing at the remote
    # HEAD — which may be a *different* branch than the one the current
    # branch tracks. This caused pulls to silently not update (or merge
    # the wrong branch) when the local branch wasn't the remote default.
    #
    # We resolve the upstream (e.g. ``origin/main``) and extract the
    # branch name from it. If the current branch has no upstream, we
    # fall back to the current branch name itself.
    if branch is None:
        branch = _git_upstream_branch(path) or get_current_branch(path)
        if not branch or branch == "DETACHED":
            return False, "Could not determine current branch or upstream", 0

    # Record HEAD before the fetch so we can compute a concise summary.
    old_head = _git_rev_parse(path)

    fetch_argv = ["fetch", remote, branch]
    try:
        fetch = _run_git(path, fetch_argv, timeout=600)
    except subprocess.TimeoutExpired:
        _cleanup_stale_lock(path)
        return False, "Fetch timeout after 600s; please retry", 0
    if fetch.returncode != 0:
        _cleanup_stale_lock(path)
        return False, fetch.stderr.strip() or f"Fetch failed (exit {fetch.returncode})", 0

    # Merge the *specific* remote ref, not FETCH_HEAD. After a
    # targeted ``git fetch origin <branch>`` FETCH_HEAD is correct, but
    # using ``<remote>/<branch>`` is explicit and survives any future
    # change to the fetch refspec.
    #
    # ``origin/<branch>`` only exists when the repo's fetch refspec covers
    # that branch. A targeted ``git fetch origin <branch>`` still succeeds
    # without it (it writes FETCH_HEAD), so the merge would die with
    # "not something we can merge". Fall back to FETCH_HEAD.
    merge_ref = f"{remote}/{branch}"
    if _run_git(path, ["rev-parse", "--verify", merge_ref], timeout=10).returncode != 0:
        merge_ref = "FETCH_HEAD"
    merge_argv = ["merge", "--ff-only", merge_ref]
    try:
        merge = _run_git(path, merge_argv, timeout=30)
    except subprocess.TimeoutExpired:
        _cleanup_stale_lock(path)
        return False, "Merge timeout after 30s; please retry", 0
    if merge.returncode != 0:
        _cleanup_stale_lock(path)
        stderr = (merge.stderr or "").lower()
        if "not possible to fast-forward" in stderr or "diverged" in stderr:
            return False, "Local branch diverged from upstream; rebase or reset first", 0
        return False, merge.stderr.strip() or f"Merge failed (exit {merge.returncode})", 0

    # Synthesize a concise summary instead of dumping raw git stdout
    # (which includes progress lines, file lists, etc. that fill the
    # notification toast).
    new_head = _git_rev_parse(path)
    if not old_head or not new_head or old_head == new_head:
        return True, "Already up to date.", 0

    count = _count_commits_in_range(path, old_head, new_head)
    return True, f"Fast-forwarded {count} commit(s) ({old_head[:7]} → {new_head[:7]})", count


def _git_status_porcelain(path: str) -> list[str]:
    """Return the list of modified/untracked files, or raise on error."""
    # Recovery is best-effort: a stale lock from a crashed prior call
    # must not block this read-only status probe.
    _cleanup_stale_lock(path)
    result = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


class GitPullRequest(BaseModel):
    """Body for POST /projects/{name}/git-pull."""

    remote: str = Field("origin", description="Remote name (default origin)")
    branch: Optional[str] = Field(
        None,
        description=(
            "Branch name to pull (default: let git use the current branch's "
            "upstream tracking config)"
        ),
    )


class GitPullResponse(BaseModel):
    """Response payload for POST /projects/{name}/git-pull."""

    success: bool = Field(False, description="Whether the pull succeeded")
    project: str = Field("", description="Project name")
    message: str = Field("", description="git pull stdout")
    pulled: bool = Field(
        False,
        description=(
            "Whether new commits were actually fetched and applied to the "
            "local branch. False when the response was rejected (dirty "
            "working tree) or when 'git pull' reported 'Already up to "
            "date.' — the UI uses this flag to decide whether to "
            "re-render the project list."
        ),
    )
    current_branch: str = Field("", description="Current branch after pull")
    pulled_at: Optional[datetime] = Field(
        None, description="When the pull completed"
    )
    added_commits: int = Field(
        0,
        description=(
            "Number of new commits fast-forwarded. 0 when the branch was "
            "already up to date."
        ),
    )


class BatchPullResult(BaseModel):
    """Per-project result inside a pull-all response."""

    project: str = Field(..., description="Project name")
    success: bool = Field(False, description="Whether the pull succeeded")
    pulled: bool = Field(False, description="True when new commits landed")
    status: str = Field(
        ...,
        description=(
            "One of: pulled, already_up_to_date, dirty, diverged, failed, "
            "timeout, no_remote"
        ),
    )
    message: str = Field("", description="Concise human-readable summary")
    added_commits: int = Field(0, description="Commits fast-forwarded (0 if none)")


class BatchPullResponse(BaseModel):
    """Aggregate result for POST /projects/pull-all."""

    success: bool = Field(True, description="Overall: True unless a server error occurred")
    total: int = Field(0, description="Total projects attempted")
    pulled: int = Field(0, description="Projects that received new commits")
    already_up_to_date: int = Field(0, description="Projects already at the latest commit")
    dirty_skipped: int = Field(0, description="Projects skipped (uncommitted changes)")
    diverged: int = Field(0, description="Projects whose branch diverged")
    failed: int = Field(0, description="Projects that errored for other reasons")
    results: List[BatchPullResult] = Field(default_factory=list, description="Per-project details")


@router.post("/projects/{name}/git-pull", response_model=GitPullResponse)
async def git_pull_project(
    name: str, payload: Optional[GitPullRequest] = None
) -> GitPullResponse:
    """Pull the current (or requested) branch from the given remote.

    Refuses (HTTP 409) if the working tree has uncommitted changes — the
    caller must commit or stash first. On success re-scans the project
    and invalidates the project list cache so the next ``/projects`` call
    returns fresh data.

    The request body is optional: when omitted, the pull runs with the
    default remote (``"origin"``) and the current branch's upstream
    tracking config (``payload.branch is None``). This matches the
    common UI flow where the dashboard's Pull button POSTs without a
    body.
    """
    # Backwards-compatible defaults: an omitted body should behave
    # exactly as ``{"remote": "origin", "branch": null}`` would.
    remote = payload.remote if payload is not None else "origin"
    branch = payload.branch if payload is not None else None
    pulled_at = datetime.now(timezone.utc)

    try:
        row = _handle_project_lookup(name)
        project_path = row.get("path")
        if not project_path:
            raise HTTPException(
                status_code=500, detail=f"Project '{name}' has no recorded path"
            )

        # 1. Refuse when the working tree is dirty.
        try:
            dirty_files = _git_status_porcelain(project_path)
        except subprocess.CalledProcessError as exc:
            log.warning("git_pull_project: git status failed for %s: %s", name, exc)
            raise HTTPException(
                status_code=400, detail=f"Git status failed for project '{name}'"
            )

        if dirty_files:
            return GitPullResponse(
                success=False,
                project=name,
                message=(
                    f"Cannot pull: {len(dirty_files)} uncommitted "
                    "change(s). Commit or stash first."
                ),
                pulled=False,
                current_branch=row.get("current_branch", ""),
                pulled_at=pulled_at,
            )

        # 2. Pull (run in a thread so the async endpoint doesn't block
        #    the event loop — otherwise concurrent pulls on other
        #    projects are serialized behind this one's 600s fetch).
        ok, msg, added = await asyncio.to_thread(
            _git_pull, project_path, remote, branch
        )
        if not ok:
            log.error("git_pull_project: pull failed for %s: %s", name, msg)
            # Map the semantic failure modes the helper can return
            # to the right HTTP status so the dashboard UI can react
            # sensibly (retry vs. show "branch diverged" vs. server bug).
            msg_lower = msg.lower()
            if "timeout" in msg_lower:
                # 504: the underlying git call hit our timeout. The
                # helper already cleaned up the stale .git/index.lock,
                # so the next retry should not be blocked by it.
                raise HTTPException(status_code=504, detail=f"Pull failed: {msg}")
            if "diverged" in msg_lower:
                # 409: the local branch is not a fast-forward of the
                # upstream — the user has to rebase or reset. We
                # deliberately did NOT do that for them; creating a
                # merge commit on a tracked dashboard project is what
                # produced the recurring "stale lock + accidental merge"
                # pattern this replaces.
                raise HTTPException(status_code=409, detail=f"Pull failed: {msg}")
            raise HTTPException(status_code=500, detail=f"Pull failed: {msg}")

        # _git_pull now returns added_commits (0 when already up to
        # date), so we no longer need string matching on the message
        # to decide whether to re-scan.
        actually_pulled = added > 0

        # 3. Re-scan and update DB only when something actually changed.
        # On an "Already up to date." pull there is no new data to record,
        # so we skip the scan + DB write + cache invalidation entirely —
        # the client can keep showing the existing project list.
        current_branch = row.get("current_branch", "")
        if actually_pulled:
            # Single scan — the old code scanned twice (once for
            # record_scan, again for current_branch), wasting a full
            # git rev-parse walk per pull.
            try:
                scan = await asyncio.to_thread(scan_project, project_path, name)
                last_commit = scan.get("last_commit") or {}
                record_scan(
                    project_name=scan["project"],
                    path=scan["path"],
                    remote_url=scan.get("remote_url"),
                    current_branch=scan.get("branch") or current_branch,
                    last_commit_hash=last_commit.get("hash"),
                    last_commit_message=last_commit.get("message"),
                    last_commit_date=last_commit.get("date"),
                    last_push_date=scan.get("last_push"),
                    is_dirty=1 if scan.get("dirty") else 0,
                    branch_count=len(scan.get("branches", [])),
                )
                # Wildcard-invalidate every paginated /projects cache entry
                # so the next read picks up the new last_commit / ahead /
                # behind values from the freshly-written scan row.
                cache.delete_pattern("project_manager:projects")
                current_branch = scan.get("branch") or current_branch
            except Exception as exc:
                log.warning(
                    "git_pull_project: re-scan after pull failed for %s: %s",
                    name,
                    exc,
                )
                # Non-fatal: the pull succeeded even if re-scan failed.

        return GitPullResponse(
            success=True,
            project=name,
            message=msg,
            pulled=actually_pulled,
            current_branch=current_branch,
            pulled_at=pulled_at,
            added_commits=added,
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_pull_project: unexpected error for name=%s", name)
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


def _git_current_branch_for_response(path: str) -> str:
    """Return the current branch name for the repo at *path* (helper)."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    branch = result.stdout.strip()
    return "DETACHED" if branch == "HEAD" else branch


# ---------------------------------------------------------------------------
# Batch pull (pull-all)
# ---------------------------------------------------------------------------


def _do_pull_project(
    name: str,
    project_path: str,
    remote: str = "origin",
    branch: str | None = None,
) -> BatchPullResult:
    """Synchronous single-project pull — designed for ``asyncio.to_thread``.

    Returns a :class:`BatchPullResult` instead of raising ``HTTPException``
    so it can be called in a batch context without aborting the whole
    batch.  The same logic (dirty check → ``_git_pull`` → rescan) is
    used by both the single-project endpoint and the batch endpoint,
    so there is exactly one code path for pull behaviour.
    """
    try:
        dirty_files = _git_status_porcelain(project_path)
    except subprocess.CalledProcessError:
        return BatchPullResult(
            project=name, success=False, pulled=False,
            status="failed", message="Git status failed",
            added_commits=0,
        )

    if dirty_files:
        return BatchPullResult(
            project=name, success=True, pulled=False,
            status="dirty",
            message=f"Cannot pull: {len(dirty_files)} uncommitted change(s)",
            added_commits=0,
        )

    ok, msg, added = _git_pull(project_path, remote, branch)
    if not ok:
        msg_lower = msg.lower()
        if "timeout" in msg_lower:
            status = "timeout"
        elif "diverged" in msg_lower:
            status = "diverged"
        else:
            status = "failed"
        return BatchPullResult(
            project=name, success=False, pulled=False,
            status=status, message=msg, added_commits=added,
        )

    if added > 0:
        try:
            scan = scan_project(project_path, name)
            last_commit = scan.get("last_commit") or {}
            record_scan(
                project_name=scan["project"],
                path=scan["path"],
                remote_url=scan.get("remote_url"),
                current_branch=scan.get("branch"),
                last_commit_hash=last_commit.get("hash"),
                last_commit_message=last_commit.get("message"),
                last_commit_date=last_commit.get("date"),
                last_push_date=scan.get("last_push"),
                is_dirty=1 if scan.get("dirty") else 0,
                branch_count=len(scan.get("branches", [])),
            )
            cache.delete_pattern("project_manager:projects")
        except Exception as exc:
            log.warning("pull_all: re-scan failed for %s: %s", name, exc)
            msg = f"{msg} (re-scan warning: {exc})"

    status = "pulled" if added > 0 else "already_up_to_date"
    return BatchPullResult(
        project=name, success=True, pulled=added > 0,
        status=status, message=msg, added_commits=added,
    )


@router.post("/projects/pull-all", response_model=BatchPullResponse)
async def pull_all_projects(
    payload: Optional[GitPullRequest] = None,
) -> BatchPullResponse:
    """Pull all clean projects in parallel.

    Fires concurrent ``git fetch`` + ``git merge --ff-only`` for every
    tracked project — each in its own thread via ``asyncio.to_thread``.
    Dirty projects (uncommitted changes) are *skipped*, not failed.
    Returns an aggregate summary with per-project breakdowns.

    This solves the "only one project pulls" problem: the old single-pull
    endpoint blocked the uvicorn event loop with a 600s ``communicate()``
    call, so concurrent pull requests were serialized. Running each
    pull in a thread pool lets all projects pull simultaneously.

    The request body is optional (same semantics as
    POST /projects/{name}/git-pull): ``remote`` defaults to ``"origin"``,
    ``branch`` defaults to ``None`` (resolve the current branch per-repo).
    """
    remote = payload.remote if payload is not None else "origin"
    branch = payload.branch if payload is not None else None

    scans = list_all_latest_scans()
    # Keep only projects that have a recorded path.
    candidates = [s for s in scans if s.get("path")]
    if not candidates:
        return BatchPullResponse(
            success=True, total=0, pulled=0,
            already_up_to_date=0, dirty_skipped=0,
            diverged=0, failed=0, results=[],
        )

    # Each project pulls in its own thread — no blocking the event loop.
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                _do_pull_project,
                scan["project_name"],
                scan["path"],
                remote,
                branch,
            )
            for scan in candidates
        )
    )

    result_list = list(results)
    return BatchPullResponse(
        success=True,
        total=len(result_list),
        pulled=sum(1 for r in result_list if r.status == "pulled"),
        already_up_to_date=sum(
            1 for r in result_list if r.status == "already_up_to_date"
        ),
        dirty_skipped=sum(1 for r in result_list if r.status == "dirty"),
        diverged=sum(1 for r in result_list if r.status == "diverged"),
        failed=sum(
            1 for r in result_list
            if r.status in ("failed", "timeout", "no_remote")
        ),
        results=result_list,
    )


class GitCommitRequest(BaseModel):
    """Body for POST /projects/{name}/git-commit."""

    message: str = Field(..., min_length=1, max_length=500)
    commit_and_push: bool = Field(
        True,
        description="Push after commit if remote has upstream",
    )


class GitPushRequest(BaseModel):
    """Body for POST /projects/{name}/git-push."""

    remote: str = Field("origin", description="Remote name (default origin)")
    branch: Optional[str] = Field(
        None, description="Branch name (default: current)"
    )


class GitPushResult(BaseModel):
    """Subset of a git push result, embedded inside commit/push responses."""

    success: bool = Field(False, description="Whether the push succeeded")
    message: str = Field("", description="git push stdout")
    stderr: str = Field("", description="git push stderr")


class GitStatusResponse(BaseModel):
    """Response payload for GET /projects/{name}/git-status."""

    branch: str = Field("", description="Current branch (or '' if detached)")
    ahead: int = Field(0, description="Commits ahead of upstream")
    behind: int = Field(0, description="Commits behind upstream")
    modified_files: list[dict] = Field(
        default_factory=list,
        description="Modified/staged/deleted/renamed files with status codes",
    )
    untracked_files: list[dict] = Field(
        default_factory=list, description="Untracked files"
    )
    detached: bool = Field(False, description="Whether HEAD is detached")
    has_upstream: bool = Field(
        False, description="Whether the current branch tracks an upstream"
    )
    last_commit_hash: Optional[str] = Field(
        None, description="Hash of the most recent commit"
    )
    last_commit_message: Optional[str] = Field(
        None, description="Message of the most recent commit"
    )


class GitCommitResponse(BaseModel):
    """Response payload for POST /projects/{name}/git-commit."""

    success: bool = Field(False, description="Whether the commit succeeded")
    message: str = Field("", description="git commit stdout")
    stderr: str = Field("", description="git commit stderr")
    push: Optional["GitPushResult"] = Field(
        None, description="Push result when commit_and_push was requested"
    )


class GitPushResponse(BaseModel):
    """Response payload for POST /projects/{name}/git-push."""

    success: bool = Field(False, description="Whether the push succeeded")
    message: str = Field("", description="git push stdout")
    stderr: str = Field("", description="git push stderr")


class GitStashResponse(BaseModel):
    """Response payload for POST /projects/{name}/git-stash."""

    success: bool = Field(False, description="Whether the stash succeeded")
    message: str = Field("", description="git stash stdout")
    stderr: str = Field("", description="git stash stderr")


class GitFetchRequest(BaseModel):
    """Body for POST /projects/{name}/git/fetch (optional — ``{}`` is fine)."""

    remote: str = Field("origin", description="Remote name (default origin)")


class GitFetchResponseDTO(BaseModel):
    """Response payload for POST /projects/{name}/git/fetch."""

    success: bool = Field(False, description="Whether the fetch succeeded")
    message: str = Field("", description="Concise human-readable summary")
    stderr: str = Field("", description="git fetch stderr")


class FileOpRequestPayloadDTO(BaseModel):
    """Body for the per-file git operations (stage / unstage / discard)."""

    filepath: str = Field(..., description="Repo-relative path of the file")


class FileOpResponseDTO(BaseModel):
    """Response payload for the per-file git operations."""

    success: bool = Field(False, description="Whether the operation succeeded")
    message: str = Field("", description="Concise human-readable summary")
    stderr: str = Field("", description="git stderr")
    deleted: bool = Field(False, description="Whether the file is now gone from disk")


class GitDiffResponseDTO(BaseModel):
    """Response payload for GET /projects/{name}/git/diff.

    ``worktree_diff`` / ``staged_diff`` are snake_case on the wire — the
    mobile client keys off those exact names.
    """

    success: bool = Field(False, description="Whether the diffs were produced")
    filepath: str = Field("", description="Echo of the requested repo-relative path")
    worktree_diff: str = Field("", description="Unstaged diff (`git diff`)")
    staged_diff: str = Field("", description="Staged diff (`git diff --cached`)")
    binary: bool = Field(False, description="Binary file — diffs are omitted")
    truncated: bool = Field(False, description="A diff hit MAX_DIFF_CHARS")
    message: str = Field("", description="Error summary (empty on success)")

@router.post("/projects/{name}/git-stash", response_model=GitStashResponse)
async def git_stash_endpoint(name: str) -> GitStashResponse:
    """Move all uncommitted changes (including untracked) to the stash.

    This is the escape hatch from the commit modal when the user has
    changes they want to set aside before doing anything else. The
    stash entry is preserved on the stack (``stash@{0}``) so the
    user can ``git stash pop`` later.

    Returns:
        GitStashResponse with ``success`` and ``message`` fields.
    """
    try:
        row = _handle_project_lookup(name)
        project_path = row["path"]
        if not project_path:
            raise HTTPException(
                status_code=500, detail=f"Project '{name}' has no recorded path"
            )
        result = git_stash(project_path, include_untracked=True)
        if not result.get("success"):
            raise HTTPException(
                status_code=400,
                detail=f"git stash failed: {result.get('message', 'unknown')} :: {result.get('stderr', '')}".strip(" :"),
            )
        return GitStashResponse(
            success=True,
            message=result.get("message", "Stashed."),
            stderr=result.get("stderr", ""),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_stash_endpoint: unexpected error for name=%s", name)
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )



@router.get(
    "/projects/{name}/git-status", response_model=GitStatusResponse
)
async def get_project_git_status(name: str) -> GitStatusResponse:
    """Return working-tree status for a project (branch, ahead/behind, dirty files).

    Resolves the project path from the latest DB scan, runs ``git status
    --porcelain --branch --ahead-behind`` via :func:`git_scanner.get_git_status`,
    and merges the fresh last-commit hash/message from ``get_last_commit``.

    Returns HTTP 404 when the project has never been scanned, and HTTP 500
    when the git command itself fails (git_scanner returns an empty dict).
    """
    try:
        row = _handle_project_lookup(name)
        project_path = row.get("path")
        if not project_path:
            raise HTTPException(
                status_code=500,
                detail=f"Project '{name}' has no recorded path",
            )

        status = get_git_status(project_path)
        if not status:
            # Empty dict means the underlying git command failed.
            raise HTTPException(
                status_code=500,
                detail=f"git status failed for project '{name}'",
            )

        last_commit = get_last_commit(project_path) or {}

        return GitStatusResponse(
            branch=status.get("branch", ""),
            ahead=status.get("ahead", 0),
            behind=status.get("behind", 0),
            modified_files=status.get("modified_files", []),
            untracked_files=status.get("untracked_files", []),
            detached=bool(status.get("detached", False)),
            has_upstream=bool(status.get("has_upstream", False)),
            last_commit_hash=last_commit.get("hash"),
            last_commit_message=last_commit.get("message"),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(
            "get_project_git_status: unexpected error for name=%s", name
        )
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


@router.post(
    "/projects/{name}/git-commit", response_model=GitCommitResponse
)
async def git_commit_project(
    name: str, payload: GitCommitRequest
) -> GitCommitResponse:
    """Stage all changes and commit them, optionally pushing to the remote.

    Delegates to :func:`git_scanner.git_commit` (which transparently retries
    unsigned when GPG signing fails) and, when ``commit_and_push`` is True
    and the commit succeeded, to :func:`git_scanner.git_push`.

    Invalidates the project list cache so the next ``/projects`` call reflects
    the new state.
    """
    try:
        row = _handle_project_lookup(name)
        project_path = row.get("path")
        if not project_path:
            raise HTTPException(
                status_code=500,
                detail=f"Project '{name}' has no recorded path",
            )

        result = git_commit(project_path, payload.message, gpg_sign=True)

        push_result: Optional["GitPushResult"] = None
        if result.get("success") and payload.commit_and_push:
            push = git_push(project_path)
            push_result = GitPushResult(
                success=bool(push.get("success", False)),
                message=push.get("message", ""),
                stderr=push.get("stderr", ""),
            )

        # Always invalidate the cache when commit succeeded — the working
        # tree just changed and any cached scan is stale. The paginated
        # list caches under namespaced keys like
        # "project_manager:projects:q=<q>:page=<p>:size=<s>" — a
        # delete_pattern wildcard wipe keeps every permutation fresh.
        if result.get("success"):
            cache.delete_pattern("project_manager:projects")

        return GitCommitResponse(
            success=bool(result.get("success", False)),
            message=result.get("message", ""),
            stderr=result.get("stderr", ""),
            push=push_result,
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(
            "git_commit_project: unexpected error for name=%s", name
        )
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


@router.post(
    "/projects/{name}/git-push", response_model=GitPushResponse
)
async def git_push_project(
    name: str, payload: GitPushRequest
) -> GitPushResponse:
    """Push the current (or requested) branch to the given remote.

    Delegates to :func:`git_scanner.git_push`. When ``branch`` is omitted
    the helper resolves the current branch via ``git rev-parse``.
    """
    try:
        row = _handle_project_lookup(name)
        project_path = row.get("path")
        if not project_path:
            raise HTTPException(
                status_code=500,
                detail=f"Project '{name}' has no recorded path",
            )

        result = git_push(project_path, payload.remote, payload.branch)
        return GitPushResponse(
            success=bool(result.get("success", False)),
            message=result.get("message", ""),
            stderr=result.get("stderr", ""),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(
            "git_push_project: unexpected error for name=%s", name
        )
        raise HTTPException(
            status_code=500, detail=f"Unexpected error: {type(exc).__name__}"
        )


# ---------------------------------------------------------------------------
# Git per-file operations + fetch/diff (mobile contract)
# ---------------------------------------------------------------------------

# ponytail: 400k chars per diff side — plenty for a real source file and
# small enough to survive a mobile JSON payload. Raise it if the mobile
# client ever truncates real diffs.
MAX_DIFF_CHARS = 400_000


def _safe_repo_path(root: str, filepath: str) -> Path | None:
    """Resolve *filepath* inside the repo at *root*, or ``None`` if it escapes.

    Every git invocation built from a client-supplied path goes through
    here first, so a ``../../etc/passwd`` (or a symlink pointing outside)
    can never reach git. Returns the *resolved absolute* path — callers
    must pass that to git, never the raw user string.
    """
    if not filepath or not filepath.strip():
        return None
    try:
        root_resolved = Path(root).resolve()
        candidate = (root_resolved / filepath).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def _repo_relative(root: str, target: Path) -> str:
    """Repo-relative POSIX path for *target*, for a ``HEAD:<path>`` lookup.

    ``git cat-file`` wants the path as git stores it — forward slashes, no
    leading ``./``. The caller has already run :func:`_safe_repo_path`, so the
    target is inside the repo and the relative form is well defined.
    """
    return Path(target).resolve().relative_to(Path(root).resolve()).as_posix()


def _git_project_root(name: str) -> str | None:
    """Return the on-disk path recorded for *name*, or ``None`` if unknown.

    Resolves through the module-level ``get_latest_scan`` so tests can
    monkey-patch ``plugin_api.get_latest_scan``.
    """
    row = get_latest_scan(project_name=name)
    if not row:
        return None
    return row.get("path") or None


@router.post("/projects/{name}/git/fetch", response_model=GitFetchResponseDTO)
async def git_fetch_project(
    name: str, payload: Optional[GitFetchRequest] = None
) -> GitFetchResponseDTO | JSONResponse:
    """Fetch *remote* into the local repo without touching the working tree.

    The body is optional — the mobile client may send ``{}`` or nothing,
    in which case ``origin`` is used.
    """
    try:
        project_path = _git_project_root(name)
        if not project_path:
            return _err(404, "PROJECT_NOT_FOUND", f"Project '{name}' not found.")
        remote = (payload.remote if payload else "origin") or "origin"
        try:
            result = _run_git(project_path, ["fetch", remote], timeout=600)
        except subprocess.TimeoutExpired:
            _cleanup_stale_lock(project_path)
            return GitFetchResponseDTO(
                success=False,
                message="Fetch timeout after 600s; please retry",
                stderr="",
            )
        stderr = result.stderr or ""
        if result.returncode != 0:
            return GitFetchResponseDTO(
                success=False,
                message=stderr.strip() or f"Fetch failed (exit {result.returncode})",
                stderr=stderr,
            )
        # Refs moved — any cached project list is now stale.
        cache.delete_pattern("project_manager:projects")
        return GitFetchResponseDTO(
            success=True, message=f"Fetched from {remote}", stderr=""
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_fetch_project: unexpected error for name=%s", name)
        return _err(500, "GIT_FETCH_FAILED", f"Unexpected error: {type(exc).__name__}")


def _git_op_failure(
    result: subprocess.CompletedProcess, verb: str
) -> Optional[FileOpResponseDTO]:
    """Failure DTO for a non-zero git exit, or ``None`` when it succeeded."""
    if result.returncode == 0:
        return None
    stderr = result.stderr or ""
    return FileOpResponseDTO(
        success=False,
        message=stderr.strip() or f"{verb} failed (exit {result.returncode})",
        stderr=stderr,
    )


def _resolve_file_target(
    name: str, filepath: str
) -> tuple[str, Path] | FileOpResponseDTO | JSONResponse:
    """Resolve *name* + *filepath* to ``(repo_path, absolute_file_path)``.

    Returns the contract 404 when the project is unknown, and a
    ``success=False`` DTO when the path escapes the repo — the mobile
    client renders that message instead of a stack trace.
    """
    project_path = _git_project_root(name)
    if not project_path:
        return _err(404, "PROJECT_NOT_FOUND", f"Project '{name}' not found.")
    target = _safe_repo_path(project_path, filepath)
    if target is None:
        return FileOpResponseDTO(
            success=False,
            message="Path escapes the repository root",
            stderr="",
            deleted=False,
        )
    return project_path, target


async def _git_file_op(
    name: str, payload: FileOpRequestPayloadDTO, argv: list[str], verb: str
) -> FileOpResponseDTO | JSONResponse:
    """Shared plumbing for stage / unstage.

    *argv* is the git sub-command; ``--`` and the validated absolute path
    are appended, so a filename that looks like a flag (``-f``) stays a
    filename and the raw user string never reaches git.
    """
    resolved = _resolve_file_target(name, payload.filepath)
    if not isinstance(resolved, tuple):
        return resolved
    project_path, target = resolved
    try:
        result = _run_git(project_path, argv + ["--", str(target)], timeout=30)
    except subprocess.TimeoutExpired:
        _cleanup_stale_lock(project_path)
        return FileOpResponseDTO(
            success=False, message=f"{verb} timed out; please retry", stderr=""
        )
    failure = _git_op_failure(result, verb)
    if failure is not None:
        return failure
    cache.delete_pattern("project_manager:projects")
    return FileOpResponseDTO(success=True, message=f"{verb} succeeded.", stderr="")


@router.post("/projects/{name}/git/stage", response_model=FileOpResponseDTO)
async def git_stage_file(
    name: str, payload: FileOpRequestPayloadDTO
) -> FileOpResponseDTO | JSONResponse:
    """Stage a single file (``git add -- <path>``)."""
    try:
        return await _git_file_op(name, payload, ["add"], "Stage")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_stage_file: unexpected error for name=%s", name)
        return _err(500, "GIT_STAGE_FAILED", f"Unexpected error: {type(exc).__name__}")


@router.post("/projects/{name}/git/unstage", response_model=FileOpResponseDTO)
async def git_unstage_file(
    name: str, payload: FileOpRequestPayloadDTO
) -> FileOpResponseDTO | JSONResponse:
    """Unstage a single file (``git restore --staged -- <path>``)."""
    try:
        return await _git_file_op(name, payload, ["restore", "--staged"], "Unstage")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_unstage_file: unexpected error for name=%s", name)
        return _err(500, "GIT_UNSTAGE_FAILED", f"Unexpected error: {type(exc).__name__}")


@router.post("/projects/{name}/git/discard", response_model=FileOpResponseDTO)
async def git_discard_file(
    name: str, payload: FileOpRequestPayloadDTO
) -> FileOpResponseDTO | JSONResponse:
    """Throw away local changes to one file.

    Untracked files are removed from disk (``git clean -f``); tracked files
    are restored from HEAD in both the index and the worktree (which also
    brings back a file deleted in the worktree). ``deleted`` tells the
    client whether the file is now gone.
    """
    try:
        resolved = _resolve_file_target(name, payload.filepath)
        if not isinstance(resolved, tuple):
            return resolved
        project_path, target = resolved

        # Which branch? `git ls-files` only says the path is in the INDEX,
        # which is not enough: a staged-new file (`A `) is in the index and in
        # no commit, and `git restore --source=HEAD` on it silently removes it
        # from disk with nothing to restore. That is data loss behind a dialog
        # promising a HEAD restore, so the discriminator is "does HEAD have
        # this path", not "is the index aware of it".
        in_index = (
            _run_git(
                project_path,
                ["ls-files", "--error-unmatch", "--", str(target)],
                timeout=30,
            ).returncode
            == 0
        )
        in_head = (
            _run_git(
                project_path,
                ["cat-file", "-e", f"HEAD:{_repo_relative(project_path, target)}"],
                timeout=30,
            ).returncode
            == 0
        )
        # Only a path that exists in a commit can be restored from it.
        # Everything else is untracked-or-added and must go through the clean
        # path so `deleted` tells the truth.
        argv = (
            ["restore", "--source=HEAD", "--staged", "--worktree"]
            if in_head
            else (["reset", "--", str(target)] if in_index else ["clean", "-f"])
        )
        try:
            discard = _run_git(
                project_path, argv + ["--", str(target)], timeout=30
            )
        except subprocess.TimeoutExpired:
            _cleanup_stale_lock(project_path)
            return FileOpResponseDTO(
                success=False, message="Discard timed out; please retry", stderr=""
            )
        failure = _git_op_failure(discard, "Discard")
        if failure is not None:
            return failure
        cache.delete_pattern("project_manager:projects")
        return FileOpResponseDTO(
            success=True,
            message="Discard succeeded.",
            stderr="",
            deleted=not target.exists(),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_discard_file: unexpected error for name=%s", name)
        return _err(500, "GIT_DISCARD_FAILED", f"Unexpected error: {type(exc).__name__}")


def _is_binary_numstat(stdout: str) -> bool:
    """True when a ``--numstat`` line reports a binary file (``-\\t``)."""
    return any(line.startswith("-\t") for line in stdout.splitlines())


@router.get("/projects/{name}/git/diff", response_model=GitDiffResponseDTO)
async def git_diff_file(name: str, file: str) -> GitDiffResponseDTO | JSONResponse:
    """Return the unstaged and staged diffs for one file.

    Binary files are reported as ``binary=True`` with both diffs empty —
    raw binary bytes rendered as text are useless to the mobile client.
    Oversized diffs are cut at :data:`MAX_DIFF_CHARS` and flagged.
    """
    try:
        project_path = _git_project_root(name)
        if not project_path:
            return _err(404, "PROJECT_NOT_FOUND", f"Project '{name}' not found.")
        target = _safe_repo_path(project_path, file)
        if target is None:
            return GitDiffResponseDTO(
                success=False,
                filepath=file or "",
                worktree_diff="",
                staged_diff="",
                binary=False,
                truncated=False,
                message="Path escapes the repository root",
            )

        def _diff(argv: list[str]) -> subprocess.CompletedProcess:
            return _run_git(project_path, argv + ["--", str(target)], timeout=30)

        numstat = _diff(["diff", "--numstat"])
        cached_numstat = _diff(["diff", "--cached", "--numstat"])
        if numstat.returncode != 0 or cached_numstat.returncode != 0:
            failed = numstat if numstat.returncode != 0 else cached_numstat
            return GitDiffResponseDTO(
                success=False,
                filepath=file,
                worktree_diff="",
                staged_diff="",
                binary=False,
                truncated=False,
                message=(failed.stderr or "git diff failed").strip(),
            )

        if _is_binary_numstat(numstat.stdout or "") or _is_binary_numstat(
            cached_numstat.stdout or ""
        ):
            return GitDiffResponseDTO(
                success=True,
                filepath=file,
                worktree_diff="",
                staged_diff="",
                binary=True,
                truncated=False,
                message="Binary file — diff not available.",
            )

        worktree = _diff(["diff"])
        staged = _diff(["diff", "--cached"])
        if worktree.returncode != 0 or staged.returncode != 0:
            failed = worktree if worktree.returncode != 0 else staged
            return GitDiffResponseDTO(
                success=False,
                filepath=file,
                worktree_diff="",
                staged_diff="",
                binary=False,
                truncated=False,
                message=(failed.stderr or "git diff failed").strip(),
            )

        worktree_diff = worktree.stdout or ""
        staged_diff = staged.stdout or ""
        truncated = (
            len(worktree_diff) > MAX_DIFF_CHARS or len(staged_diff) > MAX_DIFF_CHARS
        )
        if truncated:
            worktree_diff = worktree_diff[:MAX_DIFF_CHARS]
            staged_diff = staged_diff[:MAX_DIFF_CHARS]
        return GitDiffResponseDTO(
            success=True,
            filepath=file,
            worktree_diff=worktree_diff,
            staged_diff=staged_diff,
            binary=False,
            truncated=truncated,
            message="",
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("git_diff_file: unexpected error for name=%s", name)
        return _err(500, "GIT_DIFF_FAILED", f"Unexpected error: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Watch paths CRUD
# ---------------------------------------------------------------------------


def _watch_path_row_to_item(row: dict) -> WatchPathItem:
    """Convert a raw watch_paths DB row into a WatchPathItem."""

    def _parse_dt(val: Optional[str]) -> Optional[datetime]:
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None

    return WatchPathItem(
        id=row["id"],
        path=row["path"],
        type=row["type"],
        label=row.get("label"),
        enabled=bool(row.get("enabled", 1)),
        created_at=_parse_dt(row.get("created_at")) or datetime.now(timezone.utc),
    )


@router.post("/watch-paths", response_model=WatchPathItem)
async def create_watch_path(payload: AddWatchPathRequest) -> WatchPathItem:
    """Add a new watch path entry.

    Validates that *payload.path* points at an existing directory before
    delegating to the DB layer. Returns HTTP 409 when the path is already
    registered, HTTP 400 for any other validation failure, and invalidates
    the project list cache so subsequent ``/projects`` calls re-scan.
    """
    if not os.path.isdir(payload.path):
        raise HTTPException(
            status_code=400, detail=f"path is not a directory: {payload.path}"
        )

    try:
        new_id = add_watch_path(
            path=payload.path,
            type=payload.type,
            label=payload.label,
        )
    except ValueError as exc:
        msg = str(exc)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc

    # Wildcard-invalidate every paginated /projects cache entry so the
    # next read fetches the new watch_path from the DB.
    cache.delete_pattern("project_manager:projects")

    row = next(
        (r for r in list_watch_paths(include_disabled=True) if r["id"] == new_id),
        None,
    )
    if row is None:
        # Extremely rare race (delete between insert and read); surface 500.
        raise HTTPException(
            status_code=500, detail=f"watch path {new_id} disappeared after insert"
        )
    return _watch_path_row_to_item(row)


@router.get("/watch-paths", response_model=WatchPathListResponse)
async def list_watch_paths_endpoint(
    include_disabled: bool = False,
) -> WatchPathListResponse:
    """Return all registered watch paths (filtered by enabled flag by default)."""
    rows = list_watch_paths(include_disabled=include_disabled)
    items = [_watch_path_row_to_item(r) for r in rows]
    return WatchPathListResponse(watch_paths=items, total=len(items))


class RenameRequest(BaseModel):
    label: str


@router.post("/watch-paths/reorder")
async def reorder_watch_paths_endpoint(payload: dict) -> dict:
    ids = payload.get("ids")
    if (
        not isinstance(ids, list)
        or not ids
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in ids)
    ):
        raise HTTPException(status_code=400, detail="ids must be a non-empty list of integers")
    reorder_watch_paths(ids)
    # Wildcard-invalidate every paginated /projects cache entry so the
    # new sort_order is picked up on the next read.
    cache.delete_pattern("project_manager:projects")
    return {"reordered": True, "ids": ids}


@router.get("/watch-paths/suggest")
async def suggest_watch_paths(prefix: str = "") -> dict:
    prefix = prefix.strip()
    if len(prefix) < 2:
        raise HTTPException(status_code=400, detail="prefix must be at least 2 characters")

    parent = os.path.dirname(prefix)
    fragment = os.path.basename(prefix)
    try:
        suggestions = [
            os.path.join(parent, entry)
            for entry in os.listdir(parent)
            if entry.startswith(fragment)
            and os.path.isdir(os.path.join(parent, entry))
            and not os.path.islink(os.path.join(parent, entry))
        ][:10]
    except (OSError, PermissionError):
        suggestions = []
    return {"suggestions": suggestions}


@router.patch("/watch-paths/{watch_path_id}")
async def rename_watch_path_endpoint(watch_path_id: int, payload: RenameRequest) -> dict:
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="label must not be empty")
    if rename_watch_path(watch_path_id, label) == 0:
        raise HTTPException(
            status_code=404, detail=f"watch path {watch_path_id} not found"
        )
    # Wildcard-invalidate every paginated /projects cache entry so the
    # new label is picked up on the next read.
    cache.delete_pattern("project_manager:projects")
    return {"renamed": True, "id": watch_path_id, "label": label}


@router.delete("/watch-paths/{watch_path_id}")
async def delete_watch_path_endpoint(watch_path_id: int) -> dict:
    """Delete a watch path by id. Returns 404 if it didn't exist."""
    deleted = delete_watch_path(watch_path_id)
    if deleted == 0:
        raise HTTPException(
            status_code=404, detail=f"watch path {watch_path_id} not found"
        )
    # Wildcard-invalidate every paginated /projects cache entry so the
    # removed watch_path stops contributing projects.
    cache.delete_pattern("project_manager:projects")
    return {"deleted": True, "id": watch_path_id}


# ---------------------------------------------------------------------------
# Worktree + branch picker (per-project)
# ---------------------------------------------------------------------------
# Mounted last so its ``/projects/{name}/worktrees/...`` paths don't fight
# with the project-level endpoints above. See worktrees.py for the routing
# notes / contract.
try:
    # Drop any stale copy the hermes-plugins-server.py may have imported
    # under a different sys.path; we want the worktrees module that lives
    # next to this file.
    sys.modules.pop("worktrees", None)
    from worktrees import (  # type: ignore
        router as worktrees_router,
        # The git/* endpoints below use the same contract error shape as
        # the worktree routes: {"error": ..., "code": ...} instead of
        # FastAPI's {"detail": ...}, because the mobile client switches on
        # `code`. Bound late on purpose — the import must stay at the
        # bottom of this file (sys.path collision).
        _err,
    )

    router.include_router(worktrees_router)
    log.info("Mounted worktrees router at /projects/{name}/worktrees")
except ImportError as exc:
    log.warning("worktrees router not mounted: %s", exc)

    def _err(status: int, code: str, message: str) -> JSONResponse:
        """Contract-shaped error response (fallback when worktrees is absent)."""
        return JSONResponse(
            status_code=status, content={"error": message, "code": code}
        )
