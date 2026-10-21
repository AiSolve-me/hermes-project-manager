#!/usr/bin/env python3
"""Self-check: a caller-supplied git remote/branch must never reach the argv.

Reproduces the hole this guards, then proves it closed. The RED case is the
parent's own probe on the UNPATCHED code:

    git fetch --upload-pack="touch /tmp/PM_PWNED" .   # as a POSITIONAL remote
    -> command executed? True

git parses a leading-dash positional as an OPTION, so `--upload-pack=<cmd>`
runs `<cmd>` locally. `git push` has the mirror form, `--receive-pack`. With
the router unauthenticated, any web page could reach it.

Run: /usr/local/lib/hermes-agent/venv/bin/python dashboard/test_git_arg_injection.py
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

from git_scanner import _valid_branch, _valid_remote  # noqa: E402

MARKER = "/tmp/PM_PWNED"


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
    )


def _repo_with_remote() -> str:
    """A repo with an 'origin' remote, so the validators have something to pass."""
    bare = tempfile.mkdtemp()
    _git("init", "-b", "main", "--bare", cwd=bare)
    work = tempfile.mkdtemp()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    with open(os.path.join(work, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("a\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    _git("remote", "add", "origin", bare, cwd=work)
    return work


def _clear_marker() -> None:
    if os.path.exists(MARKER):
        os.remove(MARKER)


def test_injection_payloads_rejected_by_both_validators() -> None:
    repo = _repo_with_remote()
    for payload in (
        f"--upload-pack=touch {MARKER}",
        f"--receive-pack=touch {MARKER}",
        "--upload-pack=touch /tmp/PM_PWNED",
        "-x",
        "",
    ):
        assert not _valid_remote(repo, payload), f"remote accepted: {payload!r}"
        assert not _valid_branch(payload), f"branch accepted: {payload!r}"


def test_legitimate_values_accepted() -> None:
    repo = _repo_with_remote()
    assert _valid_remote(repo, "origin"), "a normal remote must pass"
    assert _valid_branch("main"), "a normal branch must pass"
    assert _valid_branch("feature/per-file-git"), "a slashed branch must pass"
    # An unknown but dash-free name is deliberately passed through: git reports
    # it with its own ordinary error, and the client renders that message. The
    # security boundary is the leading dash, not remote membership.
    assert _valid_remote(repo, "nonexistent"), "unknown-but-harmless remote must pass through"
    assert not _valid_branch("has space"), "invalid ref must fail"


def test_injection_is_dead_end_to_end() -> None:
    """The real proof: through the endpoint, against a real repo."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import plugin_api

    repo = _repo_with_remote()
    original = plugin_api.get_latest_scan
    plugin_api.get_latest_scan = lambda project_name=None: {"name": "x", "path": repo}
    _clear_marker()
    try:
        app = FastAPI()
        app.include_router(plugin_api.router)
        client = TestClient(app)
        for payload in (
            {"filepath": "a.txt", "remote": f"--upload-pack=touch {MARKER}"},
            {"filepath": "a.txt", "remote": f"--receive-pack=touch {MARKER}"},
        ):
            resp = client.post("/projects/x/git/fetch", json=payload)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["success"] is False, f"payload was NOT rejected: {body}"
        # A legitimate fetch still works.
        ok = client.post("/projects/x/git/fetch", json={"remote": "origin"})
        assert ok.json()["success"] is True, ok.text
    finally:
        plugin_api.get_latest_scan = original

    assert not os.path.exists(MARKER), "INJECTION EXECUTED — the fix is not holding"
    print(f"[TEST] marker {MARKER} absent after the payload round-trip")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"[PASS] {_name}")
    print("[ALL PASS] git argument-injection self-check")
