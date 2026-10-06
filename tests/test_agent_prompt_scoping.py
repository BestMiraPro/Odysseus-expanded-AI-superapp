"""The agent prompt carries only what the turn can use.

Found on a real run: "Remember this about me: I prefer metric units" got the
"Terminus local-machine mode" rules (which tell the model to avoid memory
tools) because ask_user/update_plan, present on every turn, sat in the
trigger set. The same prompt carried ~6k characters describing 31 Browser MCP
tools that were not offered, and the model answered about browser tools.
"""
from unittest.mock import MagicMock

import pytest

agent_loop = pytest.importorskip("src.agent_loop")
from src.mcp_manager import McpManager  # noqa: E402

TERMINUS = "Terminus local-machine mode"


def _text(out):
    return "\n".join(str(m.get("content") or "") for m in out)


def _build(relevant_tools, mcp_mgr=None, text="Remember this about me: I prefer metric units."):
    agent_loop.reset_base_prompt_cache()
    out, _ = agent_loop._build_system_prompt(
        messages=[{"role": "user", "content": text}],
        model="test-model", active_document=None, mcp_mgr=mcp_mgr, owner=None,
        relevant_tools=relevant_tools,
    )
    return out


def test_loop_primitives_do_not_switch_on_local_machine_rules():
    out = _build({"manage_memory", "ask_user", "update_plan", "web_search"})
    assert TERMINUS not in _text(out)


def test_file_tools_still_get_local_machine_rules():
    out = _build({"bash", "read_file", "ask_user"})
    assert TERMINUS in _text(out)


def _fake_mgr():
    mgr = MagicMock()
    mgr.get_tool_descriptions_for_prompt = MagicMock(return_value="\n\nmcp__srv__do_thing: Does it.")
    mgr.get_all_openai_schemas = MagicMock(return_value=[])
    return mgr


def test_mcp_catalog_is_skipped_when_no_mcp_tool_is_offered():
    mgr = _fake_mgr()
    out = _build({"manage_memory", "ask_user"}, mcp_mgr=mgr)
    mgr.get_tool_descriptions_for_prompt.assert_not_called()
    assert "mcp__srv__do_thing" not in _text(out)


def test_mcp_catalog_lists_only_offered_mcp_tools():
    mgr = _fake_mgr()
    _build({"mcp__srv__do_thing", "ask_user"}, mcp_mgr=mgr)
    assert mgr.get_tool_descriptions_for_prompt.call_args.kwargs["only"] == {"mcp__srv__do_thing"}


def test_mcp_keyword_turns_keep_the_full_catalog():
    # Text-tool (non-native) models are offered every MCP tool on these turns
    # and learn their arguments from this text.
    mgr = _fake_mgr()
    out = _build({"ask_user"}, mcp_mgr=mgr, text="open the browser and take a screenshot")
    assert mgr.get_tool_descriptions_for_prompt.call_args.kwargs["only"] is None
    assert "mcp__srv__do_thing" in _text(out)


def test_manager_filters_descriptions_by_qualified_name():
    mgr = McpManager()
    mgr._tools = {"srv": [{"name": "keep", "description": "Kept."}, {"name": "drop", "description": "Dropped."}]}
    mgr._connections = {"srv": {"status": "connected", "name": "Srv", "identity": ""}}

    assert "mcp__srv__drop" in mgr.get_tool_descriptions_for_prompt()
    text = mgr.get_tool_descriptions_for_prompt(only={"mcp__srv__keep"})
    assert "mcp__srv__keep" in text and "mcp__srv__drop" not in text
    assert mgr.get_tool_descriptions_for_prompt(only=set()) == ""
