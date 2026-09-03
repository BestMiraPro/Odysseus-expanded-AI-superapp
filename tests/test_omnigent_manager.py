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


def test_codex_session_route_patch_inherits_top_level_reasoning_and_explicit_yolo():
    import src.omnigent_manager as omnigent_manager

    patcher = getattr(omnigent_manager, "_patch_codex_session_route_source", None)
    assert callable(patcher), "Codex session-route patcher is missing"

    source = '''
    model_override, reasoning_effort = validate_session_model_metadata(
        model_override=(
            _native_routed_model
            if _native_smart_routing
            else _fixed_routed_model or body.model_override
        ),
        reasoning_effort=body.reasoning_effort,
    )

    if body.sub_agent_name:
        validated_launch_args = _derive_terminal_launch_args_from_spec(sub_spec)
    else:
        try:
            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(str(exc)) from exc
'''

    patched, changed = patcher(source)

    assert changed is True
    assert "ODYSSEUS_CODEX_TOP_LEVEL_DEFAULTS_PATCH" in patched
    assert "top_level_spec.llm.extra.get(\"reasoning_effort\")" in patched
    assert "body.reasoning_effort is not None" in patched
    assert "raw_top_level_yolo" in patched
    assert "--dangerously-bypass-approvals-and-sandbox" in patched
    assert "body.terminal_launch_args is None" in patched
    assert '_spec_harness(top_level_spec) == "codex-native"' in patched
    assert "_CODEX_NATIVE_HARNESS" not in patched
    assert "ODYSSEUS_CODEX_REASONING_ARGV_PATCH" in patched
    assert 'model_reasoning_effort="{reasoning_effort}"' in patched
    assert 'top_level_launch_args.extend(["-c",' in patched

    patched_again, changed_again = patcher(patched)
    assert changed_again is False
    assert patched_again == patched


def test_codex_session_route_patch_is_atomic_when_upstream_anchor_is_missing():
    from src.omnigent_manager import _patch_codex_session_route_source

    source = '''
        reasoning_effort=body.reasoning_effort,
    )
    else:
        try:
            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
    '''

    patched, changed = _patch_codex_session_route_source(source)

    assert changed is False
    assert patched == source


def test_codex_session_route_patch_upgrades_legacy_harness_constant():
    from src.omnigent_manager import _patch_codex_session_route_source

    source = '''
    # ODYSSEUS_CODEX_TOP_LEVEL_DEFAULTS_PATCH: inherit trusted bundle defaults.
    if _spec_harness(top_level_spec) == _CODEX_NATIVE_HARNESS:
        pass
    '''

    patched, changed = _patch_codex_session_route_source(source)

    assert changed is True
    assert '_spec_harness(top_level_spec) == "codex-native"' in patched
    assert "_CODEX_NATIVE_HARNESS" not in patched


def test_codex_session_route_patch_upgrades_prior_yolo_only_launch_block():
    from src.omnigent_manager import _patch_codex_session_route_source

    source = '''
    # ODYSSEUS_CODEX_TOP_LEVEL_DEFAULTS_PATCH: inherit trusted bundle defaults.
    top_level_spec = object()
        try:
            raw_top_level_yolo = (
                top_level_spec.executor.config.get("yolo")
                if top_level_spec is not None
                and top_level_spec.executor is not None
                and _spec_harness(top_level_spec) == "codex-native"
                else None
            )
            top_level_yolo = body.terminal_launch_args is None and (
                raw_top_level_yolo is True
                or (
                    isinstance(raw_top_level_yolo, str)
                    and raw_top_level_yolo.lower() == "true"
                )
            )
            validated_launch_args = _validate_terminal_launch_args(
                ["--dangerously-bypass-approvals-and-sandbox"]
                if top_level_yolo
                else body.terminal_launch_args
            )
    '''

    patched, changed = _patch_codex_session_route_source(source)

    assert changed is True
    assert "ODYSSEUS_CODEX_REASONING_ARGV_PATCH" in patched
    assert 'model_reasoning_effort="{reasoning_effort}"' in patched

    prior_without_executor_guard = source.replace(
        "                and top_level_spec.executor is not None\n",
        "",
    )
    patched_older, changed_older = _patch_codex_session_route_source(
        prior_without_executor_guard
    )
    assert changed_older is True
    assert "ODYSSEUS_CODEX_REASONING_ARGV_PATCH" in patched_older


def test_codex_forwarder_patch_settles_original_session_when_thread_rotates():
    import src.omnigent_manager as omnigent_manager

    patcher = getattr(omnigent_manager, "_patch_codex_forwarder_source", None)
    assert callable(patcher), "Codex forwarder patcher is missing"

    source = '''
            async for event in client.iter_events():
                try:
                    rotated = await _maybe_rotate_session_on_thread_started(
                        ap_client=ap_client,
                        target=target,
                        bridge_dir=bridge_dir,
                        app_server_url=app_server_url,
                        event=event,
                    )
                    if rotated:
                        forwarder_state.note_parent_rotation(target.session_id)
                        subscribe_task.cancel()
'''

    patched, changed = patcher(source)

    assert changed is True
    assert "ODYSSEUS_CODEX_MCP_ROTATION_PATCH" in patched
    assert "pre_rotation_session_id = target.session_id" in patched
    assert "session_id=pre_rotation_session_id" in patched
    assert 'reason="thread rotated"' in patched
    assert "mcp_settle_timer.cancel()" in patched
    assert "ODYSSEUS_CODEX_ROTATION_FORCE_SETTLE_PATCH" in patched
    assert "session_id=pre_rotation_session_id" in patched
    assert "servers=read_mcp_startup(bridge_dir)" in patched

    patched_again, changed_again = patcher(patched)
    assert changed_again is False
    assert patched_again == patched


def test_codex_forwarder_patch_ignores_startup_scratch_thread_rotation():
    from src.omnigent_manager import _patch_codex_forwarder_source

    source = '''
async def _maybe_rotate_session_on_thread_started(
    *,
    ap_client,
    target,
    bridge_dir,
    app_server_url: str,
    event: CodexMessage,
):
    new_thread_id = _thread_id_from_started_event(event)
    if new_thread_id is None or new_thread_id == target.thread_id:
        return False
    # A Codex AgentControl child thread emits ``thread/started`` when it
    # begins.
    if _thread_started_is_subagent(event):
        return False
'''

    patched, changed = _patch_codex_forwarder_source(source)

    assert changed is True
    assert "ODYSSEUS_CODEX_STARTUP_THREAD_GUARD_PATCH" in patched
    assert "pending_mcp_servers(read_mcp_startup(bridge_dir))" in patched
    assert "ignored scratch thread during MCP startup" in patched

    patched_again, changed_again = _patch_codex_forwarder_source(patched)
    assert changed_again is False
    assert patched_again == patched


def test_codex_forwarder_patch_keeps_first_active_turn_on_its_session():
    from src.omnigent_manager import _patch_codex_forwarder_source

    source = '''
                    rotated = await _maybe_rotate_session_on_thread_started(
                        ap_client=ap_client,
                        target=target,
                        bridge_dir=bridge_dir,
                        app_server_url=app_server_url,
                        event=event,
                    )

async def _maybe_rotate_session_on_thread_started(
    *,
    ap_client,
    target,
    bridge_dir,
    app_server_url: str,
    event: CodexMessage,
):
    new_thread_id = _thread_id_from_started_event(event)
    if new_thread_id is None or new_thread_id == target.thread_id:
        return False
'''

    patched, changed = _patch_codex_forwarder_source(source)

    assert changed is True
    assert "ODYSSEUS_CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH" in patched
    assert "forwarder_state=forwarder_state" in patched
    assert "forwarder_state: _CodexForwarderState" in patched
    assert "not forwarder_state.mcp_startup_settled" in patched


def test_codex_forwarder_patch_releases_startup_guard_on_idle_without_output():
    from src.omnigent_manager import _patch_codex_forwarder_source

    source = '''
    if _is_thread_idle_status_event(method, params) and _thread_id_from_params(params) in {
        None,
        expected_thread_id,
    }:
        await _settle_mcp_startup(
            client, session_id=session_id, bridge_dir=bridge_dir, reason="thread went idle"
        )
    '''

    patched, changed = _patch_codex_forwarder_source(source)

    assert changed is True
    assert "ODYSSEUS_CODEX_IDLE_RELEASE_PATCH" in patched
    assert "forwarder_state.mcp_startup_settled = True" in patched
