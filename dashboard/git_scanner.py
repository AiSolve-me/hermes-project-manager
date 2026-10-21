#!/usr/bin/env python3
"""
git_scanner.py — Discover git repositories under /home/github/projects/

Usage:
    from git_scanner import discover_projects, get_current_branch, get_remote_url
    projects = discover_projects()
    branch = get_current_branch('/home/github/projects/aisolve.me')
    url = get_remote_url('/home/github/projects/aisolve.me')
"""

import os
import subprocess
from pathlib import Path

# Base directory where all projects live
PROJECTS_ROOT = Path("/home/github/projects")


def _cleanup_stale_lock(path: str | Path) -> bool:
    """Remove ``<path>/.git/index.lock`` if present, returning whether we did.

    git leaves this file behind when its child processes are killed
    abruptly (timeout, SIGKILL, host crash). Without this helper, every
    subsequent git call fails with the same "fatal: Unable to create
    '.git/index.lock': File exists" error until the user manually
    ``rm``-eli the file. We ``unlink`` best-effort and return whether
    the file was actually removed, so callers can log when the
    recovery fires. The helper is safe to call from every read-only
    probe — it never blocks on a running git process and the cost is
    one ``stat`` + optional ``unlink``.
    """
    lock = Path(path) / ".git" / "index.lock"
    try:
        os.unlink(lock)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def discover_projects(root: str | Path = PROJECTS_ROOT) -> list[str]:
    """
    Walk immediate subdirectories of *root* and return the names of those
    that contain a `.git` directory (i.e. are git repositories).

    Args:
        root: Base directory to scan. Defaults to /home/github/projects.

    Returns:
        Alphabetically sorted list of project directory names.
    """
    root_path = Path(root)

    if not root_path.is_dir():
        return []

    projects: list[str] = []

    for entry in root_path.iterdir():
        # Only consider immediate subdirectories
        if entry.is_dir() and (entry / ".git").is_dir():
            projects.append(entry.name)

    projects.sort()
    return projects


def get_current_branch(path: str | Path) -> str:
    """
    Return the current git branch for the repository at *path*.

    Runs::

        git -C <path> rev-parse --abbrev-ref HEAD

    If the repository is in a detached HEAD state the command returns
    ``"HEAD"``, in which case this function returns ``"DETACHED"``.

    Args:
        path: Filesystem path to the git repository.

    Returns:
        The current branch name, or ``"DETACHED"`` when HEAD is detached.
        If the command fails (e.g. not a git repo) an empty string is
        returned.
    """
    try:
        _cleanup_stale_lock(path)
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        branch = result.stdout.strip()
        return "DETACHED" if branch == "HEAD" else branch
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return ""


def get_last_commit(path: str | Path) -> dict[str, str]:
    """
    Return the last commit for the repository at *path*.

    Runs::

        git -C <path> log -1 --format="%H|%ci|%s"

    Args:
        path: Filesystem path to the git repository.

    Returns:
        A dict with keys ``hash``, ``date``, and ``message``.
        If the command fails (e.g. not a git repo) an empty dict is
        returned.
    """
    try:
        _cleanup_stale_lock(path)
        result = subprocess.run(
            ["git", "-C", str(path), "log", "-1", "--format=%H|%ci|%s"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        line = result.stdout.strip()
        if not line:
            return {}
        parts = line.split("|", 2)
        if len(parts) != 3:
            return {}
        return {"hash": parts[0], "date": parts[1], "message": parts[2]}
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return {}


def get_remote_url(path: str | Path) -> str | None:
    """
    Return the remote URL of the ``origin`` remote for the repository at *path*.

    Runs::

        git -C <path> remote get-url origin

    Args:
        path: Filesystem path to the git repository.

    Returns:
        The ``origin`` remote URL as a string (SSH → HTTPS converted),
        or ``None`` if the repository has no ``origin`` remote.
    """
    try:
        _cleanup_stale_lock(path)
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        raw = result.stdout.strip()
        if not raw:
            return None
        return _normalize_git_url(raw)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None


def _normalize_git_url(url: str) -> str:
    """Convert git remote URLs to browser-friendly HTTPS.

    Supports:
    - git@github.com:user/repo.git → https://github.com/user/repo
    - git@gitlab.com:user/repo.git → https://gitlab.com/user/repo
    - https://token@github.com/user/repo.git → https://github.com/user/repo
    - https://github.com/user/repo.git → https://github.com/user/repo
    - https://... (already OK) → as-is
    """
    import re

    # SSH format: git@host:path/to/repo.git
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if ssh_match:
        host = ssh_match.group(1)
        path = ssh_match.group(2)
        return f"https://{host}/{path}"

    # HTTPS with embedded token: https://token@host/path.git
    if url.startswith("https://") and "@" in url:
        # Remove credentials: https://user:pass@host/path → https://host/path
        url = re.sub(r"https://[^@]+@", "https://", url)

    # HTTPS with .git suffix
    if url.startswith("https://") and url.endswith(".git"):
        return url[:-4]

    return url


def get_last_push_date(path: str | Path, branch: str) -> str | None:
    """
    Return the last push date for *branch* on the ``origin`` remote.

    Runs::

        git -C <path> reflog --date=iso -1 --format="%cd" origin/<branch>

    Args:
        path: Filesystem path to the git repository.
        branch: Branch name to query on the ``origin`` remote.

    Returns:
        The push date as an ISO-formatted string, or ``None`` if there is
        no reflog entry for ``origin/<branch>`` or the command fails.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "reflog",
                "--date=iso",
                "-1",
                "--format=%cd",
                f"origin/{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        date_str = result.stdout.strip()
        return date_str or None
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return None


def is_dirty(path: str | Path) -> bool:
    """
    Check whether the repository at *path* has uncommitted changes.

    Runs::

        git -C <path> status --porcelain

    Args:
        path: Filesystem path to the git repository.

    Returns:
        ``True`` if there are uncommitted changes, ``False`` otherwise.
        If the command fails (e.g. not a git repo) ``False`` is returned.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return bool(result.stdout.strip())
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return False


def get_branches(path: str | Path) -> list[str]:
    """
    Return local branches for the repository at *path* that still exist
    on the ``origin`` remote.

    Runs::

        git -C <path> branch -vv --format="%(refname:short)%(upstream:track)"

    Filters out branches whose upstream is marked ``[gone]`` (deleted from
    remote) and excludes the current detached HEAD pseudo-branch.

    Args:
        path: Filesystem path to the git repository.

    Returns:
        A list of branch names that have a live remote tracking ref.
        If the command fails (e.g. not a git repo) an empty list is
        returned.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "branch", "-vv", "--format=%(refname:short)%(upstream:track)"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        lines = result.stdout.strip().splitlines()
        branches = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Format: "branch-name[gone]" or "branch-name[ahead 2]" or "branch-name"
            # Extract branch name (everything before first "[")
            bracket_idx = line.find("[")
            if bracket_idx != -1:
                branch = line[:bracket_idx].strip()
                track_info = line[bracket_idx:].strip()
                # Skip branches whose upstream is gone from remote
                if "gone" in track_info.lower():
                    continue
            else:
                branch = line
            # Skip detached HEAD pseudo-branch
            if branch == "HEAD":
                continue
            branches.append(branch)
        return branches
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []


def count_ahead_behind(path: str | Path, branch: str) -> tuple[int, int]:
    """Return (ahead, behind) commit counts for *branch* against origin/<branch>.

    Runs two fast ``git rev-list --count`` commands. Does NOT call
    ``git fetch`` first (the cached upstream refs are good enough for a
    rough UI indicator; the dedicated ``/git-pull`` endpoint re-runs with
    a fresh fetch before pulling).

    Returns ``(0, 0)`` when:
        - the branch has no upstream,
        - the rev-list command fails (e.g. no network / corrupt refs),
        - the branch is detached or empty.

    Args:
        path: Filesystem path to the git repository.
        branch: Local branch name (e.g. ``"main"``).

    Returns:
        ``(ahead, behind)`` where:
        - ``ahead`` is the number of commits on *branch* not on
          ``origin/<branch>`` (i.e. local unpushed commits);
        - ``behind`` is the number of commits on ``origin/<branch>``
          not on *branch* (i.e. remote unpulled commits).
    """
    if not branch or branch == "DETACHED" or branch == "HEAD":
        return 0, 0
    ahead = 0
    behind = 0
    _cleanup_stale_lock(path)
    try:
        result = subprocess.run(
            [
                "git", "-C", str(path),
                "rev-list", "--count", f"origin/{branch}..HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            ahead = int(result.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        ahead = 0
    try:
        result = subprocess.run(
            [
                "git", "-C", str(path),
                "rev-list", "--count", f"HEAD..origin/{branch}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            behind = int(result.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        behind = 0
    return ahead, behind


def get_git_status(path: str | Path) -> dict:
    """
    Return working-tree status (branch, ahead/behind, dirty files) for *path*.

    Runs::

        git -C <path> status --porcelain --branch --ahead-behind

    Parses the ``--branch`` header and the porcelain body. The header line is
    one of:

    - ``## <branch>...origin/<branch> [<ahead>, <behind>]`` — branch tracks
      upstream with non-zero or zero diff (the bracket pair is omitted when
      the branches are equal in modern git).
    - ``## <branch>...origin/<branch> [gone]`` — upstream was deleted.
    - ``## <branch>`` — local branch without an upstream.
    - ``## HEAD (detached)`` — detached HEAD state.

    Porcelain lines are either ``XY filename`` or ``XY old -> new`` (rename /
    copy). ``X`` is the index status, ``Y`` the worktree status. ``??`` is
    untracked, ``!!`` is ignored.

    Args:
        path: Filesystem path to the git repository.

    Returns:
        A dict with keys: ``branch`` (str), ``ahead`` (int), ``behind`` (int),
        ``modified_files`` (list of ``{path, index_status, worktree_status}``),
        ``untracked_files`` (list of ``{path}``), ``detached`` (bool),
        ``has_upstream`` (bool). On any subprocess error (not a git repo,
        timeout, missing binary) an empty dict is returned.
    """
    try:
        _cleanup_stale_lock(path)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "status",
                "--porcelain",
                "--branch",
                "--ahead-behind",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return {}

    raw = result.stdout
    if not raw:
        return {}

    lines = raw.splitlines()
    branch = ""
    ahead = 0
    behind = 0
    detached = False
    has_upstream = False

    modified_files: list[dict] = []
    untracked_files: list[dict] = []

    for line in lines:
        if line.startswith("##"):
            # Header line
            header = line[2:].strip()
            if header.startswith("HEAD"):
                # Detached HEAD (e.g. "HEAD (detached at abc123)")
                detached = True
                continue
            # Detect upstream presence via "..." separator
            if "..." in header:
                has_upstream = True
                branch_part, _, rest = header.partition("...")
                branch = branch_part.strip()
                # rest is "origin/<branch>" possibly followed by " [ahead, behind]" / " [gone]"
                if rest.endswith("[gone]"):
                    has_upstream = False
                elif "[" in rest:
                    bracket_start = rest.find("[")
                    bracket_end = rest.rfind("]")
                    if bracket_start != -1 and bracket_end != -1:
                        inside = rest[bracket_start + 1 : bracket_end]
                        # Format: "ahead N, behind M"
                        for token in inside.split(","):
                            token = token.strip()
                            if token.startswith("ahead"):
                                try:
                                    ahead = int(token.split()[1])
                                except (IndexError, ValueError):
                                    pass
                            elif token.startswith("behind"):
                                try:
                                    behind = int(token.split()[1])
                                except (IndexError, ValueError):
                                    pass
            else:
                branch = header
            continue

        # Porcelain body line
        if len(line) < 3:
            continue
        x_status = line[0]
        y_status = line[1]
        # Skip ignored entries ("!! path")
        if x_status == "!" and y_status == "!":
            continue
        # Filename starts after "XY " (3 chars), handle rename/copy "old -> new"
        rest = line[3:]
        if " -> " in rest:
            # Rename/copy: keep the destination path (post-arrow)
            file_path = rest.split(" -> ", 1)[1].strip()
        else:
            file_path = rest.strip()

        # Quoted paths: git wraps paths with special chars in quotes / octal escapes.
        # Keep the raw string — callers can post-process if needed.
        if x_status == "?" and y_status == "?":
            untracked_files.append({"path": file_path})
        else:
            modified_files.append(
                {
                    "path": file_path,
                    "index_status": x_status,
                    "worktree_status": y_status,
                }
            )

    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "modified_files": modified_files,
        "untracked_files": untracked_files,
        "detached": detached,
        "has_upstream": has_upstream,
    }


def git_commit(path: str | Path, message: str, gpg_sign: bool = True) -> dict:
    """
    Stage all changes and commit them in the repository at *path*.

    Runs::

        git -C <path> add -A
        git -C <path> commit -m <message> [-S | --no-gpg-sign]

    When ``gpg_sign`` is True and the signed commit fails because no GPG
    secret key is available, the helper transparently retries with
    ``--no-gpg-sign`` so a missing signing key does not block ordinary
    workflow commits.

    Args:
        path: Filesystem path to the git repository.
        message: Commit message.
        gpg_sign: Whether to attempt a GPG-signed commit (default True).

    Returns:
        A dict with ``success`` (bool), ``message`` (str), ``stderr`` (str).
        ``success`` is True only when the commit command returned 0.
    """
    path_str = str(path)
    try:
        subprocess.run(
            ["git", "-C", path_str, "add", "-A"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        return {
            "success": False,
            "message": f"git add failed: {exc}",
            "stderr": stderr.strip() if isinstance(stderr, str) else "",
        }

    sign_args = ["-S"] if gpg_sign else []
    try:
        result = subprocess.run(
            ["git", "-C", path_str, "commit", "-m", message, *sign_args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return {
            "success": True,
            "message": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.CalledProcessError as exc:
        stderr_text = getattr(exc, "stderr", "") or ""
        # If GPG signing failed due to missing key, retry unsigned.
        if gpg_sign and (
            "gpg: skipped" in stderr_text
            or "No secret key" in stderr_text
            or "failed to sign" in stderr_text.lower()
        ):
            try:
                retry = subprocess.run(
                    [
                        "git",
                        "-C",
                        path_str,
                        "commit",
                        "-m",
                        message,
                        "--no-gpg-sign",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
                return {
                    "success": True,
                    "message": retry.stdout.strip()
                    + " (GPG fallback: unsigned)",
                    "stderr": retry.stderr.strip(),
                }
            except subprocess.CalledProcessError as retry_exc:
                retry_stderr = getattr(retry_exc, "stderr", "") or ""
                return {
                    "success": False,
                    "message": f"commit failed (unsigned retry): {retry_exc}",
                    "stderr": retry_stderr.strip(),
                }
        return {
            "success": False,
            "message": f"commit failed: {exc}",
            "stderr": stderr_text.strip(),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        stderr_text = getattr(exc, "stderr", "") or ""
        return {
            "success": False,
            "message": f"commit failed: {exc}",
            "stderr": stderr_text.strip(),
        }


def git_stash(path: str | Path, include_untracked: bool = True) -> dict:
    """
    Move all uncommitted changes (and optionally untracked files) to the
    git stash stack.

    Runs::

        git -C <path> stash push -u

    The stash entry is left on the stack (``stash@{0}``) so the user can
    ``git stash pop`` later. This is the same escape-hatch behaviour the
    worktree checkout endpoint uses for ``stash: true``.

    Args:
        path: Filesystem path to the git repository.
        include_untracked: Pass ``-u`` so untracked files are stashed too
            (default True). When False, only tracked changes are stashed.

    Returns:
        A dict with ``success`` (bool), ``message`` (str), ``stderr`` (str).
        ``success`` is True only when the stash command returned 0.
    """
    path_str = str(path)
    cmd = ["git", "-C", path_str, "stash", "push"]
    if include_untracked:
        cmd.append("-u")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return {
            "success": True,
            "message": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.CalledProcessError as exc:
        stderr_text = getattr(exc, "stderr", "") or ""
        return {
            "success": False,
            "message": f"git stash failed: {exc}",
            "stderr": stderr_text.strip(),
        }
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {
            "success": False,
            "message": f"git stash failed: {exc}",
            "stderr": "",
        }


def _valid_remote(path: str | Path, remote: str) -> bool:
    """True when *remote* is safe to pass to git as a positional.

    A caller-supplied remote must never reach ``git fetch`` / ``git push`` as a
    positional: git parses a leading-dash argument as an OPTION, and
    ``git fetch --upload-pack=<cmd> .`` executes ``<cmd>`` locally. Rejecting
    the leading dash closes that.

    An unknown-but-harmless name ("upstream" when there is none) is NOT
    rejected here: git reports it with its own ordinary error, and passing it
    through keeps that message intact for the client. The security boundary is
    the dash, not the membership test.
    """
    return bool(remote) and not remote.startswith("-")


def _valid_branch(branch: str) -> bool:
    """True when *branch* is a safe ref name.

    Same positional-argument hazard as :func:`_valid_remote`, so defer to
    git's own parser rather than hand-rolling rules it might disagree with.
    """
    if not branch or branch.startswith("-"):
        return False
    try:
        checked = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return checked.returncode == 0


def git_push(path: str | Path, remote: str = "origin", branch: str | None = None) -> dict:
    """
    Push the current branch (or *branch*) to *remote*.

    When *branch* is None the helper resolves the current branch via
    ``git rev-parse --abbrev-ref HEAD`` and pushes that.

    Runs::

        git -C <path> push <remote> <branch>

    Args:
        path: Filesystem path to the git repository.
        remote: Remote name (default ``"origin"``).
        branch: Branch name. When ``None``, the current branch is used.

    Returns:
        A dict with ``success`` (bool, True iff returncode == 0),
        ``message`` (str), ``stderr`` (str).
    """
    path_str = str(path)
    # Validate BEFORE anything reaches the argv: a caller-supplied remote or
    # branch passed positionally is parsed by git as an option, and
    # --upload-pack / --receive-pack execute a command locally.
    if not _valid_remote(path_str, remote):
        return {
            "success": False,
            "message": f"push rejected: unknown remote {remote!r}",
            "stderr": "",
        }
    if branch is None:
        try:
            head = subprocess.run(
                ["git", "-C", path_str, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            branch = head.stdout.strip()
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ) as exc:
            stderr_text = getattr(exc, "stderr", "") or ""
            return {
                "success": False,
                "message": f"could not resolve current branch: {exc}",
                "stderr": stderr_text.strip(),
            }

    if not _valid_branch(branch):
        return {
            "success": False,
            "message": f"push rejected: invalid branch name {branch!r}",
            "stderr": "",
        }

    try:
        result = subprocess.run(
            ["git", "-C", path_str, "push", remote, branch],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        stderr_text = getattr(exc, "stderr", "") or ""
        return {
            "success": False,
            "message": f"push failed: {exc}",
            "stderr": stderr_text.strip(),
        }

    return {
        "success": result.returncode == 0,
        "message": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def scan_project(
    path: str | Path,
    name: str,
    watch_path_sort_order: int = 0,
) -> dict:
    """
    Perform a full git scan of the project at *path*.

    Calls all available scanner helpers and returns a single dict
    containing every collected piece of information.  Each helper is
    wrapped in its own try/except so that a failure in one area does
    not prevent the others from being collected.

    Args:
        path: Filesystem path to the git repository.
        name: Human-readable project name (used as ``project`` in the
            returned dict).
        watch_path_sort_order: ``sort_order`` value inherited from the
            watch-path entry that produced this project. The frontend
            uses this as a stable sort key in ``list_projects``
            (default ``0``).

    Returns:
        A dict with the following keys:

        - ``project``      – the supplied *name*
        - ``path``         – absolute path to the repository
        - ``remote_url``   – ``origin`` remote URL (or ``None``)
        - ``branch``       – current branch name (or ``""``)
        - ``last_commit``  – dict with ``hash``, ``date``, ``message``
          (or ``{}``)
        - ``last_push``    – ISO date string of last push to
          ``origin/<branch>`` (or ``None``)
        - ``dirty``        – ``True`` if there are uncommitted changes
        - ``branches``     – list of all local branch names (or ``[]``)
        - ``watch_path_sort_order`` – the inherited ``sort_order`` value
          (default ``0``)
    """
    path = Path(path).resolve()

    # Current branch first — needed for last_push_date
    branch = get_current_branch(path)

    # Collect everything with per-field graceful fallback
    try:
        remote_url = get_remote_url(path)
    except Exception:
        remote_url = None

    try:
        last_commit = get_last_commit(path)
    except Exception:
        last_commit = {}

    try:
        last_push = get_last_push_date(path, branch) if branch else None
    except Exception:
        last_push = None

    try:
        dirty = is_dirty(path)
    except Exception:
        dirty = False

    try:
        branches = get_branches(path)
    except Exception:
        branches = []

    try:
        ahead, behind = count_ahead_behind(path, branch) if branch else (0, 0)
    except Exception:
        ahead, behind = 0, 0

    return {
        "project": name,
        "path": str(path),
        "remote_url": remote_url,
        "branch": branch,
        "last_commit": last_commit,
        "last_push": last_push,
        "dirty": dirty,
        "branches": branches,
        "watch_path_sort_order": watch_path_sort_order,
        "ahead": ahead,
        "behind": behind,
    }


def scan_all_projects(
    paths: list[dict] | None = None,
    root: str | Path = PROJECTS_ROOT,
) -> list[dict]:
    """Discover and scan projects from configured paths.

    When *paths* is provided, each entry must contain a ``path`` and a
    ``type`` of either ``scan_root`` or ``single_repo``.  Omitting *paths*
    preserves the legacy behavior of discovering repositories directly under
    *root*.

    Args:
        paths: Optional list of scan path definitions.
        root: Base directory for the legacy discovery behavior. Defaults to
            /home/github/projects.

    Returns:
        A list of dicts as returned by :func:`scan_project`.
        Projects that fail to scan are skipped (logged to stderr) so that
        one broken repository does not break the whole batch.
    """
    import sys

    if paths is None:
        project_entries = [
            (Path(root) / name, name, 0)
            for name in discover_projects(root)
        ]
    else:
        if not paths:
            return []

        project_entries = []
        for entry in paths:
            entry_path = Path(entry["path"])
            sort_order = int(entry.get("sort_order", 0) or 0)
            if entry["type"] == "scan_root":
                project_entries.extend(
                    (entry_path / name, name, sort_order)
                    for name in discover_projects(entry["path"])
                )
            elif entry["type"] == "single_repo":
                project_entries.append(
                    (entry_path, entry.get("name") or entry_path.name, sort_order)
                )

    results: list[dict] = []
    for path, name, sort_order in project_entries:
        try:
            results.append(scan_project(path, name=name, watch_path_sort_order=sort_order))
        except Exception as exc:
            print(f"[git_scanner] skipping {name}: {exc}", file=sys.stderr)

    return results


if __name__ == "__main__":
    print(discover_projects())
