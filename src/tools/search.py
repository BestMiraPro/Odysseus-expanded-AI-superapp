"""Search-domain tool implementations.

Extracted from tool_implementations.py as part of slice 1 (#4082/#4071).
Holds the search_chats tool.
``src.tool_implementations`` re-exports these for backward compatibility.
"""
import logging
from typing import Dict

logger = logging.getLogger(__name__)


async def do_search_chats(query: str, limit: int = 20, owner: str | None = None) -> Dict:
    """Search past session transcripts for the calling user's sessions only.

    Without an owner filter this used to leak EVERY user's chat history
    into the agent's `search_chats` results (v2 review HIGH-11). The
    caller in `tool_execution.execute_tool_block` now plumbs the owner
    through; legacy callers without owner pass through as before but
    will only see legacy/null-owner rows.
    """
    try:
        from src.session_search import search_session_messages

        results = search_session_messages(query, limit=limit, owner=owner)
        if not results:
            return {"results": f"No chats found matching \"{query}\"."}

        # Group by session to avoid duplicate links
        seen_sessions = {}
        for result in results:
            if result.session_id not in seen_sessions:
                seen_sessions[result.session_id] = result

        lines = [f"Found {len(seen_sessions)} session(s) matching \"{query}\":\n"]
        for sid, result in seen_sessions.items():
            lines.append(f"- [**{result.session_name}**](#session-{sid})")
            lines.append(f"  Open: [Open chat](#session-{sid})")
            lines.append(f"  Match ({result.role}): {result.content_snippet}")
            if result.context_before:
                before = result.context_before[-1]
                lines.append(f"  Before ({before['role']}): {before['content'][:180]}")
            if result.context_after:
                after = result.context_after[0]
                lines.append(f"  After ({after['role']}): {after['content'][:180]}")
            lines.append("")

        return {"results": "\n".join(lines)}
    except Exception as e:
        logger.error(f"search_chats failed: {e}")
        return {"error": str(e), "exit_code": 1}


def _history_args(content: str, key: str) -> Dict:
    """Tool content is the JSON args object, or plain text for the main field."""
    import json

    raw = (content or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {key: raw.split("\n")[0].strip()}


async def do_search_history(content: str, owner: str | None = None) -> Dict:
    """Search the user's imported claude.ai / ChatGPT / Claude Code / Codex history."""
    import asyncio

    from src import conversation_archive as archive

    args = _history_args(content, "query")
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "search_history needs a query", "exit_code": 1}
    source = args.get("source") or args.get("sources") or []
    sources = [source] if isinstance(source, str) else list(source)
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    try:
        results = await asyncio.to_thread(archive.search, owner, query, sources=sources, limit=limit)
        if not results:
            total = (await asyncio.to_thread(archive.stats, owner))["total"]
            if not total:
                return {"results": "The conversation archive is empty: no claude.ai, ChatGPT, Claude Code "
                                   "or Codex history has been imported yet (Council -> History)."}
        return {"results": archive.format_search_results(results)}
    except Exception as e:
        logger.error(f"search_history failed: {e}")
        return {"error": str(e), "exit_code": 1}


async def do_read_history(content: str, owner: str | None = None) -> Dict:
    """Read one archived conversation (paged)."""
    import asyncio

    from src import conversation_archive as archive

    args = _history_args(content, "conversation_id")
    conv_id = str(args.get("conversation_id") or args.get("id") or "").strip()
    if not conv_id:
        return {"error": "read_history needs a conversation_id from search_history", "exit_code": 1}
    try:
        start = int(args.get("start") or 0)
    except (TypeError, ValueError):
        start = 0
    conv = await asyncio.to_thread(archive.read, owner, conv_id, start=start, limit=30)
    return {"results": archive.format_conversation(conv)}
