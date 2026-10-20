#!/usr/bin/env python3
"""DB edge case teszt: init_db futtatasa."""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "project_manager.db")
)
from db import init_db

init_db()
print("DB inicializalva")
