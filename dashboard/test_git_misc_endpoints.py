#!/usr/bin/env python3
"""Backend contract tests for six previously untested project-manager routes.

Covered routes (all six had ZERO coverage before this file):

* ``POST /projects/{name}/git-push``     — push the current / requested branch
* ``POST /projects/{name}/git-stash``    — ``git stash push -u``
* ``POST /projects/refresh``             — bust the scan cache + rescan
* ``POST /projects/pull-all``            — batch pull across tracked projects
* ``POST /watch-paths/reorder``          — persist the drag order
* ``GET  /watch-paths/suggest``          — directory autocomplete

Run it standalone (exits non-zero on the first failure):

    cd plugins/project-manager/dashboard
    PYTHONPATH= python3 test_git_misc_endpoints.py

The router is mounted on a bare ``FastAPI()`` app, exactly like
``test_git_fetch_endpoint.py`` — no plugin host, no auth middleware.
Every test drives a REAL git repository under a temp dir and asserts on
the on-disk / on-the-wire result, never on the response alone.

Isolation: the SQLite file the module under test opens is redirected to a
throwaway file per test (see ``_fresh_db``), so the real
``$HERMES_HOME/plugin-data/project-manager/project-manager.db`` and the
user's real watch paths are never touched.
"""

from __future__ import annotations

import contextlib
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_DASHBOARD = os.path.dirname(os.path.abspath(__file__))
if _DASHBOARD not in sys.path:
    sys.path.insert(0, _DASHBOARD)

# Point the DB at a throwaway file BEFORE anything imports `db`, so an
# import-time read of the env var can never land on the real database.
# The override is scoped to the import below — `db` captures the path in
# `_DB_PATH` at import time, and restoring the env var keeps a sibling
# test module that re-imports `db` on the real (or its own) database.
_TMP_HOME = tempfile.mkdtemp(prefix="pm-misc-home-")
_PREV_DB_ENV = os.environ.get("PROJECT_MANAGER_DB")
os.environ["PROJECT_MANAGER_DB"] = os.path.join(_TMP_HOME, "bootstrap.db")

# Drop stale modules so we load OUR versions, not someone else's.
for _mod in ("db", "models", "git_scanner", "cache", "plugin_api", "worktrees"):
    sys.modules.pop(_mod, None)

import plugin_api  # noqa: E402
import db  # noqa: E402  (the very module instance plugin_api imported)
from cache import cache as project_cache  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

if _PREV_DB_ENV is None:
    os.environ.pop("PROJECT_MANAGER_DB", None)
else:
    os.environ["PROJECT_MANAGER_DB"] = _PREV_DB_ENV

atexit.register(shutil.rmtree, _TMP_HOME, True)


# ---------------------------------------------------------------------------
# App + fixtures
# ---------------------------------------------------------------------------


def _make_client() -> TestClient:
    """Mount the plugin router on a bare app — no DB, no plugin host."""
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


CLIENT = _make_client()


@contextlib.contextmanager
def _fresh_db():
    """Point the DB module at a brand-new SQLite file for one test.

    ``db.get_db_pool()`` reads the module globals at call time, so
    swapping ``_DB_PATH`` + clearing the pool hands the whole process
    (plugin_api included) an isolated database for the duration of the
    block. The previous pool is closed and the old path restored on exit.
    """
    tmpdir = tempfile.mkdtemp(prefix="pm-misc-db-")
    old_path, old_pool = db._DB_PATH, db._pool_instance
    db._DB_PATH = Path(tmpdir) / "project-manager.db"
    db._pool_instance = None
    try:
        db.init_db()
        yield db
    finally:
        if db._pool_instance is not None:
            db._pool_instance.close_all()
        db._DB_PATH, db._pool_instance = old_path, old_pool
        shutil.rmtree(tmpdir, ignore_errors=True)


@contextlib.contextmanager
def _workdir():
    """A temp dir that is removed when the block exits."""
    tmpdir = tempfile.mkdtemp(prefix="pm-misc-work-")
    try:
        yield Path(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _clear_project_cache() -> None:
    """Drop the cached ``/projects`` page so a listing is never stale."""
    project_cache.delete_pattern("project_manager:projects")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _make_bare(path: Path) -> Path:
    """A bare remote repository (the push/pull target)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", "--bare", str(path), cwd=path.parent)
    return path


def _make_repo(path: Path) -> Path:
    """A working repository on ``main`` with an identity configured."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@test.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    return path


def _commit(repo: Path, name: str, content: str, message: str = "change") -> None:
    (repo / name).write_text(content)
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", message, cwd=repo)


def _clone_of(bare: Path, path: Path) -> Path:
    """Clone *bare* into *path* (the clone tracks ``origin/main``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", str(bare), str(path), cwd=path.parent)
    return path


def _register_project(name: str, path: Path) -> None:
    """Record a scan row so the route can resolve *name* to *path*."""
    db.record_scan(
        project_name=name,
        path=str(path),
        current_branch="main",
        last_commit_hash=_git("rev-parse", "HEAD", cwd=path).strip(),
    )


# ---------------------------------------------------------------------------
# POST /projects/{name}/git-push
# ---------------------------------------------------------------------------


def test_git_push_success_moves_the_remote_ref() -> None:
    """A real push: the bare remote's branch ref must equal local HEAD."""
    with _fresh_db(), _workdir() as tmp:
        bare = _make_bare(tmp / "remote.git")
        clone = _make_repo(tmp / "clone")
        _commit(clone, "README.md", "seed\n", "seed")
        _git("remote", "add", "origin", str(bare), cwd=clone)
        # Seed the remote with the clone's first commit.
        _git("push", "-u", "origin", "main", cwd=clone)
        remote_after_seed = _git("rev-parse", "refs/heads/main", cwd=bare).strip()
        assert remote_after_seed == _git("rev-parse", "HEAD", cwd=clone).strip()
        _register_project("demo", clone)

        # A second change exists only locally — pushing must publish it.
        _commit(clone, "second.txt", "second\n", "second")
        local_head = _git("rev-parse", "HEAD", cwd=clone).strip()
        assert local_head != remote_after_seed

        resp = CLIENT.post(
            "/projects/demo/git-push", json={"remote": "origin", "branch": None}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True, body
        assert set(body) == {"success", "message", "stderr"}, body

        remote_head = _git("rev-parse", "refs/heads/main", cwd=bare).strip()
        assert remote_head == local_head, (
            f"push did not reach the remote: {remote_head} != {local_head}"
        )


def test_git_push_to_unreachable_remote_reports_git_failure() -> None:
    """A remote that exists but points nowhere is a 200 with success=false.

    The route reports git-level failures in the DTO — it does not raise a
    4xx — so the assertion is on the body shape AND on the remote ref
    staying where it was.
    """
    with _fresh_db(), _workdir() as tmp:
        bare = _make_bare(tmp / "remote.git")
        clone = _make_repo(tmp / "clone")
        _commit(clone, "README.md", "seed\n", "seed")
        _git("remote", "add", "origin", str(bare), cwd=clone)
        _git("push", "-u", "origin", "main", cwd=clone)
        _git("remote", "add", "broken", str(tmp / "missing.git"), cwd=clone)
        _register_project("demo", clone)
        before = _git("rev-parse", "refs/heads/main", cwd=bare).strip()

        resp = CLIENT.post(
            "/projects/demo/git-push", json={"remote": "broken", "branch": None}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False, body
        assert body["stderr"].strip(), f"expected git's error text; got {body}"
        assert set(body) == {"success", "message", "stderr"}, body
        after = _git("rev-parse", "refs/heads/main", cwd=bare).strip()
        assert after == before, "a failed push must not move the remote ref"


def test_git_push_of_unknown_branch_reports_git_failure() -> None:
    """A branch that does not exist locally is rejected by git itself."""
    with _fresh_db(), _workdir() as tmp:
        bare = _make_bare(tmp / "remote.git")
        clone = _make_repo(tmp / "clone")
        _commit(clone, "README.md", "seed\n", "seed")
        _git("remote", "add", "origin", str(bare), cwd=clone)
        _git("push", "-u", "origin", "main", cwd=clone)
        _register_project("demo", clone)
        before = _git("rev-parse", "refs/heads/main", cwd=bare).strip()

        resp = CLIENT.post(
            "/projects/demo/git-push",
            json={"remote": "origin", "branch": "no-such-branch"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is False, body
        assert body["stderr"].strip(), f"expected git's error text; got {body}"
        after = _git("rev-parse", "refs/heads/main", cwd=bare).strip()
        assert after == before, "a failed push must not move the remote ref"


def test_git_push_rejects_option_shaped_remote_before_git_runs() -> None:
    """A leading-dash remote is refused by the guard, before git sees it.

    git parses a positional ``--receive-pack=<cmd>`` as an option and
    runs the command locally, so the guard is the only thing between the
    request and local code execution. The marker file must not exist
    afterwards, and the rejection must be OUR message (empty stderr),
    not git's.
    """
    with _fresh_db(), _workdir() as tmp:
        bare = _make_bare(tmp / "remote.git")
        clone = _make_repo(tmp / "clone")
        _commit(clone, "README.md", "seed\n", "seed")
        _git("remote", "add", "origin", str(bare), cwd=clone)
        _git("push", "-u", "origin", "main", cwd=clone)
        _register_project("demo", clone)
        marker = tmp / "PUSH_PWNED"

        for payload in (
            f"--receive-pack=touch {marker}",
            f"--upload-pack=touch {marker}",
        ):
            resp = CLIENT.post("/projects/demo/git-push", json={"remote": payload})

            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["success"] is False, body
            assert "rejected" in body["message"].lower(), body
            assert body["stderr"] == "", (
                f"our reason must not pose as git stderr: {body}"
            )
            assert not marker.exists(), (
                f"INJECTION EXECUTED for {payload!r} — the guard is not holding"
            )


def test_git_push_unknown_project_returns_404() -> None:
    """An unscanned project is a 404 with FastAPI's ``detail`` envelope."""
    with _fresh_db():
        resp = CLIENT.post("/projects/nope/git-push", json={"remote": "origin"})
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["detail"].lower(), resp.text


# ---------------------------------------------------------------------------
# POST /projects/{name}/git-stash
# ---------------------------------------------------------------------------


def test_git_stash_clears_the_worktree_including_untracked_files() -> None:
    """``git stash push -u`` must take the untracked file with it.

    The response says "stashed"; the proof is on disk: the working tree
    is clean AND the untracked file is gone.
    """
    with _fresh_db(), _workdir() as tmp:
        repo = _make_repo(tmp / "repo")
        _commit(repo, "tracked.txt", "committed\n", "init")
        _register_project("demo", repo)

        (repo / "tracked.txt").write_text("modified\n")
        (repo / "untracked.txt").write_text("scratch\n")
        before = _git("status", "--porcelain", cwd=repo)
        assert "tracked.txt" in before, before
        assert "untracked.txt" in before, before

        resp = CLIENT.post("/projects/demo/git-stash")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True, body
        assert set(body) == {"success", "message", "stderr"}, body

        after = _git("status", "--porcelain", cwd=repo)
        assert not (repo / "untracked.txt").exists(), (
            "untracked file survived — the stash did not use -u"
        )
        assert after.strip() == "", f"working tree is not clean:\n{after}"
        assert (repo / "tracked.txt").read_text() == "committed\n", (
            "tracked modification was not restored to HEAD"
        )
        assert _git("stash", "list", cwd=repo).strip(), (
            "the stash entry must stay on the stack for `git stash pop`"
        )


def test_git_stash_unknown_project_returns_404() -> None:
    """An unscanned project is a 404 with FastAPI's ``detail`` envelope."""
    with _fresh_db():
        resp = CLIENT.post("/projects/nope/git-stash")
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["detail"].lower(), resp.text


# ---------------------------------------------------------------------------
# POST /projects/refresh
# ---------------------------------------------------------------------------


def test_refresh_returns_the_documented_shape_and_is_idempotent() -> None:
    """RefreshResponse carries ``refreshed``/``count``/``scanned_at``.

    Calling it twice must not duplicate the scan rows — the route wipes
    ``project_scans`` before re-inserting.
    """
    with _fresh_db(), _workdir() as tmp:
        root = tmp / "root"
        (root / "repo-a").mkdir(parents=True)
        _make_repo(root / "repo-a")

        resp = CLIENT.post(
            "/watch-paths", json={"path": str(root), "type": "scan_root"}
        )
        assert resp.status_code == 200, resp.text

        first = CLIENT.post("/projects/refresh")
        assert first.status_code == 200, first.text
        body = first.json()
        assert set(body) == {"refreshed", "count", "scanned_at"}, body
        assert body["refreshed"] is True, body
        assert body["count"] == 1, body
        assert body["scanned_at"], body

        second = CLIENT.post("/projects/refresh")
        assert second.status_code == 200, second.text
        assert second.json()["count"] == body["count"], second.text

        _clear_project_cache()
        listing = CLIENT.get("/projects")
        assert listing.status_code == 200, listing.text
        assert listing.json()["total"] == 1, listing.text


def test_refresh_after_adding_a_watch_path_makes_the_repo_visible() -> None:
    """A newly added watch path only shows up after the next refresh."""
    with _fresh_db(), _workdir() as tmp:
        root = tmp / "root"
        (root / "repo-a").mkdir(parents=True)
        _make_repo(root / "repo-a")
        solo = tmp / "repo-b"
        _make_repo(solo)

        added = CLIENT.post(
            "/watch-paths", json={"path": str(root), "type": "scan_root"}
        )
        assert added.status_code == 200, added.text

        assert CLIENT.post("/projects/refresh").json()["count"] == 1
        _clear_project_cache()
        listing = CLIENT.get("/projects").json()
        assert listing["total"] == 1, listing
        assert [p["name"] for p in listing["projects"]] == ["repo-a"], listing

        # A second watch path covering another repo.
        added = CLIENT.post(
            "/watch-paths", json={"path": str(solo), "type": "single_repo"}
        )
        assert added.status_code == 200, added.text

        refreshed = CLIENT.post("/projects/refresh")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["count"] == 2, refreshed.text

        _clear_project_cache()
        listing = CLIENT.get("/projects").json()
        assert listing["total"] == 2, listing
        assert sorted(p["name"] for p in listing["projects"]) == [
            "repo-a",
            "repo-b",
        ], listing


# ---------------------------------------------------------------------------
# POST /projects/pull-all
# ---------------------------------------------------------------------------


def test_pull_all_totals_match_reality() -> None:
    """Batch pull over a mixed fleet: pulled / up-to-date / dirty.

    The dirty project is the interesting one: it must land in
    ``dirty_skipped`` and NOT in ``failed`` — skipping is the whole point
    of the batch endpoint.
    """
    with _fresh_db(), _workdir() as tmp:
        # Two projects behind their own remote, one already up to date,
        # one dirty.
        behind = []
        for name in ("behind-a", "behind-b"):
            bare = _make_bare(tmp / f"{name}.git")
            seed = _make_repo(tmp / f"{name}-seed")
            _commit(seed, "README.md", "seed\n", "seed")
            _git("remote", "add", "origin", str(bare), cwd=seed)
            _git("push", "-u", "origin", "main", cwd=seed)
            clone = _clone_of(bare, tmp / name)
            # The remote moves on; the clone does not.
            _commit(seed, "remote.txt", "remote\n", "remote")
            _git("push", "origin", "main", cwd=seed)
            behind.append((name, clone, bare))

        uptodate_bare = _make_bare(tmp / "uptodate.git")
        uptodate_seed = _make_repo(tmp / "uptodate-seed")
        _commit(uptodate_seed, "README.md", "seed\n", "seed")
        _git("remote", "add", "origin", str(uptodate_bare), cwd=uptodate_seed)
        _git("push", "-u", "origin", "main", cwd=uptodate_seed)
        uptodate = _clone_of(uptodate_bare, tmp / "uptodate")

        dirty_bare = _make_bare(tmp / "dirty.git")
        dirty_seed = _make_repo(tmp / "dirty-seed")
        _commit(dirty_seed, "README.md", "seed\n", "seed")
        _git("remote", "add", "origin", str(dirty_bare), cwd=dirty_seed)
        _git("push", "-u", "origin", "main", cwd=dirty_seed)
        dirty = _clone_of(dirty_bare, tmp / "dirty")
        (dirty / "README.md").write_text("local edit\n")

        for name, clone, _bare in behind:
            _register_project(name, clone)
        _register_project("uptodate", uptodate)
        _register_project("dirty", dirty)

        expected_heads = {
            name: _git("rev-parse", "refs/heads/main", cwd=bare).strip()
            for name, _clone, bare in behind
        }
        dirty_head_before = _git("rev-parse", "HEAD", cwd=dirty).strip()

        resp = CLIENT.post("/projects/pull-all", json={"remote": "origin"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {
            "success",
            "total",
            "pulled",
            "already_up_to_date",
            "dirty_skipped",
            "diverged",
            "failed",
            "results",
        }, body

        assert body["total"] == 4, body
        assert body["pulled"] == 2, body
        assert body["already_up_to_date"] == 1, body
        assert body["dirty_skipped"] == 1, body
        assert body["diverged"] == 0, body
        assert body["failed"] == 0, body
        assert body["success"] is True, body

        by_project = {r["project"]: r for r in body["results"]}
        assert set(by_project) == {
            "behind-a",
            "behind-b",
            "uptodate",
            "dirty",
        }, by_project
        for name in ("behind-a", "behind-b"):
            assert by_project[name]["status"] == "pulled", by_project[name]
            assert by_project[name]["pulled"] is True, by_project[name]
            assert by_project[name]["added_commits"] == 1, by_project[name]
        assert by_project["uptodate"]["status"] == "already_up_to_date", by_project
        assert by_project["uptodate"]["pulled"] is False, by_project
        assert by_project["dirty"]["status"] == "dirty", by_project
        assert by_project["dirty"]["pulled"] is False, by_project

        # The clones really moved.
        for name, clone, _bare in behind:
            assert _git("rev-parse", "HEAD", cwd=clone).strip() == expected_heads[name], (
                f"{name} did not fast-forward to the remote tip"
            )
        # The dirty one was skipped, so it stayed where it was.
        assert _git("rev-parse", "HEAD", cwd=dirty).strip() == dirty_head_before, (
            "the dirty project must not be pulled"
        )


# ---------------------------------------------------------------------------
# POST /watch-paths/reorder
# ---------------------------------------------------------------------------


def test_reorder_persists_sort_order_and_changes_the_listing_order() -> None:
    """The drag order must land in the ``sort_order`` column.

    The API model (``WatchPathItem``) does not expose ``sort_order``, so
    the column is asserted directly on the DB and the ordering is
    asserted through ``GET /watch-paths``.
    """
    with _fresh_db(), _workdir() as tmp:
        ids = []
        for name in ("one", "two", "three"):
            target = tmp / name
            target.mkdir()
            resp = CLIENT.post(
                "/watch-paths", json={"path": str(target), "type": "scan_root"}
            )
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["id"])

        listing = CLIENT.get("/watch-paths")
        assert listing.status_code == 200, listing.text
        assert [w["id"] for w in listing.json()["watch_paths"]] == ids, listing.text

        wanted = [ids[2], ids[0], ids[1]]
        resp = CLIENT.post("/watch-paths/reorder", json={"ids": wanted})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"reordered": True, "ids": wanted}, body

        with db.get_db_pool().connection() as conn:
            rows = conn.execute(
                "SELECT id, sort_order FROM watch_paths ORDER BY sort_order ASC"
            ).fetchall()
        assert [(r["id"], r["sort_order"]) for r in rows] == [
            (ids[2], 0),
            (ids[0], 1),
            (ids[1], 2),
        ], rows

        listing = CLIENT.get("/watch-paths")
        assert [w["id"] for w in listing.json()["watch_paths"]] == wanted, listing.text


def test_reorder_rejects_a_bad_payload() -> None:
    """``ids`` must be a non-empty list of integers (booleans rejected)."""
    with _fresh_db():
        for payload in ({"ids": []}, {"ids": "nope"}, {"ids": [True]}, {"ids": [1.5]}):
            resp = CLIENT.post("/watch-paths/reorder", json=payload)
            assert resp.status_code == 400, f"{payload}: {resp.text}"
            assert "ids must be a non-empty list of integers" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /watch-paths/suggest
# ---------------------------------------------------------------------------


def test_suggest_returns_only_directories() -> None:
    """Files are not suggestions; directories are."""
    with _workdir() as tmp:
        (tmp / "alpha").mkdir()
        (tmp / "alpha.txt").write_text("not a directory\n")

        resp = CLIENT.get("/watch-paths/suggest", params={"prefix": str(tmp / "al")})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"suggestions"}, body
        assert body["suggestions"] == [str(tmp / "alpha")], body


def test_suggest_excludes_symlinks() -> None:
    """A symlinked directory is not offered as a watch path."""
    with _workdir() as tmp:
        parent = tmp / "parent"
        parent.mkdir()
        (parent / "real").mkdir()
        os.symlink(parent / "real", parent / "link")

        # A trailing separator makes the route list the children of
        # *parent* (dirname of the prefix) instead of *parent* itself.
        resp = CLIENT.get(
            "/watch-paths/suggest", params={"prefix": str(parent) + os.sep}
        )
        assert resp.status_code == 200, resp.text
        suggestions = resp.json()["suggestions"]
        assert str(parent / "real") in suggestions, suggestions
        assert str(parent / "link") not in suggestions, suggestions


def test_suggest_on_a_missing_parent_returns_an_empty_list() -> None:
    """A non-existent prefix is an empty list, not an error."""
    with _workdir() as tmp:
        resp = CLIENT.get(
            "/watch-paths/suggest", params={"prefix": str(tmp / "nope" / "ab")}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"suggestions": []}, resp.text


def test_suggest_rejects_a_prefix_shorter_than_two_characters() -> None:
    with _fresh_db():
        resp = CLIENT.get("/watch-paths/suggest", params={"prefix": "a"})
        assert resp.status_code == 400, resp.text
        assert "at least 2 characters" in resp.json()["detail"], resp.text


def test_suggest_is_not_gated_by_the_blocked_path_list() -> None:
    """The suggest route lists directories; the blocklist guards the ADD.

    ``_BLOCKED_PATHS`` (dashboard/db.py) is enforced by ``add_watch_path``,
    i.e. by ``POST /watch-paths`` — the autocomplete itself happily offers
    ``/etc``. This test pins both halves of that contract: the suggestion
    exists, and the add is still refused.
    """
    with _fresh_db():
        resp = CLIENT.get("/watch-paths/suggest", params={"prefix": "/etc"})
        assert resp.status_code == 200, resp.text
        assert "/etc" in resp.json()["suggestions"], resp.text

        added = CLIENT.post("/watch-paths", json={"path": "/etc", "type": "scan_root"})
        assert added.status_code == 400, added.text
        assert "blocked system path" in added.json()["detail"], added.text


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    failures: list[str] = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            print(f"[FAIL] {name}: {exc}")
            failures.append(name)
        except Exception as exc:  # noqa: BLE001 - report anything unexpected
            print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
        else:
            print(f"[PASS] {name}")

    shutil.rmtree(_TMP_HOME, ignore_errors=True)
    if failures:
        print(f"\nFAILED: {len(failures)} test(s): {', '.join(failures)}")
        return 1
    print("\nALL PASS — six previously untested project-manager routes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
