#!/usr/bin/env python3
"""Contract tests for ``GET /projects/{name}/git/diff``.

The router is mounted on a bare ``FastAPI()`` app — no DB, no server.
``plugin_api.get_latest_scan`` is monkey-patched so the endpoint resolves
the project to a throwaway git repo built per test.

Scenarios: worktree diff, staged-vs-worktree separation, clean file,
binary file, truncation at ``MAX_DIFF_CHARS``, path-escape rejection,
and the 404 error contract for an unknown project.
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


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> str:
    """Create a repo on branch ``main`` with one committed ``a.txt``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=str(repo))
    _git("config", "user.email", "test@test.com", cwd=str(repo))
    _git("config", "user.name", "Test", cwd=str(repo))
    (repo / "a.txt").write_text("old\n")
    _git("add", ".", cwd=str(repo))
    _git("commit", "-m", "init", cwd=str(repo))
    return str(repo)


def _client(monkeypatch, repo: str | None) -> TestClient:
    """Mount the router with ``get_latest_scan`` pointed at *repo*."""
    row = {"name": "demo", "path": repo} if repo else None
    monkeypatch.setattr(
        plugin_api, "get_latest_scan", lambda project_name=None: row
    )
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def _diff(client: TestClient, file: str = "a.txt") -> dict:
    resp = client.get("/projects/demo/git/diff", params={"file": file})
    assert resp.status_code == 200, f"expected 200; got {resp.status_code}"
    return resp.json()


def test_diff_returns_worktree_changes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (Path(repo) / "a.txt").write_text("new\n")
    body = _diff(_client(monkeypatch, repo))

    assert body["success"] is True, body
    assert body["binary"] is False, body
    assert body["truncated"] is False, body
    assert body["filepath"] == "a.txt", body
    assert "-old" in body["worktree_diff"], body
    assert "+new" in body["worktree_diff"], body


def test_diff_returns_staged_changes_separately(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    # First change is staged, second one stays in the worktree.
    (Path(repo) / "a.txt").write_text("staged\n")
    _git("add", "a.txt", cwd=repo)
    (Path(repo) / "a.txt").write_text("staged\nunstaged\n")
    body = _diff(_client(monkeypatch, repo))

    assert body["success"] is True, body
    assert "-old" in body["staged_diff"], body
    assert "+staged" in body["staged_diff"], body
    assert "unstaged" not in body["staged_diff"], body
    assert "-old" not in body["worktree_diff"], body
    assert "+staged" not in body["worktree_diff"], body
    assert "+unstaged" in body["worktree_diff"], body


def test_diff_clean_file_is_empty(tmp_path, monkeypatch):
    body = _diff(_client(monkeypatch, _make_repo(tmp_path)))

    assert body["success"] is True, body
    assert body["worktree_diff"] == "", body
    assert body["staged_diff"] == "", body
    assert body["binary"] is False, body
    assert body["truncated"] is False, body


def test_diff_binary_file_flag(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    blob = Path(repo) / "blob.bin"
    blob.write_bytes(bytes(range(256)))  # committed first -> tracked as binary
    _git("add", "blob.bin", cwd=repo)
    _git("commit", "-m", "add blob", cwd=repo)
    # Flip one byte — ``bytes(range(256))[-1]`` is already 0xff.
    blob.write_bytes(b"\x00\x01" + bytes(range(256))[2:-1])

    body = _diff(_client(monkeypatch, repo), file="blob.bin")
    assert body["success"] is True, body
    assert body["binary"] is True, body
    assert body["worktree_diff"] == "", body
    assert body["staged_diff"] == "", body


def test_diff_truncated_flag(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    a = Path(repo) / "a.txt"
    a.write_text("staged\n" + "s\n" * 200)
    _git("add", "a.txt", cwd=repo)
    a.write_text("staged\n" + "s\n" * 200 + "unstaged\n" + "u\n" * 200)

    monkeypatch.setattr(plugin_api, "MAX_DIFF_CHARS", 500)
    body = _diff(_client(monkeypatch, repo))

    assert body["success"] is True, body
    assert body["truncated"] is True, body
    assert len(body["worktree_diff"]) == 500, len(body["worktree_diff"])
    assert len(body["staged_diff"]) == 500, len(body["staged_diff"])


def test_diff_path_escape_rejected(tmp_path, monkeypatch):
    client = _client(monkeypatch, _make_repo(tmp_path))
    resp = client.get(
        "/projects/demo/git/diff", params={"file": "../../etc/passwd"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is False, body
    assert body["worktree_diff"] == "", body
    assert body["staged_diff"] == "", body
    assert "repository root" in body["message"], body


def test_diff_unknown_project_returns_404_contract(monkeypatch):
    resp = _client(monkeypatch, None).get(
        "/projects/demo/git/diff", params={"file": "a.txt"}
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "PROJECT_NOT_FOUND", body
    assert "detail" not in body, body
