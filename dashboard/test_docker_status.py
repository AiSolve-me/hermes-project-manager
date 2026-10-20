#!/usr/bin/env python3
"""Self-check for docker_status helpers. python3 test_docker_status.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)

for mod in ("cache", "docker_status"):
    sys.modules.pop(mod, None)

from docker_status import has_docker, match_project, parse_docker_ps, parse_host_ports


def test_has_docker() -> None:
    with tempfile.TemporaryDirectory() as d:
        assert has_docker(d) is False
        Path(d, "README.md").write_text("x\n")
        assert has_docker(d) is False
        Path(d, "Dockerfile").write_text("FROM scratch\n")
        assert has_docker(d) is True
    with tempfile.TemporaryDirectory() as d:
        Path(d, "docker-compose.prod.yml").write_text("services: {}\n")
        assert has_docker(d) is True
    with tempfile.TemporaryDirectory() as d:
        Path(d, "compose.yaml").write_text("services: {}\n")
        assert has_docker(d) is True
    assert has_docker(None) is False
    assert has_docker("/no/such/path") is False


def test_parse_host_ports() -> None:
    assert parse_host_ports("0.0.0.0:43175->3000/tcp, [::]:43175->3000/tcp") == [43175]
    assert parse_host_ports("127.0.0.1:3000->3000/tcp") == [3000]
    assert parse_host_ports("5432/tcp") == []
    assert parse_host_ports("") == []
    assert parse_host_ports("0.0.0.0:8080->80/tcp, 0.0.0.0:443->443/tcp") == [8080, 443]


def test_parse_and_join() -> None:
    raw = "\n".join(
        [
            json.dumps(
                {
                    "Labels": "com.docker.compose.project.working_dir=/home/github/projects/aisolve.me,com.docker.compose.oneoff=False",
                    "Ports": "0.0.0.0:43167->3000/tcp, [::]:43167->3000/tcp",
                }
            ),
            json.dumps(
                {
                    "Labels": "com.docker.compose.project.working_dir=/home/github/projects/aisolve.me",
                    "Ports": "127.0.0.1:5555->5432/tcp",
                }
            ),
            json.dumps(
                {
                    "Labels": "com.docker.compose.project.working_dir=/home/github/projects/dead",
                    "Ports": "",
                }
            ),
        ]
    )
    stacks = parse_docker_ps(raw)
    running, ports = match_project("/home/github/projects/aisolve.me", stacks)
    assert running is True
    assert ports == [5555, 43167]
    running_dead, ports_dead = match_project("/home/github/projects/dead", stacks)
    assert running_dead is True
    assert ports_dead == []
    running_nope, ports_nope = match_project("/home/github/projects/nope", stacks)
    assert running_nope is False
    assert ports_nope == []
    assert parse_docker_ps("") == {}
    assert parse_docker_ps("not-json") == {}


if __name__ == "__main__":
    test_has_docker()
    test_parse_host_ports()
    test_parse_and_join()
    print("all passed")
