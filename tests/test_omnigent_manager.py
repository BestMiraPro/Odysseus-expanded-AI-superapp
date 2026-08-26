"""Omnigent manager startup patch tests."""

from __future__ import annotations


def test_builtin_agent_route_patch_exposes_generated_crews_without_codex_dedupe():
    from src.omnigent_manager import _patch_builtin_agent_route_source

    source = """
def _to_agent_object(agent, agent_cache):
    harness = "codex-native"
    return AgentObject(
        id=agent.id,
        name=agent.name,
        harness=harness,
        builtin=agent.session_id is None and agent.id == builtin_agent_id(agent.name),
    )
"""

    patched, changed = _patch_builtin_agent_route_source(source)

    assert changed is True
    assert "ODYSSEUS_GENERATED_CREW_PICKER_PATCH" in patched
    assert "picker_harness = harness" in patched
    assert "picker_builtin = agent.session_id is None and agent.id == builtin_agent_id(agent.name)" in patched
    assert "agent.name.startswith(\"crew-\") and agent.name != \"crew-claude\"" in patched
    assert "picker_harness = \"openai-agents\"" in patched
    assert "picker_builtin = False" not in patched
    assert "picker_harness = None" not in patched
    assert "harness=picker_harness" in patched
    assert "builtin=picker_builtin" in patched

    patched_again, changed_again = _patch_builtin_agent_route_source(patched)
    assert changed_again is False
    assert patched_again == patched


def test_builtin_agent_route_patch_upgrades_old_builtin_false_patch():
    from src.omnigent_manager import _patch_builtin_agent_route_source

    source = """
def _to_agent_object(agent, agent_cache):
    harness = "codex-native"
    # ODYSSEUS_GENERATED_CREW_PICKER_PATCH: generated crew variants are selectable
    picker_harness = harness
    picker_builtin = agent.session_id is None and agent.id == builtin_agent_id(agent.name)
    if agent.session_id is None and agent.name.startswith("crew-") and agent.name != "crew-claude":
        picker_harness = None
        picker_builtin = False

    return AgentObject(
        id=agent.id,
        name=agent.name,
        harness=picker_harness,
        builtin=picker_builtin,
    )
"""

    patched, changed = _patch_builtin_agent_route_source(source)

    assert changed is True
    assert "picker_builtin = False" not in patched
    assert "picker_harness = \"openai-agents\"" in patched
    assert "picker_harness = None" not in patched
    assert "builtin=picker_builtin" in patched


def test_builtin_agent_route_patch_upgrades_old_marker_without_harness_mask():
    from src.omnigent_manager import _patch_builtin_agent_route_source

    source = """
def _to_agent_object(agent, agent_cache):
    harness = "codex-native"
    # ODYSSEUS_GENERATED_CREW_PICKER_PATCH: generated crew variants are selectable
    picker_harness = harness
    picker_builtin = agent.session_id is None and agent.id == builtin_agent_id(agent.name)

    return AgentObject(
        id=agent.id,
        name=agent.name,
        harness=picker_harness,
        builtin=picker_builtin,
    )
"""

    patched, changed = _patch_builtin_agent_route_source(source)

    assert changed is True
    assert "agent.name.startswith(\"crew-\") and agent.name != \"crew-claude\"" in patched
    assert "picker_harness = \"openai-agents\"" in patched
    assert "picker_harness = None" not in patched
    assert "picker_builtin = False" not in patched
    assert "harness=picker_harness" in patched
    assert "builtin=picker_builtin" in patched
