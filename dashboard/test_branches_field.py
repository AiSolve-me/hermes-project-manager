#!/usr/bin/env python3
"""Test that _scan_to_project_item returns branches."""
import sys
import os

_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)

for mod in ("db", "models", "git_scanner", "cache", "plugin_api"):
    sys.modules.pop(mod, None)

from plugin_api import _scan_to_project_item
from git_scanner import scan_all_projects

scans = scan_all_projects()
if scans:
    item = _scan_to_project_item(scans[0])
    print("branches field:", item.branches)
    print("branch_count:", item.branch_count)
    print("current_branch:", item.current_branch)
else:
    print("No projects scanned")
