#!/usr/bin/env python3
"""Self-check: discard on a STAGED-NEW file must not lose data silently.

A file that is staged-new (`A ` in porcelain) is in the index and in no
commit. `git restore --source=HEAD` on it removes it from disk and has
nothing to restore — the user loses the file while the dialog promised a
HEAD restore. The fix discriminates on "does HEAD have this path", not on
"is the index aware of it".

Written after the bug, so it is the RED that must have failed first:
before the fix this file was deleted from disk and `deleted` came back
true with the same "restores it from HEAD" copy the client shows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_DASHBOARD = os.path.dirname(os.path.abspath(__file__))
if _DASHBOARD not in sys.path:
    sys.path.insert(0, _DASHBOARD)
for _mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(_mod, None)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import plugin_api  # noqa: E402


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
    )


def _repo_with_staged_new_file() -> tuple[str, str]:
    """A repo whose only change is a staged-but-uncommitted new file."""
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "base.txt"), "w", encoding="utf-8") as fh:
        fh.write("base\n")
    _git("init", "-b", "main", cwd=tmp)
    _git("config", "user.email", "t@example.invalid", cwd=tmp)
    _git("config", "user.name", "t", cwd=tmp)
    _git("add", "-A", cwd=tmp)
    _git("commit", "-q", "-m", "init", cwd=tmp)

    with open(os.path.join(tmp, "secret.env"), "w", encoding="utf-8") as fh:
        fh.write("TOKEN=abc\n")
    _git("add", "secret.env", cwd=tmp)
    return tmp, "secret.env"


def test_discard_staged_new_file_does_not_delete_it() -> None:
    """The staged-new file is un-staged, not deleted.

    `git reset -- <path>` takes it out of the index and leaves the bytes on
    disk. That is the only outcome that neither loses data nor invents a
    HEAD version to restore.
    """
    repo, rel = _repo_with_staged_new_file()
    on_disk = os.path.join(repo, rel)
    assert os.path.exists(on_disk), "fixture must start with the file present"

    import plugin_api as pa

    original = pa.get_latest_scan
    pa.get_latest_scan = lambda project_name=None: {"name": "x", "path": repo}
    try:
        result = pa.git_discard_file.__wrapped__ if False else None
        from fastapi.testclient import TestClient as _TC

        app = FastAPI()
        app.include_router(pa.router)
        client = _TC(app)
        resp = client.post(
            "/projects/x/git/discard",
            json={"filepath": rel},
        )
    finally:
        pa.get_latest_scan = original

    body = resp.json()
    assert resp.status_code == 200, body
    assert body["success"] is True, body
    assert os.path.exists(on_disk), (
        "discard deleted a file that exists in no commit — unrecoverable data loss"
    )
    assert body["deleted"] is False, (
        f"deleted=True but the file is still on disk: {body}"
    )
    # And it must be back to plain untracked, not staged.
    status = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == "?? secret.env", (
        f"expected an untracked file after discard, got: {status!r}"
    )


def test_discard_untracked_file_still_deletes_it() -> None:
    """The untracked branch is unchanged: `git clean -f` still removes it."""
    repo = tempfile.mkdtemp()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    with open(os.path.join(repo, "keep.txt"), "w", encoding="utf-8") as fh:
        fh.write("keep\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)

    stray = os.path.join(repo, "stray.log")
    with open(stray, "w", encoding="utf-8") as fh:
        fh.write("noise\n")

    import plugin_api as pa
    from fastapi.testclient import TestClient as _TC

    original = pa.get_latest_scan
    pa.get_latest_scan = lambda project_name=None: {"name": "x", "path": repo}
    try:
        app = FastAPI()
        app.include_router(pa.router)
        client = _TC(app)
        resp = client.post("/projects/x/git/discard", json={"filepath": "stray.log"})
    finally:
        pa.get_latest_scan = original

    body = resp.json()
    assert body["success"] is True, body
    assert not os.path.exists(stray), "an untracked file must still be removable"
    assert body["deleted"] is True, body


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"[TEST] {_name} passed")
    print("[TEST] all passed")
