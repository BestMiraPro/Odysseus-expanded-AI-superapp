"""Run the Codex CLI (``codex exec --json``) on a ChatGPT Subscription seat.

Each run gets a private, throwaway ``CODEX_HOME`` holding an ``auth.json``
built from the access token Odysseus already keeps fresh for the ChatGPT
Subscription endpoint. The refresh token is deliberately left out: Codex can
never rotate it, so Odysseus stays the only owner of the login and the two
cannot log each other out. A run that outlives the access token fails with a
sign-in error instead.

The JSONL events are translated into Odysseus SSE chunks: ``delta`` text,
``thinking`` deltas, ``tool_start`` / ``tool_output`` for shell commands, MCP
calls, web searches and file edits, then ``usage`` and ``[DONE]``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)

SANDBOX_READ_ONLY = "read-only"
SANDBOX_FULL = "danger-full-access"

_DEFAULT_TIMEOUT = 900
_STDERR_TAIL = 4000
_OUTPUT_PREVIEW = 1500
_READ_LIMIT = 32 * 1024 * 1024
_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


class CodexError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class CodexRun:
    """Everything one ``codex exec`` needs."""

    model: str
    prompt: str
    access_token: str
    sandbox: str = SANDBOX_READ_ONLY
    cwd: Optional[str] = None
    effort: Optional[str] = None
    web_search: bool = True
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # name -> {command, args, env}
    extra_config: List[str] = field(default_factory=list)


def find_cli() -> Optional[str]:
    configured = os.getenv("CODEX_CLI_PATH", "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    return shutil.which("codex")


_sandbox_ok: Optional[bool] = None


def sandbox_works() -> bool:
    """Whether Codex's own sandbox can start here (checked once per process).

    On Linux it needs unprivileged user namespaces, which Docker's default
    seccomp profile denies, so inside the Odysseus container every sandboxed
    shell command fails with a bwrap error.
    """
    global _sandbox_ok
    if _sandbox_ok is None:
        cli = find_cli()
        home = tempfile.mkdtemp(prefix="ody-codex-probe-")
        try:
            proc = subprocess.run([cli, "sandbox", "--", "true"], capture_output=True, timeout=30,
                                  env=_env(home)) if cli else None
            _sandbox_ok = bool(proc and proc.returncode == 0)
        except Exception:
            _sandbox_ok = False
        finally:
            shutil.rmtree(home, ignore_errors=True)
    return _sandbox_ok


def _jwt_claims(token: str) -> Dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def auth_json(access_token: str) -> Dict[str, Any]:
    """Codex's ChatGPT ``auth.json`` for an access token, without a refresh token."""
    claims = _jwt_claims(access_token)
    account = ((claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")) or ""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "OPENAI_API_KEY": None,
        # The access token carries the same account/plan claims Codex reads
        # from the id token.
        "tokens": {"id_token": access_token, "access_token": access_token,
                   "refresh_token": "", "account_id": account},
        "last_refresh": now,
    }


def _toml_str(value: str) -> str:
    # A JSON string literal is a valid TOML basic string.
    return json.dumps(str(value), ensure_ascii=False)


def build_args(cli: str, run: CodexRun, cwd: str) -> List[str]:
    args = [cli]
    if run.web_search:
        args.append("--search")
    args += ["exec", "--json", "--skip-git-repo-check", "--ephemeral", "-C", cwd, "-m", run.model]
    if run.sandbox == SANDBOX_FULL:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        args += ["-s", run.sandbox]
    if run.effort in _EFFORTS:
        args += ["-c", f"model_reasoning_effort={_toml_str(run.effort)}"]
    for name, spec in run.mcp_servers.items():
        base = f"mcp_servers.{name}"
        args += ["-c", f"{base}.command={_toml_str(spec['command'])}",
                 "-c", f"{base}.args=[{', '.join(_toml_str(a) for a in spec.get('args', []))}]"]
        env = spec.get("env") or {}
        if env:
            inline = ", ".join(f"{k}={_toml_str(v)}" for k, v in env.items())
            args += ["-c", f"{base}.env={{{inline}}}"]
        # Odysseus's own servers only read; without this Codex refuses every
        # call ("requires approval, but approval policy is never").
        args += ["-c", f"{base}.startup_timeout_sec=30",
                 "-c", f'{base}.default_tools_approval_mode="approve"']
    # ODYSSEUS_CODEX_CONFIG: extra `-c key=value` overrides, one per line
    # (e.g. a custom model provider).
    env_extra = [ln.strip() for ln in os.getenv("ODYSSEUS_CODEX_CONFIG", "").splitlines() if ln.strip()]
    for item in [*run.extra_config, *env_extra]:
        args += ["-c", item]
    args.append("-")      # prompt from stdin
    return args


def friendly_error(text: str) -> tuple:
    low = (text or "").lower()
    if any(h in low for h in ("401", "unauthorized", "sign in again", "log in again", "token could not be refreshed",
                              "not logged in", "invalid_token", "expired")):
        return 401, "ChatGPT Subscription sign-in expired. Reconnect it in Council or Settings."
    if "usage limit" in low or "rate limit" in low or "429" in low:
        return 429, "ChatGPT usage limit reached. Try again later."
    if "model" in low and ("not supported" in low or "not found" in low or "does not exist" in low):
        return 400, "This model is not available to Codex on your ChatGPT plan. Pick another model."
    return 502, "The Codex CLI run failed."


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _error_chunk(status: int, text: str) -> str:
    return f"event: error\ndata: {json.dumps({'status': status, 'text': text, 'error': text})}\n\n"


def _summary(item: Dict[str, Any]) -> tuple:
    """(tool name, one-line description) for a Codex tool item."""
    kind = item.get("type")
    if kind == "command_execution":
        return "shell", str(item.get("command") or "")[:300]
    if kind == "mcp_tool_call":
        args = item.get("arguments")
        desc = ""
        if isinstance(args, dict):
            desc = str(args.get("query") or args.get("conversation_id") or json.dumps(args, ensure_ascii=False))
        elif args:
            desc = str(args)
        return str(item.get("tool") or "mcp"), desc[:300]
    if kind == "web_search":
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        query = item.get("query") or action.get("query") or action.get("url") or ""
        return "web_search", str(query)[:300]
    if kind == "file_change":
        paths = [str(c.get("path")) for c in item.get("changes") or [] if isinstance(c, dict)]
        return "edit", ", ".join(paths)[:300]
    if kind == "todo_list":
        return "plan", f"{len(item.get('items') or [])} steps"
    return str(kind or "tool"), ""


def _tool_output(item: Dict[str, Any]) -> tuple:
    """(output preview, exit code) for a completed Codex tool item."""
    kind = item.get("type")
    failed = item.get("status") == "failed"
    if kind == "command_execution":
        code = item.get("exit_code")
        return str(item.get("aggregated_output") or "")[-_OUTPUT_PREVIEW:], (code if isinstance(code, int) else int(failed))
    if kind == "mcp_tool_call":
        if item.get("error"):
            err = item["error"]
            return str(err.get("message") if isinstance(err, dict) else err)[:_OUTPUT_PREVIEW], 1
        result = item.get("result")
        if isinstance(result, dict):
            parts = [str(c.get("text") or "") for c in result.get("content") or [] if isinstance(c, dict)]
            text = "\n".join(p for p in parts if p) or json.dumps(result, ensure_ascii=False)
        else:
            text = "" if result is None else str(result)
        return text[:_OUTPUT_PREVIEW], int(failed)
    if kind == "file_change":
        lines = [f"{c.get('kind', 'update')} {c.get('path')}" for c in item.get("changes") or [] if isinstance(c, dict)]
        return "\n".join(lines)[:_OUTPUT_PREVIEW], int(failed)
    if kind == "todo_list":
        lines = [("[x] " if t.get("completed") else "[ ] ") + str(t.get("text") or "")
                 for t in item.get("items") or [] if isinstance(t, dict)]
        return "\n".join(lines)[:_OUTPUT_PREVIEW], 0
    if kind == "web_search":
        lines = [f"{r.get('title') or r.get('domain') or ''} - {r.get('url')}".strip(" -")
                 for r in item.get("results") or [] if isinstance(r, dict) and r.get("url")]
        return "\n".join(lines)[:_OUTPUT_PREVIEW], int(failed)
    return "", int(failed)


_TOOL_ITEMS = ("command_execution", "mcp_tool_call", "web_search", "file_change", "todo_list")


class EventTranslator:
    """``codex exec --json`` events -> Odysseus SSE chunks."""

    def __init__(self) -> None:
        self.done = False
        self.failed = False
        self.text_emitted = False
        self._started: set = set()
        self._last_error = ""

    def feed(self, obj: Any) -> List[str]:
        if not isinstance(obj, dict) or self.done:
            return []
        kind = obj.get("type")
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = item.get("type")
        out: List[str] = []
        if kind == "item.started" and itype in _TOOL_ITEMS:
            if itype == "web_search" and not _summary(item)[1]:
                # The query only arrives with item.completed; start it then.
                return out
            self._started.add(item.get("id"))
            name, desc = _summary(item)
            out.append(_sse({"type": "tool_start", "tool": name, "command": desc, "id": item.get("id")}))
        elif kind == "item.completed":
            if itype == "agent_message" and item.get("text"):
                self.text_emitted = True
                out.append(_sse({"delta": str(item["text"])}))
            elif itype == "reasoning" and item.get("text"):
                out.append(_sse({"delta": str(item["text"]), "thinking": True}))
            elif itype in _TOOL_ITEMS:
                if item.get("id") not in self._started:
                    name, desc = _summary(item)
                    out.append(_sse({"type": "tool_start", "tool": name, "command": desc, "id": item.get("id")}))
                name, _ = _summary(item)
                text, code = _tool_output(item)
                out.append(_sse({"type": "tool_output", "tool": name, "output": text, "exit_code": code,
                                 "id": item.get("id")}))
            elif itype == "error" and item.get("message"):
                self._last_error = str(item["message"])
        elif kind == "turn.completed":
            usage = obj.get("usage") or {}
            try:
                out.append(_sse({"type": "usage", "data": {
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0)
                    + int(usage.get("reasoning_output_tokens") or 0),
                }}))
            except (TypeError, ValueError):
                pass
            self.done = True
            out.append("data: [DONE]\n\n")
        elif kind == "turn.failed":
            self.done = True
            self.failed = True
            message = str(((obj.get("error") or {}).get("message")) or self._last_error)
            status, text = friendly_error(message)
            out.append(_error_chunk(status, text))
        elif kind == "error":
            message = str(obj.get("message") or "")
            if not message.lower().startswith("reconnecting"):
                self._last_error = message
        return out

    def finish(self, returncode: Optional[int], stderr_tail: str) -> List[str]:
        if self.done:
            return []
        self.done = True
        if self.text_emitted and returncode == 0:
            return ["data: [DONE]\n\n"]
        self.failed = True
        status, text = friendly_error(f"{self._last_error}\n{stderr_tail}")
        if returncode == 127 or "no such file" in (stderr_tail or "").lower():
            status, text = 503, "The Codex CLI is not installed on the Odysseus host."
        logger.warning("codex exec ended without completing rc=%s", returncode)
        return [_error_chunk(status, text)]


def _parse(raw: bytes) -> Any:
    line = raw.decode("utf-8", errors="replace").strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _kill(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _env(codex_home: str) -> Dict[str, str]:
    from src.omnigent_manager import scrubbed_env

    env = scrubbed_env()
    # OPENAI_API_KEY would switch Codex to API-key billing; this run is the
    # ChatGPT plan.
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"):
        env.pop(key, None)
    env["CODEX_HOME"] = codex_home
    return env


async def stream(run: CodexRun, *, timeout: Optional[float] = None) -> AsyncIterator[str]:
    """Stream one Codex run as Odysseus SSE chunks. Closing it kills the CLI."""
    cli = find_cli()
    if not cli:
        yield _error_chunk(503, "The Codex CLI is not installed on the Odysseus host "
                                "(npm install -g @openai/codex).")
        return
    if not run.access_token:
        yield _error_chunk(401, "ChatGPT Subscription is not connected.")
        return
    home = tempfile.mkdtemp(prefix="ody-codex-")
    scratch = None
    try:
        fd = os.open(os.path.join(home, "auth.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(auth_json(run.access_token), fh)
        cwd = run.cwd
        if not cwd:
            scratch = tempfile.mkdtemp(prefix="ody-codex-work-")
            cwd = scratch
        args = build_args(cli, run, cwd)
        env = _env(home)
    except Exception:
        shutil.rmtree(home, ignore_errors=True)
        raise

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    box: Dict[str, Optional[subprocess.Popen]] = {"proc": None}
    stderr: List[str] = []

    def put(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass

    def drain_stderr(proc) -> None:
        try:
            data = proc.stderr.read() if proc.stderr else b""
        except Exception:
            data = b""
        stderr.append((data or b"").decode("utf-8", errors="replace")[-_STDERR_TAIL:])

    def worker() -> None:
        try:
            kwargs: Dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=cwd, env=env, **kwargs)
        except Exception as exc:
            logger.warning("codex CLI failed to start error_type=%s", type(exc).__name__)
            put(("exit", 127))
            return
        box["proc"] = proc
        if stop.is_set():
            _kill(proc)
        err = threading.Thread(target=drain_stderr, args=(proc,), daemon=True)
        err.start()
        try:
            proc.stdin.write(run.prompt.encode("utf-8"))
            proc.stdin.close()
        except Exception:
            pass
        try:
            for raw in iter(proc.stdout.readline, b""):
                if stop.is_set():
                    break
                if len(raw) > _READ_LIMIT:
                    continue
                obj = _parse(raw)
                if obj is not None:
                    put(("obj", obj))
        finally:
            if stop.is_set():
                _kill(proc)
            rc = proc.wait()
            err.join(timeout=2)
            put(("exit", rc))

    threading.Thread(target=worker, name="codex-cli", daemon=True).start()
    translator = EventTranslator()
    idle = float(timeout or _DEFAULT_TIMEOUT)
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=idle)
            except asyncio.TimeoutError:
                yield _error_chunk(504, "Codex timed out.")
                return
            if kind == "obj":
                for chunk in translator.feed(value):
                    yield chunk
                if translator.done:
                    return
            else:
                for chunk in translator.finish(value, "".join(stderr)):
                    yield chunk
                return
    finally:
        stop.set()
        _kill(box["proc"])
        shutil.rmtree(home, ignore_errors=True)
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
