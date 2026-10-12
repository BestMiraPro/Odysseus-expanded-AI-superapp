"""Tools for Council seats: what each member may do, and the engine that runs it.

Two levels (per council):

- ``research`` (default): web search and fetch, read and search files in the
  workspace, the user's conversation history (claude.ai, ChatGPT, Claude Code,
  Codex) and their Odysseus chats.
- ``full``: everything in research plus shell commands, Python and file
  edits in the workspace, like Claude Code / Codex on a laptop. Only an admin
  (or a single-user install) can turn it on.

``off`` keeps the plain text-only council.

The engine follows the seat:

- Claude Subscription -> the Claude Code CLI with its own tools, plus the
  Odysseus context MCP server for the history.
- ChatGPT Subscription -> the Codex CLI (``codex exec``) with live web search,
  its sandbox, and the same MCP server.
- Any other endpoint (API, local) -> the Odysseus agent loop restricted to the
  matching built-in tools.

Every engine yields Odysseus SSE chunks, including ``tool_start`` /
``tool_output`` events, so ``council.run_member`` treats them alike.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOLS_OFF = "off"
TOOLS_RESEARCH = "research"
TOOLS_FULL = "full"
TOOL_MODES = (TOOLS_OFF, TOOLS_RESEARCH, TOOLS_FULL)

APP_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_SERVER = APP_ROOT / "mcp_servers" / "council_context_server.py"
MCP_NAME = "odysseus"
MCP_TOOLS = ("search_history", "read_history", "search_odysseus_chats")

# Claude Code built-in tools per level.
CLAUDE_RESEARCH_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep", "TodoWrite"]
CLAUDE_FULL_TOOLS = CLAUDE_RESEARCH_TOOLS + ["Bash", "Edit", "Write", "NotebookEdit"]

# Odysseus agent-loop tools per level (API and local seats).
AGENT_RESEARCH_TOOLS = frozenset({
    "web_search", "web_fetch", "read_file", "grep", "glob", "ls", "get_workspace",
    "search_chats", "search_history", "read_history", "todowrite",
})
AGENT_FULL_TOOLS = AGENT_RESEARCH_TOOLS | {"bash", "python", "write_file", "edit_file", "apply_patch"}

AGENT_MAX_ROUNDS = 12


@dataclass
class ToolSettings:
    mode: str = TOOLS_RESEARCH
    owner: Optional[str] = None
    workspace: Optional[str] = None      # vetted directory, or None

    @property
    def enabled(self) -> bool:
        return self.mode in (TOOLS_RESEARCH, TOOLS_FULL)

    @property
    def full(self) -> bool:
        return self.mode == TOOLS_FULL


def normalize_mode(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in TOOL_MODES else TOOLS_RESEARCH


# ── Prompt ──

def tools_brief(settings: ToolSettings, stage: str) -> str:
    """What a member can do, appended to its system prompt."""
    if not settings.enabled:
        return ""
    lines = [
        "\n\nYou have tools. Use them whenever they would make the answer more correct or more specific; "
        "skip them for questions you can answer well from knowledge alone:",
        "- Web search and page fetch for current facts, prices, releases, documentation. Cite the URLs you rely on.",
        "- search_history / read_history: the user's own past conversations with Claude (claude.ai), ChatGPT, "
        "Claude Code and Codex. Use them when the question refers to the user's earlier discussions, plans, "
        "decisions or projects. Mention which conversation you drew on.",
        "- Searching the user's chats inside Odysseus.",
    ]
    if settings.workspace:
        lines.append(f"- Reading and searching files in the workspace {settings.workspace}.")
    if settings.full:
        lines.append("- Running shell commands and Python, and editing files"
                     + (" in the workspace." if settings.workspace else "."))
    if stage == "review":
        lines.append("As a reviewer, use the tools to check claims you doubt instead of guessing.")
    lines.append("Finish with your complete answer as plain text after the last tool call; "
                 "text written before a tool call is treated as working notes.")
    return "\n".join(lines)


def with_tools_brief(messages: List[Dict[str, Any]], settings: ToolSettings, stage: str) -> List[Dict[str, Any]]:
    brief = tools_brief(settings, stage)
    if not brief:
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = f"{m.get('content') or ''}{brief}"
            return out
    return [{"role": "system", "content": brief.strip()}, *out]


# ── MCP server for the CLIs ──

def mcp_server_spec(owner: Optional[str]) -> Dict[str, Any]:
    from core.database import DATABASE_URL

    env = {
        "ODYSSEUS_MCP_OWNER": owner or "",
        "PYTHONPATH": str(APP_ROOT),
        "DATABASE_URL": DATABASE_URL,
    }
    for key in ("ODYSSEUS_DATA_DIR", "DATA_DIR", "HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {"command": sys.executable, "args": [str(CONTEXT_SERVER)], "env": env}


def claude_native_tools(settings: ToolSettings):
    from src.claude_subscription import NativeTools

    builtin = list(CLAUDE_FULL_TOOLS if settings.full else CLAUDE_RESEARCH_TOOLS)
    allowed = builtin + [f"mcp__{MCP_NAME}__{t}" for t in MCP_TOOLS]
    config = json.dumps({"mcpServers": {MCP_NAME: mcp_server_spec(settings.owner)}})
    return NativeTools(builtin=builtin, allowed=allowed, mcp_config=config, cwd=settings.workspace)


# ── Engines ──

def _bearer(headers: Optional[Dict[str, str]]) -> str:
    for key, value in (headers or {}).items():
        if key.lower() == "authorization" and str(value).lower().startswith("bearer "):
            return str(value)[7:].strip()
    return ""


async def _claude_stream(url, model, messages, headers, settings: ToolSettings, timeout) -> AsyncIterator[str]:
    from src import reasoning_effort
    from src.claude_subscription import stream_chat
    from src.llm_core import _sanitize_llm_messages

    effort, _ = reasoning_effort.resolve("claude-subscription", url, model, headers)
    async for chunk in stream_chat(url, model, _sanitize_llm_messages(messages), timeout=timeout,
                                   effort=effort, tools=claude_native_tools(settings)):
        yield chunk


async def _codex_stream(url, model, messages, headers, settings: ToolSettings, timeout) -> AsyncIterator[str]:
    from src import codex_cli, reasoning_effort
    from src.claude_subscription import build_prompt

    effort, headers = reasoning_effort.resolve("chatgpt-subscription", url, model, headers)
    system, prompt = build_prompt(messages)
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    run = codex_cli.CodexRun(
        model=model,
        prompt=full_prompt,
        access_token=_bearer(headers),
        sandbox=codex_cli.SANDBOX_FULL if settings.full else codex_cli.SANDBOX_READ_ONLY,
        cwd=settings.workspace,
        effort=effort,
        mcp_servers={MCP_NAME: mcp_server_spec(settings.owner)},
    )
    async for chunk in codex_cli.stream(run, timeout=timeout):
        yield chunk


async def _agent_stream(url, model, messages, headers, settings: ToolSettings, workload) -> AsyncIterator[str]:
    """Odysseus agent loop limited to the council's tools."""
    from src.agent_loop import stream_agent_loop
    from src.tool_policy import ToolPolicy, known_tool_names

    allowed = set(AGENT_FULL_TOOLS if settings.full else AGENT_RESEARCH_TOOLS)
    if not settings.workspace:
        allowed -= {"read_file", "grep", "glob", "ls", "get_workspace", "write_file", "edit_file", "apply_patch"}
    blocked = frozenset(known_tool_names() - allowed)
    policy = ToolPolicy(disabled_tools=blocked, mode="council", disable_mcp=True)
    # Nobody can answer an approval card mid-council. Full tools are the
    # admin's go-ahead for the whole run, as for the Claude Code and Codex
    # seats; in research mode a call the external-context gate holds back is
    # refused and the member carries on.
    gated = "allow" if settings.full else "refuse"
    # Shell and Python start in the workspace, else in a scratch folder of
    # their own rather than the app's data folder.
    scratch = None if settings.workspace or not settings.full else tempfile.mkdtemp(prefix="ody-council-")
    # The agent loop bills each model call to the budget ledger itself.
    yield f"data: {json.dumps({'type': 'self_billed'})}\n\n"
    try:
        async for chunk in stream_agent_loop(
            endpoint_url=url,
            model=model,
            messages=messages,
            headers=headers,
            max_rounds=AGENT_MAX_ROUNDS,
            owner=settings.owner,
            relevant_tools=allowed,
            tool_policy=policy,
            workspace=settings.workspace or scratch,
            workload=workload,
            gated_actions=gated,
        ):
            yield _approval_refused(chunk, settings.owner)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)


def _approval_refused(chunk: str, owner: Optional[str]) -> str:
    """A tool that needs the user's approval cannot get it inside a council run.

    Retire the pending grant (nobody can answer it) and show the call as
    refused, the same way scheduled tasks handle it.
    """
    if not chunk.startswith("data: ") or '"tool_approval"' not in chunk:
        return chunk
    try:
        data = json.loads(chunk[6:])
    except ValueError:
        return chunk
    approval = data.get("ask_user") if isinstance(data, dict) else None
    if not (isinstance(approval, dict) and approval.get("kind") == "tool_approval"):
        return chunk
    try:
        from src.tool_approvals import tool_approval_store

        tool_approval_store.consume(approval.get("approval_id"), decision="deny", owner=owner, session_id=None)
    except Exception:
        pass
    data.pop("ask_user", None)
    data["output"] = "Refused: this action needs approval, which a council run cannot ask for."
    data["exit_code"] = 1
    return f"data: {json.dumps(data)}\n\n"


def stream_fn_for(settings: ToolSettings):
    """A ``council.run_member`` stream function that gives members tools."""
    from src.llm_core import _detect_provider, stream_llm

    async def fn(url: str, model: str, messages: List[Dict[str, Any]], *, headers=None,
                 timeout: int = 600, workload: str = "foreground", stage: str = "opinions"):
        if not settings.enabled:
            async for chunk in stream_llm(url, model, messages, headers=headers, timeout=timeout,
                                          workload=workload):
                yield chunk
            return
        msgs = with_tools_brief(messages, settings, stage)
        provider = _detect_provider(url)
        if provider == "claude-subscription":
            source = _claude_stream(url, model, msgs, headers, settings, timeout)
        elif provider == "chatgpt-subscription":
            source = _codex_stream(url, model, msgs, headers, settings, timeout)
        else:
            source = _agent_stream(url, model, msgs, headers, settings, workload)
        async for chunk in source:
            yield chunk

    fn.accepts_stage = True
    return fn
