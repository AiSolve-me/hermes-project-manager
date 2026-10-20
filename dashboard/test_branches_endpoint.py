#!/usr/bin/env python3
"""Direct test for the branches endpoint — no HTTP server needed."""

import sys
import os

_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)

for mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(mod, None)

import asyncio
from plugin_api import list_project_branches
from models import BranchItem


async def test():
    # Test existing project
    result = await list_project_branches("aisolve.me")
    print(f"[TEST] aisolve.me branches: {len(result)}")
    for b in result:
        marker = " <-- CURRENT" if b.is_current else ""
        print(f"  - {b.name}{marker}")
    assert all(isinstance(b, BranchItem) for b in result)
    assert any(b.is_current for b in result), "No current branch found"

    # Test non-existent project
    from fastapi import HTTPException

    try:
        await list_project_branches("nonexistent-project-12345")
        assert False, "Should have raised 404"
    except HTTPException as exc:
        assert exc.status_code == 404
        print(f"[TEST] 404 for nonexistent project: OK")

    print("[TEST] All passed")


asyncio.run(test())
