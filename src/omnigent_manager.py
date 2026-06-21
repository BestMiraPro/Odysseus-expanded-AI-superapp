"""Small process wrapper for the local Omnigent CLI/server."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


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

    def _resolve_command(self) -> str | None:
        if self._command:
            return self._command
        for candidate in ("omnigent", "omni"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

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

    def _run(self, args: list[str], timeout: float | None = None) -> OmnigentCommandResult:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
            check=False,
        )
        return OmnigentCommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def _version(self, command: str) -> str | None:
        for args in ([command, "version"], [command, "--version"]):
            try:
                result = self._run(args, timeout=4)
            except Exception:
                continue
            text = (result.stdout or result.stderr or "").strip()
            if result.exit_code == 0 and text:
                return text.splitlines()[0].strip()
        return None

    def _server_status(self, command: str) -> dict[str, Any]:
        try:
            result = self._run([command, "server", "status", "--json"], timeout=5)
        except subprocess.TimeoutExpired:
            return {"running": False, "error": "Omnigent status timed out"}
        except Exception as exc:
            return {"running": False, "error": str(exc)}

        raw = (result.stdout or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                pass

        text = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        lower = text.lower()
        return {
            "running": result.exit_code == 0 and ("running" in lower or "http://" in lower or "https://" in lower),
            "error": None if result.exit_code == 0 else (text or f"exit {result.exit_code}"),
            "raw": text,
        }

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

    def start(self) -> dict[str, Any]:
        command = self._resolve_command()
        if not command:
            raise RuntimeError("Omnigent CLI not found on PATH")
        result = self._run([command, "server", "start"], timeout=15)
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
