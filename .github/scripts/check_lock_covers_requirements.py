#!/usr/bin/env python3
"""Check requirements.lock.txt against requirements.txt.

Docker and the Linux CI lanes install the lock, not requirements.txt, so drift
between the two is invisible at runtime. That is not hypothetical: the
September 5, 2026 review found 18 declared packages missing from a working
checkout.

Three distinct questions, deliberately reported separately:

1. **Name coverage** — is every direct requirement pinned at all?
2. **Constraint compatibility** — does the pin satisfy the declared specifier?
   Comparing names alone let ``package>=1`` become ``package>=2`` while the
   lock still pinned ``package==1.5``.
3. **Full resolver freshness** — whether the lock is the resolution pip would
   produce today. Only a real resolve answers that; this script does not, and
   says so. Extras are flagged for that deeper check because a lightweight
   comparison cannot prove an extra's transitive closure is present.

The lock is a full transitive resolution, so it is expected to be a superset of
requirements.txt. Only the direction "every direct requirement is satisfied" is
checked.

Usage:
    python .github/scripts/check_lock_covers_requirements.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.markers import UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


REPO = Path(__file__).resolve().parent.parent.parent

# The runtime the lock is generated for (scripts/lock_requirements.sh runs
# inside python:3.14-slim on linux/amd64). Markers are evaluated against this,
# so a requirement excluded here is intentionally skipped rather than reported
# as an unpinned dependency.
TARGET_ENVIRONMENT = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "os_name": "posix",
    "python_version": "3.14",
    "python_full_version": "3.14.0",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "extra": "",
}


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return canonicalize_name(name)


def requirement_name(line: str):
    """Return the normalised distribution name, or None for a non-requirement.

    Kept as a thin helper so callers that only need the name (and lines the
    full parser rejects) still work.
    """
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    match = re.match(r"^([A-Za-z0-9._-]+)", line)
    return normalise(match.group(1)) if match else None


def _requirement_lines(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            yield line


def parse_requirement(line: str):
    """Return a Requirement, or None when the line cannot be parsed as one."""
    try:
        return Requirement(line)
    except InvalidRequirement:
        return None


def applies_to_target(req: Requirement) -> bool:
    """Is this requirement installed on the runtime the lock targets?"""
    if req.marker is None:
        return True
    try:
        return req.marker.evaluate(TARGET_ENVIRONMENT)
    except UndefinedEnvironmentName:
        # An unknown marker variable: assume it applies rather than silently
        # dropping a dependency from the check.
        return True


def _requirements(path: Path):
    for line in _requirement_lines(path):
        req = parse_requirement(line)
        if req is not None and applies_to_target(req):
            yield req


def locked_versions(lock: Path) -> dict:
    """Map canonical name -> pinned Version for every ``name==version`` line."""
    pinned = {}
    for line in _requirement_lines(lock):
        req = parse_requirement(line)
        if req is None:
            continue
        for spec in req.specifier:
            if spec.operator in ("==", "==="):
                try:
                    pinned[normalise(req.name)] = Version(spec.version)
                except InvalidVersion:
                    pass
                break
    return pinned


def missing_from_lock(requirements: Path, lock: Path) -> list:
    """Direct requirements the lock does not pin at all, in file order."""
    locked = set(locked_versions(lock))
    seen, missing = set(), []
    for req in _requirements(requirements):
        name = normalise(req.name)
        if name not in locked and name not in seen:
            seen.add(name)
            missing.append(name)
    return missing


def incompatible_with_lock(requirements: Path, lock: Path) -> list:
    """Requirements whose declared specifier the pinned version violates."""
    pinned = locked_versions(lock)
    problems = []
    for req in _requirements(requirements):
        name = normalise(req.name)
        version = pinned.get(name)
        if version is None:
            continue                      # missing_from_lock reports this
        if not req.specifier:
            continue                      # unconstrained: any pin is fine
        if not req.specifier.contains(version, prereleases=True):
            problems.append(
                f"{name}: requirements.txt asks for '{req.specifier}' "
                f"but the lock pins {version}"
            )
    return problems


def extras_needing_resolution(requirements: Path) -> list:
    """Requirements declaring extras, which need a real resolve to verify.

    An extra pulls in its own dependency set; comparing top-level names cannot
    show whether that set is present in the lock.
    """
    found = []
    for req in _requirements(requirements):
        if req.extras:
            found.append((normalise(req.name), sorted(req.extras)))
    return found


def unsupported_forms(requirements: Path) -> list:
    """Requirement forms this check cannot reason about."""
    problems = []
    for line in _requirement_lines(requirements):
        req = parse_requirement(line)
        if req is None:
            problems.append(f"{line!r}: could not be parsed as a requirement")
            continue
        if req.url:
            problems.append(
                f"{normalise(req.name)}: URL requirements are not verifiable "
                f"against a version lock ({req.url})"
            )
    return problems


def main(requirements: Path = None, lock: Path = None) -> int:
    requirements = requirements or REPO / "requirements.txt"
    lock = lock or REPO / "requirements.lock.txt"

    if not lock.exists():
        print(f"error: {lock.name} is missing. Run scripts/lock_requirements.sh.")
        return 1

    failed = False

    unsupported = unsupported_forms(requirements)
    if unsupported:
        failed = True
        print(f"error: {requirements.name} uses forms this check cannot verify:")
        for problem in unsupported:
            print(f"  - {problem}")

    missing = missing_from_lock(requirements, lock)
    if missing:
        failed = True
        print(f"error: {lock.name} does not pin these direct requirements:")
        for name in missing:
            print(f"  - {name}")

    incompatible = incompatible_with_lock(requirements, lock)
    if incompatible:
        failed = True
        print(f"error: {lock.name} violates a declared constraint:")
        for problem in incompatible:
            print(f"  - {problem}")

    if failed:
        print("\nRun scripts/lock_requirements.sh and commit the result.")
        return 1

    total = len({normalise(r.name) for r in _requirements(requirements)})
    print(f"{lock.name}: all {total} applicable runtime requirements are pinned "
          f"and satisfy their declared constraints.")

    extras = extras_needing_resolution(requirements)
    if extras:
        print("\nnote: these declare extras, whose transitive closure only a "
              "real resolve can verify:")
        for name, names in extras:
            print(f"  - {name}[{','.join(names)}]")
    print("note: this checks pins against constraints, not that the lock is "
          "the resolution pip would produce today. The docker-build lane "
          "installs the lock on the shipped runtime.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
