-- Project Manager Plugin Database Schema

CREATE TABLE IF NOT EXISTS project_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    path TEXT NOT NULL,
    remote_url TEXT,
    current_branch TEXT,
    last_commit_hash TEXT,
    last_commit_message TEXT,
    last_commit_date TEXT,
    last_push_date TEXT,
    is_dirty INTEGER DEFAULT 0,
    branch_count INTEGER DEFAULT 0,
    scanned_at TEXT NOT NULL,
    watch_path_sort_order INTEGER NOT NULL DEFAULT 0,
    ahead INTEGER NOT NULL DEFAULT 0,
    behind INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_project_scans_name ON project_scans(project_name);
CREATE INDEX IF NOT EXISTS idx_project_scans_scanned_at ON project_scans(scanned_at);

CREATE TABLE IF NOT EXISTS watch_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK(type IN ('scan_root', 'single_repo')),
    label TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watch_paths_path ON watch_paths(path);
