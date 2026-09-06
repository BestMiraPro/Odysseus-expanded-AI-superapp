#!/usr/bin/env python3
"""Fail when requirements.lock.txt has drifted from requirements.txt.

Docker and the Linux CI lanes install the lock, not requirements.txt, so a
runtime dependency added to requirements.txt but never locked is simply absent
at runtime. That is not hypothetical: the September 5, 2026 review found 18
declared packages missing from a working checkout.

The lock is a full transitive resolution, so it is expected to be a superset of
requirements.txt. This only checks direction: every direct requirement must
appear in the lock.

Usage:
    python .github/scripts/check_lock_covers_requirements.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent.parent

# Strip anything that can follow a distribution name in a requirement line:
# extras, version specifiers, environment markers, and URL forms.
_NAME = re.compile(r"^([A-Za-z0-9._-]+)")


def normalise(name: str) -> str:
    """PEP 503 name normalisation: lowercase, runs of -_. become a single -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(line: str):
    """Return the normalised distribution name, or None for a non-requirement."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    match = _NAME.match(line)
    if not match:
        return None
    return normalise(match.group(1))


def _names(path: Path) -> list:
    return [
        name
        for name in (
            requirement_name(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        )
        if name
    ]


def missing_from_lock(requirements: Path, lock: Path) -> list:
    """Direct requirements that the lock does not pin, in file order."""
    locked = set(_names(lock))
    seen, missing = set(), []
    for name in _names(requirements):
        if name not in locked and name not in seen:
            seen.add(name)
            missing.append(name)
    return missing


def main(requirements: Path = None, lock: Path = None) -> int:
    requirements = requirements or REPO / "requirements.txt"
    lock = lock or REPO / "requirements.lock.txt"

    if not lock.exists():
        print(f"error: {lock.name} is missing. Run scripts/lock_requirements.sh.")
        return 1

    missing = missing_from_lock(requirements, lock)
    if missing:
        print(f"error: {lock.name} is stale — these are in {requirements.name} "
              f"but not locked:")
        for name in missing:
            print(f"  - {name}")
        print("\nRun scripts/lock_requirements.sh and commit the result.")
        return 1

    print(f"{lock.name} covers all {len(set(_names(requirements)))} runtime "
          f"requirements.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
