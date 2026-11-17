"""Tools for Council seats: what each member may do, and the engine that runs it.

Two levels (per council):

- ``research`` (default): web search and fetch, read and search files in the
  workspace, the user's conversation history (claude.ai, ChatGPT, Claude Code,
  Codex) and their Odysseus chats.
- ``full``: everything in research plus shell commands, Python and file
  edits in the workspace, like Claude Code / Codex on a laptop. Only an admin
  (or a single-user install) can turn it on.

``off`` keeps the plain text-only council.

The engine follows the seat:

- Claude Subscription -> the Claude Code CLI with its own tools, plus the
  Odysseus context MCP server for the history.
- ChatGPT Subscription -> the Codex CLI (``codex exec``) with live web search,
  its sandbox, and the same MCP server.
- Any other endpoint (API, local) -> the Odysseus agent loop restricted to the
  matching built-in tools.

Every engine yields Odysseus SSE chunks, including ``tool_start`` /
``tool_output`` events, so ``council.run_member`` treats them alike.

With seat isolation (the Docker image, src/seat_user.py) each engine's
tool-running process starts as the separate seat user: the Claude Code CLI,
``codex exec``, and the agent loop's shell and Python. The agent loop's file
tools stay in Odysseus's process, confined to the workspace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from src import seat_user

logger = logging.getLogger(__name__)

TOOLS_OFF = "off"
TOOLS_RESEARCH = "research"
TOOLS_FULL = "full"
TOOL_MODES = (TOOLS_OFF, TOOLS_RESEARCH, TOOLS_FULL)

APP_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_SERVER = APP_ROOT / "mcp_servers" / "council_context_server.py"
MCP_NAME = "odysseus"
MCP_TOOLS = ("search_history", "read_history", "search_odysseus_chats")

# Claude Code built-in tools per level.
CLAUDE_RESEARCH_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep", "TodoWrite"]
CLAUDE_FULL_TOOLS = CLAUDE_RESEARCH_TOOLS + ["Bash", "Edit", "Write", "NotebookEdit"]

# Odysseus agent-loop tools per level (API and local seats).
AGENT_RESEARCH_TOOLS = frozenset({
    "web_search", "web_fetch", "read_file", "grep", "glob", "ls", "get_workspace",
    "search_chats", "search_history", "read_history", "todowrite",
})
AGENT_FULL_TOOLS = AGENT_RESEARCH_TOOLS | {"bash", "python", "write_file", "edit_file", "apply_patch"}

AGENT_MAX_ROUNDS = 12

# Codex features a council seat must not have: the user's ChatGPT connectors
# (Gmail, Slack, Drive, ...), plugins, goals, image generation, browser and
# computer use, sub-agents. Seats stay on the council's own tools.
CODEX_FEATURES_OFF = ("apps", "plugins", "remote_plugin", "goals", "image_generation", "browser_use",
                      "browser_use_external", "computer_use", "multi_agent", "tool_suggest")


@dataclass
class ToolSettings:
    mode: str = TOOLS_RESEARCH
    owner: Optional[str] = None
    workspace: Optional[str] = None      # vetted directory, or None
    # Environment overrides for every tool-running seat's processes: the
    # Claude Code / Codex CLI (and so their tools) or the agent loop's shell
    # and Python. A value of None removes the variable.
    env: Dict[str, Optional[str]] = field(default_factory=dict)
    # Model calls one API/local seat may make in a stage (each tool call costs
    # one). The Claude Code and Codex CLIs pace themselves.
    max_rounds: int = AGENT_MAX_ROUNDS
    # The seat user every process that runs the seat's tools starts as
    # (src/seat_user.py). A work run passes its seat's own, kept for the whole
    # run; otherwise stream_fn_for takes one per call when seat isolation is
    # available.
    seat: Optional[seat_user.Seat] = None

    @property
    def enabled(self) -> bool:
        return self.mode in (TOOLS_RESEARCH, TOOLS_FULL)

    @property
    def full(self) -> bool:
        return self.mode == TOOLS_FULL


def normalize_mode(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in TOOL_MODES else TOOLS_RESEARCH


# ── Credentials ──

# Credentials for Odysseus's own API that a deployment may put in Odysseus's
# environment: the internal-tool token (loopback calls as any user, approvals
# included) and an integration's API token. Seats never inherit them, so a
# seat cannot approve its own request.
ODYSSEUS_API_ENV = ("ODYSSEUS_INTERNAL_TOKEN", "ODYSSEUS_API_TOKEN")


def credential_free_git_env(config_dir: str) -> Dict[str, Optional[str]]:
    """Environment overrides that keep git and gh off the owner's logins.

    Drops gh's token variables and the SSH agent, points gh at an empty config
    folder (so it finds no stored login) and git at a global config in
    ``config_dir`` that resets ``credential.helper`` to none, skips the system
    config (osxkeychain and the like) and turns off prompts.

    This stops accidental and habitual use of the owner's GitHub and SSH
    credentials (``git push``, ``gh pr merge``). It is not a sandbox: a seat
    with an unconfined shell can undo any of it.
    """
    gitconfig = os.path.join(config_dir, "gitconfig")
    # Created, never opened through a name someone else made: with seat
    # isolation the seat user can write in ``config_dir`` too.
    with open(gitconfig, "x", encoding="utf-8") as fh:
        fh.write("[credential]\n\thelper =\n")
    return {
        "GH_TOKEN": None,
        "GITHUB_TOKEN": None,
        "GH_ENTERPRISE_TOKEN": None,
        "GITHUB_ENTERPRISE_TOKEN": None,
        "SSH_AUTH_SOCK": None,
        "GH_CONFIG_DIR": os.path.join(config_dir, "gh"),
        "GIT_CONFIG_GLOBAL": gitconfig,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        # Offers no key (no agent, no ~/.ssh/config IdentityFile, no default
        # key files) and never prompts.
        "GIT_SSH_COMMAND": "ssh -F /dev/null -o IdentitiesOnly=yes -o IdentityFile=/dev/null "
                           "-o IdentityAgent=none -o BatchMode=yes",
    }


# ── Prompt ──

def tools_brief(settings: ToolSettings, stage: str) -> str:
    """What a member can do, appended to its system prompt."""
    if not settings.enabled:
        return ""
    lines = [
        "\n\nYou have tools. Use them whenever they would make the answer more correct or more specific; "
        "skip them for questions you can answer well from knowledge alone:",
        "- Web search and page fetch for current facts, prices, releases, documentation. Cite the URLs you rely on.",
        "- search_history / read_history: the user's own past conversations with Claude (claude.ai), ChatGPT, "
        "Claude Code and Codex. Use them when the question refers to the user's earlier discussions, plans, "
        "decisions or projects. Mention which conversation you drew on.",
        "- Searching the user's chats inside Odysseus.",
    ]
    if settings.workspace:
        lines.append(f"- Reading and searching files in the workspace {settings.workspace}.")
    if settings.full:
        lines.append("- Running shell commands and Python, and editing files"
                     + (" in the workspace." if settings.workspace else "."))
    if stage == "review":
        lines.append("As a reviewer, use a few tool calls to check claims you doubt instead of guessing, "
                     "then write the review itself: every answer, ending with the FINAL RANKING.")
    if stage in ("review", "synthesis"):
        lines.append("Each member answered in its own separate session, and the engines behind the seats "
                     "differ, so their tools and results can differ from yours. Don't mark an answer wrong "
                     "only because your own tools or results differ; say what you could and couldn't verify.")
    lines.append("Finish with your complete answer as plain text after the last tool call; "
                 "text written before a tool call is treated as working notes.")
    return "\n".join(lines)


def with_tools_brief(messages: List[Dict[str, Any]], settings: ToolSettings, stage: str) -> List[Dict[str, Any]]:
    brief = tools_brief(settings, stage)
    if not brief:
        return messages
    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = f"{m.get('content') or ''}{brief}"
            return out
    return [{"role": "system", "content": brief.strip()}, *out]


# ── MCP server for the CLIs ──

def mcp_server_spec(owner: Optional[str]) -> Dict[str, Any]:
    from core.database import DATABASE_URL

    env = {
        "ODYSSEUS_MCP_OWNER": owner or "",
        "PYTHONPATH": str(APP_ROOT),
        "DATABASE_URL": DATABASE_URL,
    }
    for key in ("ODYSSEUS_DATA_DIR", "DATA_DIR", "HOME"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {"command": sys.executable, "args": [str(CONTEXT_SERVER)], "env": env}


def _start_history_bridge(sock: str, spec: Dict[str, Any]) -> subprocess.Popen:
    """Serve the history MCP server, as Odysseus, to whoever connects to the socket ``sock``."""
    server = " ".join([spec["command"], *spec["args"]])
    return subprocess.Popen(["socat", f"UNIX-LISTEN:{sock},fork,perm=0660", f"EXEC:{server}"],
                            env={**spec["env"], "PATH": os.environ.get("PATH", os.defpath)}, cwd=str(APP_ROOT),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


@asynccontextmanager
async def _history_server(settings: ToolSettings):
    """The history MCP server spec for a CLI seat's config.

    The server reads the database. A seat running as the seat user can do
    neither that nor hold the database URL, so the server runs as Odysseus
    behind a socket in the seat's folder (its own group's, so no other seat
    can connect), and the seat's CLI reaches it through socat.
    """
    spec = mcp_server_spec(settings.owner)
    if settings.seat is None:
        yield spec
        return
    sock = os.path.join(settings.seat.folder, "odysseus-history.sock")
    bridge = _start_history_bridge(sock, spec)
    try:
        for _ in range(100):
            if os.path.exists(sock) or bridge.poll() is not None:
                break
            await asyncio.sleep(0.02)
        yield {"command": "socat", "args": ["STDIO", f"UNIX-CONNECT:{sock}"]}
    finally:
        try:
            os.killpg(bridge.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge.kill()


def claude_native_tools(settings: ToolSettings, history: Optional[Dict[str, Any]] = None):
    from src.claude_subscription import NativeTools

    builtin = list(CLAUDE_FULL_TOOLS if settings.full else CLAUDE_RESEARCH_TOOLS)
    if not settings.full and not settings.workspace:
        # Research without a workspace has no files to read; without this the
        # seat could read anything on the host (the API seats get no file tools).
        builtin = [t for t in builtin if t not in ("Read", "Glob", "Grep")]
    allowed = builtin + [f"mcp__{MCP_NAME}__{t}" for t in MCP_TOOLS]
    config = json.dumps({"mcpServers": {MCP_NAME: history or mcp_server_spec(settings.owner)}})
    return NativeTools(builtin=builtin, allowed=allowed, mcp_config=config, cwd=settings.workspace,
                       env=settings.env, seat=settings.seat)


# ── Engines ──

def _bearer(headers: Optional[Dict[str, str]]) -> str:
    for key, value in (headers or {}).items():
        if key.lower() == "authorization" and str(value).lower().startswith("bearer "):
            return str(value)[7:].strip()
    return ""


async def _claude_stream(url, model, messages, headers, settings: ToolSettings, timeout) -> AsyncIterator[str]:
    from src import reasoning_effort
    from src.claude_subscription import stream_chat
    from src.llm_core import _sanitize_llm_messages

    effort, _ = reasoning_effort.resolve("claude-subscription", url, model, headers)
    async with _history_server(settings) as history:
        async for chunk in stream_chat(url, model, _sanitize_llm_messages(messages), timeout=timeout,
                                       effort=effort, tools=claude_native_tools(settings, history)):
            yield chunk


async def _codex_stream(url, model, messages, headers, settings: ToolSettings, timeout) -> AsyncIterator[str]:
    from src import codex_cli, reasoning_effort
    from src.claude_subscription import build_prompt

    effort, headers = reasoning_effort.resolve("chatgpt-subscription", url, model, headers)
    system, prompt = build_prompt(messages)
    full_prompt = f"{system}\n\n{prompt}" if system else prompt
    features_off = list(CODEX_FEATURES_OFF)
    if not settings.full and not await asyncio.to_thread(codex_cli.sandbox_works):
        # Research runs shell commands in Codex's read-only sandbox, which
        # cannot start here (e.g. inside Docker): every command would fail.
        features_off.append("shell_tool")
    async with _history_server(settings) as history:
        run = codex_cli.CodexRun(
            model=model,
            prompt=full_prompt,
            access_token=_bearer(headers),
            sandbox=codex_cli.SANDBOX_FULL if settings.full else codex_cli.SANDBOX_READ_ONLY,
            cwd=settings.workspace,
            effort=effort,
            mcp_servers={MCP_NAME: history},
            extra_config=[f"features.{name}=false" for name in features_off],
            env=settings.env,
            seat=settings.seat,
        )
        async for chunk in codex_cli.stream(run, timeout=timeout):
            yield chunk


async def _agent_stream(url, model, messages, headers, settings: ToolSettings, workload) -> AsyncIterator[str]:
    """Odysseus agent loop limited to the council's tools."""
    from src.agent_loop import stream_agent_loop
    from src.tool_execution import subprocess_env_overrides
    from src.tool_policy import ToolPolicy, known_tool_names

    allowed = set(AGENT_FULL_TOOLS if settings.full else AGENT_RESEARCH_TOOLS)
    if not settings.workspace:
        allowed -= {"read_file", "grep", "glob", "ls", "get_workspace", "write_file", "edit_file", "apply_patch"}
    blocked = frozenset(known_tool_names() - allowed)
    policy = ToolPolicy(disabled_tools=blocked, mode="council", disable_mcp=True)
    # Nobody can answer an approval card mid-council. The council's tool level
    # is the user's go-ahead for the whole run, as for the Claude Code and
    # Codex seats; the policy above already limits research to research tools.
    gated = "allow"
    # Shell and Python start in the workspace, else in a scratch folder of
    # their own rather than the app's data folder: the seat's own folder when
    # they run as the seat user (it is removed with the seat).
    scratch = None
    if settings.full and not settings.workspace:
        scratch = settings.seat.folder if settings.seat else tempfile.mkdtemp(prefix="ody-council-")
    # The agent loop bills each model call to the budget ledger itself.
    yield f"data: {json.dumps({'type': 'self_billed'})}\n\n"
    try:
        with subprocess_env_overrides(settings.env, seat=settings.seat):
            async for chunk in stream_agent_loop(
                endpoint_url=url,
                model=model,
                messages=messages,
                headers=headers,
                max_rounds=settings.max_rounds,
                # No output cap, as for the tool-less seats: a reasoning model
                # would otherwise spend 4096 tokens thinking and never answer.
                max_tokens=0,
                owner=settings.owner,
                relevant_tools=allowed,
                tool_policy=policy,
                workspace=settings.workspace or scratch,
                workload=workload,
                gated_actions=gated,
            ):
                yield _approval_refused(chunk, settings.owner)
    finally:
        if scratch and settings.seat is None:
            shutil.rmtree(scratch, ignore_errors=True)


def _approval_refused(chunk: str, owner: Optional[str]) -> str:
    """A tool that needs the user's approval cannot get it inside a council run.

    Retire the pending grant (nobody can answer it) and show the call as
    refused, the same way scheduled tasks handle it.
    """
    if not chunk.startswith("data: ") or '"tool_approval"' not in chunk:
        return chunk
    try:
        data = json.loads(chunk[6:])
    except ValueError:
        return chunk
    approval = data.get("ask_user") if isinstance(data, dict) else None
    if not (isinstance(approval, dict) and approval.get("kind") == "tool_approval"):
        return chunk
    try:
        from src.tool_approvals import tool_approval_store

        tool_approval_store.consume(approval.get("approval_id"), decision="deny", owner=owner, session_id=None)
    except Exception:
        pass
    data.pop("ask_user", None)
    data["output"] = "Refused: this action needs approval, which a council run cannot ask for."
    data["exit_code"] = 1
    return f"data: {json.dumps(data)}\n\n"


def stream_fn_for(settings: ToolSettings):
    """A ``council.run_member`` stream function that gives members tools."""
    from src.llm_core import _detect_provider, stream_llm

    async def fn(url: str, model: str, messages: List[Dict[str, Any]], *, headers=None,
                 timeout: int = 600, workload: str = "foreground", stage: str = "opinions"):
        if not settings.enabled:
            async for chunk in stream_llm(url, model, messages, headers=headers, timeout=timeout,
                                          workload=workload):
                yield chunk
            return
        from src.constants import DATA_DIR

        msgs = with_tools_brief(messages, settings, stage)
        provider = _detect_provider(url)
        # Nobody watches a seat, so it runs without the owner's GitHub and SSH
        # logins (see credential_free_git_env), with a throwaway git/gh config
        # of its own that is removed when this seat's run ends, and without
        # any credential for Odysseus's own API (last, so nothing re-adds one).
        # With seat isolation its processes run as a seat user of its own,
        # and that folder, outside the data folder, is that user's alone. When
        # the call ends everything still running as the user is killed.
        with ExitStack() as stack:
            user = settings.seat
            if user is not None:
                # A work run's seat: its user and home last the run, the folder this call.
                config_dir = tempfile.mkdtemp(prefix="call-", dir=user.home)
                stack.callback(user.remove, config_dir)
                stack.callback(user.kill_all)
                user.share(config_dir, 0o2770)
                user = replace(user, folder=config_dir)
            elif seat_user.available():
                user = stack.enter_context(seat_user.seat())
                config_dir = user.folder
            else:
                config_dir = stack.enter_context(tempfile.TemporaryDirectory(
                    prefix="council-seat-", dir=DATA_DIR, ignore_cleanup_errors=True))
            seat = replace(settings, env={**credential_free_git_env(config_dir), **settings.env,
                                          **dict.fromkeys(ODYSSEUS_API_ENV)}, seat=user)
            if provider == "claude-subscription":
                source = _claude_stream(url, model, msgs, headers, seat, timeout)
            elif provider == "chatgpt-subscription":
                source = _codex_stream(url, model, msgs, headers, seat, timeout)
            else:
                source = _agent_stream(url, model, msgs, headers, seat, workload)
            async for chunk in source:
                yield chunk

    fn.accepts_stage = True
    return fn
