"""Claude subscription provider, served through the official Claude Code CLI.

A Claude Pro/Max subscription is not an API key. Anthropic only allows its
subscription sign-in to be used by Claude Code itself, so this provider never
calls ``api.anthropic.com`` with a subscription token. It runs the user's own
``claude`` binary headless (``claude -p --output-format stream-json``) and
translates that stream into the same SSE chunks ``llm_core.stream_llm`` yields,
so every Odysseus feature that takes an endpoint + model (chat, Study, Council,
research) can use the subscription like any other model.

Two sign-in modes, both stored as an owner-scoped ``ProviderAuthSession``:

* ``token`` - a long-lived token from ``claude setup-token`` (Claude's own
  OAuth flow, run by the CLI on any machine). It is encrypted at rest and only
  ever handed to the CLI process as ``CLAUDE_CODE_OAUTH_TOKEN``.
* ``host``  - the Claude login already present on the machine running
  Odysseus (``claude auth login``). Admin-only, since it is a host resource.

The endpoint row points at a sentinel URL on the reserved ``.invalid`` TLD
(RFC 2606) whose path carries the auth-session id. Nothing ever dials it: the
URL only routes calls here and names which credentials to use, so the token
never travels in request headers or lands in the plaintext sessions table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from typing import Any, AsyncIterator, Dict, Iterable, Iterator, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CLAUDE_SUBSCRIPTION_PROVIDER = "claude-subscription"
CLAUDE_SUBSCRIPTION_LABEL = "Claude Subscription"
CLAUDE_SUBSCRIPTION_HOST = "claude-subscription.invalid"

AUTH_MODE_TOKEN = "token"
AUTH_MODE_HOST = "host"

# Current Claude models. The CLI also accepts the bare aliases (opus, sonnet,
# haiku, fable) and always resolves them to the newest model of that tier.
DEFAULT_MODELS = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable-5-1",
    "claude-haiku-4-5-20251001",
)

# Model ids reach argv, so they must look like model ids and nothing else. A
# value starting with "-" would otherwise be parsed as a CLI flag.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}$")
# `claude setup-token` tokens are opaque but printable and short.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{20,4096}$")

DEFAULT_SYSTEM_PROMPT = "You are Claude, an AI assistant made by Anthropic, answering inside Odysseus."

_DEFAULT_TIMEOUT = 600
_STDERR_TAIL = 4000
_READ_LIMIT = 32 * 1024 * 1024

# Environment the CLI must not inherit. API-key variables would silently switch
# the CLI from the subscription to pay-as-you-go API billing; the session
# variables belong to whichever Claude Code session launched Odysseus.
_STRIPPED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SSE_PORT",
)
# In token mode the stored token decides the account, so provider switches and
# any ambient token are dropped as well.
_STRIPPED_ENV_TOKEN_MODE = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
)

_AUTH_FAILURE_HINTS = (
    "/login",
    "invalid api key",
    "oauth token has expired",
    "oauth token is invalid",
    "authentication_error",
    "not logged in",
    "please run claude setup-token",
)

_concurrency_lock = threading.Lock()
_concurrency_sem: Optional[threading.BoundedSemaphore] = None


class ClaudeSubscriptionError(RuntimeError):
    """Claude subscription setup or call failure with a user-safe message."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _max_concurrency() -> int:
    try:
        return max(1, min(16, int(os.getenv("CLAUDE_SUBSCRIPTION_MAX_CONCURRENCY", "4"))))
    except ValueError:
        return 4


def _semaphore() -> threading.BoundedSemaphore:
    global _concurrency_sem
    with _concurrency_lock:
        if _concurrency_sem is None:
            _concurrency_sem = threading.BoundedSemaphore(_max_concurrency())
        return _concurrency_sem


# ---------------------------------------------------------------------------
# URLs and models
# ---------------------------------------------------------------------------

def endpoint_base_url(auth_id: str) -> str:
    return f"https://{CLAUDE_SUBSCRIPTION_HOST}/{auth_id}"


def is_claude_subscription_base(url: Optional[str]) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == CLAUDE_SUBSCRIPTION_HOST


def auth_id_from_url(url: Optional[str]) -> str:
    """The auth-session id carried in the first path segment of the sentinel URL."""
    if not is_claude_subscription_base(url):
        return ""
    path = (urlparse(url or "").path or "").strip("/")
    first = path.split("/", 1)[0] if path else ""
    return first if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", first or "") else ""


def default_models() -> List[str]:
    raw = os.getenv("CLAUDE_SUBSCRIPTION_MODELS", "").strip()
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip() and _MODEL_ID_RE.match(m.strip())]
        if models:
            return models
    return list(DEFAULT_MODELS)


def valid_model_id(model: Optional[str]) -> bool:
    return bool(model) and bool(_MODEL_ID_RE.match(str(model)))


def valid_token(token: Optional[str]) -> bool:
    return bool(token) and bool(_TOKEN_RE.match(str(token)))


# ---------------------------------------------------------------------------
# CLI discovery and environment
# ---------------------------------------------------------------------------

def find_cli() -> Optional[str]:
    """Absolute path of the Claude Code CLI, or None when it is not installed."""
    configured = os.getenv("CLAUDE_CLI_PATH", "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".claude", "local", "claude"),
        os.path.join(home, ".local", "bin", "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def cli_env(mode: str, token: Optional[str]) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    if mode == AUTH_MODE_TOKEN:
        for key in _STRIPPED_ENV_TOKEN_MODE:
            env.pop(key, None)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    return env


def _workdir() -> str:
    """An empty directory outside any project, so no CLAUDE.md is picked up."""
    path = os.path.join(tempfile.gettempdir(), "odysseus-claude-subscription")
    os.makedirs(path, exist_ok=True)
    return path


def cli_version(timeout: float = 10.0) -> Optional[str]:
    cli = find_cli()
    if not cli:
        return None
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True,
                             timeout=timeout, cwd=_workdir(), env=cli_env(AUTH_MODE_HOST, None))
    except Exception:
        return None
    text = (out.stdout or "").strip().splitlines()
    return text[0][:80] if text else None


def host_login_status(timeout: float = 15.0) -> Dict[str, Any]:
    """`claude auth status --json` for the machine running Odysseus."""
    cli = find_cli()
    if not cli:
        return {"loggedIn": False, "error": "cli_missing"}
    try:
        out = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True,
                             timeout=timeout, cwd=_workdir(), env=cli_env(AUTH_MODE_HOST, None))
        data = json.loads(out.stdout or "{}")
    except Exception as exc:
        logger.debug("claude auth status failed error_type=%s", type(exc).__name__)
        return {"loggedIn": False, "error": "status_failed"}
    if not isinstance(data, dict):
        return {"loggedIn": False, "error": "status_failed"}
    return {
        "loggedIn": bool(data.get("loggedIn")),
        "authMethod": str(data.get("authMethod") or "")[:40],
        "apiProvider": str(data.get("apiProvider") or "")[:40],
    }


# ---------------------------------------------------------------------------
# Messages -> CLI prompt
# ---------------------------------------------------------------------------

def _content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind in ("image_url", "image", "input_image"):
                    parts.append("[image omitted]")
                else:
                    parts.append(str(part.get("text") or part.get("content") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def build_prompt(messages: Iterable[Dict]) -> tuple[str, str]:
    """Split OpenAI-style messages into (system_prompt, stdin_prompt).

    Headless Claude Code takes one user prompt per call, so earlier turns are
    replayed as a transcript ahead of the latest user message.
    """
    system_parts: List[str] = []
    turns: List[tuple[str, str]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        text = _content_text(msg.get("content")).strip()
        if role == "system":
            if text:
                system_parts.append(text)
            continue
        if role not in ("user", "assistant", "tool"):
            role = "user"
        if text:
            turns.append((role, text))

    system = "\n\n".join(system_parts)
    if not turns:
        return system, ""
    last_role, last_text = turns[-1]
    if last_role != "user":
        # Ending on an assistant/tool turn: ask for the continuation explicitly.
        history, latest = turns, "Continue from where the conversation above left off."
    else:
        history, latest = turns[:-1], last_text
    if not history:
        return system, latest
    lines = [
        "The conversation so far is below, oldest first. Reply to the latest user "
        "message as the assistant; do not repeat the transcript.",
        "",
        "<conversation>",
    ]
    for role, text in history:
        lines.append(f'<turn role="{role}">\n{text}\n</turn>')
    lines += ["</conversation>", "", "Latest user message:", latest]
    return system, "\n".join(lines)


def cli_args(cli: str, model: str, system_file: str, effort: Optional[str] = None) -> List[str]:
    args = [
        cli, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file", system_file,
    ]
    if effort in ("low", "medium", "high", "xhigh", "max"):
        args += ["--effort", effort]
    return args


# ---------------------------------------------------------------------------
# stream-json -> Odysseus SSE
# ---------------------------------------------------------------------------

def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _error_chunk(status: int, text: str) -> str:
    return f"event: error\ndata: {json.dumps({'status': status, 'text': text, 'error': text, 'fallback_eligible': status >= 500})}\n\n"


def friendly_error(text: str) -> tuple[int, str]:
    """Map CLI failure text to (status, message) without echoing raw output."""
    low = (text or "").lower()
    if any(h in low for h in _AUTH_FAILURE_HINTS):
        return 401, ("Claude Subscription sign-in is missing or expired. Reconnect it in "
                     "Council or Settings (run `claude setup-token` for a fresh token).")
    if "rate limit" in low or "usage limit" in low or "limit reached" in low or "429" in low:
        return 429, "Claude Subscription usage limit reached. It resets on Claude's schedule; try again later."
    if "model" in low and ("not found" in low or "invalid" in low or "not available" in low):
        return 400, "This Claude model is not available on your subscription. Pick another Claude model."
    if "overloaded" in low or "529" in low:
        return 503, "Claude is overloaded right now. Try again in a moment."
    return 502, "Claude Subscription request failed. Check the Claude Code CLI on the Odysseus host."


class StreamTranslator:
    """Turns parsed ``claude -p --output-format stream-json`` objects into SSE chunks."""

    def __init__(self, requested_model: str):
        self.requested_model = requested_model
        self.text_emitted = False
        self.done = False
        self.failed = False
        self._assistant_text: List[str] = []
        self._model_announced = False

    def feed(self, obj: Any) -> List[str]:
        if not isinstance(obj, dict) or self.done:
            return []
        kind = obj.get("type")
        out: List[str] = []
        if kind == "system" and obj.get("subtype") == "init":
            reported = obj.get("model")
            if (isinstance(reported, str) and reported.strip() and not self._model_announced
                    and reported.strip().lower() != (self.requested_model or "").strip().lower()):
                self._model_announced = True
                out.append(_sse({"type": "model_actual", "requested_model": self.requested_model,
                                 "model": reported.strip()}))
        elif kind == "stream_event":
            event = obj.get("event") or {}
            if event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                    if delta["text"]:
                        self.text_emitted = True
                        out.append(_sse({"delta": delta["text"]}))
                elif delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                    if delta["thinking"]:
                        out.append(_sse({"delta": delta["thinking"], "thinking": True}))
        elif kind == "assistant":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    self._assistant_text.append(block["text"])
        elif kind == "result":
            self.done = True
            if obj.get("is_error") or str(obj.get("subtype") or "").startswith("error"):
                self.failed = True
                status, text = friendly_error(str(obj.get("result") or obj.get("subtype") or ""))
                out.append(_error_chunk(status, text))
                return out
            if not self.text_emitted:
                # Older CLIs without partial messages: emit the whole reply once.
                full = "".join(self._assistant_text) or str(obj.get("result") or "")
                if full:
                    self.text_emitted = True
                    out.append(_sse({"delta": full}))
            usage = obj.get("usage") or {}
            if isinstance(usage, dict):
                try:
                    in_tok = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) \
                        + int(usage.get("cache_creation_input_tokens") or 0)
                    out_tok = int(usage.get("output_tokens") or 0)
                    out.append(_sse({"type": "usage", "data": {"input_tokens": in_tok, "output_tokens": out_tok}}))
                except (TypeError, ValueError):
                    pass
            out.append("data: [DONE]\n\n")
        return out

    def finish(self, returncode: Optional[int], stderr_tail: str) -> List[str]:
        """Close a stream that ended without a ``result`` object."""
        if self.done:
            return []
        self.done = True
        if self.text_emitted and returncode == 0:
            return ["data: [DONE]\n\n"]
        self.failed = True
        if stderr_tail.strip():
            logger.warning("claude subscription CLI exited rc=%s without a result", returncode)
        status, text = friendly_error(stderr_tail)
        return [_error_chunk(status, text)]


def _parse_line(line: str) -> Any:
    line = (line or "").strip()
    if not line or not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _database_handles():
    from core.database import ProviderAuthSession, SessionLocal
    return ProviderAuthSession, SessionLocal


def load_credentials(auth_id: str) -> tuple[str, Optional[str]]:
    """(mode, token) for an auth-session id; raises when it no longer exists."""
    if not auth_id:
        raise ClaudeSubscriptionError("Claude Subscription is not connected.", 401)
    ProviderAuthSession, SessionLocal = _database_handles()
    db = SessionLocal()
    try:
        row = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.id == auth_id,
            ProviderAuthSession.provider == CLAUDE_SUBSCRIPTION_PROVIDER,
        ).first()
        if row is None:
            raise ClaudeSubscriptionError("Claude Subscription is not connected.", 401)
        mode = row.auth_mode or AUTH_MODE_TOKEN
        token = row.access_token or None
        if mode == AUTH_MODE_TOKEN and not token:
            raise ClaudeSubscriptionError("Claude Subscription token is missing. Reconnect it.", 401)
        return mode, token
    finally:
        db.close()


def resolve_runtime_credentials(auth_id: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """Runtime (base_url, api_key) for ``endpoint_resolver.resolve_endpoint_runtime``.

    The api_key is deliberately empty: the CLI reads the token itself at call
    time, so it never enters request headers or persisted session rows.
    """
    ProviderAuthSession, SessionLocal = _database_handles()
    db = SessionLocal()
    try:
        q = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.id == auth_id,
            ProviderAuthSession.provider == CLAUDE_SUBSCRIPTION_PROVIDER,
        )
        if owner:
            q = q.filter(ProviderAuthSession.owner == owner)
        row = q.first()
        if row is None:
            raise ClaudeSubscriptionError("Claude Subscription credentials were not found for this user.", 401)
        return {
            "provider": CLAUDE_SUBSCRIPTION_PROVIDER,
            "base_url": endpoint_base_url(row.id),
            "api_key": None,
            "auth_mode": row.auth_mode or AUTH_MODE_TOKEN,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Running the CLI
# ---------------------------------------------------------------------------

class _Invocation:
    """Prepared CLI call: argv, env, stdin and a private system-prompt file."""

    def __init__(self, url: str, model: str, messages: List[Dict], effort: Optional[str] = None,
                 credentials: Optional[tuple] = None):
        if not valid_model_id(model):
            raise ClaudeSubscriptionError("Invalid Claude model id.")
        cli = find_cli()
        if not cli:
            raise ClaudeSubscriptionError(
                "The Claude Code CLI is not installed on the Odysseus host. Install it "
                "(npm install -g @anthropic-ai/claude-code) and reconnect.", 503)
        mode, token = credentials or load_credentials(auth_id_from_url(url))
        system, prompt = build_prompt(messages)
        # Without a system prompt the CLI uses its own coding-agent prompt.
        system = system or DEFAULT_SYSTEM_PROMPT
        if not prompt:
            raise ClaudeSubscriptionError("Nothing to send: the conversation has no user message.")
        # Large Odysseus system prompts would overflow argv (32 KB on Windows),
        # so the prompt goes through a private file instead.
        self._tmpdir = tempfile.mkdtemp(prefix="ody-claude-")
        system_file = os.path.join(self._tmpdir, "system.md")
        fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system)
        self.args = cli_args(cli, model, system_file, effort)
        self.env = cli_env(mode, token)
        self.stdin = prompt.encode("utf-8")
        self.cwd = _workdir()

    def cleanup(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


def _popen(inv: _Invocation) -> subprocess.Popen:
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        inv.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=inv.cwd, env=inv.env, **kwargs,
    )


def _kill(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.kill()
    except Exception:
        pass


def _drain_stderr(proc: subprocess.Popen, sink: List[str]) -> None:
    try:
        data = proc.stderr.read() if proc.stderr else b""
    except Exception:
        data = b""
    sink.append((data or b"").decode("utf-8", errors="replace")[-_STDERR_TAIL:])


def _write_stdin(proc: subprocess.Popen, payload: bytes) -> None:
    try:
        if proc.stdin:
            proc.stdin.write(payload)
            proc.stdin.close()
    except Exception:
        pass


async def stream_chat(
    url: str,
    model: str,
    messages: List[Dict],
    *,
    timeout: Optional[float] = None,
    effort: Optional[str] = None,
    credentials: Optional[tuple] = None,
) -> AsyncIterator[str]:
    """Stream one Claude reply as Odysseus SSE chunks (``data: {"delta": ...}``).

    ``timeout`` is an idle timeout, like the read timeout of the HTTP providers:
    it restarts whenever the CLI emits something, so a long answer that keeps
    streaming is never cut off.

    The CLI runs in a worker thread with blocking pipes rather than an asyncio
    subprocess, which needs a Proactor loop on Windows that uvicorn does not
    always run. Closing the generator (client gone, task cancelled) kills it.
    """
    try:
        inv = _Invocation(url, model, messages, effort, credentials)
    except ClaudeSubscriptionError as exc:
        yield _error_chunk(exc.status, str(exc))
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    proc_box: Dict[str, Optional[subprocess.Popen]] = {"proc": None}
    stop = threading.Event()
    stderr_sink: List[str] = []
    idle = float(timeout or _DEFAULT_TIMEOUT)

    def _put(item) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass  # loop closed

    def _worker() -> None:
        sem = _semaphore()
        sem.acquire()
        try:
            if stop.is_set():
                _put(("exit", None))
                return
            try:
                proc = _popen(inv)
            except Exception as exc:
                logger.warning("claude subscription CLI failed to start error_type=%s", type(exc).__name__)
                _put(("exit", -1))
                return
            proc_box["proc"] = proc
            if stop.is_set():
                _kill(proc)
            err_thread = threading.Thread(target=_drain_stderr, args=(proc, stderr_sink), daemon=True)
            err_thread.start()
            threading.Thread(target=_write_stdin, args=(proc, inv.stdin), daemon=True).start()
            try:
                for raw in iter(proc.stdout.readline, b""):
                    if stop.is_set():
                        break
                    if len(raw) > _READ_LIMIT:
                        continue
                    obj = _parse_line(raw.decode("utf-8", errors="replace"))
                    if obj is not None:
                        _put(("obj", obj))
            finally:
                if stop.is_set():
                    _kill(proc)
                rc = proc.wait()
                err_thread.join(timeout=2)
                _put(("exit", rc))
        finally:
            sem.release()

    threading.Thread(target=_worker, name="claude-subscription-cli", daemon=True).start()
    translator = StreamTranslator(model)
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=idle)
            except asyncio.TimeoutError:
                yield _error_chunk(504, "Claude Subscription timed out.")
                return
            if kind == "obj":
                for chunk in translator.feed(value):
                    yield chunk
                if translator.done:
                    return
            else:
                for chunk in translator.finish(value, "".join(stderr_sink)):
                    yield chunk
                return
    finally:
        stop.set()
        _kill(proc_box["proc"])
        inv.cleanup()


def run_chat_sync(url: str, model: str, messages: List[Dict], *, timeout: Optional[float] = None) -> str:
    """Blocking single reply for ``llm_core.llm_call``. Raises ClaudeSubscriptionError."""
    inv = _Invocation(url, model, messages)
    sem = _semaphore()
    sem.acquire()
    try:
        proc = _popen(inv)
        try:
            out, err = proc.communicate(inv.stdin, timeout=float(timeout or _DEFAULT_TIMEOUT))
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            raise ClaudeSubscriptionError("Claude Subscription timed out.", 504)
    finally:
        sem.release()
        inv.cleanup()
    translator = StreamTranslator(model)
    text: List[str] = []
    chunks: List[str] = []
    for line in (out or b"").decode("utf-8", errors="replace").splitlines():
        obj = _parse_line(line)
        if obj is not None:
            chunks += translator.feed(obj)
    chunks += translator.finish(proc.returncode, (err or b"").decode("utf-8", errors="replace")[-_STDERR_TAIL:])
    for chunk in chunks:
        if chunk.startswith("event: error"):
            data = json.loads(chunk.split("data: ", 1)[1])
            raise ClaudeSubscriptionError(data.get("text") or "Claude Subscription request failed.",
                                          int(data.get("status") or 502))
        for line in chunk.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                payload = json.loads(line[6:])
                if isinstance(payload.get("delta"), str) and not payload.get("thinking"):
                    text.append(payload["delta"])
    return "".join(text)


async def verify(mode: str, token: Optional[str], model: str = "haiku", timeout: float = 120) -> None:
    """One tiny real call with candidate credentials, before anything is stored.

    Raises ClaudeSubscriptionError with a user-safe message on failure.
    """
    messages = [{"role": "user", "content": "Reply with the single word: ready"}]
    got_text = False
    async for chunk in stream_chat(endpoint_base_url("verify"), model, messages, timeout=timeout,
                                   credentials=(mode, token)):
        if chunk.startswith("event: error"):
            data = json.loads(chunk.split("data: ", 1)[1])
            raise ClaudeSubscriptionError(data.get("text") or "Claude Subscription check failed.",
                                          int(data.get("status") or 502))
        if '"delta"' in chunk:
            got_text = True
    if not got_text:
        raise ClaudeSubscriptionError("Claude Subscription returned no reply.", 502)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def provision(owner: Optional[str], mode: str, token: Optional[str]) -> Dict[str, Any]:
    """Create or update the owner's Claude Subscription auth session + endpoint."""
    from core.database import ModelEndpoint, ProviderAuthSession, SessionLocal, utcnow_naive

    if mode not in (AUTH_MODE_TOKEN, AUTH_MODE_HOST):
        raise ValueError("Unknown Claude Subscription sign-in mode")
    if mode == AUTH_MODE_TOKEN and not valid_token(token):
        raise ValueError("That does not look like a Claude setup token")
    models = default_models()
    db = SessionLocal()
    try:
        auth = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.provider == CLAUDE_SUBSCRIPTION_PROVIDER,
            ProviderAuthSession.owner == owner,
        ).first()
        if auth is None:
            auth = ProviderAuthSession(
                id=uuid.uuid4().hex[:12],
                provider=CLAUDE_SUBSCRIPTION_PROVIDER,
                owner=owner,
                label=CLAUDE_SUBSCRIPTION_LABEL,
                base_url="",
            )
            db.add(auth)
        auth.base_url = endpoint_base_url(auth.id)
        auth.auth_mode = mode
        auth.access_token = token if mode == AUTH_MODE_TOKEN else None
        auth.refresh_token = None
        auth.last_refresh = utcnow_naive()

        ep = db.query(ModelEndpoint).filter(
            ModelEndpoint.provider_auth_id == auth.id,
            ModelEndpoint.owner == owner,
        ).first()
        if ep is None:
            ep = ModelEndpoint(
                id=uuid.uuid4().hex[:8],
                name=CLAUDE_SUBSCRIPTION_LABEL,
                base_url=auth.base_url,
                model_type="llm",
                endpoint_kind="api",
                owner=owner,
            )
            db.add(ep)
            ep.cached_models = json.dumps(models)
        elif not ep.cached_models:
            ep.cached_models = json.dumps(models)
        ep.name = CLAUDE_SUBSCRIPTION_LABEL
        ep.base_url = auth.base_url
        ep.api_key = None
        ep.provider_auth_id = auth.id
        ep.is_enabled = True
        ep.supports_tools = False
        ep.model_type = "llm"
        ep.endpoint_kind = "api"
        ep.model_refresh_mode = "manual"
        db.commit()
        try:
            result_models = json.loads(ep.cached_models or "[]")
        except Exception:
            result_models = models
        result = {
            "id": ep.id,
            "name": ep.name,
            "base_url": ep.base_url,
            "models": result_models,
            "mode": mode,
            "auth_id": auth.id,
        }
    finally:
        db.close()
    _invalidate_models_cache()
    return result


def disconnect(owner: Optional[str]) -> Dict[str, Any]:
    """Remove the owner's Claude Subscription endpoint(s) and stored token."""
    from core.database import ModelEndpoint, ProviderAuthSession, SessionLocal

    db = SessionLocal()
    removed_eps = 0
    try:
        auths = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.provider == CLAUDE_SUBSCRIPTION_PROVIDER,
            ProviderAuthSession.owner == owner,
        ).all()
        for auth in auths:
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.provider_auth_id == auth.id).all():
                db.delete(ep)
                removed_eps += 1
            db.delete(auth)
        db.commit()
        removed_auth = len(auths)
    finally:
        db.close()
    _invalidate_models_cache()
    return {"removed_endpoints": removed_eps, "removed_auth": removed_auth}


def connection(owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """The owner's current Claude Subscription endpoint, if connected."""
    from core.database import ModelEndpoint, ProviderAuthSession, SessionLocal

    db = SessionLocal()
    try:
        auth = db.query(ProviderAuthSession).filter(
            ProviderAuthSession.provider == CLAUDE_SUBSCRIPTION_PROVIDER,
            ProviderAuthSession.owner == owner,
        ).first()
        if auth is None:
            return None
        ep = db.query(ModelEndpoint).filter(ModelEndpoint.provider_auth_id == auth.id).first()
        try:
            models = json.loads(ep.cached_models or "[]") if ep else []
        except Exception:
            models = []
        return {
            "auth_id": auth.id,
            "mode": auth.auth_mode or AUTH_MODE_TOKEN,
            "endpoint_id": ep.id if ep else None,
            "enabled": bool(ep and ep.is_enabled),
            "models": models,
            "connected_at": auth.last_refresh.isoformat() if auth.last_refresh else None,
        }
    finally:
        db.close()


def _invalidate_models_cache() -> None:
    try:
        from routes.model_routes import _invalidate_models_cache as invalidate

        invalidate()
    except Exception:
        pass


def iter_text(chunks: Iterable[str]) -> Iterator[str]:
    """Text deltas from SSE chunks (thinking excluded). Test/diagnostic helper."""
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload.get("delta"), str) and not payload.get("thinking"):
                    yield payload["delta"]
