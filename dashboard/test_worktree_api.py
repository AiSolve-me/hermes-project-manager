"""TDD suite for the project-manager worktree + branch picker backend API.

AISO-57 / AISO-59 — contract locked with the Frontend (AISO-60 / b4019c18)
in the contract request thread. 5 endpoints, contract accepted.

This suite runs against the real FastAPI app created by
``project_manager_server.create_app()``. The dashboard's git repo is
monkey-patched to a temp directory so we can:

* Add a real worktree on a real branch
* Make a worktree dirty / detached / locked
* Cover the four "delete" rejection cases + the "select" dirty 409
* Cover the four "checkout" rejection cases

Run from the project-manager directory:
    python3 -m pytest dashboard/test_worktree_api.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Make dashboard + project_manager_server importable.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))  # for `project_manager_server`

# Drop stale modules so we load OUR versions, not someone else's.
for _mod in ("db", "models", "git_scanner", "cache", "plugin_api", "worktrees"):
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient  # noqa: E402

from project_manager_server import create_app  # noqa: E402
import worktrees  # noqa: E402  — module under test


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@test.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    # A first commit so HEAD points somewhere and `git worktree add` works.
    (path / "README.md").write_text("init\n")
    _git("add", ".", cwd=path)
    _git("commit", "-m", "init", cwd=path)
    # Add a `hermes` branch so the "is_protected" path is exercised.
    _git("checkout", "-b", "hermes", cwd=path)
    _git("checkout", "main", cwd=path)


@pytest.fixture()
def dashboard_repo(tmp_path, monkeypatch):
    """Create a real git repo at tmp_path/dashboard with a `hermes` branch and
    a worktree on a side branch `agent/feat`. The dashboard_repo points at
    tmp_path/dashboard and we monkey-patch the resolver to return it."""
    repo = tmp_path / "dashboard"
    _make_repo(repo)

    wt = tmp_path / "wt-feat"
    _git("worktree", "add", "-b", "agent/feat", str(wt), cwd=repo)

    # Make the module under test see this repo as the dashboard.
    monkeypatch.setattr(worktrees, "_resolve_dashboard_repo_root", lambda: repo)

    # Also reset the in-memory selected_id to a known default before each test.
    worktrees._selected_id = None  # noqa: SLF001

    return {"repo": repo, "wt": wt}


@pytest.fixture()
def client():
    # create_app runs init_db() which is idempotent; we don't care about
    # the watch_paths/project_scans tables for the worktree routes.
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Helpers for assertions
# ---------------------------------------------------------------------------


def _id_for(path: Path) -> str:
    """Mirror worktrees._make_worktree_id — keep tests in sync with impl."""
    return worktrees._make_worktree_id(path)


# ---------------------------------------------------------------------------
# GET /worktrees
# ---------------------------------------------------------------------------


def test_list_worktrees_returns_all(dashboard_repo, client):
    r = client.get("/worktrees")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "worktrees" in body
    assert "current_worktree_id" in body
    paths = {wt["path"] for wt in body["worktrees"]}
    assert str(dashboard_repo["repo"]) in paths
    assert str(dashboard_repo["wt"]) in paths

    # The first worktree (the main checkout) is "current" — its branch
    # is whatever `repo` is on at fixture time (`main`).
    current = [wt for wt in body["worktrees"] if wt["is_current"]]
    assert len(current) == 1
    assert current[0]["path"] == str(dashboard_repo["repo"])

    # IDs follow the contract: wt_<sha256(path)[:8]>.
    expected_id = _id_for(dashboard_repo["repo"])
    assert body["current_worktree_id"] == expected_id


def test_list_worktree_object_shape(dashboard_repo, client):
    r = client.get("/worktrees")
    assert r.status_code == 200
    wt = r.json()["worktrees"][0]
    for key in (
        "id",
        "path",
        "branch",
        "head",
        "is_current",
        "is_locked",
        "is_protected",
        "is_dirty",
    ):
        assert key in wt, f"missing key: {key}"


def test_list_worktrees_marks_main_branch_as_protected(dashboard_repo, client):
    r = client.get("/worktrees")
    body = r.json()
    main_wt = next(
        wt for wt in body["worktrees"] if wt["path"] == str(dashboard_repo["repo"])
    )
    assert main_wt["branch"] == "main"
    assert main_wt["is_protected"] is True
    assert main_wt["is_current"] is True


def test_list_worktrees_marks_hermes_branch_as_protected(tmp_path, monkeypatch, client):
    """If the dashboard's repo is on a `hermes` branch, that worktree is
    protected even if it's not the main branch."""
    repo = tmp_path / "dash-hermes"
    _make_repo(repo)
    # Repo is on `hermes` (see _make_repo: we switched to hermes last and
    # then back to main; recreate the scenario differently). Force the
    # checkout to hermes.
    _git("checkout", "hermes", cwd=repo)
    monkeypatch.setattr(worktrees, "_resolve_dashboard_repo_root", lambda: repo)
    worktrees._selected_id = None  # noqa: SLF001

    r = client.get("/worktrees")
    wt = next(w for w in r.json()["worktrees"] if w["path"] == str(repo))
    assert wt["branch"] == "hermes"
    assert wt["is_protected"] is True


# ---------------------------------------------------------------------------
# POST /worktrees/{id}/select
# ---------------------------------------------------------------------------


def test_select_worktree_ok(dashboard_repo, client):
    wt_id = _id_for(dashboard_repo["wt"])
    r = client.post(f"/worktrees/{wt_id}/select", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_id"] == wt_id
    assert any(w["id"] == wt_id for w in body["worktrees"])
    # Module state persisted.
    assert worktrees._selected_id == wt_id  # noqa: SLF001


def test_select_worktree_unknown_id_returns_404(dashboard_repo, client):
    r = client.post("/worktrees/wt_doesnotexist/select", json={})
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "WORKTREE_NOT_FOUND"
    assert "not found" in body["error"].lower()


def test_select_worktree_dirty_returns_409(dashboard_repo, client):
    wt_path = dashboard_repo["wt"]
    # Make the side worktree dirty.
    (wt_path / "scratch.txt").write_text("uncommitted\n")
    _git("add", "scratch.txt", cwd=wt_path)

    wt_id = _id_for(wt_path)
    r = client.post(f"/worktrees/{wt_id}/select", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "WORKTREE_DIRTY"


def test_list_worktrees_current_id_reflects_selection(dashboard_repo, client):
    """After `POST /worktrees/{id}/select`, the next `GET /worktrees`
    must report that same id as ``current_worktree_id`` — not the
    dashboard's ``is_current`` worktree. This is the contract the UI
    relies on to keep the radio button checked after a remount.
    """
    wt_id = _id_for(dashboard_repo["wt"])

    # Sanity: before select, current is the dashboard worktree.
    pre = client.get("/worktrees").json()
    assert pre["current_worktree_id"] == _id_for(dashboard_repo["repo"])

    # Select the side worktree.
    r = client.post(f"/worktrees/{wt_id}/select", json={})
    assert r.status_code == 200, r.text

    # Now GET /worktrees must report the selected id as current.
    post = client.get("/worktrees").json()
    assert post["current_worktree_id"] == wt_id, (
        f"GET /worktrees returned current_worktree_id={post['current_worktree_id']!r}; "
        f"expected the just-selected id {wt_id!r}"
    )
    # The dashboard's own is_current worktree is still flagged as such
    # in the per-row data (that's a separate, per-worktree attribute) —
    # but it must NOT be what `current_worktree_id` points at.
    dashboard_wt = next(
        wt for wt in post["worktrees"] if wt["path"] == str(dashboard_repo["repo"])
    )
    assert dashboard_wt["is_current"] is True
    assert dashboard_wt["id"] != post["current_worktree_id"]


def test_list_worktrees_stale_selected_id_falls_back_to_dashboard(
    dashboard_repo, client
):
    """If `_selected_id` points at a worktree that no longer exists
    (e.g. the user selected it, then it was deleted by another process),
    ``GET /worktrees`` must transparently fall back to the dashboard's
    ``is_current`` worktree — not return the stale id or 500.
    """
    # Hand-craft a stale selection (id that doesn't match any real worktree).
    worktrees._selected_id = "wt_doesnotexist"  # noqa: SLF001

    r = client.get("/worktrees")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_worktree_id"] == _id_for(dashboard_repo["repo"])
    # The stale id should be cleared so subsequent calls don't loop.
    assert worktrees._selected_id is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# DELETE /worktrees/{id}
# ---------------------------------------------------------------------------


def _delete(client, path: str, body: dict | None = None):
    """Starlette ``TestClient.delete()`` in modern versions has no body
    kwarg. Round-trip via ``client.request("DELETE", ...)`` which
    inherits from httpx and supports ``content=``."""
    import json as _json
    headers = {"content-type": "application/json"}
    return client.request(
        "DELETE", path, content=_json.dumps(body or {}), headers=headers
    )


def test_delete_worktree_ok(dashboard_repo, client):
    """Happy path: remove the side worktree (not current, not protected,
    not dirty, not locked)."""
    wt_id = _id_for(dashboard_repo["wt"])
    r = _delete(client, f"/worktrees/{wt_id}", {"force": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["removed_id"] == wt_id
    # The worktree is no longer in the list.
    assert not any(w["id"] == wt_id for w in body["worktrees"])
    # Filesystem gone.
    assert not dashboard_repo["wt"].exists()


def test_delete_current_worktree_rejected(dashboard_repo, client):
    wt_id = _id_for(dashboard_repo["repo"])
    r = _delete(client, f"/worktrees/{wt_id}")
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "WORKTREE_IS_CURRENT"
    assert "running the dashboard" in body["error"].lower()


def test_delete_protected_branch_rejected(dashboard_repo, client):
    """Add a worktree on `hermes` (a protected branch) and try to delete it."""
    hermes_wt = dashboard_repo["repo"].parent / "wt-hermes"
    # ``git worktree add <path> <branch>`` — for an existing branch the
    # path comes first, then the branch as the commit-ish.
    _git("worktree", "add", str(hermes_wt), "hermes", cwd=dashboard_repo["repo"])

    wt_id = _id_for(hermes_wt)
    r = _delete(client, f"/worktrees/{wt_id}")
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "WORKTREE_PROTECTED"
    assert "hermes" in body["error"]
    # The worktree is still on disk.
    assert hermes_wt.exists()


def test_delete_locked_worktree_rejected(dashboard_repo, client):
    """Lock a worktree via `git worktree lock` and verify DELETE rejects."""
    wt = dashboard_repo["wt"]
    _git("worktree", "lock", "--reason", "test fixture", str(wt), cwd=dashboard_repo["repo"])
    try:
        wt_id = _id_for(wt)
        r = _delete(client, f"/worktrees/{wt_id}")
        assert r.status_code == 400
        body = r.json()
        assert body["code"] == "WORKTREE_LOCKED"
        assert "test fixture" in body["error"]
    finally:
        _git("worktree", "unlock", str(wt), cwd=dashboard_repo["repo"])


def test_delete_dirty_worktree_rejected_without_force(dashboard_repo, client):
    wt_path = dashboard_repo["wt"]
    (wt_path / "dirty.txt").write_text("uncommitted\n")
    _git("add", "dirty.txt", cwd=wt_path)

    wt_id = _id_for(wt_path)
    r = _delete(client, f"/worktrees/{wt_id}")
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "WORKTREE_DIRTY"
    # Worktree still on disk.
    assert wt_path.exists()


def test_delete_dirty_worktree_allowed_with_force(dashboard_repo, client):
    wt_path = dashboard_repo["wt"]
    (wt_path / "dirty.txt").write_text("uncommitted\n")
    _git("add", "dirty.txt", cwd=wt_path)

    wt_id = _id_for(wt_path)
    r = _delete(client, f"/worktrees/{wt_id}", {"force": True})
    assert r.status_code == 200, r.text
    assert not wt_path.exists()


def test_delete_unknown_id_returns_404(dashboard_repo, client):
    r = _delete(client, "/worktrees/wt_doesnotexist")
    assert r.status_code == 404
    assert r.json()["code"] == "WORKTREE_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /branches
# ---------------------------------------------------------------------------


def test_list_branches_returns_local_and_remote(dashboard_repo, client):
    """The `agent/feat` local branch is present and `origin/main` shows up
    only if we set up a fake remote. We don't push here — the contract
    requires remote entries to be present, but a fresh fixture has no
    remote, so we accept that the response can be local-only.
    The CRITICAL assertions: (1) local branches are listed, (2) the
    `current_branch` field is populated, (3) `is_current` is set."""
    r = client.get("/branches")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "branches" in body
    assert "current_branch" in body

    names = {b["name"] for b in body["branches"]}
    assert "main" in names
    assert "hermes" in names
    assert "agent/feat" in names

    current = [b for b in body["branches"] if b["is_current"]]
    assert len(current) == 1
    assert current[0]["name"] == "main"
    assert body["current_branch"] == "main"


def test_list_branches_with_fake_remote(tmp_path, monkeypatch, client):
    """If `origin` exists, remote branches show up with is_remote=true and
    the origin/ prefix stripped from `name`."""
    repo = tmp_path / "dash-remote"
    _make_repo(repo)

    # Create a bare remote and push main + hermes. ``git init --bare``
    # creates the target dir, but the subprocess cwd must exist first —
    # use the tmp root.
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(tmp_path / "origin.git")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    _git("remote", "add", "origin", str(tmp_path / "origin.git"), cwd=repo)
    _git("push", "origin", "main", cwd=repo)
    _git("push", "origin", "hermes", cwd=repo)

    monkeypatch.setattr(worktrees, "_resolve_dashboard_repo_root", lambda: repo)
    worktrees._selected_id = None  # noqa: SLF001

    r = client.get("/branches")
    assert r.status_code == 200
    body = r.json()
    remote = [b for b in body["branches"] if b["is_remote"]]
    remote_names = {b["name"] for b in remote}
    # The remote origin/ prefix is stripped from `name`.
    assert "main" in remote_names
    # The `upstream` field carries the full upstream ref.
    main_remote = next(b for b in remote if b["name"] == "main")
    assert main_remote["upstream"] == "origin/main"


# ---------------------------------------------------------------------------
# POST /branches/checkout
# ---------------------------------------------------------------------------


def test_checkout_branch_ok(dashboard_repo, client):
    """Switch the dashboard worktree to a fresh local branch. We pick a
    new branch name that is NOT held by another worktree (the side
    worktree holds ``agent/feat``) so the ``BRANCH_CHECKED_OUT_ELSEWHERE``
    409 doesn't fire.
    """
    branch = "local-only"
    # Create the local branch in the dashboard's repo. ``local-only`` is
    # not checked out anywhere so the backend can switch to it.
    _git("checkout", "-b", branch, cwd=dashboard_repo["repo"])
    # Switch back to main so the test exercises the actual "switch from
    # main to local-only" path that the UI does.
    _git("checkout", "main", cwd=dashboard_repo["repo"])

    r = client.post(
        "/branches/checkout", json={"branch": branch}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["branch"] == branch
    assert body["previous_branch"] == "main"
    # The dashboard's worktree is now on the new branch.
    current_branch = _git(
        "rev-parse", "--abbrev-ref", "HEAD", cwd=dashboard_repo["repo"]
    ).stdout.strip()
    assert current_branch == branch


def test_checkout_branch_remote_only_rejected(tmp_path, monkeypatch, client):
    """A branch that exists ONLY on the remote (origin/remote-only-test)
    is rejected with BRANCH_IS_REMOTE_ONLY. We add a fake remote, push a
    branch to it, and verify the checkout fails with the right code."""
    repo = tmp_path / "dash-remote-only"
    _make_repo(repo)
    # ``git init --bare`` creates the target dir itself but the cwd
    # passed to subprocess must exist — use the test tmp root.
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(tmp_path / "origin.git")],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    _git("remote", "add", "origin", str(tmp_path / "origin.git"), cwd=repo)
    # Create a remote-only branch by branching from a fresh state and
    # pushing it without ever checking it out locally.
    _git("checkout", "--orphan", "remote-only-test", cwd=repo)
    (repo / "marker.txt").write_text("m\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "remote-only marker", cwd=repo)
    _git("push", "origin", "HEAD:remote-only-test", cwd=repo)
    # Reset the local orphan branch back to main so the local ref doesn't
    # exist either.
    _git("checkout", "main", cwd=repo)
    _git("branch", "-D", "remote-only-test", cwd=repo)

    monkeypatch.setattr(worktrees, "_resolve_dashboard_repo_root", lambda: repo)
    worktrees._selected_id = None  # noqa: SLF001

    r = client.post(
        "/branches/checkout", json={"branch": "remote-only-test"}
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "BRANCH_IS_REMOTE_ONLY"
    assert "remote-only-test" in body["error"]


def test_checkout_branch_dirty_rejected(dashboard_repo, client):
    (dashboard_repo["repo"] / "uncommitted.txt").write_text("x\n")
    _git("add", "uncommitted.txt", cwd=dashboard_repo["repo"])

    r = client.post(
        "/branches/checkout", json={"branch": "agent/feat"}
    )
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "WORKTREE_DIRTY"


def test_checkout_branch_detached_rejected(tmp_path, monkeypatch, client):
    """Detach HEAD on the dashboard worktree, then try to checkout."""
    repo = tmp_path / "dash-detached"
    _make_repo(repo)
    # Detach HEAD.
    head_sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    _git("checkout", head_sha, cwd=repo)
    monkeypatch.setattr(worktrees, "_resolve_dashboard_repo_root", lambda: repo)
    worktrees._selected_id = None  # noqa: SLF001

    r = client.post(
        "/branches/checkout", json={"branch": "agent/feat"}
    )
    # The dashboard's worktree is detached — the contract says the backend
    # must reject with WORKTREE_DETACHED 400. Note we don't have agent/feat
    # on this fresh repo, so the precondition is "detached" rather than
    # "branch exists".
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "WORKTREE_DETACHED"


def test_checkout_branch_unknown_returns_404(dashboard_repo, client):
    r = client.post(
        "/branches/checkout", json={"branch": "does-not-exist-branch"}
    )
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "BRANCH_NOT_FOUND"
    assert "does-not-exist-branch" in body["error"]


# ---------------------------------------------------------------------------
# Error shape contract — every worktree-route error is {error, code}
# ---------------------------------------------------------------------------


def test_error_shape_consistency(dashboard_repo, client):
    """All error responses from worktree routes use {error, code}."""
    cases = [
        ("POST", "/worktrees/wt_missing/select", {}, 404, "POST"),
        ("DELETE", "/worktrees/wt_missing", {}, 404, "DELETE"),
        ("POST", "/branches/checkout", {"branch": "definitely-not-real"}, 404, "POST"),
    ]
    for method, path, body, expected_status, _op in cases:
        if method == "POST":
            r = client.post(path, json=body)
        else:
            r = _delete(client, path, body)
        assert r.status_code == expected_status, f"{method} {path}: {r.text}"
        payload = r.json()
        assert "error" in payload, f"{method} {path}: no 'error' key: {payload}"
        assert "code" in payload, f"{method} {path}: no 'code' key: {payload}"
