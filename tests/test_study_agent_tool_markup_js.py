"""Tool calls a model writes as text must not stay on screen as raw JSON.

Models without native tool calls (ChatGPT Subscription, Claude Subscription)
write ```tool_call fences or <invoke> markup. The server parses and runs them,
but the tutor had already streamed that text into the reply, so every call sat
in the chat as a JSON box until a reload.
"""

from __future__ import annotations

import json

from tests._study_js_harness import REPO, extract_const, needs_node, run_js

pytestmark = needs_node

AGENT_JS = REPO / "static" / "js" / "studyAgent.js"


def _hide(text: str) -> str:
    prelude = extract_const("TOOL_MARKUP", AGENT_JS.read_text(encoding="utf-8"))
    epilogue = f"console.log(JSON.stringify(hideToolMarkup({json.dumps(text)})));"
    return run_js(prelude, "hideToolMarkup", epilogue=epilogue, source_path=AGENT_JS)


def test_fenced_tool_call_is_hidden():
    text = ('Let me check the exam.\n```tool_call\n{"name":"search_materials",'
            '"arguments":{"query":"Corn Laws"}}\n```\nDone.')
    assert _hide(text) == "Let me check the exam.\n\nDone."


def test_invoke_and_tool_call_tags_are_hidden():
    text = ('Reading.\n<function_calls><invoke name="get_material"><parameter name="page">1'
            '</parameter></invoke></function_calls>\n<tool_call>{"name":"list_subjects"}</tool_call>')
    assert _hide(text) == "Reading."


def test_a_block_still_streaming_is_hidden():
    assert _hide('Looking.\n```tool_call\n{"name":"search_materials","argu') == "Looking."


def test_a_reply_that_is_only_a_tool_call_renders_nothing():
    assert _hide('```tool_call\n{"name":"get_material","arguments":{}}\n```') == ""


def test_ordinary_code_blocks_are_kept():
    text = "Example:\n```python\nprint('hi')\n```"
    assert _hide(text) == text
