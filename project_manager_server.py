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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


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

    # CORS middleware - allow requests from the Hermes dashboard
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Restrict in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
