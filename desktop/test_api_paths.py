#!/usr/bin/env python3
"""Self-check: the SHIPPED desktop plugin.js matches the real router.

The Desktop runtime loader evaluates ``desktop/plugin.js`` from a blob: URL and
refuses every import outside its four-key allowlist, so that one file IS the
plugin — there are no other source modules to keep in sync. This reads that
file (not a copy, not a split original) and asserts:

  1. every REST path it builds resolves to a route the mounted
     project-manager router actually serves;
  2. the three per-file ops (stage / unstage / discard) are wired;
  3. diff and fetch are wired;
  4. a project name is always encodeURIComponent'd;
  5. the plugin's id equals the backend namespace (``plugin.yaml`` name:) —
     ctx.rest prefixes /api/plugins/<id>, so a drifted id 404s every call;
  6. it has no relative import specifier — a blob:/data: module has no base to
     resolve one against, so the loader would refuse to load the file at all.

Run from the project-manager directory:
    /usr/local/lib/hermes-agent/venv/bin/python desktop/test_api_paths.py
"""

from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DASHBOARD = os.path.join(os.path.dirname(_HERE), "dashboard")
for _p in (_DASHBOARD, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
for _mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(_mod, None)

import plugin_api

# The one file the Desktop runtime loader evaluates.
PLUGIN_JS = os.path.join(_HERE, "plugin.js")
PLUGIN_YAML = os.path.join(os.path.dirname(_HERE), "plugin.yaml")

PER_FILE_OPS = ("stage", "unstage", "discard")


def _plugin_source() -> str:
    with open(PLUGIN_JS, encoding="utf-8") as fh:
        return fh.read()


def route_paths() -> set[str]:
    """Every '/projects...' route the router serves, in FastAPI path form."""
    return {
        r.path
        for r in plugin_api.router.routes
        if getattr(r, "path", "").startswith("/projects")
    }


def _first_arg(expr: str) -> str:
    """The first call argument of `expr`, brackets and template holes ignored."""
    depth = 0
    for i, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return expr[:i]

    return expr


def _door_args(src: str) -> list[str]:
    """Every ``door()(<path>)`` first argument, in source order."""
    out = []
    for m in re.finditer(r"door\(\)\(", src):
        i, depth = m.end(), 1
        while i < len(src) and depth:
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if not depth:
                    break
            i += 1
        out.append(_first_arg(src[m.end():i]).strip())

    return out


def _route_form(raw: str) -> str:
    """A door() path argument as a FastAPI route path.

    `${proj(name)}` is the one helper that builds a project path, so it maps to
    the router's `{name}`; anything else interpolated into the path is a value
    we cannot name, so it becomes a `{param}` placeholder.
    """
    if re.fullmatch(r"proj\([^)]*\)", raw):
        return "/projects/{name}"

    if raw.startswith("`") and raw.endswith("`"):
        raw = raw[1:-1]
    elif raw[:1] in "'\"":
        raw = raw[1:-1]

    path = raw.split("?")[0].replace("${proj(name)}", "/projects/{name}")
    path = re.sub(r"\$\{[^{}]*\}", "{param}", path)

    return path if path.startswith("/") else f"/{path}"


def called_paths() -> set[str]:
    """Every route the shipped plugin.js actually calls."""
    src = _plugin_source()
    args = _door_args(src)

    # A regex that silently stops matching would leave this empty and make the
    # route assertion vacuously true — so the count is pinned to what is there.
    # Six live doors. It was 13 before the dead ones were deleted (fetchProject,
    # fetchBranches, fetchCommits, gitPull, gitPush, gitCommit, gitStash — none
    # of them called); a seventh would mean a new REST call to declare here.
    assert len(args) == 6, (
        f"expected 6 door() calls in plugin.js, found {len(args)} — the "
        f"extraction drifted or a REST door was added without a route check: {args}"
    )

    paths = set()
    for raw in args:
        path = _route_form(raw)
        if "${action}" in raw:
            paths |= {path.replace("{param}", op) for op in PER_FILE_OPS}
        else:
            paths.add(path)

    return paths


def test_every_called_path_is_a_real_route() -> None:
    """Each path plugin.js builds must match a route the router serves.

    The paths are READ OUT of plugin.js — proj() template literals, plain
    literals, and the one `${action}` hole fileOp fills for all three ops —
    rather than restated here, so a typo in the plugin cannot pass.
    """
    routes = route_paths()
    missing = sorted(p for p in called_paths() if p not in routes)
    assert not missing, f"plugin.js calls routes the router does not serve: {missing}"


def test_per_file_ops_are_all_wired() -> None:
    """The fileOp builder must exist and the router must serve all three."""
    src = _plugin_source()
    assert re.search(r"const fileOp\s*=", src), "fileOp builder missing from plugin.js"
    assert "/git/${action}" in src, "fileOp must build the path from the action"
    routes = route_paths()
    for action in PER_FILE_OPS:
        assert f"/projects/{{name}}/git/{action}" in routes, (
            f"router has no /git/{action} route"
        )


def test_diff_and_fetch_paths_are_present() -> None:
    src = _plugin_source()
    assert "/git/diff?file=" in src, "fetchDiff must send the file query param"
    assert "/git/fetch" in src, "gitFetch path missing"
    routes = route_paths()
    assert "/projects/{name}/git/diff" in routes
    assert "/projects/{name}/git/fetch" in routes


def test_no_path_traversal_is_possible() -> None:
    """Project names go through encodeURIComponent — no raw interpolation.

    ctx.rest rejects '..' segments, but a name is also interpolated into
    plugin.js's own template literal, so double-encode it here: the guard is
    that no door() argument concatenates a name without encoding it, and that
    every project path is built through the single proj() helper.
    """
    src = _plugin_source()
    proj = re.search(r"const proj = name => `([^`]+)`", src)
    assert proj, "plugin.js must build project paths through a single helper"
    assert "encodeURIComponent(name)" in proj.group(1), (
        f"proj helper interpolates the name unencoded: {proj.group(1)!r}"
    )

    for raw in _door_args(src):
        assert "${name}" not in raw, f"a door() call interpolates the raw name: {raw!r}"
        if raw.startswith("`") and "${proj(name)}" not in raw and raw != "proj(name)":
            assert "name" not in re.sub(r"\$\{proj\(name\)\}|\$\{[^{}]*\}", "", raw), (
                f"a project path bypasses the proj() helper: {raw!r}"
            )


def test_plugin_id_is_the_backend_namespace() -> None:
    """ctx.rest prefixes /api/plugins/<id> — the id must be the router's name.

    pluginRest() builds `${base}/api/plugins/${pluginId}${suffix}` and the host
    mounts this router under `/api/plugins/<plugin.yaml name>`. An id that
    drifts from that name 404s every call in the app while every test that
    stubs the REST door still passes — which is exactly how
    `project-manager-desktop` shipped.
    """
    src = _plugin_source()
    plugin_id = re.search(r"^\s*id:\s*['\"]([^'\"]+)['\"]", src, re.M)
    assert plugin_id, "plugin.js has no id field"

    with open(PLUGIN_YAML, encoding="utf-8") as fh:
        manifest = fh.read()
    namespace = re.search(r"^name:\s*(\S+)\s*$", manifest, re.M)
    assert namespace, f"{PLUGIN_YAML} has no name: field"

    assert plugin_id.group(1) == namespace.group(1), (
        f"plugin.js id {plugin_id.group(1)!r} != plugin.yaml name "
        f"{namespace.group(1)!r}; ctx.rest would call "
        f"/api/plugins/{plugin_id.group(1)}/... and every route 404s"
    )


def test_no_relative_import_survives() -> None:
    """The loader evaluates a blob: URL with no module base, and its allowlist
    is only react + @hermes/plugin-sdk — a relative specifier fails to load the
    plugin at all, and nothing else in this repo would notice."""
    src = _plugin_source()
    dead = re.findall(r"""(?:from|import)\s*\(?\s*['"](\.[^'"]*)['"]""", src)
    assert not dead, f"relative import specifier(s) in plugin.js: {sorted(set(dead))}"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
            print(f"[TEST] {_name} passed")
    print("[TEST] all passed")
