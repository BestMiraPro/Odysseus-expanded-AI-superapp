"""Native function calls to tail_serve_output must reach the dispatcher.

tail_serve_output has a native schema (FUNCTION_TOOL_SCHEMAS) and a dispatch
branch in tool_execution, but it was missing from TOOL_TAGS, so
function_call_to_tool_block rejected it as "Unknown function call" and the
cookbook crash-diagnosis step silently never ran.
"""
import json

from src.agent_tools import TOOL_TAGS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_parsing import _TOOL_NAME_MAP
from src.tool_security import BUILTIN_EMAIL_TOOLS


def test_tail_serve_output_native_call_maps_to_tool_block():
    assert "tail_serve_output" in TOOL_TAGS
    block = function_call_to_tool_block(
        "tail_serve_output", json.dumps({"session_id": "serve-abc12345", "tail": 150})
    )
    assert block is not None
    assert block.tool_type == "tail_serve_output"
    assert json.loads(block.content) == {"session_id": "serve-abc12345", "tail": 150}


def test_every_native_schema_is_a_known_tool_tag():
    names = [
        s["function"]["name"]
        for s in FUNCTION_TOOL_SCHEMAS
        if s.get("type") == "function"
    ]
    unknown = [
        n for n in names
        if _TOOL_NAME_MAP.get(n, n) not in TOOL_TAGS
        and n not in BUILTIN_EMAIL_TOOLS
        and not n.startswith("mcp__")
    ]
    assert unknown == []
