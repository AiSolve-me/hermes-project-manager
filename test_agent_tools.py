#!/usr/bin/env python3
"""Self-check for the five project-manager agent tools, in-process.

Run from the repo root:

    PYTHONPATH= /usr/local/lib/hermes-agent/venv/bin/python test_agent_tools.py

The tools used to be thin httpx clients against a standalone FastAPI server on
127.0.0.1:9120, which only exists on the machine that hand-wrote the systemd
unit — under a normal install all five returned "not reachable". They now import
this plugin's own ``dashboard/`` modules and call them directly.

Checks, in order:

1. A temp git repo with one commit, registered in a **temp** watch-path DB.
   ``db._DB_PATH`` is repointed before the first pool is built so the test never
   touches the real ``dashboard/project-manager.db``.
2. Each of the five tools returns a JSON string with ``success: true`` and the
   fields its schema in ``__init__.py`` promises.
3. No network: ``httpx`` must not even be in ``sys.modules`` after importing the
   plugin, and every tool call runs with ``socket.socket`` poisoned so any
   attempt to open a connection raises.

No pytest, no fixtures — same style as ``dashboard/test_*.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.abspath(__file__))
_ROOT_PATH = Path(_ROOT)
_DASHBOARD = os.path.join(_ROOT, "dashboard")

# Same sys.modules pop loop the plugin itself uses: drop any other plugin's
# db/models/git_scanner/cache so this test can never bind to a foreign module.
for _mod in ("db", "models", "git_scanner", "cache", "docker_status", "plugin_api"):
    sys.modules.pop(_mod, None)

if _DASHBOARD not in sys.path:
    sys.path.insert(0, _DASHBOARD)

# --- 0. The plugin must not drag in an HTTP client ------------------------
_tmpdir = tempfile.mkdtemp(prefix="pm-agent-tools-")
_TMP_DB = Path(_tmpdir) / "agent-tools-test.db"

_spec = importlib.util.spec_from_file_location(
    "pm_plugin_under_test", os.path.join(_ROOT, "__init__.py")
)
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

# __init__.py pops "db" from sys.modules before importing it, so the plugin's db
# is a *different* module object than a plain `import db` would give. Repoint
# the plugin's own copy — that is the one whose _DB_PATH get_db_pool() reads.
dbmod = sys.modules["db"]
dbmod._DB_PATH = _TMP_DB
dbmod._pool_instance = None  # a pool would otherwise bind the real DB path

assert "httpx" not in sys.modules, "importing the plugin pulled in httpx"
assert not hasattr(plugin, "_BASE"), "the old HTTP base URL constant is still there"
src = (_ROOT_PATH / "__init__.py").read_text()
assert "httpx" not in src, "httpx is still referenced in __init__.py"
assert "hermes-plugins-api" not in src, (
    "the 'start hermes-plugins-api' hint is still in __init__.py; no such "
    "shipped unit exists"
)


# --- helpers --------------------------------------------------------------

def _git(*args: str, cwd: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _call(fn, **args):
    """Call a tool with sockets poisoned; return the parsed JSON payload."""
    real_socket = socket.socket

    def _poisoned(*a, **kw):
        raise AssertionError(
            f"{fn.__name__} opened a socket — the tools must be in-process"
        )

    socket.socket = _poisoned
    try:
        raw = fn(args)
    finally:
        socket.socket = real_socket

    assert isinstance(raw, str), f"{fn.__name__} returned {type(raw)}, not str"
    return json.loads(raw)


def _expect_ok(payload: dict, tool: str) -> dict:
    assert payload.get("success") is True, f"{tool} failed: {payload}"
    assert "error" not in payload, f"{tool} returned both success and error: {payload}"
    return payload


# --- 1. temp repo + temp DB ----------------------------------------------

repo = Path(_tmpdir) / "demo-repo"
repo.mkdir()
_git("init", "--initial-branch=main", cwd=repo)
_git("config", "user.email", "test@test.com", cwd=repo)
_git("config", "user.name", "Test", cwd=repo)
(repo / "README.md").write_text("hello\n")
_git("add", ".", cwd=repo)
_git("commit", "-m", "initial commit", cwd=repo)
expected_hash = _git("rev-parse", "HEAD", cwd=repo)

dbmod.init_db()
dbmod.add_watch_path(path=str(repo), type="single_repo", label="agent-tools-test")
wp_rows = dbmod.get_enabled_watch_paths()
assert len(wp_rows) == 1 and wp_rows[0]["path"] == str(repo), wp_rows

# --- 2. the five tools ---------------------------------------------------

# list_projects — no HTTP, no server, fresh process, empty scan table.
# The tool's own _scan_now() fallback must populate it from the watch path.
payload = _expect_ok(_call(plugin.list_projects), "list_projects")
assert payload["total"] == 1, payload
proj = payload["projects"][0]
for field in (
    "name",
    "path",
    "remote_url",
    "current_branch",
    "last_commit_hash",
    "last_commit_message",
    "last_commit_date",
    "last_push_date",
    "is_dirty",
    "branch_count",
    "branches",
    "watch_path_sort_order",
    "ahead",
    "behind",
):
    assert field in proj, f"list_projects missing schema field {field!r}: {proj}"
assert proj["name"] == "demo-repo", proj
assert proj["path"] == str(repo.resolve()), proj
assert proj["current_branch"] == "main", proj
assert proj["last_commit_hash"] == expected_hash, proj
assert proj["last_commit_message"] == "initial commit", proj
assert payload["scanned_at"], payload

# the optional search filter still filters
searched = _expect_ok(
    _call(plugin.list_projects, search="demo"), "list_projects(search)"
)
assert searched["total"] == 1, searched
searched = _expect_ok(
    _call(plugin.list_projects, search="no-such-project"), "list_projects(search)"
)
assert searched["total"] == 0 and searched["projects"] == [], searched

# get_project
payload = _expect_ok(_call(plugin.get_project, name="demo-repo"), "get_project")
for field in ("name", "path", "current_branch", "last_commit_hash", "scanned_at"):
    assert field in payload, f"get_project missing schema field {field!r}: {payload}"
assert payload["name"] == "demo-repo", payload
assert payload["last_commit_hash"] == expected_hash, payload

# get_project — unknown name and missing arg both return JSON errors
missing = _call(plugin.get_project, name="nope")
assert missing["success"] is False and "not found" in missing["error"], missing
blank = _call(plugin.get_project, name="")
assert blank["success"] is False and blank["error"] == "name is required", blank

# list_branches
payload = _expect_ok(
    _call(plugin.list_branches, name="demo-repo"), "list_branches"
)
assert payload["total"] == 1, payload
branch = payload["branches"][0]
for field in ("name", "is_current", "last_commit_hash", "last_commit_message"):
    assert field in branch, f"list_branches missing schema field {field!r}: {branch}"
assert branch["name"] == "main", branch
assert branch["is_current"] is True, branch
assert branch["last_commit_hash"] == expected_hash, branch
assert branch["last_commit_message"] == "initial commit", branch

# get_recent_commits
payload = _expect_ok(
    _call(plugin.get_recent_commits, name="demo-repo"), "get_recent_commits"
)
assert len(payload["commits"]) == 1, payload
commit = payload["commits"][0]
for field in ("hash", "message", "date", "author"):
    assert field in commit, f"get_recent_commits missing schema field {field!r}: {commit}"
assert commit["hash"] == expected_hash, commit
assert commit["message"] == "initial commit", commit
assert commit["author"] == "Test", commit

# limit is honoured, and a junk limit falls back to the default instead of raising
assert len(_expect_ok(
    _call(plugin.get_recent_commits, name="demo-repo", limit=1), "limit"
)["commits"]) == 1
assert len(_expect_ok(
    _call(plugin.get_recent_commits, name="demo-repo", limit="garbage"), "junk limit"
)["commits"]) == 1

# list_watch_paths
payload = _expect_ok(_call(plugin.list_watch_paths), "list_watch_paths")
assert payload["total"] == 1, payload
wp = payload["watch_paths"][0]
for field in ("id", "path", "type", "label", "enabled", "created_at"):
    assert field in wp, f"list_watch_paths missing schema field {field!r}: {wp}"
assert wp["path"] == str(repo), wp
assert wp["type"] == "single_repo" and wp["enabled"] is True, wp

# --- 3. registration still matches plugin.yaml ---------------------------


def yaml_tools() -> list:
    """Read provides_tools out of plugin.yaml without a yaml dependency."""
    import re

    text = (_ROOT_PATH / "plugin.yaml").read_text()
    block = re.search(r"provides_tools:\n((?:\s+-\s+\S+\n)+)", text)
    assert block, "provides_tools block not found in plugin.yaml"
    return [{"name": line.strip("- \n")} for line in block.group(1).splitlines()]


registered: list = []


class _Ctx:
    def register_tool(self, name, toolset, schema, handler):
        registered.append(name)


plugin.register(_Ctx())
declared_names = [t["name"] for t in yaml_tools()]
assert registered == declared_names, (registered, declared_names)
assert registered == [
    "list_projects",
    "get_project",
    "list_branches",
    "get_recent_commits",
    "list_watch_paths",
], registered

# the real DB was never opened
assert dbmod._DB_PATH == Path(_tmpdir) / "agent-tools-test.db"
assert not Path(_DASHBOARD, "agent-tools-test.db").exists()

print("PASS: all five tools work in-process with no HTTP server and no network")
print(f"      temp DB: {dbmod._DB_PATH}")
print(f"      registered tools: {registered}")
