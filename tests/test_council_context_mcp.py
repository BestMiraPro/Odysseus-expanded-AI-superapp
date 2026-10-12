"""The council context MCP server answers over stdio, scoped to its owner."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_servers" / "council_context_server.py"


def _seed(db_url: str) -> None:
    """Import one claude.ai conversation for 'dinis' in a fresh database."""
    code = f"""
import json, os
os.environ["DATABASE_URL"] = {db_url!r}
from src import conversation_archive as ca
data = [{{"uuid": "c-1", "name": "Farm plan", "chat_messages": [
    {{"sender": "human", "text": "olive grove prices in Alto Alentejo"}},
    {{"sender": "assistant", "text": "About 10k EUR per hectare."}}]}}]
ca.store("dinis", ca.parse_conversations_json(data))
"""
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True,
                   env={**os.environ, "DATABASE_URL": db_url, "PYTHONPATH": str(ROOT)})


def test_mcp_server_search_and_read(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db_url = f"sqlite:///{tmp_path / 'app.db'}"
    _seed(db_url)

    async def run(owner: str):
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)],
            env={**os.environ, "DATABASE_URL": db_url, "ODYSSEUS_MCP_OWNER": owner,
                 "PYTHONPATH": str(ROOT)},
            cwd=str(tmp_path),
        )
        # The server's stderr goes nowhere: an inherited captured stream can
        # fail to start the child on Windows (see test_subprocess_stream_hygiene).
        with open(os.devnull, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    found = await session.call_tool("search_history", {"query": "olive grove"})
                    text = found.content[0].text
                    conv_id = None
                    for part in text.split():
                        if part.startswith("id="):
                            conv_id = part[3:]
                    read_text = ""
                    if conv_id:
                        read_res = await session.call_tool("read_history", {"conversation_id": conv_id})
                        read_text = read_res.content[0].text
                    return tools, text, read_text

    tools, text, read_text = asyncio.run(run("dinis"))
    assert tools == {"search_history", "read_history", "search_odysseus_chats"}
    assert "Farm plan" in text
    assert "About 10k EUR per hectare." in read_text

    _, other, _ = asyncio.run(run("someone-else"))
    assert "Farm plan" not in other
