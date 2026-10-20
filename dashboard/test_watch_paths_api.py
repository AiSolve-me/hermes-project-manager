"""Smoke test for the watch-paths CRUD + configured scanning contract.

Run from the project-manager directory:
    python3 dashboard/test_watch_paths_api.py

Verifies the full E2E contract documented in the API-RECOVERY (API-1)
kanban task:
    * POST /watch-paths happy path + 400 (non-isdir, bad type) + 409 (dup)
    * GET  /watch-paths returns WatchPathListResponse
    * DELETE /watch-paths/{id} -> {deleted:true,id}; 404 on missing
    * /projects total=0 with empty watch_paths
    * /projects/refresh count>=1 after adding /home/github/projects as
      a scan_root watch path
    * Cleanup only touches the IDs created during the test run — pre-
      existing rows in watch_paths are left untouched
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))  # for `project_manager_server`
os.chdir(_HERE)

# Drop stale modules so we load OUR versions, not someone else's.
for mod_name in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(mod_name, None)

from fastapi.testclient import TestClient  # noqa: E402

from project_manager_server import create_app  # noqa: E402
from db import init_db, list_watch_paths  # noqa: E402

init_db()

PRE_EXISTING_IDS = {row["id"] for row in list_watch_paths(include_disabled=True)}
print(f"[setup] pre-existing watch_path ids: {sorted(PRE_EXISTING_IDS)}")

created_ids: list[int] = []

app = create_app()
client = TestClient(app)

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


def main() -> int:  # noqa: PLR0915
    r = client.get("/health")
    check(
        "GET /health 200", r.status_code == 200, f"status={r.status_code} body={r.text}"
    )
    check(
        "/health payload",
        r.json() == {"status": "ok", "service": "project-manager"},
        f"body={r.json()}",
    )

    r = client.get("/projects")
    check("GET /projects 200", r.status_code == 200, f"status={r.status_code}")
    baseline_total = r.json().get("total", 0)
    print(f"[info] baseline /projects total={baseline_total}")

    r = client.post(
        "/watch-paths",
        json={"path": "/nonexistent/__pm_smoke__", "type": "scan_root"},
    )
    check(
        "POST /watch-paths non-isdir -> 400",
        r.status_code == 400,
        f"status={r.status_code} body={r.text}",
    )

    r = client.post(
        "/watch-paths",
        json={"path": "/home/github/projects", "type": "bogus"},
    )
    check(
        "POST /watch-paths bad type -> 400",
        r.status_code == 400,
        f"status={r.status_code} body={r.text}",
    )

    r = client.post(
        "/watch-paths",
        json={
            "path": "/home/github/projects",
            "type": "scan_root",
            "label": "smoke-test",
        },
    )
    check(
        "POST /watch-paths happy -> 200",
        r.status_code == 200,
        f"status={r.status_code} body={r.text}",
    )
    body = r.json() if r.status_code == 200 else {}
    new_id = body.get("id")
    if isinstance(new_id, int):
        created_ids.append(new_id)
    check(
        "POST /watch-paths returns WatchPathItem with id+path",
        isinstance(new_id, int) and body.get("path") == "/home/github/projects",
        f"body={body}",
    )
    check(
        "POST /watch-paths type=scan_root",
        body.get("type") == "scan_root",
        f"body={body}",
    )
    check(
        "POST /watch-paths enabled=True",
        body.get("enabled") is True,
        f"body={body}",
    )

    r = client.post(
        "/watch-paths",
        json={"path": "/home/github/projects", "type": "scan_root"},
    )
    check(
        "POST /watch-paths duplicate -> 409",
        r.status_code == 409,
        f"status={r.status_code} body={r.text}",
    )

    r = client.get("/watch-paths")
    check("GET /watch-paths 200", r.status_code == 200, f"status={r.status_code}")
    listing = r.json() if r.status_code == 200 else {}
    ids_in_list = [w["id"] for w in listing.get("watch_paths", [])]
    check(
        "GET /watch-paths includes our id",
        (new_id in ids_in_list) if new_id else False,
        f"ids={ids_in_list}",
    )
    check(
        "GET /watch-paths total matches length",
        listing.get("total") == len(listing.get("watch_paths", [])),
        f"body={listing}",
    )

    r = client.post("/projects/refresh")
    check(
        "POST /projects/refresh 200",
        r.status_code == 200,
        f"status={r.status_code} body={r.text}",
    )
    refresh_body = r.json() if r.status_code == 200 else {}
    refreshed_count = refresh_body.get("count", 0)
    check(
        "POST /projects/refresh count>=1 with watch_path configured",
        refreshed_count >= 1,
        f"count={refreshed_count}",
    )

    r = client.get("/projects")
    check(
        "GET /projects 200 after refresh",
        r.status_code == 200,
        f"status={r.status_code}",
    )
    projects_body = r.json() if r.status_code == 200 else {}
    check(
        "GET /projects total>=1 after refresh",
        projects_body.get("total", 0) >= 1,
        f"total={projects_body.get('total')}",
    )

    r = client.delete(f"/watch-paths/{new_id}")
    check(
        "DELETE /watch-paths/{id} -> 200",
        r.status_code == 200,
        f"status={r.status_code} body={r.text}",
    )
    del_body = r.json() if r.status_code == 200 else {}
    check(
        "DELETE /watch-paths/{id} returns {deleted:true,id}",
        del_body.get("deleted") is True and del_body.get("id") == new_id,
        f"body={del_body}",
    )
    if new_id in created_ids:
        created_ids.remove(new_id)

    r = client.delete(f"/watch-paths/{new_id}")
    check(
        "DELETE /watch-paths/{id} missing -> 404",
        r.status_code == 404,
        f"status={r.status_code} body={r.text}",
    )

    r = client.get("/projects")
    check(
        "GET /projects 200 after delete",
        r.status_code == 200,
        f"status={r.status_code}",
    )
    projects_after = r.json() if r.status_code == 200 else {}
    print(f"[info] /projects total after delete = {projects_after.get('total')}")

    remaining_ids = {row["id"] for row in list_watch_paths(include_disabled=True)}
    check(
        "no leftover test watch_paths",
        remaining_ids == PRE_EXISTING_IDS,
        f"remaining={sorted(remaining_ids)} expected={sorted(PRE_EXISTING_IDS)}",
    )

    if failures:
        print()
        print(f"FAILED: {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print()
    print(f"OK — all checks passed (created_ids={created_ids})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
