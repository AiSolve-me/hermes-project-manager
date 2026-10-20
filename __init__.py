"""Project Manager — Hermes plugin bridge to the FastAPI plugin server.

This plugin is a thin wrapper over the FastAPI endpoints exposed by
``hermes-plugins-server.py`` at ``http://127.0.0.1:9120/api/plugins/project-manager/``.

When the FastAPI server is running, each tool here issues an HTTP request to the
corresponding endpoint and returns the JSON response to the agent. When the
server is not reachable, the tools return a structured error so the agent can
retry or report.

All handlers return a JSON string. Success and errors alike.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

_BASE = "http://127.0.0.1:9120/api/plugins/project-manager"
_TIMEOUT = 15.0


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
# Tool handlers — make the HTTP call to the FastAPI plugin server.
# ----------------------------------------------------------------------

def _err(msg: str) -> str:
    return json.dumps({"error": msg, "plugin": "project-manager"})


def list_projects(args: Dict[str, Any], **kwargs) -> str:
    search = (args.get("search") or "").strip()
    url = f"{_BASE}/projects"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(url, params={"search": search} if search else None)
            r.raise_for_status()
            return r.text
    except httpx.ConnectError:
        return _err("hermes-plugins-api server not reachable on 127.0.0.1:9120. "
                    "Run: systemctl --user start hermes-plugins-api")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def get_project(args: Dict[str, Any], **kwargs) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(f"{_BASE}/projects/{name}")
            r.raise_for_status()
            return r.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _err(f"Project '{name}' not found")
        return _err(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.ConnectError:
        return _err("hermes-plugins-api server not reachable on 127.0.0.1:9120")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def list_branches(args: Dict[str, Any], **kwargs) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(f"{_BASE}/projects/{name}/branches")
            r.raise_for_status()
            return r.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _err(f"Project '{name}' not found")
        return _err(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.ConnectError:
        return _err("hermes-plugins-api server not reachable on 127.0.0.1:9120")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def get_recent_commits(args: Dict[str, Any], **kwargs) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return _err("name is required")
    limit = int(args.get("limit") or 20)
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(f"{_BASE}/projects/{name}/commits", params={"limit": limit})
            r.raise_for_status()
            return r.text
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return _err(f"Project '{name}' not found")
        return _err(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.ConnectError:
        return _err("hermes-plugins-api server not reachable on 127.0.0.1:9120")
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


def list_watch_paths(args: Dict[str, Any], **kwargs) -> str:
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(f"{_BASE}/watch-paths")
            r.raise_for_status()
            return r.text
    except httpx.ConnectError:
        return _err("hermes-plugins-api server not reachable on 127.0.0.1:9120")
    except Exception as exc:
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
