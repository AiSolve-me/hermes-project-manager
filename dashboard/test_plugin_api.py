#!/usr/bin/env python3
"""Quick smoke test for plugin_api.py — run standalone."""

import sys
import os

# Add dashboard dir to path
_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)

# Clear stale modules
for mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(mod, None)

from plugin_api import router, _scan_to_project_item
from git_scanner import scan_all_projects

print("[TEST] Import OK")
print("[TEST] Router routes:", [r.path for r in router.routes])

scans = scan_all_projects()
print(f"[TEST] Scanned {len(scans)} projects")

if scans:
    item = _scan_to_project_item(scans[0])
    print(
        f"[TEST] First project: {item.name} | branch={item.current_branch} | dirty={item.is_dirty} | branches={item.branch_count}"
    )

print("[TEST] All passed")
