"""Small process wrapper for the local Omnigent CLI/server."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.constants import DATA_DIR


INSTALL_GUIDANCE = {
    "recommended": "curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh",
    "alternatives": [
        "uv tool install omnigent",
        'pip install "omnigent"',
        "brew install omnigent-ai/tap/omnigent",
        "uv tool install -q --python 3.12 git+https://github.com/omnigent-ai/omnigent.git",
    ],
    "source": "https://github.com/omnigent-ai/omnigent",
}

_PICKER_PATCH_MARKER = "ODYSSEUS_GENERATED_CREW_PICKER_PATCH"
_CODEX_TOP_LEVEL_DEFAULTS_PATCH_MARKER = "ODYSSEUS_CODEX_TOP_LEVEL_DEFAULTS_PATCH"
_CODEX_REASONING_ARGV_PATCH_MARKER = "ODYSSEUS_CODEX_REASONING_ARGV_PATCH"
_CODEX_MCP_ROTATION_PATCH_MARKER = "ODYSSEUS_CODEX_MCP_ROTATION_PATCH"
_CODEX_ROTATION_FORCE_SETTLE_PATCH_MARKER = "ODYSSEUS_CODEX_ROTATION_FORCE_SETTLE_PATCH"
_CODEX_STARTUP_THREAD_GUARD_PATCH_MARKER = "ODYSSEUS_CODEX_STARTUP_THREAD_GUARD_PATCH"
_CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH_MARKER = (
    "ODYSSEUS_CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH"
)
_CODEX_IDLE_RELEASE_PATCH_MARKER = "ODYSSEUS_CODEX_IDLE_RELEASE_PATCH"


def _patch_codex_session_route_source(source: str) -> tuple[str, bool]:
    """Propagate bundled Codex coordinator defaults into native sessions.

    Omnigent 0.10.0 only derives native launch flags for named sub-agents.
    Top-level custom ``codex-native`` bundles therefore lose both their
    ``llm.reasoning_effort`` and explicit ``executor.config.yolo`` values.
    """
    legacy_launch_defaults = (
        "            raw_top_level_yolo = (\n"
        "                top_level_spec.executor.config.get(\"yolo\")\n"
        "                if top_level_spec is not None\n"
        "                and top_level_spec.executor is not None\n"
        "                and _spec_harness(top_level_spec) == \"codex-native\"\n"
        "                else None\n"
        "            )\n"
        "            top_level_yolo = body.terminal_launch_args is None and (\n"
        "                raw_top_level_yolo is True\n"
        "                or (\n"
        "                    isinstance(raw_top_level_yolo, str)\n"
        "                    and raw_top_level_yolo.lower() == \"true\"\n"
        "                )\n"
        "            )\n"
        "            validated_launch_args = _validate_terminal_launch_args(\n"
        "                [\"--dangerously-bypass-approvals-and-sandbox\"]\n"
        "                if top_level_yolo\n"
        "                else body.terminal_launch_args\n"
        "            )\n"
    )
    enhanced_launch_defaults = (
        "            top_level_codex = (\n"
        "                top_level_spec is not None\n"
        "                and top_level_spec.executor is not None\n"
        "                and _spec_harness(top_level_spec) == \"codex-native\"\n"
        "            )\n"
        "            raw_top_level_yolo = (\n"
        "                top_level_spec.executor.config.get(\"yolo\")\n"
        "                if top_level_codex\n"
        "                else None\n"
        "            )\n"
        "            top_level_yolo = body.terminal_launch_args is None and (\n"
        "                raw_top_level_yolo is True\n"
        "                or (\n"
        "                    isinstance(raw_top_level_yolo, str)\n"
        "                    and raw_top_level_yolo.lower() == \"true\"\n"
        "                )\n"
        "            )\n"
        "            top_level_launch_args = []\n"
        "            if top_level_yolo:\n"
        "                top_level_launch_args.append(\n"
        "                    \"--dangerously-bypass-approvals-and-sandbox\"\n"
        "                )\n"
        f"            # {_CODEX_REASONING_ARGV_PATCH_MARKER}: the remote TUI\n"
        "            # owns the live thread settings, so persist the validated effort\n"
        "            # there as well as on the Omnigent conversation row.\n"
        "            if (\n"
        "                body.terminal_launch_args is None\n"
        "                and top_level_codex\n"
        "                and reasoning_effort is not None\n"
        "            ):\n"
        "                top_level_launch_args.extend([\"-c\", f'model_reasoning_effort=\"{reasoning_effort}\"'])\n"
        "            validated_launch_args = _validate_terminal_launch_args(\n"
        "                top_level_launch_args\n"
        "                if body.terminal_launch_args is None and top_level_launch_args\n"
        "                else body.terminal_launch_args\n"
        "            )\n"
    )

    if _CODEX_TOP_LEVEL_DEFAULTS_PATCH_MARKER in source:
        upgraded = source.replace(
            "_spec_harness(top_level_spec) == _CODEX_NATIVE_HARNESS",
            '_spec_harness(top_level_spec) == "codex-native"',
        )
        if _CODEX_REASONING_ARGV_PATCH_MARKER not in upgraded:
            legacy_variants = (
                legacy_launch_defaults,
                legacy_launch_defaults.replace(
                    "                and top_level_spec.executor is not None\n",
                    "",
                    1,
                ),
            )
            for legacy in legacy_variants:
                candidate = upgraded.replace(legacy, enhanced_launch_defaults, 1)
                if candidate != upgraded:
                    upgraded = candidate
                    break
        return upgraded, upgraded != source

    effort_needle = "        reasoning_effort=body.reasoning_effort,\n"
    metadata_needle = "    model_override, reasoning_effort = validate_session_model_metadata(\n"
    launch_needle = (
        "    else:\n"
        "        try:\n"
        "            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)\n"
    )
    if (
        metadata_needle not in source
        or effort_needle not in source
        or launch_needle not in source
    ):
        return source, False

    spec_loader = (
        f"    # {_CODEX_TOP_LEVEL_DEFAULTS_PATCH_MARKER}: inherit trusted bundle defaults.\n"
        "    top_level_spec = None\n"
        "    if body.sub_agent_name is None and agent_cache is not None:\n"
        "        try:\n"
        "            top_level_spec = agent_cache.load(\n"
        "                agent.id, agent.bundle_location, expand_env=agent.session_id is None\n"
        "            ).spec\n"
        "        except (KeyError, AttributeError, ValueError, ImportError, OSError):\n"
        "            _logger.debug(\n"
        "                \"top-level Codex defaults: agent %r failed to load\",\n"
        "                agent.name,\n"
        "                exc_info=True,\n"
        "            )\n"
        "\n"
    )
    effort_replacement = (
        "        reasoning_effort=(\n"
        "            body.reasoning_effort\n"
        "            if body.reasoning_effort is not None\n"
        "            else (\n"
        "                top_level_spec.llm.extra.get(\"reasoning_effort\")\n"
        "                if top_level_spec is not None and top_level_spec.llm is not None\n"
        "                else None\n"
        "            )\n"
        "        ),\n"
    )
    launch_replacement = "    else:\n        try:\n" + enhanced_launch_defaults

    patched = source.replace(
        metadata_needle,
        spec_loader + metadata_needle,
        1,
    )
    patched = patched.replace(effort_needle, effort_replacement, 1)
    patched = patched.replace(launch_needle, launch_replacement, 1)
    return patched, patched != source


def _patch_codex_forwarder_source(source: str) -> tuple[str, bool]:
    """Clear a synthesized MCP-startup band on its original conversation.

    Codex can start a replacement native thread before MCP startup settles.
    Omnigent mutates the target session first, so the eventual clear event is
    posted to the replacement while the original browser remains on
    ``Starting MCP servers`` indefinitely.
    """
    patched = source
    changed = False

    state_call_needle = (
        "                        app_server_url=app_server_url,\n"
        "                        event=event,\n"
    )
    state_signature_needle = (
        "    app_server_url: str,\n"
        "    event: CodexMessage,\n"
    )
    if (
        _CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH_MARKER not in patched
        and state_call_needle in patched
        and state_signature_needle in patched
    ):
        patched = patched.replace(
            state_call_needle,
            (
                "                        app_server_url=app_server_url,\n"
                "                        forwarder_state=forwarder_state,\n"
                "                        event=event,\n"
            ),
            1,
        )
        patched = patched.replace(
            state_signature_needle,
            (
                "    app_server_url: str,\n"
                "    forwarder_state: _CodexForwarderState,\n"
                "    event: CodexMessage,\n"
            ),
            1,
        )
        changed = True

    guard_needle = (
        "    new_thread_id = _thread_id_from_started_event(event)\n"
        "    if new_thread_id is None or new_thread_id == target.thread_id:\n"
        "        return False\n"
    )
    if (
        _CODEX_STARTUP_THREAD_GUARD_PATCH_MARKER not in patched
        and guard_needle in patched
    ):
        state_guard = ""
        guard_condition = "    if pending_mcp_servers(read_mcp_startup(bridge_dir)):\n"
        if "    forwarder_state: _CodexForwarderState,\n" in patched:
            state_guard = (
                f"    # {_CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH_MARKER}: ignore\n"
                "    # startup scratch threads until the producing thread emits output.\n"
            )
            guard_condition = (
                "    if (\n"
                "        not forwarder_state.mcp_startup_settled\n"
                "        or pending_mcp_servers(read_mcp_startup(bridge_dir))\n"
                "    ):\n"
            )
        guard_replacement = guard_needle + "".join(
            (
                f"    # {_CODEX_STARTUP_THREAD_GUARD_PATCH_MARKER}: the remote TUI may\n",
                "    # announce an unused scratch thread while the web-started first turn\n",
                "    # is still waiting for MCP. Keep ownership on the producing thread.\n",
                state_guard,
                guard_condition,
                "        _logger.info(\n",
                "            \"Codex forwarder ignored scratch thread during MCP startup: \"\n",
                "            \"current=%s candidate=%s\",\n",
                "            target.thread_id,\n",
                "            new_thread_id,\n",
                "        )\n",
                "        return False\n",
            )
        )
        patched = patched.replace(guard_needle, guard_replacement, 1)
        changed = True

    old_guard_condition = "    if pending_mcp_servers(read_mcp_startup(bridge_dir)):\n"
    if (
        _CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH_MARKER not in patched
        and "    forwarder_state: _CodexForwarderState,\n" in patched
        and old_guard_condition in patched
    ):
        patched = patched.replace(
            old_guard_condition,
            (
                f"    # {_CODEX_ACTIVE_TURN_ROTATION_GUARD_PATCH_MARKER}: ignore\n"
                "    # startup scratch threads until the producing thread emits output.\n"
                "    if (\n"
                "        not forwarder_state.mcp_startup_settled\n"
                "        or pending_mcp_servers(read_mcp_startup(bridge_dir))\n"
                "    ):\n"
            ),
            1,
        )
        changed = True

    settle_tail = (
        "                            await _settle_mcp_startup(\n"
        "                                ap_client,\n"
        "                                session_id=pre_rotation_session_id,\n"
        "                                bridge_dir=bridge_dir,\n"
        "                                reason=\"thread rotated\",\n"
        "                            )\n"
    )
    forced_settle_tail = settle_tail + (
        f"                            # {_CODEX_ROTATION_FORCE_SETTLE_PATCH_MARKER}:\n"
        "                            # bridge state may already be settled even though the\n"
        "                            # original browser still owns a pending startup band.\n"
        "                            await _post_mcp_startup(\n"
        "                                ap_client,\n"
        "                                session_id=pre_rotation_session_id,\n"
        "                                servers=read_mcp_startup(bridge_dir),\n"
        "                            )\n"
    )
    if (
        _CODEX_ROTATION_FORCE_SETTLE_PATCH_MARKER not in patched
        and settle_tail in patched
    ):
        patched = patched.replace(settle_tail, forced_settle_tail, 1)
        changed = True

    idle_settle_needle = (
        "        await _settle_mcp_startup(\n"
        "            client, session_id=session_id, bridge_dir=bridge_dir, reason=\"thread went idle\"\n"
        "        )\n"
    )
    if (
        _CODEX_IDLE_RELEASE_PATCH_MARKER not in patched
        and idle_settle_needle in patched
    ):
        patched = patched.replace(
            idle_settle_needle,
            idle_settle_needle
            + (
                f"        # {_CODEX_IDLE_RELEASE_PATCH_MARKER}: an idle/cancelled first\n"
                "        # turn also ends startup ownership even without model output.\n"
                "        if forwarder_state is not None:\n"
                "            forwarder_state.mcp_startup_settled = True\n"
            ),
            1,
        )
        changed = True

    if _CODEX_MCP_ROTATION_PATCH_MARKER in patched:
        return patched, changed

    rotate_needle = (
        "                    rotated = await _maybe_rotate_session_on_thread_started(\n"
    )
    rotated_branch = (
        "                    if rotated:\n"
        "                        forwarder_state.note_parent_rotation(target.session_id)\n"
    )
    if rotate_needle not in patched or rotated_branch not in patched:
        return patched, changed

    patched = patched.replace(
        rotate_needle,
        (
            f"                    # {_CODEX_MCP_ROTATION_PATCH_MARKER}: remember the band owner.\n"
            "                    pre_rotation_session_id = target.session_id\n"
            + rotate_needle
        ),
        1,
    )
    patched = patched.replace(
        rotated_branch,
        (
            "                    if rotated:\n"
            "                        if mcp_settle_timer is not None:\n"
            "                            mcp_settle_timer.cancel()\n"
            "                            with contextlib.suppress(asyncio.CancelledError):\n"
            "                                await mcp_settle_timer\n"
            "                            mcp_settle_timer = None\n"
            "                        try:\n"
            "                            await _settle_mcp_startup(\n"
            "                                ap_client,\n"
            "                                session_id=pre_rotation_session_id,\n"
            "                                bridge_dir=bridge_dir,\n"
            "                                reason=\"thread rotated\",\n"
            "                            )\n"
            "                        except Exception:\n"
            "                            _logger.warning(\n"
            "                                \"Codex MCP startup settlement failed during thread rotation\",\n"
            "                                exc_info=True,\n"
            "                            )\n"
            "                        forwarder_state.note_parent_rotation(target.session_id)\n"
        ),
        1,
    )
    if _CODEX_ROTATION_FORCE_SETTLE_PATCH_MARKER not in patched:
        patched = patched.replace(settle_tail, forced_settle_tail, 1)
    return patched, patched != source


def _patch_builtin_agent_route_source(source: str) -> tuple[str, bool]:
    """Mask generated crew variants in OmniGENT's picker-only metadata.

    The real agent bundle keeps its Codex executor harness. This changes only
    the `/v1/agents` response so OmniGENT's web UI displays generated
    `crew-*` variants by name instead of collapsing them into the Codex wrapper.
    """
    return_needle = "    return AgentObject(\n"
    if return_needle not in source:
        return source, False
    picker_block = (
        f"    # {_PICKER_PATCH_MARKER}: generated crew variants are selectable\n"
        "    # templates, not native harness wrappers. Keep the stored spec's\n"
        "    # executor harness and builtin status intact; use a non-wrapper\n"
        "    # harness metadata value so the main picker avoids codex-native\n"
        "    # dedupe while the fork dialog still treats the agent as selectable.\n"
        "    picker_harness = harness\n"
        "    picker_builtin = agent.session_id is None and agent.id == builtin_agent_id(agent.name)\n"
        "    if agent.session_id is None and agent.name.startswith(\"crew-\") and agent.name != \"crew-claude\":\n"
        "        picker_harness = \"openai-agents\"\n"
        "\n"
    )
    marker = f"    # {_PICKER_PATCH_MARKER}:"
    marker_at = source.find(marker)
    if marker_at >= 0:
        return_at = source.find(return_needle, marker_at)
        if return_at < 0:
            return source, False
        patched = source[:marker_at] + picker_block + source[return_at:]
    else:
        patched = source.replace(return_needle, f"{picker_block}{return_needle}", 1)
    patched = patched.replace("        harness=harness,\n", "        harness=picker_harness,\n", 1)
    builtin_block = (
        "        builtin=agent.session_id is None and agent.id == builtin_agent_id(agent.name),\n"
    )
    if builtin_block in patched:
        patched = patched.replace(builtin_block, "        builtin=picker_builtin,\n", 1)
    else:
        long_builtin_block = (
            "        builtin=agent.session_id is None and agent.id == builtin_agent_id(agent.name),\n"
        )
        patched = patched.replace(long_builtin_block, "        builtin=picker_builtin,\n", 1)
    if "        harness=picker_harness,\n" not in patched or "        builtin=picker_builtin,\n" not in patched:
        return source, False
    return patched, patched != source


def _omnigent_source_candidates(command: str, relative_path: Path) -> list[Path]:
    candidates: list[Path] = []
    try:
        resolved = Path(command).resolve()
    except Exception:
        resolved = Path(command)
    lib_roots = [
        Path("/opt/uv/tools/omnigent/lib"),
        Path.home() / ".local" / "share" / "uv" / "tools" / "omnigent" / "lib",
        resolved.parent.parent / "tools" / "omnigent" / "lib",
        resolved.parent.parent.parent / "tools" / "omnigent" / "lib",
    ]
    rel = Path("site-packages") / "omnigent" / relative_path
    seen: set[Path] = set()
    for root in lib_roots:
        try:
            matches = root.glob("python*/" + str(rel).replace("\\", "/"))
        except Exception:
            continue
        for match in matches:
            try:
                key = match.resolve()
            except Exception:
                key = match
            if key not in seen:
                seen.add(key)
                candidates.append(match)
    return candidates


def _builtin_agent_route_candidates(command: str) -> list[Path]:
    return _omnigent_source_candidates(
        command,
        Path("server") / "routes" / "builtin_agents.py",
    )


@dataclass(slots=True)
class OmnigentCommandResult:
    exit_code: int
    stdout: str
    stderr: str


class OmnigentManager:
    """Manage a local Omnigent install without taking ownership of its config."""

    def __init__(self, command: str | None = None, timeout: float = 8.0):
        self._command = command
        self.timeout = timeout
        # Omnigent writes state to ~/.omnigent. The container drops to a
        # non-root user whose HOME (/root, inherited) isn't writable, so point
        # it at the persisted, writable data volume instead.
        self._home = Path(DATA_DIR) / "omnigent-home"
        # Omnigent's server binds this loopback URL (default 6767). We probe it
        # directly for liveness instead of the slow/unreliable `server status`.
        self._ui_url = "http://127.0.0.1:6767"
        self._version_cache: str | None = None

    def _resolve_command(self) -> str | None:
        if self._command:
            return self._command
        for candidate in ("omnigent", "omni"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    def _patch_picker_metadata(self, command: str) -> None:
        for path in _builtin_agent_route_candidates(command):
            try:
                source = path.read_text()
                patched, changed = _patch_builtin_agent_route_source(source)
                if changed:
                    path.write_text(patched)
            except Exception:
                continue

    def _patch_codex_native_runtime(self, command: str) -> None:
        patches = (
            (
                Path("server") / "routes" / "_sessions" / "orchestration.py",
                _patch_codex_session_route_source,
            ),
            (Path("codex_native_forwarder.py"), _patch_codex_forwarder_source),
        )
        for relative_path, patcher in patches:
            for path in _omnigent_source_candidates(command, relative_path):
                try:
                    source = path.read_text()
                    patched, changed = patcher(source)
                    if changed:
                        compile(patched, str(path), "exec")
                        backup = path.with_suffix(path.suffix + ".bak-odysseus-codex")
                        if not backup.exists():
                            shutil.copyfile(path, backup)
                        path.write_text(patched)
                except Exception:
                    continue

    def _cli_worker(
        self,
        worker_id: str,
        label: str,
        source: str,
        commands: tuple[str, ...],
        missing_hint: str,
    ) -> dict[str, Any]:
        command = next((resolved for name in commands if (resolved := shutil.which(name))), None)
        return {
            "id": worker_id,
            "label": label,
            "source": source,
            "available": bool(command),
            "status": "ready" if command else "missing",
            "command": command,
            "hint": "Ready to launch from Omnigent." if command else missing_hint,
        }

    def _run(
        self,
        args: list[str],
        timeout: float | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> OmnigentCommandResult:
        env = dict(os.environ)
        try:
            self._home.mkdir(parents=True, exist_ok=True)
            env["HOME"] = str(self._home)
        except Exception:
            pass
        if env_extra:
            env.update({k: str(v) for k, v in env_extra.items() if v})
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
            check=False,
            env=env,
        )
        return OmnigentCommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def _version(self, command: str) -> str | None:
        # Version is static for the life of the process; cache it so repeated
        # status() calls (every modal refresh) don't re-spawn the CLI.
        if self._version_cache is not None:
            return self._version_cache or None
        found = ""
        for args in ([command, "--version"], [command, "version"]):
            try:
                result = self._run(args, timeout=4)
            except Exception:
                continue
            text = (result.stdout or result.stderr or "").strip()
            if result.exit_code == 0 and text:
                found = text.splitlines()[0].strip()
                break
        self._version_cache = found
        return found or None

    def _server_status(self, command: str) -> dict[str, Any]:
        # `omnigent server status` is slow (~3-5s) and reports the server as
        # not-running once it's orphaned from the short-lived subprocess that
        # started it (daemon_attached: false), even while it answers HTTP fine.
        # A direct HTTP probe of the loopback server is fast and accurate.
        url = self._ui_url
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                running = (getattr(resp, "status", 200) or 200) < 500
        except urllib.error.HTTPError as exc:
            running = exc.code < 500  # a 4xx still means the server is up
        except Exception:
            running = False
        return {"running": running, "url": url if running else None}

    def _sync_bridge(self, url: str | None) -> None:
        """Keep the Docker-published :6868 bridge pointed at Omnigent's port."""
        if not url or not shutil.which("socat"):
            return
        bridge_port = os.environ.get("OMNIGENT_BRIDGE_PORT", "6868")
        match = re.search(r":(\d+)$", url.rstrip("/"))
        if not (bridge_port and match):
            return
        target_port = match.group(1)
        if target_port == bridge_port:
            return
        try:
            subprocess.run(
                ["pkill", "-f", f"socat TCP-LISTEN:{bridge_port},"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            pass
        try:
            subprocess.Popen(
                [
                    "socat",
                    f"TCP-LISTEN:{bridge_port},fork,reuseaddr",
                    f"TCP:127.0.0.1:{target_port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        command = self._resolve_command()
        if not command:
            return {
                "installed": False,
                "command": None,
                "version": None,
                "running": False,
                "url": None,
                "sessions": None,
                "log_path": None,
                "error": "Omnigent CLI not found on PATH",
            }

        server = self._server_status(command)
        return {
            "installed": True,
            "command": command,
            "version": self._version(command),
            "running": bool(server.get("running")),
            "url": server.get("url") or server.get("base_url") or server.get("server_url"),
            "sessions": server.get("sessions") or server.get("session_count"),
            "log_path": server.get("log_path") or server.get("log"),
            "pid": server.get("pid"),
            "error": server.get("error"),
        }

    def restart(self, env_extra: dict[str, str] | None = None) -> dict[str, Any]:
        # Built-in agents (the UI picker) seed only at server startup, so a
        # config/agent change needs a fresh start to take effect.
        try:
            self.stop()
        except Exception:
            pass
        return self.start(env_extra=env_extra)

    def start(self, env_extra: dict[str, str] | None = None) -> dict[str, Any]:
        command = self._resolve_command()
        if not command:
            raise RuntimeError("Omnigent CLI not found on PATH")
        self._patch_picker_metadata(command)
        self._patch_codex_native_runtime(command)
        result = self._run([command, "server", "start"], timeout=30, env_extra=env_extra)
        # Learn the URL Omnigent actually bound (it prints e.g. "Started
        # background server at http://127.0.0.1:6767") so the probe matches.
        match = re.search(r"https?://127\.0\.0\.1:\d+", f"{result.stdout}\n{result.stderr}")
        if match:
            self._ui_url = match.group(0)
            self._sync_bridge(self._ui_url)
        status = self.status()
        status.update({
            "last_command": "omnigent server start",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        })
        return status

    def stop(self) -> dict[str, Any]:
        command = self._resolve_command()
        if not command:
            raise RuntimeError("Omnigent CLI not found on PATH")
        result = self._run([command, "server", "stop"], timeout=15)
        status = self.status()
        status.update({
            "last_command": "omnigent server stop",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        })
        return status

    def sessions(self) -> dict[str, Any]:
        status = self.status()
        url = (status.get("url") or "").rstrip("/")
        if not status.get("running") or not url:
            return {"sessions": [], "running": bool(status.get("running")), "url": url or None}

        try:
            with urllib.request.urlopen(f"{url}/v1/sessions", timeout=5) as resp:
                payload = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            return {"sessions": [], "running": True, "url": url, "error": str(exc)}
        except Exception as exc:
            return {"sessions": [], "running": True, "url": url, "error": str(exc)}

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {"sessions": [], "running": True, "url": url, "raw": payload}
        if isinstance(data, dict):
            data.setdefault("sessions", [])
            data.setdefault("running", True)
            data.setdefault("url", url)
            return data
        return {"sessions": data if isinstance(data, list) else [], "running": True, "url": url}

    def workers(self) -> dict[str, Any]:
        """Report the worker roster Omnigent can use from this runtime."""
        return {
            "recommended_prompt": "start claude and codex",
            "workers": [
                self._cli_worker(
                    "claude_code",
                    "Claude",
                    "subscription",
                    ("claude", "claude-code"),
                    "Install or log in to Claude Code where Omnigent runs.",
                ),
                self._cli_worker(
                    "codex",
                    "Codex",
                    "subscription",
                    ("codex",),
                    "Install or log in to Codex where Omnigent runs.",
                ),
                {
                    "id": "chatgpt_subscription",
                    "label": "ChatGPT",
                    "source": "Odysseus account",
                    "available": True,
                    "status": "linkable",
                    "hint": "Link your ChatGPT subscription through Odysseus.",
                },
                {
                    "id": "api_endpoint",
                    "label": "API endpoint",
                    "source": "Odysseus models",
                    "available": True,
                    "status": "ready",
                    "hint": "Use any model endpoint already configured in Odysseus.",
                },
                {
                    "id": "glm_free_web",
                    "label": "GLM website",
                    "source": "free web",
                    "available": False,
                    "status": "note",
                    "hint": "Open the free GLM website separately; it is not an API connector.",
                },
            ],
        }
