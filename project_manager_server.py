#!/usr/bin/env python3
"""
Project Manager Standalone API Server
=====================================

Runs the project-manager plugin API as a separate FastAPI server.
This is needed because Hermes v0.17.0+ no longer auto-imports Python
backend routes from user-installed plugins (GHSA-5qr3-c538-wm9j).

The server mounts the same routes that were previously served under
/api/plugins/project-manager/ in the Hermes dashboard.

Usage:
    python3 project_manager_server.py
    python3 project_manager_server.py --port 9120

Environment:
    PROJECT_MANAGER_PORT - Server port (default: 9120)
    PROJECT_MANAGER_HOST - Server host (default: 127.0.0.1)
"""

from __future__ import annotations

import os
import sys
import argparse
import logging
import secrets
from pathlib import Path

# Add the plugin dashboard directory to sys.path so we can import the modules
_DASHBOARD_DIR = Path(__file__).parent / "dashboard"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

# Remove stale modules from cache
for mod_name in ["db", "models", "git_scanner", "cache", "plugin_api"]:
    sys.modules.pop(mod_name, None)

# Import the plugin modules we need (db.init_db is idempotent — CREATE TABLE IF NOT EXISTS)
from plugin_api import router as project_manager_router
from db import init_db

# FastAPI imports
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# The router serves writes: git commit, push, discard (which runs
# `git clean -f`) and worktree removal. On 127.0.0.1 with no auth, every web
# page the user opens could reach them, because a wildcard CORS origin tells
# the browser it may send the cross-origin request. The token below is the
# boundary; the origin allowlist is defence in depth and allows nothing by
# default.
_API_TOKEN = secrets.token_urlsafe(32)


def _token_of(request: Request) -> str | None:
    """The bearer token for *request*, or None.

    Never read it from the query string: a token in a URL leaks into access
    logs, proxy logs and the Referer header of every outbound link.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-project-manager-token")


def _require_token(request: Request) -> JSONResponse | None:
    """A 401 response when the request carries no valid token, else None."""
    presented = _token_of(request)
    if presented and secrets.compare_digest(presented, _API_TOKEN):
        return None
    return JSONResponse(
        status_code=401,
        content={
            "error": "missing or invalid API token",
            "code": "UNAUTHORIZED",
        },
    )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Ensure schema exists (idempotent — tables created with IF NOT EXISTS).
    # Must run BEFORE serving so plugin_api doesn't hit a missing watch_paths table.
    init_db()

    app = FastAPI(
        title="Project Manager API",
        description="Git project overview — branches, commits, remotes, and sync status",
        version="1.0.0",
    )

    # CORS - allow requests from the Hermes dashboard
    # Explicit allowlist, empty by default. A wildcard origin plus
    # allow_credentials is what made every write route reachable from any web
    # page; with the token required, allowing no origin is the safe default.
    allowed = [
        o.strip()
        for o in os.environ.get("PROJECT_MANAGER_CORS_ORIGINS", "").split(",")
        if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        denied = _require_token(request)
        if denied is not None:
            return denied
        return await call_next(request)

    # Mount the plugin router at root (nginx handles the /api/plugins/<name>/ prefix)
    app.include_router(
        project_manager_router,
        prefix="",
        tags=["project-manager"],
    )

    @app.get("/health")
    async def health_check() -> dict:
        return {"status": "ok", "service": "project-manager"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Manager API Server")
    parser.add_argument(
        "--host",
        default=os.getenv("PROJECT_MANAGER_HOST", "127.0.0.1"),
        help="Server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PROJECT_MANAGER_PORT", "9120")),
        help="Server port (default: 9120)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (development only)",
    )
    args = parser.parse_args()

    app = create_app()

    import uvicorn

    log.info(
        "Starting Project Manager API server on http://%s:%d",
        args.host,
        args.port,
    )
    log.info(
        "API endpoints available at http://%s:%d/api/plugins/project-manager/",
        args.host,
        args.port,
    )
    # Printed once, at startup: the token exists only for this run, so there
    # is nothing to store and nothing to rotate.
    log.info("API token (send as 'Authorization: Bearer <token>'): %s", _API_TOKEN)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
