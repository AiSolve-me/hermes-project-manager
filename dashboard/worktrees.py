"""
Project Manager — Worktree + branch picker backend (per-project version)
========================================================================

AISO-57 / AISO-59 — contract locked with the Frontend (AISO-60).

This is a **per-project** refactor of the original dashboard-only worktree
backend. The original implementation resolved a single "dashboard repo" via
``_resolve_dashboard_repo_root()`` (walking up from the plugin file to find
``.git``), which restricted worktree operations to a single repository — the
one that hosted the dashboard itself. Project Manager tracks many projects
(``/home/github/projects/*`` for ``scan_root`` watch paths), so the worktree
endpoints now operate on **whichever project the caller specifies**.

Five endpoints, all name-scoped:

* ``GET    /projects/{name}/worktrees``              — list *project* worktrees
* ``POST   /projects/{name}/worktrees/{wt_id}/select`` — set the per-project active worktree
* ``DELETE /projects/{name}/worktrees/{wt_id}``       — remove a worktree (with safety nets)
* ``GET    /projects/{name}/branches``                — local + remote branches of the
                                                       active worktree
* ``POST   /projects/{name}/branches/checkout``       — switch the active worktree to a
                                                       local branch

The router is mounted under the same ``/project-manager`` prefix as the rest
of the plugin (FastAPI handles the path conflict because the plugin_api
router already has its own ``/projects/{name}/...`` endpoints for unrelated
stuff — we use a *different* path pattern that lives below ``/projects/{name}/``).

Public surface at module level:

* :data:`router` — FastAPI ``APIRouter`` exposing the five endpoints above
* :data:`_selected_id_by_project` — per-project in-memory selection
* :func:`_resolve_project_repo` — pulls the project's on-disk path from the
  Project Manager DB (test hook via monkey-patching)
* :func:`_make_worktree_id` — the ``wt_<sha256(path)[:8]>`` scheme from the
  contract

Error shape (per the contract): every worktree-route error returns a
``JSONResponse`` with ``{"error": "<msg>", "code": "<CODE>"}``. The rest of
the plugin API still uses FastAPI's ``{"detail": ...}`` shape via
``HTTPException`` — the new endpoints deliberately opt into the contract
shape so the UI can switch on ``code`` reliably.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Import db lazily so a stale cached copy from a previous reload cannot
# poison the project lookup. The plugin_api.py ensures the dashboard dir is
# on sys.path, so a plain ``import db`` works.
try:
    import db as _pm_db  # type: ignore
except ImportError:  # pragma: no cover — only happens if sys.path is wrong
    _pm_db = None  # type: ignore


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["project-manager"], prefix="/projects")

# ---------------------------------------------------------------------------
# Routing notes
# ---------------------------------------------------------------------------
#
# We live under the same ``/projects/{name}/...`` namespace as the rest of
# the plugin_api router, but the paths below are namespaced so they don't
# collide with the existing endpoints:
#
#   /projects/{name}/branches           ← plugin_api list_project_branches (kept)
#   /projects/{name}/commits            ← plugin_api get_project_commits
#   /projects/{name}/git-pull           ← plugin_api git-pull
#
#   /projects/{name}/worktrees                  ← worktrees.list_worktrees
#   /projects/{name}/worktrees/{wt_id}/select   ← worktrees.select_worktree
#   /projects/{name}/worktrees/{wt_id}          ← worktrees.delete_worktree
#   /projects/{name}/worktrees/branches         ← worktrees.list_branches
#   /projects/{name}/worktrees/checkout         ← worktrees.checkout_branch
#
# The worktree "branches" endpoint is intentionally *not* at the bare
# ``/projects/{name}/branches`` path so it doesn't fight with the existing
# project-branches endpoint above. The frontend contract is therefore:
# - GET  /projects/{name}/branches           → project repo's local branches
#   (compat with the existing UI list)
# - GET  /projects/{name}/worktrees          → worktree list of the project
# - GET  /projects/{name}/worktrees/branches → local + remote branches of the
#   *active worktree* (may differ from the project repo's branches
#   when a non-main worktree is selected)
# ---------------------------------------------------------------------------

# Per-project in-memory state. Test hooks reset this by reassigning the
# reference (``worktrees._selected_id_by_project = {}``).
_selected_id_by_project: dict[str, Optional[str]] = {}

# Branches that may never be removed from the UI — these are the default
# protected branches the contract names. The "is_current" check below
# short-circuits first, so the current worktree is implicitly protected.
_PROTECTED_BRANCHES = frozenset({"main", "master", "hermes"})


# ---------------------------------------------------------------------------
# Public helpers (also used by tests)
# ---------------------------------------------------------------------------


def _make_worktree_id(path: Path) -> str:
    """Stable worktree ID per the contract: ``wt_<sha256(path)[:8]>``."""
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]
    return f"wt_{digest}"


def _resolve_project_repo(project_name: str) -> Path:
    """Return the on-disk path of *project_name* as recorded by the
    Project Manager DB.

    The lookup goes through the latest ``project_scans`` row for the
    project name. If the project is unknown (DB has no row), or the DB
    module is unavailable, this raises ``FileNotFoundError`` so the
    endpoint can surface ``PROJECT_NOT_FOUND`` cleanly.

    Tests can monkey-patch this to bypass the DB and point at a temp
    repo.
    """
    if _pm_db is None:
        raise FileNotFoundError(
            f"Project '{project_name}' not found (db module unavailable)."
        )
    scan = _pm_db.get_latest_scan(project_name)
    if scan is None:
        raise FileNotFoundError(f"Project '{project_name}' not found.")
    path = Path(scan["path"]).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Project '{project_name}' path missing: {path}")
    return path


# ---------------------------------------------------------------------------
# Low-level git helpers (return structured dicts, raise on failure)
# ---------------------------------------------------------------------------


def _run_git(
    *args: str,
    cwd: Optional[Path] = None,
    timeout: int = 10,
) -> subprocess.CompletedProcess:
    """Run a git command. ``cwd`` defaults to :func:`_resolve_project_repo`
    with the ``project_name`` provided in the calling context — but to
    keep this helper project-agnostic, callers must pass ``cwd``."""
    if cwd is None:
        raise ValueError("cwd is required for _run_git (project-scoped).")
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


def _parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` into a list of worktree
    dicts. Each entry has: ``path``, ``head``, ``branch``, ``locked``,
    ``lock_reason``, ``prunable``, ``is_bare``.

    The porcelain format is a sequence of records separated by blank
    lines; each record starts with ``worktree <path>``.
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in output.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            # ``refs/heads/<name>`` — strip the refs/heads/ prefix.
            current["branch"] = (
                value[len("refs/heads/") :] if value.startswith("refs/heads/") else value
            )
        elif key == "detached":
            current["branch"] = ""
        elif key == "locked":
            current["locked"] = True
            # Reason is on the same line after the keyword, git uses
            # either "locked" or "locked <reason>".
            current["lock_reason"] = value or ""
        elif key == "prunable":
            current["prunable"] = True
            current["prunable_reason"] = value or ""
        elif key == "bare":
            current["is_bare"] = True
        else:
            # Forward-compatibility: keep unknown keys.
            current[key] = value
    if current:
        records.append(current)
    return records


def _is_worktree_dirty(path: Path) -> bool:
    try:
        result = _run_git("status", "--porcelain", cwd=path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return bool(result.stdout.strip())


def _is_detached(path: Path) -> bool:
    try:
        # ``symbolic-ref --quiet HEAD`` exits 0 on a normal branch and
        # non-zero (silenced) on detached HEAD.
        _run_git("symbolic-ref", "--quiet", "HEAD", cwd=path)
        return False
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return True


def _worktree_short_sha(path: Path) -> str:
    try:
        return _run_git("rev-parse", "--short", "HEAD", cwd=path).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# Worktree enumeration (per-project)
# ---------------------------------------------------------------------------


def _enrich_worktree(
    raw: dict[str, Any], project_repo_root: Path
) -> dict[str, Any]:
    """Add the contract fields onto a parsed worktree record.

    ``is_current`` is set when the worktree path matches the project's
    main repo path (the one the DB recorded for the project). This is
    the per-project equivalent of the original dashboard check.
    """
    path = Path(raw["path"]).resolve()
    branch = raw.get("branch", "")
    head = raw.get("head", "") or _worktree_short_sha(path)
    is_bare = bool(raw.get("is_bare"))
    is_locked = bool(raw.get("locked"))
    is_current = (not is_bare) and (path == project_repo_root)
    is_dirty = (not is_bare) and _is_worktree_dirty(path)
    is_protected = is_current or (branch in _PROTECTED_BRANCHES)

    return {
        "id": _make_worktree_id(path),
        "path": str(path),
        "branch": branch,
        "head": head,
        "is_current": is_current,
        "is_locked": is_locked,
        "is_protected": is_protected,
        "is_dirty": is_dirty,
        # Extra fields the UI doesn't strictly need but make debugging
        # easier; the contract doesn't promise these so the UI ignores
        # unknown keys.
        "lock_reason": raw.get("lock_reason", ""),
        "prunable": bool(raw.get("prunable")),
        "is_bare": is_bare,
    }


def _list_worktrees(project_name: str) -> list[dict[str, Any]]:
    """List *project_name*'s worktrees in the contract shape."""
    try:
        project_root = _resolve_project_repo(project_name)
    except FileNotFoundError as exc:
        log.warning("resolve project %s failed: %s", project_name, exc)
        return []

    try:
        result = _run_git("worktree", "list", "--porcelain", cwd=project_root)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("git worktree list failed for %s: %s", project_root, exc)
        return []

    raw_records = _parse_worktree_porcelain(result.stdout)
    # Filter out bare and prunable entries from the user-facing list;
    # the UI is for the user's working worktrees, not bare mirrors or
    # stale entries.
    enriched = []
    for raw in raw_records:
        if raw.get("is_bare") or raw.get("prunable"):
            continue
        enriched.append(_enrich_worktree(raw, project_root))
    return enriched


def _find_worktree_by_id(project_name: str, worktree_id: str) -> Optional[dict[str, Any]]:
    for wt in _list_worktrees(project_name):
        if wt["id"] == worktree_id:
            return wt
    return None


def _current_worktree(project_name: str) -> dict[str, Any]:
    """Resolve the *selected* worktree for *project_name*, falling back to
    the project's own worktree if nothing has been selected yet."""
    selected = _selected_id_by_project.get(project_name)
    if selected is not None:
        wt = _find_worktree_by_id(project_name, selected)
        if wt is not None:
            return wt
        # Stale id (the worktree it pointed at was removed) — fall back.
        _selected_id_by_project[project_name] = None

    for wt in _list_worktrees(project_name):
        if wt["is_current"]:
            return wt
    # No worktree is current (degenerate) — return the first one or an
    # empty sentinel so callers can produce a useful error.
    wts = _list_worktrees(project_name)
    if wts:
        return wts[0]
    return {
        "id": "",
        "path": "",
        "branch": "",
        "head": "",
        "is_current": False,
        "is_locked": False,
        "is_protected": True,  # pretend protected to fail safe
        "is_dirty": False,
    }


# ---------------------------------------------------------------------------
# Error response helper
# ---------------------------------------------------------------------------


def _err(status: int, code: str, message: str) -> JSONResponse:
    """Build the contract-shaped error response."""
    return JSONResponse(
        status_code=status,
        content={"error": message, "code": code},
    )


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class WorktreeSelectRequest(BaseModel):
    """Body for ``POST /projects/{name}/worktrees/{wt_id}/select``. Empty object is fine."""

    model_config = {"extra": "allow"}


class WorktreeDeleteRequest(BaseModel):
    """Body for ``DELETE /projects/{name}/worktrees/{wt_id}``."""

    force: bool = Field(
        False,
        description=(
            "When true, allow removing a worktree with uncommitted changes. "
            "Only overrides the WORKTREE_DIRTY check; other protections "
            "(is_current, is_locked, is_protected) still apply."
        ),
    )


class BranchCheckoutRequest(BaseModel):
    """Body for ``POST /projects/{name}/worktrees/checkout``.

    ``stash`` runs ``git stash push -u`` before checkout so the working
    tree becomes clean; the stash is left on the stack (``stash@{0}``)
    for the user to pop later. ``force`` skips the dirty check entirely
    and lets ``git switch`` roll the working tree forward — the user
    owns whatever happens to the uncommitted changes.
    """

    branch: str = Field(..., description="Local branch name to switch to.")
    stash: bool = Field(
        False,
        description=(
            "If true, run `git stash push -u` before checkout so the "
            "working tree is clean. The stash is preserved on the stack "
            "for the user to restore later."
        ),
    )
    force: bool = Field(
        False,
        description=(
            "If true, skip the WORKTREE_DIRTY check and let `git switch` "
            "proceed even with uncommitted changes. The user is responsible "
            "for any resulting loss of uncommitted work."
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{project_name}/worktrees")
async def list_worktrees(project_name: str) -> JSONResponse:
    """List *project_name*'s worktrees.

    Returns 200 with ``{worktrees, current_worktree_id}`` on success.
    Returns 404 with ``PROJECT_NOT_FOUND`` if the project is unknown
    or its path is missing.
    Returns 500 with ``WORKTREE_LIST_FAILED`` if the underlying
    ``git worktree list`` call fails.
    """
    try:
        project_root = _resolve_project_repo(project_name)
    except FileNotFoundError as exc:
        return _err(404, "PROJECT_NOT_FOUND", str(exc))

    try:
        worktrees_list = _list_worktrees(project_name)
    except Exception as exc:  # noqa: BLE001  — surface any failure as a clean error
        log.exception("list_worktrees failed for %s", project_name)
        return _err(500, "WORKTREE_LIST_FAILED", f"Failed to list worktrees: {exc}")

    if not worktrees_list:
        # The contract says 500 with a clear message if the porcelain
        # command produced nothing — a fresh empty repo has no worktrees
        # to list, but that's degenerate. Use a different code.
        if not (project_root / ".git").exists():
            return _err(
                404,
                "NOT_A_GIT_REPO",
                f"{project_root} is not a git repository.",
            )
        return _err(500, "WORKTREE_LIST_FAILED", "git worktree list returned no entries.")

    current_id = _current_worktree(project_name)["id"]
    if not current_id:
        # _current_worktree returned its empty sentinel — first row wins.
        current_id = worktrees_list[0]["id"]

    return JSONResponse(
        content={
            "worktrees": worktrees_list,
            "current_worktree_id": current_id,
        }
    )


@router.post("/{project_name}/worktrees/{worktree_id}/select")
async def select_worktree(
    project_name: str, worktree_id: str, request: Request
) -> JSONResponse:
    """Mark *worktree_id* as the active worktree for *project_name*.

    Errors (contract shape):
    * 404 ``PROJECT_NOT_FOUND`` — unknown project
    * 404 ``WORKTREE_NOT_FOUND`` — unknown id
    * 409 ``WORKTREE_DIRTY``      — selected worktree has uncommitted changes
    """
    try:
        _resolve_project_repo(project_name)
    except FileNotFoundError as exc:
        return _err(404, "PROJECT_NOT_FOUND", str(exc))

    target = _find_worktree_by_id(project_name, worktree_id)
    if target is None:
        return _err(404, "WORKTREE_NOT_FOUND", f"Worktree '{worktree_id}' not found.")

    if target["is_dirty"]:
        return _err(
            409,
            "WORKTREE_DIRTY",
            "Worktree has uncommitted changes. Commit or stash before switching.",
        )

    _selected_id_by_project[project_name] = worktree_id
    log.info("worktree selected: %s on %s (%s)", worktree_id, project_name, target["path"])
    return JSONResponse(
        content={
            "selected_id": worktree_id,
            "worktrees": _list_worktrees(project_name),
        }
    )


@router.delete("/{project_name}/worktrees/{worktree_id}")
async def delete_worktree(
    project_name: str, worktree_id: str, request: Request
) -> JSONResponse:
    """Remove a worktree via ``git worktree remove [--force] <path>``.

    Protections, in order:
    1. is_current  → 400 ``WORKTREE_IS_CURRENT``
    2. is_locked   → 400 ``WORKTREE_LOCKED``
    3. is_protected → 400 ``WORKTREE_PROTECTED``
    4. is_dirty    → 400 ``WORKTREE_DIRTY`` (overridable with ``force=true``)

    The order matters: a worktree that is both current and protected
    must surface the "current" reason so the UI can render the most
    actionable message.
    """
    try:
        project_root = _resolve_project_repo(project_name)
    except FileNotFoundError as exc:
        return _err(404, "PROJECT_NOT_FOUND", str(exc))

    target = _find_worktree_by_id(project_name, worktree_id)
    if target is None:
        return _err(404, "WORKTREE_NOT_FOUND", f"Worktree '{worktree_id}' not found.")

    # Parse body (optional). Force default = False.
    force = False
    try:
        body_bytes = await request.body()
        if body_bytes:
            payload = json.loads(body_bytes)
            force = bool(payload.get("force", False))
    except json.JSONDecodeError:
        return _err(400, "INVALID_BODY", "Request body must be valid JSON.")

    if target["is_current"]:
        return _err(
            400,
            "WORKTREE_IS_CURRENT",
            "Cannot remove the project's own worktree.",
        )
    if target["is_locked"]:
        reason = target.get("lock_reason") or "no reason given"
        return _err(
            400,
            "WORKTREE_LOCKED",
            f"Worktree is locked: {reason}",
        )
    if target["is_protected"]:
        branch = target.get("branch") or "<unknown>"
        return _err(
            400,
            "WORKTREE_PROTECTED",
            f"Cannot remove protected worktree on branch '{branch}'.",
        )
    if target["is_dirty"] and not force:
        return _err(
            400,
            "WORKTREE_DIRTY",
            "Worktree has uncommitted changes. Commit, stash, or set force=true.",
        )

    # We have approval — call ``git worktree remove``.
    cmd = ["worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(target["path"])

    try:
        # Use the project's main repo as cwd — git worktree remove is
        # invoked from the repo that *owns* the worktree, not the
        # worktree itself.
        _run_git(*cmd, cwd=project_root)
    except subprocess.CalledProcessError as exc:
        log.warning("git worktree remove failed: stderr=%s", exc.stderr)
        return _err(
            500,
            "WORKTREE_REMOVE_FAILED",
            (exc.stderr or exc.stdout or str(exc)).strip() or "git worktree remove failed",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.exception("git worktree remove transport error")
        return _err(500, "WORKTREE_REMOVE_FAILED", f"git invocation failed: {exc}")

    # If the removed worktree was the active selection, fall back.
    if _selected_id_by_project.get(project_name) == worktree_id:
        _selected_id_by_project[project_name] = None

    return JSONResponse(
        content={
            "removed_id": worktree_id,
            "worktrees": _list_worktrees(project_name),
        }
    )


@router.delete("/{project_name}/worktrees")
async def delete_all_worktrees(
    project_name: str, request: Request
) -> JSONResponse:
    """Remove every non-current worktree of *project_name* in one call.

    The current worktree (the one the project's main repo lives in) is
    never removed. Locked worktrees are skipped and reported in the
    ``skipped`` array. With ``force=true`` dirty worktrees are also
    removed; otherwise dirty worktrees are skipped.

    Body (optional): ``{"force": bool}``. The endpoint never accepts a
    list of ids — the contract is "all removable worktrees", so the UI
    shows a confirmation dialog before firing the request.
    """
    try:
        project_root = _resolve_project_repo(project_name)
    except FileNotFoundError as exc:
        return _err(404, "PROJECT_NOT_FOUND", str(exc))

    # Parse optional body for force flag.
    force = False
    try:
        body_bytes = await request.body()
        if body_bytes:
            payload = json.loads(body_bytes)
            force = bool(payload.get("force", False))
    except json.JSONDecodeError:
        return _err(400, "INVALID_BODY", "Request body must be valid JSON.")

    worktrees = _list_worktrees(project_name)
    if not worktrees:
        if not (project_root / ".git").exists():
            return _err(
                404,
                "NOT_A_GIT_REPO",
                f"{project_root} is not a git repository.",
            )
        return JSONResponse(
            content={
                "removed_ids": [],
                "skipped": [{"id": "(none)", "reason": "No worktrees to remove."}],
                "worktrees": [],
            }
        )

    removed_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for wt in worktrees:
        # Never remove the current (project root) worktree.
        if wt["is_current"]:
            skipped.append({"id": wt["id"], "path": wt["path"], "reason": "WORKTREE_IS_CURRENT"})
            continue
        if wt["is_locked"]:
            skipped.append({"id": wt["id"], "path": wt["path"], "reason": "WORKTREE_LOCKED"})
            continue
        if wt["is_protected"]:
            skipped.append({"id": wt["id"], "path": wt["path"], "reason": "WORKTREE_PROTECTED"})
            continue
        if wt["is_dirty"] and not force:
            skipped.append({"id": wt["id"], "path": wt["path"], "reason": "WORKTREE_DIRTY"})
            continue

        cmd = ["worktree", "remove"]
        if force:
            cmd.append("--force")
        cmd.append(wt["path"])
        try:
            _run_git(*cmd, cwd=project_root)
            removed_ids.append(wt["id"])
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
            log.warning("bulk git worktree remove failed for %s: %s", wt["path"], stderr)
            errors.append({"id": wt["id"], "path": wt["path"], "stderr": stderr})
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.exception("bulk git worktree remove transport error")
            errors.append({"id": wt["id"], "path": wt["path"], "stderr": str(exc)})

    # If the active selection was removed, fall back to the current worktree.
    selected = _selected_id_by_project.get(project_name)
    if selected and selected in removed_ids:
        _selected_id_by_project[project_name] = None

    return JSONResponse(
        content={
            "removed_ids": removed_ids,
            "skipped": skipped,
            "errors": errors,
            "worktrees": _list_worktrees(project_name),
        }
    )


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------


def _remote_branch_exists(cwd: Path, branch: str) -> bool:
    """True if ``refs/remotes/origin/<branch>`` exists in *cwd*.

    Uses ``show-ref --verify --quiet`` which exits 0 on hit and non-zero
    on miss (silenced by ``--quiet``).
    """
    try:
        _run_git(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{branch}",
            cwd=cwd,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _list_local_branches(cwd: Path) -> list[dict[str, Any]]:
    """List local branches with upstream info via ``git for-each-ref``."""
    try:
        result = _run_git(
            "for-each-ref",
            "--format=%(HEAD)%00%(refname:short)%00%(upstream:short)",
            "refs/heads",
            cwd=cwd,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("git for-each-ref refs/heads failed: %s", exc)
        return []

    branches: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) < 3:
            # Defensive: a malformed line shouldn't take the whole endpoint down.
            continue
        head_marker, name, upstream = parts[0], parts[1], parts[2]
        branches.append(
            {
                "name": name,
                "is_current": head_marker == "*",
                "is_remote": False,
                "upstream": upstream or "",
            }
        )
    return branches


def _list_remote_branches(cwd: Path) -> list[dict[str, Any]]:
    """List remote branches via ``git for-each-ref refs/remotes/origin``.

    Only the ``origin`` remote is exposed — the dashboard's UI only needs
    to show "what's on origin" so the user knows what to pull / what
    upstream PRs exist. The local-only contract is preserved: remote
    entries are flagged ``is_remote=True`` so the UI disables them and
    the backend rejects checkout attempts on them.
    """
    try:
        result = _run_git(
            "for-each-ref",
            "--format=%(HEAD)%00%(refname:short)%00%(upstream:short)",
            "refs/remotes/origin",
            cwd=cwd,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        # ``refs/remotes/origin`` may not exist if no remote is configured
        # — that's fine, just return an empty list.
        log.debug("no refs/remotes/origin: %s", exc)
        return []

    branches: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\x00")
        if len(parts) < 3:
            continue
        head_marker, full_name, upstream = parts[0], parts[1], parts[2]
        # ``refs/remotes/origin/agent/foo`` → ``name: "agent/foo"`` and
        # ``upstream: "origin/agent/foo"`` (full upstream ref per the
        # contract decision).
        if full_name.startswith("origin/"):
            stripped = full_name[len("origin/") :]
        else:
            stripped = full_name
        # Skip origin/HEAD — it's a pointer to the default branch and the
        # UI shouldn't treat it as a checkout target.
        if stripped == "HEAD":
            continue
        branches.append(
            {
                "name": stripped,
                "is_current": False,  # Remote branches are never "current".
                "is_remote": True,
                "upstream": full_name or "",
            }
        )
    return branches


def _list_branches_for_active(project_name: str) -> tuple[list[dict[str, Any]], str]:
    """Return (branches, current_branch) for the *active* worktree of
    *project_name*.

    ``current_branch`` is the empty string if the active worktree is in
    a detached HEAD state — per the contract, that's how the UI detects
    a detached worktree (it sees ``is_current: false`` on every entry
    and an empty ``current_branch``).
    """
    try:
        project_root = _resolve_project_repo(project_name)
    except FileNotFoundError:
        return [], ""

    active = _current_worktree(project_name)
    cwd = Path(active["path"]) if active["path"] else project_root
    if not cwd or not cwd.exists():
        return [], ""

    if _is_detached(cwd):
        # Still report local + remote branches (the user might want to
        # see what's available) but mark the current branch as empty
        # and ensure no entry has is_current=True.
        local = [b for b in _list_local_branches(cwd) if not b["is_current"]]
        remote = _list_remote_branches(cwd)
        return local + remote, ""

    local = _list_local_branches(cwd)
    remote = _list_remote_branches(cwd)
    current_branch = active.get("branch", "")
    return local + remote, current_branch


@router.get("/{project_name}/worktrees/branches")
async def list_branches(project_name: str) -> JSONResponse:
    """List local + remote branches of the *active* worktree of *project_name*.

    The "active" worktree is the per-project ``_selected_id`` if set, or
    the project's own worktree if not.

    Detached HEAD: returns 200 with ``current_branch: ""`` and no
    ``is_current: true`` entry in the branch list.
    """
    try:
        branches, current_branch = _list_branches_for_active(project_name)
    except Exception as exc:  # noqa: BLE001
        log.exception("list_branches failed for %s", project_name)
        return _err(500, "BRANCH_LIST_FAILED", f"Failed to list branches: {exc}")

    return JSONResponse(
        content={
            "branches": branches,
            "current_branch": current_branch,
        }
    )


@router.post("/{project_name}/worktrees/checkout")
async def checkout_branch(
    project_name: str, payload: BranchCheckoutRequest
) -> JSONResponse:
    """Switch the active worktree of *project_name* to *payload.branch*.

    Validates, in order:
    * detached HEAD on the active worktree → 400 ``WORKTREE_DETACHED``
    * uncommitted changes              → 400 ``WORKTREE_DIRTY``
    * branch not local                 → 400 ``BRANCH_IS_REMOTE_ONLY``
    * branch unknown                   → 404 ``BRANCH_NOT_FOUND``
    """
    try:
        project_root = _resolve_project_repo(project_name)
    except FileNotFoundError as exc:
        return _err(404, "PROJECT_NOT_FOUND", str(exc))

    branch = payload.branch.strip()
    if not branch:
        return _err(400, "INVALID_BODY", "Field 'branch' is required.")

    active = _current_worktree(project_name)
    cwd = Path(active["path"]) if active.get("path") else project_root
    if not cwd or not cwd.exists():
        return _err(500, "WORKTREE_INVALID", "Active worktree path is missing or invalid.")

    # Detached HEAD — refuse before doing anything else.
    if _is_detached(cwd):
        return _err(
            400,
            "WORKTREE_DETACHED",
            "Worktree is in detached HEAD state — checkout is not possible.",
        )

    # Dirty working tree. Two escape hatches are honoured:
    #   - payload.stash=True  → git stash push -u, then checkout. The
    #                            stash is preserved on the stack so the
    #                            user can `git stash pop` later.
    #   - payload.force=True  → skip the dirty check; `git switch` will
    #                            roll the working tree forward at its own
    #                            risk (typically refusing if there are
    #                            staged changes that would conflict).
    if _is_worktree_dirty(cwd):
        if payload.stash:
            try:
                _run_git("stash", "push", "-u", cwd=cwd)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip() or str(exc)
                log.warning("git stash push failed: %s", stderr)
                return _err(
                    500,
                    "STASH_FAILED",
                    f"Failed to stash changes before checkout: {stderr}",
                )
            except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                log.exception("git stash push transport error")
                return _err(500, "STASH_FAILED", f"git stash failed: {exc}")
        elif not payload.force:
            return _err(
                400,
                "WORKTREE_DIRTY",
                f"Cannot checkout: uncommitted changes on worktree '{cwd}'.",
            )
        # payload.force=True with dirty tree: fall through and let
        # `git switch` decide what to do (it refuses with a clear
        # message if the working tree would be overwritten).

    # Resolve the branch's location. It may be:
    #  - local                  → OK, switch to it
    #  - remote-only            → 400 BRANCH_IS_REMOTE_ONLY
    #  - doesn't exist anywhere → 404 BRANCH_NOT_FOUND
    local_branches = _list_local_branches(cwd)
    if branch in {b["name"] for b in local_branches}:
        pass
    elif _remote_branch_exists(cwd, branch):
        return _err(
            400,
            "BRANCH_IS_REMOTE_ONLY",
            f"Only local branches can be checked out from the UI. Branch '{branch}' is remote-only.",
        )
    else:
        return _err(404, "BRANCH_NOT_FOUND", f"Branch '{branch}' not found.")

    previous_branch = active.get("branch", "")

    # Use ``git switch`` (not ``checkout``) — switch refuses remote-only
    # refs and never does file-level checkouts, which is exactly what
    # the contract asks for.
    try:
        _run_git("switch", branch, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip() or (exc.stdout or "").strip() or str(exc)
        log.warning("git switch failed: %s", stderr)
        # ``git switch`` prints a clear message when the branch is
        # already checked out elsewhere; surface that with a distinct
        # code so the UI can render it differently.
        if "already used by worktree" in stderr:
            return _err(
                409,
                "BRANCH_CHECKED_OUT_ELSEWHERE",
                f"Branch '{branch}' is already checked out by another worktree.",
            )
        if "invalid reference" in stderr or "not a commit" in stderr:
            return _err(404, "BRANCH_NOT_FOUND", f"Branch '{branch}' not found.")
        return _err(500, "BRANCH_CHECKOUT_FAILED", stderr)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.exception("git switch transport error")
        return _err(500, "BRANCH_CHECKOUT_FAILED", f"git invocation failed: {exc}")

    return JSONResponse(
        content={
            "branch": branch,
            "previous_branch": previous_branch,
            "stashed": bool(payload.stash),
            "worktrees": _list_worktrees(project_name),
        }
    )
