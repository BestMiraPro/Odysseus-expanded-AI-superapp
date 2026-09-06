"""A failed search must not tell the agent that the code has no matches."""

import json
import shutil

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern, has_error", [("[", True), ("absent", False)])
async def test_ripgrep_distinguishes_invalid_regex_from_no_matches(tmp_path, pattern, has_error):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep is not installed")
    from src.agent_tools.filesystem_tools import GrepTool
    from src.tool_execution import _active_workspace

    (tmp_path / "example.txt").write_text("hello\n", encoding="utf-8")
    token = _active_workspace.set(str(tmp_path))
    try:
        result = await GrepTool().execute(json.dumps({"pattern": pattern, "path": str(tmp_path)}), {})
    finally:
        _active_workspace.reset(token)
    if has_error:
        assert result["exit_code"] == 1
        assert "error" in result
    else:
        assert result["exit_code"] == 0
        assert "No matches" in result["output"]
