"""Capability probes for tests that need real OS features.

Some security assertions can only be made where the OS provides the feature
being defended against: you cannot test that a symlink escape is rejected on a
host that cannot create a symlink, and you cannot test Unix-socket handling
where there are no Unix sockets.

These probe the capability rather than the platform name. That distinction
matters:

* the production check is never weakened — only the test is scoped;
* the tests still run wherever the feature exists. Windows *can* create
  symlinks given Developer Mode or an elevated process, and GitHub's Windows
  runners are elevated, so the CI lane added alongside this still exercises
  them. A ``sys.platform == "win32"`` skip would have silently given that up.

Reported failures were: ``WinError 1314`` (no SeCreateSymbolicLinkPrivilege)
and ``AttributeError: module 'socket' has no attribute 'AF_UNIX'``.
"""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest


def _probe_symlink() -> bool:
    """Actually try it: the privilege check is per-process, not per-platform."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.write_text("x", encoding="utf-8")
            (Path(tmp) / "link").symlink_to(target)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


CAN_SYMLINK = _probe_symlink()
HAS_UNIX_SOCKETS = hasattr(socket, "AF_UNIX")

requires_symlinks = pytest.mark.skipif(
    not CAN_SYMLINK,
    reason=(
        "this host cannot create symlinks (Windows needs Developer Mode or an "
        "elevated process; WinError 1314). The production confinement check is "
        "unchanged — only this assertion needs a host that can build the "
        "attack it defends against."
    ),
)

requires_unix_sockets = pytest.mark.skipif(
    not HAS_UNIX_SOCKETS,
    reason=(
        "socket.AF_UNIX does not exist on this platform; exercised by the "
        "Linux CI lane."
    ),
)


__all__ = [
    "CAN_SYMLINK",
    "HAS_UNIX_SOCKETS",
    "requires_symlinks",
    "requires_unix_sockets",
]
