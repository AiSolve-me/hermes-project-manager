#!/usr/bin/env python3
"""Tests for POST /projects/{name}/git/discard.

Run as ``python3 -m pytest test_git_discard_endpoint.py`` from the
dashboard dir. Discard is destructive, so every scenario drives a real
git repo in a tmp_path and asserts on disk state afterwards:

1. An untracked file is removed from disk (``git clean -f``).
2. A tracked modification is restored to its HEAD content.
3. A file with a staged change *and* a further unstaged change comes
   back clean — the restore resets the index and the worktree.
4. A file deleted in the worktree (`` D``) is brought back from HEAD.
5. A path escaping the repo root is refused with ``success=False`` and
   200, and the outside file is left alone.
6. An unknown project returns the ``{"error", "code"}`` 404 contract,
   not FastAPI's ``{"detail": ...}``.

The router is mounted on a bare ``FastAPI()`` app — no DB, no plugin
host. The endpoint resolves the project through the module-level
``plugin_api.get_latest_scan``, so every test monkeypatches that symbol
to point at the temp repo (or to ``None`` for the 404 case).
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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import plugin_api


def _git(*args: str, cwd: Path) -> str:
    """Run ``git`` in *cwd*, return stdout."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout


def _make_repo(path: Path) -> Path:
    """Empty repo on ``main`` with an identity configured for commits."""
    path.mkdir()
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@test.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    return path


def _commit(repo: Path, name: str, content: str, message: str = "change") -> None:
    (repo / name).write_text(content)
    _git("add", name, cwd=repo)
    _git("commit", "-m", message, cwd=repo)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def _use_repo(monkeypatch: pytest.MonkeyPatch, repo: Path | None) -> None:
    """Point the endpoint's project lookup at *repo* (or nowhere)."""
    row = None if repo is None else {"name": "demo", "path": str(repo)}
    monkeypatch.setattr(
        plugin_api, "get_latest_scan", lambda project_name=None: row
    )


def _discard(client: TestClient, filepath: str) -> dict:
    response = client.post(
        "/projects/demo/git/discard", json={"filepath": filepath}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_discard_untracked_file_removes_it(tmp_path, monkeypatch, client) -> None:
    repo = _make_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "committed\n")
    stray = repo / "src" / "api.rs"
    stray.parent.mkdir()
    stray.write_text("scratch\n")
    _use_repo(monkeypatch, repo)

    body = _discard(client, "src/api.rs")

    assert body["success"] is True, body
    assert body["deleted"] is True, body
    assert not stray.exists(), "untracked file should be gone from disk"
    assert (repo / "tracked.txt").read_text() == "committed\n", "commit untouched"


def test_discard_tracked_modification_restores_head_content(
    tmp_path, monkeypatch, client
) -> None:
    repo = _make_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "committed\n")
    (repo / "tracked.txt").write_text("local edit\n")
    _use_repo(monkeypatch, repo)

    body = _discard(client, "tracked.txt")

    assert body["success"] is True, body
    assert body["deleted"] is False, body
    assert (repo / "tracked.txt").read_text() == "committed\n", "HEAD content"
    assert "tracked.txt" not in _git("status", "--porcelain", cwd=repo)


def test_discard_restores_staged_and_unstaged_together(
    tmp_path, monkeypatch, client
) -> None:
    repo = _make_repo(tmp_path / "repo")
    _commit(repo, "both.txt", "committed\n")
    (repo / "both.txt").write_text("staged\n")
    _git("add", "both.txt", cwd=repo)
    (repo / "both.txt").write_text("worktree\n")
    _use_repo(monkeypatch, repo)

    body = _discard(client, "both.txt")

    assert body["success"] is True, body
    assert body["deleted"] is False, body
    assert (repo / "both.txt").read_text() == "committed\n", "HEAD content"
    assert "both.txt" not in _git("status", "--porcelain", cwd=repo)


def test_discard_undoes_worktree_deletion(tmp_path, monkeypatch, client) -> None:
    repo = _make_repo(tmp_path / "repo")
    _commit(repo, "gone.txt", "committed\n")
    # Untracked-by-worktree: the index still has it, so status shows `` D``.
    (repo / "gone.txt").unlink()
    assert " D gone.txt" in _git("status", "--porcelain", cwd=repo)
    _use_repo(monkeypatch, repo)

    body = _discard(client, "gone.txt")

    assert body["success"] is True, body
    assert body["deleted"] is False, body
    assert (repo / "gone.txt").exists(), "tracked file should come back from HEAD"
    assert (repo / "gone.txt").read_text() == "committed\n"
    assert "gone.txt" not in _git("status", "--porcelain", cwd=repo)


def test_discard_path_escape_rejected(tmp_path, monkeypatch, client) -> None:
    repo = _make_repo(tmp_path / "repo")
    _commit(repo, "tracked.txt", "committed\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch\n")
    _use_repo(monkeypatch, repo)

    for escaping in ("../../etc/passwd", str(outside)):
        body = _discard(client, escaping)
        assert body["success"] is False, escaping
        assert "repository root" in body["message"].lower(), body

    assert outside.read_text() == "do not touch\n", "outside file must survive"
    assert (repo / "tracked.txt").read_text() == "committed\n"


def test_discard_unknown_project_returns_404_contract(
    tmp_path, monkeypatch, client
) -> None:
    _use_repo(monkeypatch, None)

    response = client.post(
        "/projects/demo/git/discard", json={"filepath": "tracked.txt"}
    )

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "PROJECT_NOT_FOUND", body
    assert "detail" not in body, body
