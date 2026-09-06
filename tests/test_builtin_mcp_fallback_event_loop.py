"""The subprocess fallback must keep the application's event loop responsive."""

import asyncio
import subprocess
import threading

import pytest


@pytest.mark.asyncio
async def test_npx_fallback_allows_event_loop_to_progress(monkeypatch):
    from src import builtin_mcp

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    progressed = []

    async def unsupported(*args, **kwargs):
        raise NotImplementedError

    def run(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        progressed.append(release.wait(timeout=3))
        return subprocess.CompletedProcess(args[0], 0, stdout=b"1.0\n")

    async def other_request():
        await started.wait()
        release.set()

    monkeypatch.setattr(builtin_mcp, "_is_package_in_npx_cache", lambda spec: False)
    monkeypatch.setattr(builtin_mcp.asyncio, "create_subprocess_exec", unsupported)
    monkeypatch.setattr(builtin_mcp.subprocess, "run", run)
    other = asyncio.create_task(other_request())
    try:
        assert await builtin_mcp._is_npx_package_cached("npx", "example", timeout_s=5)
        await asyncio.wait_for(other, timeout=3)
        assert progressed == [True], "npx fallback blocked unrelated async requests"
    finally:
        release.set()
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)
