#!/usr/bin/env python3
"""Endpoint tests for POST /projects/{name}/git/stage + .../git/unstage.

The stage/unstage pair is a deliberate split: ``stage`` touches the index
only (``git add -- <path>``), ``unstage`` reverts the index only
(``git restore --staged -- <path>``). Neither may touch the worktree — the
last test here asserts exactly that, because a regression there silently
throws away the user's edits.

Seven scenarios, all against a real throwaway git repo (no DB): the router
is mounted on a bare FastAPI app and ``plugin_api.get_latest_scan`` is
monkeypatched, which is the same seam ``_git_project_root`` uses.

Run:
    /usr/local/lib/hermes-agent/venv/bin/python -m pytest \
        plugins/project-manager/dashboard/test_git_stage_endpoint.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_DASHBOARD = os.path.dirname(os.path.abspath(__file__))
if _DASHBOARD not in sys.path:
    sys.path.insert(0, _DASHBOARD)
for mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(mod, None)

import plugin_api  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args], check=check, capture_output=True, text=True
    )


def _staged_files(repo: str) -> list[str]:
    """Names currently in the index (``git diff --cached --name-only``)."""
    return _git("diff", "--cached", "--name-only", cwd=repo).stdout.split()


def _make_repo(tmpdir: str) -> str:
    """A git repo pinned to branch ``main`` with one committed file."""
    repo = Path(tmpdir) / "repo"
    repo.mkdir()  # ``git init`` requires the target dir to exist.
    _git("init", "-b", "main", cwd=str(repo))
    _git("config", "user.email", "test@test.com", cwd=str(repo))
    _git("config", "user.name", "Test", cwd=str(repo))
    (repo / "tracked.txt").write_text("original\n")
    _git("add", ".", cwd=str(repo))
    _git("commit", "-m", "init", cwd=str(repo))
    return str(repo)


def _client(monkeypatch, repo: str | None) -> TestClient:
    """Mount the real router; point the project lookup at *repo*.

    ``repo=None`` simulates an unknown project — ``get_latest_scan``
    returns ``None`` and the endpoint must emit the 404 contract.
    """
    app = FastAPI()
    app.include_router(plugin_api.router)

    def _fake_scan(project_name: str | None = None, **_: object) -> dict | None:
        if repo is None:
            return None
        return {"name": project_name, "path": repo}

    monkeypatch.setattr(plugin_api, "get_latest_scan", _fake_scan)
    return TestClient(app)


def _post(client: TestClient, name: str, op: str, filepath: str):
    return client.post(f"/projects/{name}/git/{op}", json={"filepath": filepath})


def test_stage_marks_file_staged(monkeypatch) -> None:
    """A modified tracked file must land in the index."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _make_repo(tmpdir)
        client = _client(monkeypatch, repo)
        Path(repo, "tracked.txt").write_text("modified\n")

        r = _post(client, "demo", "stage", "tracked.txt")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True, body
        assert body["deleted"] is False, body
        assert "tracked.txt" in _staged_files(repo), (
            f"tracked.txt should be staged; index holds {_staged_files(repo)}"
        )


def test_stage_new_untracked_file(monkeypatch) -> None:
    """A brand-new file has no HEAD entry — ``git add`` must still index it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _make_repo(tmpdir)
        client = _client(monkeypatch, repo)
        Path(repo, "fresh.txt").write_text("new\n")

        r = _post(client, "demo", "stage", "fresh.txt")
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True, r.json()
        assert "fresh.txt" in _staged_files(repo), (
            f"fresh.txt should be staged; index holds {_staged_files(repo)}"
        )


def test_unstage_reverts_index_only(monkeypatch) -> None:
    """Unstage clears the index but MUST leave the worktree edit alone.

    This is the whole point of the split endpoint: an unstage that also
    reverted the file would destroy uncommitted work.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _make_repo(tmpdir)
        client = _client(monkeypatch, repo)
        Path(repo, "tracked.txt").write_text("modified\n")
        assert _post(client, "demo", "stage", "tracked.txt").json()["success"] is True
        assert "tracked.txt" in _staged_files(repo), "precondition: file is staged"

        r = _post(client, "demo", "unstage", "tracked.txt")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True, body
        assert body["deleted"] is False, body
        assert _staged_files(repo) == [], (
            f"index should be empty after unstage; holds {_staged_files(repo)}"
        )
        # The critical half: the edit survives on disk.
        assert Path(repo, "tracked.txt").read_text() == "modified\n", (
            "unstage must not touch the worktree"
        )


def test_path_escape_rejected_for_stage(monkeypatch) -> None:
    """Traversal + absolute paths outside the repo are refused (200/success=False)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _make_repo(tmpdir)
        client = _client(monkeypatch, repo)
        outside = Path(tmpdir) / "outside.txt"
        outside.write_text("secret\n")

        for bad in ("../../etc/passwd", "/etc/passwd", str(outside)):
            r = _post(client, "demo", "stage", bad)
            assert r.status_code == 200, f"{bad!r}: expected 200, got {r.status_code}"
            body = r.json()
            assert body["success"] is False, f"{bad!r} should be refused: {body}"
            assert "escap" in body["message"].lower(), body
            assert _staged_files(repo) == [], f"{bad!r} staged something: {_staged_files(repo)}"

        # Nothing outside the repo was read, written, or indexed.
        assert outside.read_text() == "secret\n", "file outside the repo was touched"


def test_flag_like_filename_is_not_a_git_option(monkeypatch) -> None:
    """``-rf.txt`` must stage as a filename, not reach git as an option.

    The endpoint appends ``--`` before the resolved path; without it git
    would parse the leading dash and fail (or worse, act on it).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _make_repo(tmpdir)
        client = _client(monkeypatch, repo)
        Path(repo, "-rf.txt").write_text("dash name\n")

        r = _post(client, "demo", "stage", "-rf.txt")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["success"] is True, f"flag-like name rejected: {body}"
        assert "-rf.txt" in _staged_files(repo), (
            f"-rf.txt should be staged; index holds {_staged_files(repo)}"
        )


def test_unknown_project_returns_404_contract(monkeypatch) -> None:
    """Unknown project -> 404 with the {"error", "code"} contract shape."""
    client = _client(monkeypatch, None)

    r = _post(client, "nope", "stage", "tracked.txt")
    assert r.status_code == 404, r.text
    body = r.json()
    assert body["code"] == "PROJECT_NOT_FOUND", body
    assert "detail" not in body, f"FastAPI default shape leaked: {body}"
    assert body.get("error"), body


def test_stage_empty_filepath_rejected(monkeypatch) -> None:
    """Empty path -> 200 with success=False (no git invocation)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = _make_repo(tmpdir)
        client = _client(monkeypatch, repo)

        r = _post(client, "demo", "stage", "")
        assert r.status_code == 200, r.text
        assert r.json()["success"] is False, r.json()
        assert _staged_files(repo) == [], f"nothing should be staged: {_staged_files(repo)}"
