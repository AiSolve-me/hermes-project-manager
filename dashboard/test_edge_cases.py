#!/usr/bin/env python3
"""
Edge case tesztek a git_scanner es plugin_api modulokhoz.
Futtatas: python3 test_edge_cases.py
"""

import os
import subprocess
import sys
import time

# Add dashboard dir to path
_dashboard_dir = os.path.dirname(os.path.abspath(__file__))
if _dashboard_dir not in sys.path:
    sys.path.insert(0, _dashboard_dir)

from git_scanner import (
    discover_projects,
    get_current_branch,
    get_remote_url,
    scan_project,
    scan_all_projects,
)


def test_non_git_directory():
    """1. Nem-git mappa a projects alatt — skip-eli-e?"""
    print("\n=== TEST 1: Nem-git mappa ===")
    projects = discover_projects()
    has_nem_git = "nem-git-proj" in projects
    print(f"  discover_projects() eredmeny: {len(projects)} projekt")
    print(f"  'nem-git-proj' benne van: {has_nem_git}")
    if not has_nem_git:
        print("  [PASS] Nem-git mappa helyesen skip-elve")
        return True
    else:
        print("  [FAIL] Nem-git mappa nem lett skip-elve!")
        return False


def test_no_remote():
    """2. Nincs remote — null-t ad-e vissza?"""
    print("\n=== TEST 2: Nincs remote ===")
    test_dir = "/tmp/test-no-remote"

    # Letrehozas
    if os.path.exists(test_dir):
        subprocess.run(["rm", "-rf", test_dir], check=False)
    os.makedirs(test_dir, exist_ok=True)
    subprocess.run(["git", "init", test_dir], capture_output=True, check=True)

    # Egy commit kell, kulonben a git log hibat dob
    with open(os.path.join(test_dir, "init.txt"), "w") as f:
        f.write("init\n")
    subprocess.run(["git", "-C", test_dir, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", test_dir, "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", test_dir, "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", test_dir, "commit", "-m", "init"],
        capture_output=True,
        check=True,
    )

    url = get_remote_url(test_dir)
    print(f"  get_remote_url('{test_dir}') = {url!r}")
    if url is None:
        print("  [PASS] Remote URL None (nincs remote)")
        return True
    else:
        print(f"  [FAIL] Varhato: None, kapott: {url!r}")
        return False


def test_detached_head():
    """3. Detached HEAD — 'DETACHED'-et mutat-e?"""
    print("\n=== TEST 3: Detached HEAD ===")
    test_dir = "/tmp/test-detached-head"

    if os.path.exists(test_dir):
        subprocess.run(["rm", "-rf", test_dir], check=False)
    os.makedirs(test_dir, exist_ok=True)
    subprocess.run(["git", "init", test_dir], capture_output=True, check=True)

    # Ket commit, hogy legyen mire checkout-olni
    with open(os.path.join(test_dir, "f1.txt"), "w") as f:
        f.write("v1\n")
    subprocess.run(["git", "-C", test_dir, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", test_dir, "config", "user.email", "test@test.com"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", test_dir, "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", test_dir, "commit", "-m", "first"],
        capture_output=True,
        check=True,
    )

    with open(os.path.join(test_dir, "f2.txt"), "w") as f:
        f.write("v2\n")
    subprocess.run(["git", "-C", test_dir, "add", "."], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", test_dir, "commit", "-m", "second"],
        capture_output=True,
        check=True,
    )

    # Detached HEAD checkout
    result = subprocess.run(
        ["git", "-C", test_dir, "rev-parse", "HEAD~1"],
        capture_output=True,
        text=True,
        check=True,
    )
    parent_hash = result.stdout.strip()

    subprocess.run(
        ["git", "-C", test_dir, "checkout", parent_hash],
        capture_output=True,
        check=True,
    )

    branch = get_current_branch(test_dir)
    print(f"  get_current_branch('{test_dir}') = {branch!r}")
    if branch == "DETACHED":
        print("  [PASS] Detached HEAD helyesen 'DETACHED'-kent detektalva")
        return True
    else:
        print(f"  [FAIL] Varhato: 'DETACHED', kapott: {branch!r}")
        return False


def test_timeout():
    """4. Timeout — nagy repo-nal < 10s-e a scan?"""
    print("\n=== TEST 4: Timeout (nagy repo scan sebesseg) ===")
    hermes_path = "/home/github/projects/hermes"

    start = time.perf_counter()
    result = scan_project(hermes_path, "hermes")
    elapsed = time.perf_counter() - start

    print(f"  scan_project('hermes') ido: {elapsed:.3f}s")
    print(f"  Projekt neve: {result.get('project')}")
    print(f"  Branch: {result.get('branch')}")
    print(f"  Commit count: {len(result.get('last_commit', {}))}")

    if elapsed < 10.0:
        print(f"  [PASS] Scan ido {elapsed:.3f}s < 10s")
        return True
    else:
        print(f"  [FAIL] Scan ido {elapsed:.3f}s >= 10s (tul lassu)")
        return False


def test_graceful_fallback_scan_all():
    """Bonusz: scan_all_projects graceful fallback — nem-git mappa mellett is mukodik?"""
    print("\n=== TEST 5: scan_all_projects graceful fallback ===")
    start = time.perf_counter()
    results = scan_all_projects()
    elapsed = time.perf_counter() - start

    print(f"  scan_all_projects() ido: {elapsed:.3f}s")
    print(f"  Talalt projektek: {len(results)}")

    # Ellenorizzuk, hogy nincs nem-git-proj a talalatokban
    names = [r.get("project") for r in results]
    has_bad = "nem-git-proj" in names
    print(f"  'nem-git-proj' a talalatokban: {has_bad}")

    if not has_bad and len(results) > 0:
        print("  [PASS] scan_all_projects helyesen skipeli a nem-git mappakat")
        return True
    else:
        print("  [FAIL] Problema a scan_all_projects-szal")
        return False


def main():
    print("=" * 60)
    print("Project Manager — Edge Case Tesztek")
    print("=" * 60)

    results = []
    results.append(("Nem-git mappa skip", test_non_git_directory()))
    results.append(("Nincs remote", test_no_remote()))
    results.append(("Detached HEAD", test_detached_head()))
    results.append(("Timeout / sebesseg", test_timeout()))
    results.append(("Graceful fallback", test_graceful_fallback_scan_all()))

    print("\n" + "=" * 60)
    print("OSSZEFOGLALO")
    print("=" * 60)
    passed = 0
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if ok:
            passed += 1

    print(f"\n  Osszesen: {passed}/{len(results)} teszt sikeres")

    if passed == len(results):
        print("\n  Minden edge case graceful fallback-kel kezelodik! [OK]")
        return 0
    else:
        print(f"\n  {len(results) - passed} teszt FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(main())
