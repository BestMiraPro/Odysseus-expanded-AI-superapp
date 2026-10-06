"""The generic TOOL_HANDLERS fallback must carry owner/session ctx.

A registry tool without an explicit dispatch branch reaches its handler via
the `elif tool in dynamic_handlers` fallback in tool_execution. That branch
called the handler without owner/session_id, so an owner-scoped tool ran
unscoped (ctx owner None reads as "auth disabled" to most impls).
"""
import asyncio
import importlib

from src.agent_tools import ToolBlock  # noqa: E402  (import first to avoid circular)
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block


def test_dynamic_registry_fallback_threads_owner_and_session(monkeypatch):
    seen = {}

    async def probe(content, ctx):
        seen.update(content=content, owner=ctx.get("owner"), session_id=ctx.get("session_id"))
        return {"output": "ok", "exit_code": 0}

    # Resolve the live module (other tests may reload it) — the dispatcher
    # looks TOOL_HANDLERS up from sys.modules at call time.
    agent_tools = importlib.import_module("src.agent_tools")
    monkeypatch.setitem(agent_tools.TOOL_HANDLERS, "registry_probe_tool", probe)

    desc, result = asyncio.run(execute_tool_block(
        ToolBlock("registry_probe_tool", "payload"),
        session_id="sess-1",
        owner="alice",
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))

    assert desc.startswith("registry: registry_probe_tool")
    assert result == {"output": "ok", "exit_code": 0}
    assert seen == {"content": "payload", "owner": "alice", "session_id": "sess-1"}
