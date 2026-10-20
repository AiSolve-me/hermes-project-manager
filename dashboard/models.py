"""Pydantic models for the Project Manager dashboard API."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ProjectItem(BaseModel):
    """Single project representation returned by the API."""

    name: str = Field(..., description="Project / directory name")
    path: str = Field(..., description="Absolute filesystem path to the project")
    remote_url: Optional[str] = Field(None, description="Git remote URL (origin)")
    current_branch: str = Field(..., description="Currently checked out branch")
    last_commit_hash: Optional[str] = Field(
        None, description="Hash of the most recent commit"
    )
    last_commit_message: Optional[str] = Field(
        None, description="Message of the most recent commit"
    )
    last_commit_date: Optional[datetime] = Field(
        None, description="Timestamp of the most recent commit"
    )
    last_push_date: Optional[datetime] = Field(
        None, description="Timestamp of the last push to origin"
    )
    is_dirty: bool = Field(
        False, description="Whether the working tree has uncommitted changes"
    )
    branch_count: int = Field(0, description="Number of local branches")
    branches: List[str] = Field(default_factory=list, description="All local branch names")
    watch_path_sort_order: int = Field(
        0,
        description="Sort order of the parent watch_path. Projects sorted primarily by this, then by last_commit_date desc, then by name.",
    )
    ahead: int = Field(
        0,
        description=(
            "Commits ahead of origin/<branch>. Computed at scan time via "
            "'git rev-list --count origin/<branch>..HEAD'. Zero when the "
            "branch has no upstream or the count command fails."
        ),
    )
    behind: int = Field(
        0,
        description=(
            "Commits behind origin/<branch>. Computed at scan time via "
            "'git rev-list --count HEAD..origin/<branch>'. Zero when the "
            "branch has no upstream or the count command fails."
        ),
    )
    has_docker: bool = Field(
        False,
        description=(
            "True when the project root has a Dockerfile or compose file, "
            "or a compose stack is running under this path."
        ),
    )
    docker_running: bool = Field(
        False,
        description=(
            "True when docker ps reports a compose container whose "
            "working_dir lives under this project path. Live, not scan-cached."
        ),
    )
    docker_ports: list[int] = Field(
        default_factory=list,
        description="Published host ports from docker ps for this project's stack.",
    )


class ProjectListResponse(BaseModel):
    """Response payload for listing scanned projects."""

    projects: List[ProjectItem] = Field(default_factory=list)
    total: int = Field(0, description="Total number of projects returned")
    total_filtered: int = Field(
        0,
        description=(
            "Total number of projects matching the current search query, "
            "before pagination. Equals `total` when no `q` is supplied."
        ),
    )
    page: int = Field(1, description="Current page (1-based)")
    page_size: int = Field(21, description="Number of projects per page")
    page_count: int = Field(
        0,
        description=(
            "Total number of pages for the current query / page_size. "
            "Always >= 1 (a query with zero results returns one empty page)."
        ),
    )
    scanned_at: Optional[datetime] = Field(
        None, description="When the scan was last completed"
    )


class CommitItem(BaseModel):
    """Single commit representation returned by the API."""

    hash: str = Field(..., description="Commit SHA")
    message: str = Field(..., description="Commit message")
    date: datetime = Field(..., description="Commit timestamp")
    author: str = Field(..., description="Commit author name / email")


class CommitListResponse(BaseModel):
    """Response payload for listing commits of a project."""

    commits: List[CommitItem] = Field(default_factory=list)


class BranchItem(BaseModel):
    """Single branch representation returned by the API."""

    name: str = Field(..., description="Branch name")
    is_current: bool = Field(
        False, description="Whether this branch is currently checked out"
    )


class WatchPathItem(BaseModel):
    """Single watch-path entry returned by the API."""

    id: int = Field(..., description="Watch path row id")
    path: str = Field(..., description="Absolute filesystem path")
    type: str = Field(..., description="scan_root or single_repo")
    label: Optional[str] = Field(None, description="Optional human label")
    enabled: bool = Field(True, description="Whether this entry is active")
    created_at: datetime = Field(..., description="When this watch path was added")


class WatchPathListResponse(BaseModel):
    """Response payload for listing watch paths."""

    watch_paths: List[WatchPathItem] = Field(default_factory=list)
    total: int = Field(0, description="Total number of watch paths")


class AddWatchPathRequest(BaseModel):
    """Request payload for adding a new watch path."""

    path: str = Field(..., description="Absolute filesystem path to watch")
    type: str = Field(..., description="scan_root or single_repo")
    label: Optional[str] = Field(None, description="Optional human label")
