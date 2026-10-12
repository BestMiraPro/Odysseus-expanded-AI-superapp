"""
council_context_server.py

MCP server that gives the Claude Code and Codex CLIs (Council seats) the
user's context from Odysseus:

- search_history / read_history: the imported claude.ai, ChatGPT, Claude Code
  and Codex conversations (src/conversation_archive.py)
- search_odysseus_chats: chats held inside Odysseus

Started per run by the CLI with ODYSSEUS_MCP_OWNER set to the user the run
belongs to; every query is scoped to that owner.
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT))
# The CLI starts this from its own working directory; Odysseus resolves its
# data paths from the app root.
os.chdir(APP_ROOT)

server = Server("odysseus")


def _owner():
    owner = os.environ.get("ODYSSEUS_MCP_OWNER", "").strip()
    return owner or None


def _text(body: str) -> list[TextContent]:
    return [TextContent(type="text", text=body)]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_history",
            description=(
                "Search the user's own conversation history: claude.ai and ChatGPT chats (imported "
                "data exports) and Claude Code / Codex CLI sessions. Use when the question touches "
                "something the user discussed with Claude or ChatGPT or worked on in Claude Code or "
                "Codex. Returns matching messages with a conversation_id for read_history."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Words to find"},
                    "source": {"type": "string", "enum": ["claude_ai", "chatgpt", "claude_code", "codex"],
                               "description": "Limit to one source (optional)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="read_history",
            description="Read a conversation found with search_history, 30 messages at a time.",
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "start": {"type": "integer", "description": "First message number (default 0)"},
                },
                "required": ["conversation_id"],
            },
        ),
        Tool(
            name="search_odysseus_chats",
            description="Search the user's chats inside Odysseus (any model) by keyword.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    import json

    from src.tools.search import do_read_history, do_search_chats, do_search_history

    arguments = arguments or {}
    owner = _owner()
    if name == "search_history":
        result = await do_search_history(json.dumps(arguments), owner=owner)
    elif name == "read_history":
        result = await do_read_history(json.dumps(arguments), owner=owner)
    elif name == "search_odysseus_chats":
        result = await do_search_chats(str(arguments.get("query") or ""), owner=owner)
    else:
        return _text(f"Error: unknown tool {name}")
    return _text(str(result.get("results") or result.get("error") or "(no result)"))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
