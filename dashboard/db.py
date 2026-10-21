from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
import sqlite3
from pathlib import Path
import os
import threading
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

_BLOCKED_PATHS = {
    "/", "/etc", "/var", "/usr", "/proc", "/sys", "/dev",
    "/boot", "/root/.gnupg", "/root/.ssh"
}


def _is_within(path: Path, root: Path) -> bool:
    """Return True if path is root or under root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _default_db_path() -> Path:
    """Where the SQLite file lives.

    NOT in the plugin directory: a reinstall replaces that folder wholesale and
    the user's watch paths would go with it. Plugin state belongs under
    ``$HERMES_HOME/plugin-data/``, which survives. Falls back to the plugin dir
    when there is no HERMES_HOME (a bare checkout run by hand), so nothing
    breaks.
    """
    override = os.environ.get("PROJECT_MANAGER_DB")
    if override:
        return Path(override).expanduser()
    home = os.environ.get("HERMES_HOME")
    if home:
        target = Path(home).expanduser() / "plugin-data" / "project-manager"
        target.mkdir(parents=True, exist_ok=True)
        return target / "project-manager.db"
    return Path(__file__).resolve().parent / "project-manager.db"


_DB_PATH = _default_db_path()

_pool_instance: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------


class ConnectionPool:
    """Thread-safe SQLite connection pool using a queue.

    Connections are created on demand and returned to the pool after use.
    Each thread gets its own connection from the pool, avoiding
    "database is locked" errors under concurrent access.
    """

    def __init__(
        self,
        db_path: str,
        max_connections: int = 5,
        timeout: float = 30.0,
    ) -> None:
        self._db_path = db_path
        self._max_connections = max_connections
        self._timeout = timeout
        self._pool: Queue[sqlite3.Connection] = Queue(maxsize=max_connections)
        self._lock = threading.Lock()
        self._created = 0
        self._closed = False

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new configured sqlite3 connection."""
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=self._timeout,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        """Fetch a connection from the pool or create a new one."""
        if self._closed:
            raise RuntimeError("Pool is closed")
        try:
            return self._pool.get_nowait()
        except Empty:
            pass
        with self._lock:
            if self._created < self._max_connections:
                self._created += 1
                return self._create_connection()
        try:
            return self._pool.get(timeout=self._timeout)
        except Empty:
            raise RuntimeError(
                f"Connection pool exhausted (max={self._max_connections})"
            )

    def _return_conn(self, conn: sqlite3.Connection) -> None:
        """Return a connection to the pool."""
        if self._closed:
            conn.close()
            return
        try:
            self._pool.put_nowait(conn)
        except Exception:
            conn.close()
            with self._lock:
                self._created -= 1

    def connection(self):
        """Return a context manager for getting a connection from the pool."""
        from contextlib import contextmanager

        @contextmanager
        def _connection_ctx():
            conn = self._get_conn()
            try:
                yield conn
            finally:
                self._return_conn(conn)

        return _connection_ctx()

    def close_all(self) -> None:
        """Close all connections in the pool. Call on shutdown."""
        self._closed = True
        while not self._pool.empty():
            try:
                self._pool.get_nowait().close()
            except Empty:
                break

    def __del__(self):
        """Cleanup on garbage collection."""
        try:
            self.close_all()
        except Exception:
            pass


def get_db_pool() -> ConnectionPool:
    """Get or create the global connection pool."""
    global _pool_instance
    if _pool_instance is None:
        with _pool_lock:
            if _pool_instance is None:
                _pool_instance = ConnectionPool(
                    db_path=str(_DB_PATH),
                    max_connections=5,
                    timeout=30.0,
                )
    return _pool_instance


def _get_conn():
    """Legacy compatibility - returns a context manager for pool connections.

    DEPRECATED: Use get_db_pool().connection() context manager instead.
    This function yields a connection from the pool that auto-returns on exit.
    """
    return get_db_pool().connection()


def init_db() -> None:
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    with get_db_pool().connection() as conn:
        conn.executescript(schema_path.read_text())
        conn.commit()
    # Idempotent column migrations for already-existing databases.
    # CREATE TABLE IF NOT EXISTS in schema.sql does NOT add new columns
    # to pre-existing tables, so each new column needs its own PRAGMA
    # check + ALTER TABLE pair.
    ensure_sort_order_column()
    ensure_project_scans_watch_path_sort_order_column()
    ensure_project_scans_ahead_behind_columns()


# ---------------------------------------------------------------------------
# Project scans CRUD
# ---------------------------------------------------------------------------


def record_project_scan(
    project_name: str,
    path: str,
    remote_url: Optional[str] = None,
    current_branch: Optional[str] = None,
    last_commit_hash: Optional[str] = None,
    last_commit_message: Optional[str] = None,
    last_commit_date: Optional[str] = None,
    last_push_date: Optional[str] = None,
    is_dirty: int = 0,
    branch_count: int = 0,
) -> int:
    """Insert a new project scan record. Returns the new row id."""
    scanned_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_pool().connection() as conn:
        cur = conn.execute(
            """INSERT INTO project_scans
               (project_name, path, remote_url, current_branch, last_commit_hash,
                last_commit_message, last_commit_date, last_push_date,
                is_dirty, branch_count, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_name,
                path,
                remote_url,
                current_branch,
                last_commit_hash,
                last_commit_message,
                last_commit_date,
                last_push_date,
                is_dirty,
                branch_count,
                scanned_at,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_latest_scan(project_name: str) -> dict | None:
    """Return the most recent scan for a project, or None."""
    with get_db_pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM project_scans WHERE project_name = ? ORDER BY scanned_at DESC LIMIT 1",
            (project_name,),
        ).fetchone()
        return dict(row) if row else None


def get_all_scans(limit: int = 100) -> list[dict]:
    """Return recent scans across all projects."""
    with get_db_pool().connection() as conn:
        rows = conn.execute(
            "SELECT * FROM project_scans ORDER BY scanned_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_scans_for_project(project_name: str, limit: int = 50) -> list[dict]:
    """Return recent scans for a specific project."""
    with get_db_pool().connection() as conn:
        rows = conn.execute(
            """SELECT * FROM project_scans
               WHERE project_name = ?
               ORDER BY scanned_at DESC LIMIT ?""",
            (project_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_scans_before(cutoff: datetime) -> int:
    """Delete old scans before cutoff. Returns number of deleted rows."""
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    with get_db_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM project_scans WHERE scanned_at < ?",
            (cutoff_str,),
        )
        conn.commit()
        return cur.rowcount


# ---------------------------------------------------------------------------
# CRUD functions for project_scans
# ---------------------------------------------------------------------------


def record_scan(
    project_name: str,
    path: str,
    remote_url: Optional[str] = None,
    current_branch: Optional[str] = None,
    last_commit_hash: Optional[str] = None,
    last_commit_message: Optional[str] = None,
    last_commit_date: Optional[str] = None,
    last_push_date: Optional[str] = None,
    is_dirty: int = 0,
    branch_count: int = 0,
    watch_path_sort_order: int = 0,
    ahead: int = 0,
    behind: int = 0,
) -> int:
    """Insert a new project scan record. Returns the inserted row id."""
    scanned_at = datetime.now(timezone.utc).isoformat()
    with get_db_pool().connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO project_scans (
                project_name, path, remote_url, current_branch,
                last_commit_hash, last_commit_message, last_commit_date,
                last_push_date, is_dirty, branch_count, scanned_at,
                watch_path_sort_order, ahead, behind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_name,
                path,
                remote_url,
                current_branch,
                last_commit_hash,
                last_commit_message,
                last_commit_date,
                last_push_date,
                is_dirty,
                branch_count,
                scanned_at,
                watch_path_sort_order,
                ahead,
                behind,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def get_latest_scan(project_name: str) -> Optional[Dict[str, Any]]:
    """Return the most recent scan record for the given project, or None."""
    with get_db_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM project_scans
            WHERE project_name = ?
            ORDER BY scanned_at DESC
            LIMIT 1
            """,
            (project_name,),
        ).fetchone()
        return dict(row) if row else None


def list_all_latest_scans() -> List[Dict[str, Any]]:
    """Return the latest scan record for every project (one per project)."""
    with get_db_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT ps.*
            FROM project_scans ps
            JOIN (
                SELECT project_name, MAX(scanned_at) AS max_scanned_at
                FROM project_scans
                GROUP BY project_name
            ) latest
            ON ps.project_name = latest.project_name
            AND ps.scanned_at = latest.max_scanned_at
            ORDER BY ps.project_name ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


# Allowed page sizes — kept in sync with the frontend page-size selector
# (9 / 21 / 27). Any other value is rejected by the API with 400.
ALLOWED_PAGE_SIZES = (9, 21, 27)
DEFAULT_PAGE_SIZE = 21
DEFAULT_PAGE = 1


def list_latest_scans_paginated(
    q: str = "",
    page: int = DEFAULT_PAGE,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[List[Dict[str, Any]], int]:
    """Return the paginated latest scan records matching *q*.

    Search runs over the full set of latest scans (one per project) and
    matches the *q* substring (case-insensitive) against
    ``project_name``, ``path``, ``remote_url`` and ``current_branch``.

    Args:
        q: free-text search query; empty string returns all projects.
        page: 1-based page number; values < 1 are clamped to 1.
        page_size: number of projects per page; must be one of
            ``ALLOWED_PAGE_SIZES``.

    Returns:
        A tuple of ``(rows, total_filtered)`` where ``rows`` is the
        page-slice and ``total_filtered`` is the total number of
        projects matching *q* (independent of page / page_size).
    """
    if page_size not in ALLOWED_PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    where_clauses: list[str] = []
    params: list[Any] = []
    q = (q or "").strip()
    if q:
        like = f"%{q.lower()}%"
        where_clauses.append(
            "(LOWER(project_name) LIKE ? "
            "OR LOWER(path) LIKE ? "
            "OR LOWER(IFNULL(remote_url, '')) LIKE ? "
            "OR LOWER(IFNULL(current_branch, '')) LIKE ?)"
        )
        params.extend([like, like, like, like])

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Inner query: latest scan per project (same shape as
    # list_all_latest_scans). Wrapped in a subquery so the WHERE clause
    # can run on the latest-scan-per-project result rather than every
    # historical row.
    latest_cte = """
        SELECT ps.*
        FROM project_scans ps
        JOIN (
            SELECT project_name, MAX(scanned_at) AS max_scanned_at
            FROM project_scans
            GROUP BY project_name
        ) latest
        ON ps.project_name = latest.project_name
        AND ps.scanned_at = latest.max_scanned_at
    """

    with get_db_pool().connection() as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) AS c FROM ({latest_cte}){where_sql}",
            params,
        ).fetchone()
        total_filtered = int(total_row["c"] if total_row else 0)

        rows = conn.execute(
            f"""
            SELECT * FROM ({latest_cte}){where_sql}
            ORDER BY
                watch_path_sort_order ASC,
                last_commit_date DESC,
                project_name ASC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ).fetchall()
        return [dict(row) for row in rows], total_filtered


def get_scan_history(project_name: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return recent scan history for a project, newest first."""
    with get_db_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM project_scans
            WHERE project_name = ?
            ORDER BY scanned_at DESC
            LIMIT ?
            """,
            (project_name, limit),
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Watch paths CRUD
# ---------------------------------------------------------------------------

_VALID_WATCH_PATH_TYPES = ("scan_root", "single_repo")


def add_watch_path(
    path: str,
    type: str,
    label: Optional[str] = None,
) -> int:
    """Insert a new watch path. Returns the inserted row id.

    Validates that ``path`` is absolute, ``type`` is one of the allowed
    values, and the directory exists on disk. Raises ``ValueError`` on any
    validation failure or uniqueness conflict.
    """
    resolved = Path(path).resolve()
    for blocked in _BLOCKED_PATHS:
        bp = Path(blocked)
        if resolved == bp:
            raise ValueError(f"blocked system path: {resolved}")
        # `/` is in _BLOCKED_PATHS but every absolute path is `/`-within,
        # so only apply the descendant check to non-root entries.
        if bp != Path("/") and _is_within(resolved, bp):
            raise ValueError(f"blocked system path: {resolved}")

    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("path must be an absolute path")
    if type not in _VALID_WATCH_PATH_TYPES:
        raise ValueError(
            f"type must be one of {_VALID_WATCH_PATH_TYPES}, got {type!r}"
        )
    if not Path(path).is_dir():
        raise ValueError(f"path does not exist or is not a directory: {path}")

    created_at = datetime.now(timezone.utc).isoformat()

    with get_db_pool().connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM watch_paths WHERE path = ?",
            (path,),
        ).fetchone()
        if existing is not None:
            raise ValueError("path already exists")

        cur = conn.execute(
            """
            INSERT OR ABORT INTO watch_paths (path, type, label, enabled, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (path, type, label, created_at),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def ensure_sort_order_column() -> None:
    """Add sort_order column if missing (idempotent)."""
    with get_db_pool().connection() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watch_paths)").fetchall()]
        if "sort_order" not in cols:
            conn.execute(
                "ALTER TABLE watch_paths ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()


def ensure_project_scans_watch_path_sort_order_column() -> None:
    """Add watch_path_sort_order column to project_scans if missing (idempotent).

    Stores the parent's watch_paths.sort_order snapshot at scan time, so the
    paginated list query can order projects by the watch-path ordering
    without joining the watch_paths table on every request.
    """
    with get_db_pool().connection() as conn:
        cols = [
            r[1]
            for r in conn.execute("PRAGMA table_info(project_scans)").fetchall()
        ]
        if "watch_path_sort_order" not in cols:
            conn.execute(
                "ALTER TABLE project_scans ADD COLUMN watch_path_sort_order INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()


def ensure_project_scans_ahead_behind_columns() -> None:
    """Add ahead + behind columns to project_scans if missing (idempotent).

    Stores the git rev-list --count snapshot at scan time, so the project
    list UI can show ahead/behind badges without each card firing a
    separate /git-status request on mount.
    """
    with get_db_pool().connection() as conn:
        cols = [
            r[1]
            for r in conn.execute("PRAGMA table_info(project_scans)").fetchall()
        ]
        added = False
        if "ahead" not in cols:
            conn.execute(
                "ALTER TABLE project_scans ADD COLUMN ahead INTEGER NOT NULL DEFAULT 0"
            )
            added = True
        if "behind" not in cols:
            conn.execute(
                "ALTER TABLE project_scans ADD COLUMN behind INTEGER NOT NULL DEFAULT 0"
            )
            added = True
        if added:
            conn.commit()


def reorder_watch_paths(ordered_ids: list[int]) -> None:
    """Update sort_order for each id based on its index in ordered_ids."""
    ensure_sort_order_column()
    with get_db_pool().connection() as conn:
        for idx, wid in enumerate(ordered_ids):
            conn.execute(
                "UPDATE watch_paths SET sort_order = ? WHERE id = ?", (idx, wid)
            )
        conn.commit()


def rename_watch_path(watch_path_id: int, label: str) -> int:
    """Update the label for a watch path. Returns rowcount."""
    with get_db_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE watch_paths SET label = ? WHERE id = ?", (label, watch_path_id)
        )
        conn.commit()
        return cur.rowcount


def list_watch_paths(include_disabled: bool = False) -> List[Dict[str, Any]]:
    """Return watch paths ordered by sort order, then creation time ascending.

    By default only enabled paths are returned; pass
    ``include_disabled=True`` to list every row.
    """
    ensure_sort_order_column()
    with get_db_pool().connection() as conn:
        if include_disabled:
            rows = conn.execute(
                "SELECT * FROM watch_paths ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watch_paths WHERE enabled = 1 ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]


def get_enabled_watch_paths() -> List[Dict[str, Any]]:
    """Wrapper around :func:`list_watch_paths` returning only enabled rows."""
    return list_watch_paths(include_disabled=False)


def delete_watch_path(watch_path_id: int) -> int:
    """Delete a watch path by id. Returns number of deleted rows (0 or 1)."""
    with get_db_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM watch_paths WHERE id = ?",
            (watch_path_id,),
        )
        conn.commit()
        return cur.rowcount
