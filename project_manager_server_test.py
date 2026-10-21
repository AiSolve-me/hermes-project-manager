#!/usr/bin/env python3
"""Self-check: the standalone server must not be an unauthenticated open door.

The server mounts the whole router, writes included — git commit, push,
discard (`git clean -f`), worktree removal. On 127.0.0.1 with
`allow_origins=["*"]` and `allow_credentials=True`, any web page the user
opened could drive those from JavaScript. This asserts the hole is closed.

Run: /usr/local/lib/hermes-agent/venv/bin/python project_manager_server_test.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
for _mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(_mod, None)

from fastapi.testclient import TestClient  # noqa: E402

import project_manager_server as server  # noqa: E402

# A harmless read; the route list is irrelevant, the auth gate is the point.
_SAFE_GET = "/projects?page=1&page_size=9"


def test_request_without_a_token_is_rejected() -> None:
    client = TestClient(server.create_app())
    assert client.get(_SAFE_GET).status_code == 401, "an anonymous read got through"


def test_request_with_a_wrong_token_is_rejected() -> None:
    client = TestClient(server.create_app())
    resp = client.get(_SAFE_GET, headers={"Authorization": "Bearer not-the-token"})
    assert resp.status_code == 401, "a wrong token got through"
    assert resp.json()["code"] == "UNAUTHORIZED", resp.text


def test_token_in_a_query_string_is_not_accepted() -> None:
    """A token in a URL leaks into logs and Referer headers — refuse it."""
    client = TestClient(server.create_app())
    resp = client.get(f"{_SAFE_GET}&token={server._API_TOKEN}")
    assert resp.status_code == 401, "a query-string token was accepted"


def test_valid_bearer_token_is_accepted() -> None:
    client = TestClient(server.create_app())
    resp = client.get(_SAFE_GET, headers={"Authorization": f"Bearer {server._API_TOKEN}"})
    assert resp.status_code == 200, resp.text


def test_valid_alternate_header_is_accepted() -> None:
    client = TestClient(server.create_app())
    resp = client.get(_SAFE_GET, headers={"X-Project-Manager-Token": server._API_TOKEN})
    assert resp.status_code == 200, resp.text


def test_cors_allows_no_origin_by_default() -> None:
    """No wildcard: the default allowlist is empty, so no browser origin is let in."""
    app = server.create_app()
    allowed = [
        m.kwargs.get("allow_origins")
        for m in app.user_middleware
        if m.cls.__name__ == "CORSMiddleware"
    ]
    assert allowed, "no CORS middleware found"
    assert allowed == [[]], f"default CORS allowlist is not empty: {allowed}"


def test_wildcard_cors_is_gone_from_the_source() -> None:
    with open(os.path.join(_HERE, "project_manager_server.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert 'allow_origins=["*"]' not in src, "the wildcard origin is still there"
    assert "allow_credentials=True" not in src, "credentials are still allowed"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"[PASS] {_name}")
    print("[ALL PASS] standalone-server auth self-check")
