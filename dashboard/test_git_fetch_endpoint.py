#!/usr/bin/env python3
"""Tests for POST /projects/{name}/git/fetch.

Four scenarios, all mounted against a throwaway ``FastAPI`` app so no
DB is needed — the endpoint resolves the project through the
module-level ``plugin_api.get_latest_scan``, which each test
monkeypatches:

1. A real ``git fetch`` moves ``origin/main`` forward in the clone.
2. The request body is optional — ``{}`` and no body at all both
   default to ``origin``.
3. An unknown remote is a 200 with ``success=False`` and git's stderr
   (not a 4xx) — git failure is not an HTTP failure.
4. An unknown project returns the contract 404 body
   ``{"error": ..., "code": "PROJECT_NOT_FOUND"}``, not FastAPI's
   ``{"detail": ...}``.

Branch names are pinned to ``main`` throughout so the tests are
deterministic across git versions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_DASHBOARD = os.path.dirname(os.path.abspath(__file__))
if _DASHBOARD not in sys.path:
    sys.path.insert(0, _DASHBOARD)
for mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(mod, None)

from fastapi import FastAPI
from fastapi.testclient import TestClient

import plugin_api


def _git(*args: str, cwd: str) -> str:
    """Run ``git -C <cwd> <args>`` and return stdout."""
    proc = subprocess.run(
        ["git", "-C", cwd, *args], check=True, capture_output=True, text=True
    )
    return proc.stdout


def _commit(work: Path, name: str, content: str, message: str) -> None:
    (work / name).write_text(content)
    _git("add", ".", cwd=str(work))
    _git("commit", "-m", message, cwd=str(work))


def _init_repo(work: Path, remote: str | None = None) -> None:
    """Init a repo on ``main`` with an identity and one commit.

    Pins the branch to ``main`` explicitly — otherwise the test would
    inherit whatever default the host git picked (``master`` on older
    git) and ``origin/main`` would never exist.
    """
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=str(work))
    _git("config", "user.email", "test@test.com", cwd=str(work))
    _git("config", "user.name", "Test", cwd=str(work))
    if remote:
        _git("remote", "add", "origin", remote, cwd=str(work))
    _commit(work, "README.md", "init\n", "init")


def _make_client() -> TestClient:
    """Mount the plugin router on a bare app — no DB, no lifespan."""
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def _patch_project(monkeypatch, path: str) -> None:
    monkeypatch.setattr(
        plugin_api,
        "get_latest_scan",
        lambda project_name=None: {"name": project_name, "path": path},
    )


def test_fetch_updates_remote_refs(tmp_path, monkeypatch) -> None:
    """A real fetch must advance the clone's ``origin/main`` to the
    remote tip — the endpoint's whole reason for existing.
    """
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare), cwd=str(tmp_path))

    clone = tmp_path / "clone"
    _init_repo(clone, remote=str(bare))
    _git("push", "-u", "origin", "HEAD:main", cwd=str(clone))
    # Fetch once so ``origin/main`` exists locally at the first commit.
    _git("fetch", "origin", cwd=str(clone))
    old_sha = _git("rev-parse", "origin/main", cwd=str(clone)).strip()

    # Advance the remote from a *different* clone so the first clone's
    # ``origin/main`` is behind.
    other = tmp_path / "other"
    _git("clone", "-b", "main", str(bare), str(other), cwd=str(tmp_path))
    _git("config", "user.email", "test@test.com", cwd=str(other))
    _git("config", "user.name", "Test", cwd=str(other))
    _commit(other, "second.txt", "second\n", "second")
    _git("push", "origin", "main", cwd=str(other))
    new_sha = _git("rev-parse", "main", cwd=str(other)).strip()

    before = _git("rev-parse", "origin/main", cwd=str(clone)).strip()
    assert before == old_sha, "clone should start behind the remote"
    assert before != new_sha, "fixture is broken: clone already has the tip"

    _patch_project(monkeypatch, str(clone))
    resp = _make_client().post("/projects/demo/git/fetch", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, body
    assert body["stderr"] == "", body

    after = _git("rev-parse", "origin/main", cwd=str(clone)).strip()
    assert after == new_sha, f"origin/main did not advance: {after} != {new_sha}"


def test_fetch_with_empty_body_defaults_to_origin(tmp_path, monkeypatch) -> None:
    """The body is optional: ``{}`` and no body at all must both work."""
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "--initial-branch=main", str(bare), cwd=str(tmp_path))
    clone = tmp_path / "clone"
    _init_repo(clone, remote=str(bare))
    _git("push", "-u", "origin", "HEAD:main", cwd=str(clone))

    _patch_project(monkeypatch, str(clone))
    client = _make_client()

    empty = client.post("/projects/demo/git/fetch", json={})
    assert empty.status_code == 200, empty.text
    assert empty.json()["success"] is True, empty.json()

    nothing = client.post("/projects/demo/git/fetch")
    assert nothing.status_code == 200, nothing.text
    body = nothing.json()
    assert body["success"] is True, body
    assert "origin" in body["message"], f"expected origin default; got {body}"


def test_fetch_unknown_remote_reports_stderr(tmp_path, monkeypatch) -> None:
    """A bad remote is a 200 with ``success=False`` carrying git's error.

    Deliberately NOT asserting a 4xx — the endpoint reports git-level
    failures in the DTO, and the mobile client renders that message.
    """
    clone = tmp_path / "clone"
    _init_repo(clone, remote=str(tmp_path / "remote.git"))
    _git("remote", "add", "upstream", str(tmp_path / "remote.git"), cwd=str(clone))

    _patch_project(monkeypatch, str(clone))
    resp = _make_client().post(
        "/projects/demo/git/fetch", json={"remote": "definitely-not-a-remote"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is False, body
    assert body["stderr"].strip(), f"expected git error text on stderr; got {body}"
    assert body["message"].strip(), f"expected a human-readable message; got {body}"


def test_fetch_unknown_project_returns_404_contract(monkeypatch) -> None:
    """Unknown project → 404 with the contract body, not FastAPI's
    ``{"detail": ...}``.
    """
    monkeypatch.setattr(plugin_api, "get_latest_scan", lambda project_name=None: None)

    resp = _make_client().post("/projects/nope/git/fetch", json={})

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "PROJECT_NOT_FOUND", body
    assert body["error"].strip(), f"expected an error message; got {body}"
    assert "detail" not in body, f"must use the contract body, not FastAPI's: {body}"
