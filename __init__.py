"""Project Manager — Hermes plugin agent tools, backed by the plugin's own code.

Every tool runs **in-process** inside the Hermes plugin process: it imports the
scanner/DB modules that live in ``dashboard/`` next to this file and calls them
directly. There is no HTTP server, no standalone process and no network access
involved, so the tools work under a plain ``hermes plugins install``.

Modules are imported by path relative to ``__file__`` (Hermes imports a plugin
by path, so the cwd is *not* the plugin directory) and the same
``sys.modules.pop`` + ``sys.path.insert`` dance the rest of this plugin uses is
applied first, so another plugin shipping its own ``db.py`` cannot shadow ours.
Names are bound at module scope on purpose — a late ``from db import ...`` inside
a handler re-resolves through ``sys.path`` and can pick up a foreign module.

All handlers return a JSON string. Success and errors alike: success carries
``"success": true`` plus the fields the tool schema promises, errors carry
``"success": false`` and an ``error`` message.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Import the plugin's own dashboard modules, in-process.
# ----------------------------------------------------------------------

_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
if _DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, _DASHBOARD_DIR)

# Drop stale modules so we always load OUR modules, not another plugin's.
# Same convention as dashboard/plugin_api.py and project_manager_server.py.
for _mod in ("db", "models", "git_scanner", "cache", "docker_status"):
    sys.modules.pop(_mod, None)

from cache import cache  # noqa: F401  (popped above so we load our own)
from db import (  # noqa: E402
    get_db_pool,
    get_enabled_watch_paths,
    get_latest_scan,
    init_db,
    list_latest_scans_paginated,
    record_scan,
)
# Aliased: the tool handler below is itself named list_watch_paths, and a
# same-named global would shadow the db function inside its own body.
from db import list_watch_paths as db_list_watch_paths  # noqa: E402
from docker_status import has_docker, list_running_stacks, match_project  # noqa: E402
from git_scanner import (  # noqa: E402
    get_current_branch,
    get_branches,
    scan_all_projects,
)
from models import (  # noqa: E402
    CommitItem,
    CommitListResponse,
    ProjectItem,
    ProjectListResponse,
    WatchPathItem,
    WatchPathListResponse,
)

_PAGE_SIZE = 27  # max of db.ALLOWED_PAGE_SIZES — the widest page the query allows
_SCHEMA_READY = False
_SCANNED_ONCE = False


def _ensure_schema() -> None:
    """Create the SQLite tables on first use.

    ``project_manager_server.create_app()`` normally does this; with no server
    running the agent tools are the only entry point, so they own it.
    ``init_db`` is idempotent (CREATE TABLE IF NOT EXISTS + PRAGMA-guarded
    ALTERs).
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    init_db()
    _SCHEMA_READY = True


def _err(msg: str) -> str:
    return json.dumps({"success": False, "error": msg, "plugin": "project-manager"})


# ----------------------------------------------------------------------
# Tool schemas — what the LLM sees to decide when to call each tool.
# ----------------------------------------------------------------------

LIST_PROJECTS = {
    "name": "list_projects",
    "description": (
        "List all git projects tracked by Project Manager. "
        "Returns project name, path, current branch, and last-commit metadata. "
        "Use this when the user asks 'what projects do I have?' or 'show my repos'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "search": {
                "type": "string",
                "description": "Optional substring filter on project name or path.",
            }
        },
        "required": [],
    },
}

GET_PROJECT = {
    "name": "get_project",
    "description": (
        "Get details for a single tracked project by name: path, current branch, "
        "HEAD commit, and last-scan timestamp."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The project name (as returned by list_projects).",
            }
        },
        "required": ["name"],
    },
}

LIST_BRANCHES = {
    "name": "list_branches",
    "description": (
        "List local and remote branches for a tracked project. "
        "Returns branch name, current marker, and last-commit info."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The project name.",
            }
        },
        "required": ["name"],
    },
}

GET_RECENT_COMMITS = {
    "name": "get_recent_commits",
    "description": (
        "List recent commits for a tracked project. "
        "Returns commit hash, author, timestamp, and subject."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The project name.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of commits to return (default 20).",
                "default": 20,
            }
        },
        "required": ["name"],
    },
}

LIST_WATCH_PATHS = {
    "name": "list_watch_paths",
    "description": (
        "List the directory roots Project Manager scans to discover new git projects. "
        "Use this when adding/removing repos or seeing what is monitored."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


def _dt(value: Any) -> Optional[str]:
    """Normalise a stored timestamp to ISO 8601, or ``None`` if unparseable.

    Git's ``%ci`` writes ``2026-10-21 04:00:00 +0200``; the DB may hold that raw
    or an ISO string. Pydantic rejects the former on some versions, and a bad
    row must not blow up the whole listing.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    for parser in (datetime.fromisoformat, lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")):
        try:
            return parser(text).isoformat()
        except ValueError:
            continue
    return None


def _db_row_to_project_item(row: dict) -> ProjectItem:
    """Convert a ``project_scans`` row into a ``ProjectItem``.

    Mirrors ``dashboard.plugin_api._db_row_to_project_item`` without importing
    that module (it builds a FastAPI router and mounts worktrees at import time).
    """
    return ProjectItem(
        name=row["project_name"],
        path=row["path"],
        remote_url=row.get("remote_url"),
        current_branch=row.get("current_branch", ""),
        last_commit_hash=row.get("last_commit_hash"),
        last_commit_message=row.get("last_commit_message"),
        last_commit_date=_dt(row.get("last_commit_date")),
        last_push_date=_dt(row.get("last_push_date")),
        is_dirty=bool(row.get("is_dirty", 0)),
        branch_count=row.get("branch_count", 0),
        branches=[],
        watch_path_sort_order=int(row.get("watch_path_sort_order", 0) or 0),
        ahead=int(row.get("ahead", 0) or 0),
        behind=int(row.get("behind", 0) or 0),
    )


def _annotate_docker(projects: List[ProjectItem]) -> None:
    """Stamp live compose flags, exactly as the dashboard API does."""
    try:
        stacks = list_running_stacks()
    except Exception as exc:  # docker missing, hung, whatever — flags stay False
        logger.debug("docker annotation skipped: %s", exc)
        stacks = {}
    for item in projects:
        try:
            running, ports = match_project(item.path, stacks)
            item.has_docker = running or has_docker(item.path)
            item.docker_running = running
            item.docker_ports = ports
        except OSError:
            pass


def _query_rows(search: str) -> List[dict]:
    """Every latest-scan row matching *search*, in the dashboard's sort order.

    ``list_latest_scans_paginated`` caps a page at 27 rows, so page through it
    rather than silently truncating the agent's view of the project list.
    """
    rows: List[dict] = []
    page = 1
    while page <= 1000:  # hard cap; a mismatched total must not hang the agent
        batch, total = list_latest_scans_paginated(
            q=search, page=page, page_size=_PAGE_SIZE
        )
        rows.extend(batch)
        page += 1
        if not batch or len(rows) >= total:
            return rows
    logger.warning("project-manager: stopped paging project list at page %d", page)
    return rows


def _scan_now() -> None:
    """Run one live git scan of the enabled watch paths and persist it.

    Same body as ``POST /projects/refresh`` minus the cache wipe and the
    history delete, so a stale scan table can't lose rows. Runs at most once per
    process: it is the fallback for a DB that was never refreshed, not a
    background refresher.
    """
    global _SCANNED_ONCE
    if _SCANNED_ONCE:
        return
    _SCANNED_ONCE = True
    try:
        scans = scan_all_projects(paths=get_enabled_watch_paths())
        for scan in scans:
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
    except Exception as exc:
        logger.warning("project-manager: initial scan failed: %s", exc)


def _project_rows(search: str = "") -> List[dict]:
    """Latest scan rows for *search*, scanning once if the table is still empty."""
    _ensure_schema()
    rows = _query_rows(search)
    if not rows:
        _scan_now()
        rows = _query_rows(search)
    return rows


def _lookup_path(name: str) -> Optional[str]:
    """Resolve *name* to a repo path the way ``_handle_project_lookup`` does."""
    row = get_latest_scan(project_name=name)
    return (row or {}).get("path") or None


def _git_lines(path: str, argv: List[str]) -> List[str]:
    """Run ``git -C path <argv>`` and return stdout lines; ``[]`` on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", path, *argv],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        OSError,
    ) as exc:
        logger.warning("project-manager: git %s failed in %s: %s", argv, path, exc)
        return []
    return [line for line in result.stdout.strip().splitlines() if line]


def _last_scanned_at() -> Optional[str]:
    with get_db_pool().connection() as conn:
        row = conn.execute(
            "SELECT MAX(scanned_at) AS max_scanned_at FROM project_scans"
        ).fetchone()
    return row["max_scanned_at"] if row else None


# ----------------------------------------------------------------------
# Tool handlers — in-process calls into this plugin's own scanner + DB.
# ----------------------------------------------------------------------


def list_projects(args: Dict[str, Any], **kwargs) -> str:
    """All tracked projects, optionally filtered by a name/path substring."""
    search = (args.get("search") or "").strip()
    try:
        rows = _project_rows(search)
        projects = [_db_row_to_project_item(row) for row in rows]
        _annotate_docker(projects)
        response = ProjectListResponse(
            projects=projects,
            total=len(projects),
            total_filtered=len(projects),
            page=1,
            page_size=max(len(projects), _PAGE_SIZE),
            page_count=1,
            scanned_at=_last_scanned_at(),
        )
        return json.dumps({"success": True, **response.model_dump(mode="json")})
    except Exception as exc:
        logger.exception("project-manager: list_projects failed")
        return _err(f"{type(exc).__name__}: {exc}")


def get_project(args: Dict[str, Any], **kwargs) -> str:
    """Details for one project by name."""
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        _project_rows()
        row = get_latest_scan(project_name=name)
        if not row:
            return _err(f"Project '{name}' not found")
        item = _db_row_to_project_item(row)
        _annotate_docker([item])
        return json.dumps(
            {
                "success": True,
                **item.model_dump(mode="json"),
                "scanned_at": row.get("scanned_at"),
            }
        )
    except Exception as exc:
        logger.exception("project-manager: get_project failed for %s", name)
        return _err(f"{type(exc).__name__}: {exc}")


def list_branches(args: Dict[str, Any], **kwargs) -> str:
    """Local branches for a project, each with its last-commit info."""
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        _project_rows()
        project_path = _lookup_path(name)
        if not project_path:
            return _err(f"Project '{name}' not found")

        branches = get_branches(project_path)
        current = get_current_branch(project_path)

        # One pass over refs/heads fills in the last-commit info the schema
        # promises, instead of one `git log` per branch.
        tips: Dict[str, Dict[str, str]] = {}
        for line in _git_lines(
            project_path,
            [
                "for-each-ref",
                "--format=%(refname:short)|%(objectname)|%(committerdate:iso)|%(subject)",
                "refs/heads/",
            ],
        ):
            parts = line.split("|", 3)
            if len(parts) == 4:
                tips[parts[0]] = {
                    "hash": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                }

        items = []
        for branch in branches:
            tip = tips.get(branch) or {}
            # Plain dict, not BranchItem: models.BranchItem has no last-commit
            # fields, and pydantic would silently drop them.
            items.append(
                {
                    "name": branch,
                    "is_current": branch == current,
                    "last_commit_hash": tip.get("hash"),
                    "last_commit_date": tip.get("date"),
                    "last_commit_message": tip.get("message"),
                }
            )
        return json.dumps({"success": True, "branches": items, "total": len(items)})
    except Exception as exc:
        logger.exception("project-manager: list_branches failed for %s", name)
        return _err(f"{type(exc).__name__}: {exc}")


def get_recent_commits(args: Dict[str, Any], **kwargs) -> str:
    """Most recent commits for a project, newest first."""
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        limit = max(1, min(int(args.get("limit") or 20), 500))
    except (TypeError, ValueError):
        limit = 20
    try:
        _project_rows()
        project_path = _lookup_path(name)
        if not project_path:
            return _err(f"Project '{name}' not found")

        commits = []
        for line in _git_lines(
            project_path, ["log", f"-{limit}", "--format=%H|%ci|%s|%an"]
        ):
            parts = line.split("|", 3)
            if len(parts) != 4:
                continue
            commits.append(
                CommitItem(
                    hash=parts[0].strip(),
                    message=parts[2].strip(),
                    date=_dt(parts[1]),
                    author=parts[3].strip(),
                ).model_dump(mode="json")
            )
        response = CommitListResponse(commits=commits)
        return json.dumps({"success": True, **response.model_dump(mode="json")})
    except Exception as exc:
        logger.exception("project-manager: get_recent_commits failed for %s", name)
        return _err(f"{type(exc).__name__}: {exc}")


def list_watch_paths(args: Dict[str, Any], **kwargs) -> str:
    """Enabled directory roots scanned for git projects."""
    try:
        _ensure_schema()
        items = []
        for row in db_list_watch_paths():
            items.append(
                WatchPathItem(
                    id=row["id"],
                    path=row["path"],
                    type=row["type"],
                    label=row.get("label"),
                    enabled=bool(row.get("enabled", 1)),
                    created_at=row.get("created_at"),
                ).model_dump(mode="json")
            )
        response = WatchPathListResponse(watch_paths=items, total=len(items))
        return json.dumps({"success": True, **response.model_dump(mode="json")})
    except Exception as exc:
        logger.exception("project-manager: list_watch_paths failed")
        return _err(f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------
# Plugin registration
# ----------------------------------------------------------------------

def register(ctx) -> None:
    """Wire schemas to handlers and register with the Hermes plugin context."""
    ctx.register_tool(
        name="list_projects",
        toolset="project-manager",
        schema=LIST_PROJECTS,
        handler=list_projects,
    )
    ctx.register_tool(
        name="get_project",
        toolset="project-manager",
        schema=GET_PROJECT,
        handler=get_project,
    )
    ctx.register_tool(
        name="list_branches",
        toolset="project-manager",
        schema=LIST_BRANCHES,
        handler=list_branches,
    )
    ctx.register_tool(
        name="get_recent_commits",
        toolset="project-manager",
        schema=GET_RECENT_COMMITS,
        handler=get_recent_commits,
    )
    ctx.register_tool(
        name="list_watch_paths",
        toolset="project-manager",
        schema=LIST_WATCH_PATHS,
        handler=list_watch_paths,
    )
