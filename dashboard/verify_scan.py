#!/usr/bin/env python3
import json
from git_scanner import scan_all_projects

r = scan_all_projects()
print(f"Found {len(r)} projects")
if r:
    print(json.dumps(r[0], indent=2, default=str))
