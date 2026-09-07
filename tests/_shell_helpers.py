"""Explicit shell-interpreter selection for tests that spawn bash.

Windows ships ``C:\\Windows\\System32\\bash.exe``, the WSL launcher. With no
distribution installed it exits 1 and prints nothing, so a test that spawns
bare ``"bash"`` fails with an empty stderr and an exit code that looks like a
real assertion failure. Whether it is reached at all depends on PATH order in
the pytest process, which is why these failures looked host-specific and
arbitrary.

``core.platform_compat.find_bash`` already solves this for production code: it
rejects the System32/WindowsApps stubs and falls back to Git Bash. Tests use
the same resolution here, so they exercise the interpreter the application
would actually use.

``requires_bash`` skips only when the probe finds no usable interpreter. That
is a probed missing capability, not a blanket "skip on Windows" — the
distinction the review handoff asks for.
"""

from __future__ import annotations

import pytest

from core.platform_compat import find_bash

#: Absolute path to a real bash, or None when the host has none.
BASH = find_bash()

requires_bash = pytest.mark.skipif(
    BASH is None,
    reason="no usable bash interpreter on this host "
           "(the Windows WSL stub does not count)",
)


def bash_cmd(*args: str) -> list:
    """Build an argv for the resolved bash. Call only under ``requires_bash``."""
    if BASH is None:
        raise RuntimeError("bash_cmd() used without the requires_bash guard")
    return [BASH, *args]
