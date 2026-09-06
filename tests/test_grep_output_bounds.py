"""Grep must bound what it reads, not just what it returns.

``--max-count`` caps matches *per file*. With ``capture_output=True`` the whole
cross-file result set was buffered in memory before Python sliced it down to
``max_results``, so a broad pattern over a large tree allocated hundreds of
megabytes to return 200 lines.
"""

import json
import shutil
import tracemalloc

import pytest

from src.agent_tools.filesystem_tools import _CODENAV_MAX_HITS, GrepTool
from src.tool_execution import _active_workspace


FILES = 400
LINES_PER_FILE = 220          # > _CODENAV_MAX_HITS, so the per-file cap binds
FILLER = "x" * 60


def _corpus(tmp_path):
    """~400 files that each match far more than the global result cap."""
    body = "".join(f"needle {i} {FILLER}\n" for i in range(LINES_PER_FILE))
    for n in range(FILES):
        (tmp_path / f"f{n:04d}.txt").write_text(body, encoding="utf-8")
    # Full capture would be ~400 * 200 * ~80B ≈ 6.4 MB of stdout.
    return tmp_path


async def _grep(path, **extra):
    args = {"pattern": "needle", "path": str(path)}
    args.update(extra)
    token = _active_workspace.set(str(path))
    try:
        return await GrepTool().execute(json.dumps(args), {})
    finally:
        _active_workspace.reset(token)


@pytest.mark.asyncio
async def test_large_tree_search_does_not_buffer_every_match(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    root = _corpus(tmp_path)

    tracemalloc.start()
    try:
        result = await _grep(root)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["exit_code"] == 0
    # Correctness is unchanged: still capped at the documented result limit.
    # (The trailing cap notice is clipped by the final _truncate here; the
    # small-result case below asserts the notice itself.)
    assert result["output"].count("needle") <= _CODENAV_MAX_HITS + 1
    # The bound: reading stops near the cap instead of draining ~6.4 MB.
    assert peak < 2_000_000, f"grep buffered {peak/1e6:.1f} MB to return 200 lines"


@pytest.mark.asyncio
async def test_result_cap_is_still_exact(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    for n in range(5):
        (tmp_path / f"g{n}.txt").write_text("needle a\nneedle b\nneedle c\n", encoding="utf-8")

    result = await _grep(tmp_path, max_results=4)
    lines = [ln for ln in result["output"].splitlines() if "needle" in ln]
    assert len(lines) == 4
    assert "capped at 4 matches" in result["output"]


@pytest.mark.asyncio
async def test_small_result_set_is_returned_whole(tmp_path):
    """Under the cap, nothing is truncated and no cap notice is added."""
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "one.txt").write_text("alpha\nneedle here\nbeta\n", encoding="utf-8")

    result = await _grep(tmp_path)
    assert result["exit_code"] == 0
    assert "needle here" in result["output"]
    assert "capped at" not in result["output"]


@pytest.mark.asyncio
async def test_invalid_regex_still_reports_an_error(tmp_path):
    """Early termination must not mask a genuine ripgrep failure."""
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "one.txt").write_text("hello\n", encoding="utf-8")

    result = await _grep(tmp_path, pattern="[")
    assert result["exit_code"] == 1
    assert "error" in result


@pytest.mark.asyncio
async def test_no_matches_is_not_an_error(tmp_path):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "one.txt").write_text("hello\n", encoding="utf-8")

    result = await _grep(tmp_path, pattern="absent")
    assert result["exit_code"] == 0
    assert "No matches" in result["output"]


@pytest.mark.asyncio
async def test_a_single_enormous_line_is_bounded(tmp_path):
    """One pathological line must not be read into memory in full."""
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    (tmp_path / "huge.txt").write_text("needle " + ("y" * 8_000_000) + "\n", encoding="utf-8")

    tracemalloc.start()
    try:
        result = await _grep(tmp_path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["exit_code"] == 0
    assert peak < 4_000_000, f"grep read {peak/1e6:.1f} MB for one line"


# --------------------------------------------------------------------------
# Process-level behaviour of the bounded reader
#
# These drive _run_ripgrep_bounded with stand-in producers rather than
# ripgrep, so the timeout, cap-kill, and stderr paths are exercised
# deterministically and without needing a pathological corpus on disk.
# --------------------------------------------------------------------------

_PRODUCER = (
    "import sys\n"
    "for i in range(1000000):\n"
    "    sys.stdout.write('f.txt:%d:hit\\n' % i)\n"
    "    sys.stdout.flush()\n"
)

_FAILER = "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"

_NOISY_STDERR = (
    "import sys\n"
    "sys.stderr.write('warn: skipped\\n' * 200000)\n"
    "sys.stdout.write('f.txt:1:hit\\n')\n"
)

def test_a_hung_search_times_out(monkeypatch):
    """A producer that never finishes is killed and reported, not awaited."""
    import sys
    from src.agent_tools import filesystem_tools as ft

    monkeypatch.setattr(ft, "_CODENAV_RG_TIMEOUT", 1)
    # Emits nothing and never exits, so only the timer can end the run.
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    lines, err = ft._run_ripgrep_bounded(cmd, max_hits=200)
    assert lines is None
    assert err == "grep: timed out"


def test_a_slow_producer_returns_once_the_cap_is_reached(monkeypatch):
    """Hitting the cap ends the run immediately; the process is not drained."""
    import sys
    import time
    from src.agent_tools import filesystem_tools as ft

    monkeypatch.setattr(ft, "_CODENAV_RG_TIMEOUT", 30)
    cmd = [sys.executable, "-c", _PRODUCER]

    started = time.monotonic()
    lines, err = ft._run_ripgrep_bounded(cmd, max_hits=10)
    elapsed = time.monotonic() - started

    assert err is None
    assert len(lines) == 10
    assert lines[0] == "f.txt:0:hit"
    assert elapsed < 20, "cap did not stop the read early"


def test_a_failing_process_reports_its_stderr(monkeypatch):
    import sys
    from src.agent_tools import filesystem_tools as ft

    cmd = [sys.executable, "-c", _FAILER]
    lines, err = ft._run_ripgrep_bounded(cmd, max_hits=200)
    assert lines is None
    assert "boom" in err


def test_a_noisy_stderr_does_not_deadlock(monkeypatch):
    """stderr goes to a file; a pipe would fill and block the producer."""
    import sys
    from src.agent_tools import filesystem_tools as ft

    monkeypatch.setattr(ft, "_CODENAV_RG_TIMEOUT", 30)
    cmd = [sys.executable, "-c", _NOISY_STDERR]

    lines, err = ft._run_ripgrep_bounded(cmd, max_hits=200)
    assert err is None
    assert lines == ["f.txt:1:hit"]
