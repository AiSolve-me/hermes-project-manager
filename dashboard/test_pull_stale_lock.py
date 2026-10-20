#!/usr/bin/env python3
"""Self-check for the _cleanup_stale_lock / _git_pull split.

Run as ``python3 test_pull_stale_lock.py`` from the dashboard dir. Exits
non-zero on the first failed assertion so it doubles as a CI smoke
test. Three scenarios:

1. Stale ``.git/index.lock`` is removed by ``_cleanup_stale_lock`` so a
   read-only probe (``get_current_branch``) succeeds afterwards.
2. ``_git_pull`` on a clean repo with an existing remote returns
   ``(True, 'Already up to date.')`` — fast-forward of equal tips.
3. ``_git_pull`` on a diverged branch returns ``(False, '...diverged...')``
   and **does not** create a merge commit.

No pytest, no fixtures beyond ``tempfile`` — this matches the rest of
the ``test_*.py`` suite in this directory.
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

from git_scanner import _cleanup_stale_lock, get_current_branch
from plugin_api import _git_pull


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


def _bootstrap_repo(tmpdir: str) -> tuple[str, str]:
    """Create a bare remote + a clone pointing at it, with one commit.

    Pins the branch name to ``main`` so the divergent-branch test can
    check out the matching ref deterministically — without this the test
    would inherit whichever default the host git chose (2.47+ still
    warns about ``master``).
    """
    bare = Path(tmpdir) / "remote.git"
    work = Path(tmpdir) / "work"
    work.mkdir()  # ``git init`` requires the target dir to exist.
    _git("init", "--bare", "--initial-branch=main", str(bare), cwd=tmpdir)
    _git("init", "--initial-branch=main", cwd=str(work))
    _git("config", "user.email", "test@test.com", cwd=str(work))
    _git("config", "user.name", "Test", cwd=str(work))
    _git("remote", "add", "origin", str(bare), cwd=str(work))
    (work / "README.md").write_text("init\n")
    _git("add", ".", cwd=str(work))
    _git("commit", "-m", "init", cwd=str(work))
    _git("push", "-u", "origin", "HEAD:main", cwd=str(work))
    return str(work), str(bare)


def test_cleanup_stale_lock() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        work, _ = _bootstrap_repo(tmpdir)
        lock = Path(work) / ".git" / "index.lock"
        lock.write_text("stale")
        assert lock.exists(), "lock should exist before cleanup"

        # Cleanup returns True (file was removed) and subsequent probe
        # succeeds without manual intervention.
        assert _cleanup_stale_lock(work) is True
        assert not lock.exists(), "lock should be gone after cleanup"
        # Second call is idempotent.
        assert _cleanup_stale_lock(work) is False

        assert get_current_branch(work) in {"main", "master"}, (
            f"scan should succeed after stale-lock cleanup; got {get_current_branch(work)!r}"
        )
    print("[PASS] test_cleanup_stale_lock")


def test_git_pull_already_up_to_date() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        work, _ = _bootstrap_repo(tmpdir)
        ok, msg, added = _git_pull(work, remote="origin", branch="main")
        assert ok is True, f"expected success; got {msg!r}"
        assert "already up to date" in msg.lower(), (
            f"expected no-op message; got {msg!r}"
        )
        assert added == 0, f"expected 0 added commits; got {added}"
    print("[PASS] test_git_pull_already_up_to_date")


def test_git_pull_diverged_no_merge() -> None:
    """A local branch that diverged from a remote branch must NOT be
    merged by ``_git_pull`` — the helper should refuse and surface
    'diverged' instead of producing a merge commit.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        work, _ = _bootstrap_repo(tmpdir)
        # Step 1: push a divergent branch ``other`` to the bare remote.
        # ``other`` advances ``main`` forward; ``feat`` will branch off
        # main *before* the ``other`` tip and add its own commit, so
        # ``feat`` and ``other`` are linear siblings — neither is an
        # ancestor of the other.
        _git("checkout", "main", cwd=work)
        (Path(work) / "remote-only.txt").write_text("remote only\n")
        _git("add", ".", cwd=work)
        _git("commit", "-m", "remote only", cwd=work)
        _git("push", "origin", "HEAD:other", cwd=work)

        # Step 2: from the original ``main`` tip, create ``feat`` with a
        # local-only commit. ``feat`` and ``other`` now diverge.
        # Roll work back one commit (the ``remote-only`` we just
        # added) so feat starts from the bare ``init`` tip — neither
        # ancestor of ``other`` nor equal to it.
        _git("reset", "--hard", "HEAD~1", cwd=work)
        _git("checkout", "-b", "feat", cwd=work)
        (Path(work) / "local-only.txt").write_text("local only\n")
        _git("add", ".", cwd=work)
        _git("commit", "-m", "local only", cwd=work)

        # Step 3: pull ``other``. Fetch succeeds (ref exists), but
        # ``git merge --ff-only`` cannot fast-forward ``feat`` onto
        # ``other`` — the helper must refuse with 'diverged'.
        ok, msg, added = _git_pull(work, remote="origin", branch="other")
        assert ok is False, f"expected divergence refusal; got ok={ok} msg={msg!r}"
        assert "diverged" in msg.lower(), (
            f"expected 'diverged' in message; got {msg!r}"
        )
        assert added == 0, f"expected 0 added commits on divergence; got {added}"
        # Critical: no merge commit was created.
        merges = subprocess.run(
            ["git", "-C", work, "log", "--merges", "--oneline"],
            capture_output=True, text=True, check=True,
        )
        assert merges.stdout.strip() == "", (
            f"_git_pull must not create merge commits; got:\n{merges.stdout}"
        )
    print("[PASS] test_git_pull_diverged_no_merge")


def test_git_pull_branch_none_resolves_current() -> None:
    """When ``branch=None``, ``_git_pull`` must resolve the current branch
    and fetch *only* that branch — not the remote HEAD (default branch).

    This is the core bug: a plain ``git fetch origin`` writes
    ``FETCH_HEAD`` pointing at the remote default branch, which may
    differ from the checked-out branch. If the repo checks out a
    non-default branch, the old code merged the wrong ref or silently
    failed to update.

    We verify by creating a remote with default branch ``main``, pushing
    a commit to it, then checking out a *different* branch (``feat``)
    and pulling with ``branch=None``. The pull must fast-forward
    ``feat``'s tracking ref (which we set to ``origin/main``) and report
    the new commits.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        work, _ = _bootstrap_repo(tmpdir)
        # Push a second commit to origin/main so there's something to fetch.
        _git("checkout", "main", cwd=work)
        (Path(work) / "change.txt").write_text("remote change\n")
        _git("add", ".", cwd=work)
        _git("commit", "-m", "remote change", cwd=work)
        _git("push", "origin", "main", cwd=work)

        # Roll back locally to before the remote change and switch to
        # branch ``feat`` tracking ``origin/main``. The checked-out
        # branch (``feat``) differs from the remote default (``main``).
        _git("reset", "--hard", "HEAD~1", cwd=work)
        _git("checkout", "-b", "feat", cwd=work)
        _git("branch", "--set-upstream-to=origin/main", "feat", cwd=work)

        ok, msg, added = _git_pull(work, remote="origin", branch=None)
        assert ok is True, f"expected success; got {msg!r}"
        assert "fast-forwarded" in msg.lower(), (
            f"expected fast-forward message; got {msg!r}"
        )
        assert added == 1, f"expected 1 added commit; got {added}"
    print("[PASS] test_git_pull_branch_none_resolves_current")


def main() -> int:
    try:
        test_cleanup_stale_lock()
        test_git_pull_already_up_to_date()
        test_git_pull_diverged_no_merge()
        test_git_pull_branch_none_resolves_current()
    except AssertionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    print("[ALL PASS] pull/stale-lock self-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())