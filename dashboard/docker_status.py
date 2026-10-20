"""Compose presence + running ports for the project list.

Presence is one directory listing (no subprocess). Running state and
published host ports come from one host-wide ``docker ps``, cached 10s.
Join is compose working_dir path-prefix, not compose project name.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from cache import cache

DOCKER_PS_CACHE_KEY = "project_manager:docker_ps"
DOCKER_PS_TTL = 10

_HOST_PORT_RE = re.compile(r":(\d+)->")
_WORKING_DIR_RE = re.compile(r"com\.docker\.compose\.project\.working_dir=([^,]*)")
_CONFIG_FILES_RE = re.compile(r"com\.docker\.compose\.project\.config_files=([^,]*)")


def has_docker(path: str | None) -> bool:
    if not path:
        return False
    root = Path(path)
    if not root.is_dir():
        return False
    try:
        names = [p.name.lower() for p in root.iterdir() if p.is_file()]
    except OSError:
        return False
    for name in names:
        if name == "dockerfile" or name.startswith("dockerfile."):
            return True
        if name in ("compose.yml", "compose.yaml"):
            return True
        if name.startswith("docker-compose") and name.endswith((".yml", ".yaml")):
            return True
        if name.startswith("compose.") and name.endswith((".yml", ".yaml")):
            return True
    return False


def parse_host_ports(ports_field: str) -> list[int]:
    """Host side of ``0.0.0.0:43175->3000/tcp``. Unpublished ``5432/tcp`` is ignored."""
    if not ports_field:
        return []
    seen: set[int] = set()
    out: list[int] = []
    for raw in _HOST_PORT_RE.findall(ports_field):
        port = int(raw)
        if port not in seen:
            seen.add(port)
            out.append(port)
    return out


def _root_from_labels(labels: str) -> str | None:
    if not labels:
        return None
    match = _WORKING_DIR_RE.search(labels)
    raw = match.group(1).strip() if match else ""
    if not raw:
        cfg = _CONFIG_FILES_RE.search(labels)
        raw = cfg.group(1).strip() if cfg else ""
        if raw:
            raw = str(Path(raw).parent)
    if not raw:
        return None
    try:
        return str(Path(raw).resolve())
    except OSError:
        return None


def parse_docker_ps(stdout: str) -> dict[str, list[int]]:
    """Map compose working_dir -> sorted unique published host ports.

    NDJSON (one object per line) or a JSON array. Roots with no published
    ports still appear (empty list) so running-but-internal stacks count.
    """
    if not stdout or not stdout.strip():
        return {}
    items: list[object] = []
    stripped = stdout.strip()
    if stripped.startswith("["):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
        if isinstance(loaded, list):
            items = loaded
    else:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    stacks: dict[str, list[int]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        labels = item.get("Labels") or ""
        if not isinstance(labels, str):
            continue
        root = _root_from_labels(labels)
        if not root:
            continue
        ports = parse_host_ports(str(item.get("Ports") or ""))
        existing = stacks.setdefault(root, [])
        for port in ports:
            if port not in existing:
                existing.append(port)
    for root in stacks:
        stacks[root].sort()
    return stacks


def list_running_stacks() -> dict[str, list[int]]:
    cached = cache.get(DOCKER_PS_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        cache.set(DOCKER_PS_CACHE_KEY, {}, ttl=DOCKER_PS_TTL)
        return {}
    stacks = parse_docker_ps(result.stdout if result.returncode == 0 else "")
    cache.set(DOCKER_PS_CACHE_KEY, stacks, ttl=DOCKER_PS_TTL)
    return stacks


def match_project(
    project_path: str | None, stacks: dict[str, list[int]]
) -> tuple[bool, list[int]]:
    """Return (running, published host ports) for a project path."""
    if not project_path or not stacks:
        return False, []
    try:
        proj = Path(project_path).resolve()
    except OSError:
        return False, []
    ports: list[int] = []
    running = False
    for root, root_ports in stacks.items():
        try:
            Path(root).resolve().relative_to(proj)
        except (ValueError, OSError):
            continue
        running = True
        for port in root_ports:
            if port not in ports:
                ports.append(port)
    ports.sort()
    return running, ports
