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


def _builtin_agent_route_candidates(command: str) -> list[Path]:
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
    rel = Path("site-packages") / "omnigent" / "server" / "routes" / "builtin_agents.py"
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
