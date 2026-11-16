"""Council tools: engine choice per seat, event translation, tool timelines."""
import asyncio
import json
import os

import pytest

from src import codex_cli, council, council_tools
from src.claude_subscription import NativeTools, StreamTranslator, cli_args


@pytest.fixture(autouse=True)
def data_dir(monkeypatch, tmp_path_factory):
    # Tool-running seats keep a throwaway git/gh config under the app's data folder.
    path = tmp_path_factory.mktemp("data")
    monkeypatch.setattr("src.constants.DATA_DIR", str(path))
    return path


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


def _collect(agen):
    async def run():
        return [c async for c in agen]
    return asyncio.run(run())


def _member(url="https://api.example.com/v1/chat/completions", model="m"):
    return council.Member(endpoint_id="ep", model=model, endpoint_name="EP", kind="api",
                          provider="openai", url=url, headers={"Authorization": "Bearer tok"})


# ── Engine routing ──

def _settings(mode="research", workspace=None):
    return council_tools.ToolSettings(mode=mode, owner="dinis", workspace=workspace)


def test_claude_seat_runs_claude_code_with_native_tools(monkeypatch):
    seen = {}

    async def fake_stream_chat(url, model, messages, *, timeout=None, effort=None, credentials=None, tools=None):
        seen.update(tools=tools, messages=messages)
        yield _sse({"delta": "ok"})

    monkeypatch.setattr("src.claude_subscription.stream_chat", fake_stream_chat)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: "claude-subscription")
    fn = council_tools.stream_fn_for(_settings())
    _collect(fn("https://x.claude-subscription.invalid", "claude-opus-5-5",
                [{"role": "system", "content": "SYS"}, {"role": "user", "content": "q"}], stage="review"))
    tools = seen["tools"]
    # No workspace: research tools without the file readers.
    assert tools.builtin == [t for t in council_tools.CLAUDE_RESEARCH_TOOLS if t not in ("Read", "Glob", "Grep")]
    assert "Bash" not in tools.allowed
    assert "mcp__odysseus__search_history" in tools.allowed
    cfg = json.loads(tools.mcp_config)["mcpServers"]["odysseus"]
    assert cfg["env"]["ODYSSEUS_MCP_OWNER"] == "dinis"
    assert cfg["args"][0].endswith("council_context_server.py")
    system = seen["messages"][0]["content"]
    assert system.startswith("SYS") and "search_history" in system and "check claims" in system


def test_full_mode_adds_shell_and_edits_for_claude(tmp_path):
    tools = council_tools.claude_native_tools(_settings("full", str(tmp_path)))
    assert {"Bash", "Edit", "Write"} <= set(tools.builtin)
    assert tools.cwd == str(tmp_path)
    args = cli_args("claude", "claude-opus-5-5", "/tmp/s.md", "high", tools)
    assert args[args.index("--tools") + 1].split(",") == tools.builtin
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--mcp-config" in args
    # Without tools the CLI stays tool-less, exactly as before.
    plain = cli_args("claude", "claude-opus-5-5", "/tmp/s.md")
    assert plain[plain.index("--tools") + 1] == "" and "--allowedTools" not in plain


def test_chatgpt_seat_runs_codex_with_the_seat_token(monkeypatch):
    seen = {}

    async def fake_stream(run, *, timeout=None):
        seen["run"] = run
        yield _sse({"delta": "ok"})

    monkeypatch.setattr(codex_cli, "stream", fake_stream)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: "chatgpt-subscription")
    fn = council_tools.stream_fn_for(_settings())
    _collect(fn("https://chatgpt.com/backend-api/codex", "gpt-5.6-sol",
                [{"role": "system", "content": "SYS"}, {"role": "user", "content": "the question"}],
                headers={"Authorization": "Bearer access-123"}))
    run = seen["run"]
    assert run.access_token == "access-123"
    assert run.sandbox == codex_cli.SANDBOX_READ_ONLY
    assert "the question" in run.prompt and "SYS" in run.prompt
    assert "odysseus" in run.mcp_servers


def test_api_seat_runs_agent_loop_limited_to_council_tools(monkeypatch):
    seen = {}

    async def fake_loop(**kwargs):
        seen.update(kwargs)
        yield _sse({"delta": "ok"})

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: "openai")
    fn = council_tools.stream_fn_for(_settings())
    chunks = _collect(fn("https://api.example.com/v1/chat/completions", "m", [{"role": "user", "content": "q"}]))
    assert json.loads(chunks[0][6:]) == {"type": "self_billed"}
    assert "web_search" in seen["relevant_tools"] and "search_history" in seen["relevant_tools"]
    assert "bash" not in seen["relevant_tools"]
    # No workspace: no file tools either.
    assert "read_file" not in seen["relevant_tools"]
    policy = seen["tool_policy"]
    assert policy.disable_mcp and policy.blocks("bash") and policy.blocks("send_email")
    assert not policy.blocks("web_search")
    # Nobody can approve mid-council: the tool level is the go-ahead, and the
    # policy keeps research to research tools.
    assert seen["gated_actions"] == "allow"
    assert seen["workspace"] is None
    # No output cap, so a reasoning model can still answer after thinking.
    assert seen["max_tokens"] == 0


def test_full_api_seat_runs_tools_in_a_scratch_folder(monkeypatch):
    import os

    seen = {}

    async def fake_loop(**kwargs):
        seen.update(kwargs, existed=os.path.isdir(kwargs["workspace"]))
        yield _sse({"delta": "ok"})

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: "openai")
    fn = council_tools.stream_fn_for(_settings("full"))
    _collect(fn("https://api.example.com/v1/chat/completions", "m", [{"role": "user", "content": "q"}]))
    # Full tools are the admin's go-ahead, as for the Claude Code / Codex seats.
    assert seen["gated_actions"] == "allow"
    assert "bash" in seen["relevant_tools"] and "python" in seen["relevant_tools"]
    # Shell and Python start in a throwaway folder, not the app's data folder.
    assert seen["existed"] and "ody-council-" in seen["workspace"]
    assert not os.path.exists(seen["workspace"])


def test_full_api_seat_uses_the_chosen_workspace(monkeypatch, tmp_path):
    seen = {}

    async def fake_loop(**kwargs):
        seen.update(kwargs)
        yield _sse({"delta": "ok"})

    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: "openai")
    fn = council_tools.stream_fn_for(_settings("full", str(tmp_path)))
    _collect(fn("https://api.example.com/v1/chat/completions", "m", [{"role": "user", "content": "q"}]))
    assert seen["workspace"] == str(tmp_path) and tmp_path.exists()
    assert seen["gated_actions"] == "allow"


def test_tools_off_uses_plain_streaming(monkeypatch):
    calls = []

    async def fake_stream_llm(url, model, messages, **kw):
        calls.append(messages)
        yield _sse({"delta": "plain"})

    monkeypatch.setattr("src.llm_core.stream_llm", fake_stream_llm)
    fn = council_tools.stream_fn_for(_settings("off"))
    _collect(fn("u", "m", [{"role": "system", "content": "SYS"}]))
    assert calls[0][0]["content"] == "SYS"


# ── Seat credentials: no ambient GitHub or SSH login ──

OVERRIDES = {"GH_TOKEN": None, "SSH_AUTH_SOCK": None, "GH_CONFIG_DIR": "/seat/gh"}


def _ambient_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_ambient")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/owner-agent.sock")


def _without_credentials(env):
    return "GH_TOKEN" not in env and "SSH_AUTH_SOCK" not in env and env["GH_CONFIG_DIR"] == "/seat/gh"


def test_claude_cli_starts_with_the_seat_env_overrides(monkeypatch):
    from src import claude_subscription as cs

    _ambient_credentials(monkeypatch)
    monkeypatch.setattr(cs, "find_cli", lambda: "/usr/local/bin/claude")
    started = []
    monkeypatch.setattr(cs.subprocess, "Popen", lambda args, **kw: started.append(kw["env"]))

    def popen_env(tools):
        inv = cs._Invocation("https://claude-subscription.invalid/a", "claude-opus-5-5",
                             [{"role": "user", "content": "q"}], credentials=(cs.AUTH_MODE_HOST, None),
                             tools=tools)
        try:
            cs._popen(inv)
        finally:
            inv.cleanup()
        return started.pop()

    assert _without_credentials(popen_env(NativeTools(env=OVERRIDES)))
    # No overrides: exactly the environment the CLI always got.
    assert popen_env(NativeTools()) == cs.cli_env(cs.AUTH_MODE_HOST, None)
    assert popen_env(None) == cs.cli_env(cs.AUTH_MODE_HOST, None)


def test_codex_cli_starts_with_the_seat_env_overrides(monkeypatch):
    _ambient_credentials(monkeypatch)
    monkeypatch.setattr(codex_cli, "find_cli", lambda: "/usr/local/bin/codex")
    started = []

    def fake_popen(args, **kw):
        started.append(kw["env"])
        raise OSError("not started in tests")

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)

    def popen_env(**kw):
        _collect(codex_cli.stream(codex_cli.CodexRun(model="m", prompt="p", access_token="t", **kw)))
        return started.pop()

    assert _without_credentials(popen_env(env=OVERRIDES))
    plain = popen_env()
    assert plain == codex_cli._env(plain["CODEX_HOME"])


def test_agent_loop_shell_starts_with_the_seat_env_overrides(monkeypatch):
    from src import agent_tools, tool_execution

    _ambient_credentials(monkeypatch)
    built = []

    async def fake_bash(content, ctx):
        built.append(ctx["subproc_env"])
        return {"output": "", "exit_code": 0}

    async def fake_loop(**kwargs):
        # The real loop runs each tool call in a task of its own.
        await asyncio.create_task(tool_execution._direct_fallback("bash", "git push"))
        yield _sse({"delta": "ok"})

    monkeypatch.setitem(agent_tools.TOOL_HANDLERS, "bash", fake_bash)
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)
    seat = council_tools.ToolSettings(mode="full", owner="dinis", env=OVERRIDES)
    _collect(council_tools._agent_stream("u", "m", [], None, seat, "foreground"))
    assert _without_credentials(built.pop())
    # No overrides, in or outside a council run: exactly what the shell always got.
    plain = {**os.environ, "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40",
             "HOME": tool_execution._AGENT_WORKDIR}
    _collect(council_tools._agent_stream("u", "m", [], None, _settings("full"), "foreground"))
    assert built.pop() == plain
    asyncio.run(tool_execution._direct_fallback("bash", "ls"))
    assert built.pop() == plain


def test_credential_free_git_env_covers_gh_git_and_ssh(tmp_path):
    env = council_tools.credential_free_git_env(str(tmp_path))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "SSH_AUTH_SOCK"):
        assert key in env and env[key] is None
    assert os.path.dirname(env["GH_CONFIG_DIR"]) == str(tmp_path)
    assert os.path.dirname(env["GIT_CONFIG_GLOBAL"]) == str(tmp_path)
    assert os.path.isfile(env["GIT_CONFIG_GLOBAL"])
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
    ssh = env["GIT_SSH_COMMAND"].split()
    assert ssh[0] == "ssh" and ssh[ssh.index("-F") + 1] == "/dev/null"
    assert {"IdentitiesOnly=yes", "IdentityFile=/dev/null", "IdentityAgent=none", "BatchMode=yes"} <= set(ssh)


def test_real_git_sees_no_credential_helper(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git is not installed")
    overrides = council_tools.credential_free_git_env(str(tmp_path))
    env = {k: v for k, v in {**os.environ, **overrides}.items() if v is not None}
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True, capture_output=True)
    out = subprocess.run(["git", "config", "--get-all", "credential.helper"], cwd=repo, env=env,
                         capture_output=True, text=True)
    # Only the empty value that resets the helper list; no system, Xcode or
    # user-level helper (osxkeychain, gh auth git-credential, ...) is read.
    assert [helper for helper in out.stdout.splitlines() if helper.strip()] == []


@pytest.mark.parametrize("mode", ["research", "full"])
@pytest.mark.parametrize("provider", ["claude-subscription", "chatgpt-subscription", "openai"])
def test_tool_turns_run_without_the_owners_git_credentials(monkeypatch, data_dir, provider, mode):
    from src import tool_execution

    seen = {}
    made = []
    real = council_tools.credential_free_git_env
    monkeypatch.setattr(council_tools, "credential_free_git_env", lambda d: made.append(real(d)) or made[-1])

    def record(env):
        seen.update(env=env, dirs=os.listdir(data_dir))

    async def fake_stream_chat(url, model, messages, *, timeout=None, effort=None, credentials=None, tools=None):
        record(tools.env)
        yield _sse({"delta": "ok"})

    async def fake_codex(run, *, timeout=None):
        record(run.env)
        yield _sse({"delta": "ok"})

    async def fake_loop(**kwargs):
        record(tool_execution._subprocess_env_overrides.get())
        yield _sse({"delta": "ok"})

    monkeypatch.setattr("src.claude_subscription.stream_chat", fake_stream_chat)
    monkeypatch.setattr(codex_cli, "stream", fake_codex)
    monkeypatch.setattr(codex_cli, "sandbox_works", lambda: True)
    monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_loop)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: provider)
    fn = council_tools.stream_fn_for(_settings(mode))
    _collect(fn("u", "m", [{"role": "user", "content": "q"}], headers={"Authorization": "Bearer t"}))
    assert seen["env"] == made[0]
    # One throwaway folder under the data folder for the run, gone once it ends.
    assert len(seen["dirs"]) == 1
    assert made[0]["GIT_CONFIG_GLOBAL"].startswith(os.path.join(str(data_dir), seen["dirs"][0]))
    assert os.listdir(data_dir) == []


def test_tools_off_turns_get_no_credential_overrides(monkeypatch, data_dir):
    calls = []

    async def fake_stream_llm(url, model, messages, **kw):
        calls.append(kw)
        yield _sse({"delta": "plain"})

    monkeypatch.setattr("src.llm_core.stream_llm", fake_stream_llm)
    _collect(council_tools.stream_fn_for(_settings("off"))("u", "m", [{"role": "user", "content": "q"}]))
    assert calls == [{"headers": None, "timeout": 600, "workload": "foreground"}]
    assert os.listdir(data_dir) == []


def test_agent_approval_requests_are_refused(monkeypatch):
    consumed = {}

    class Store:
        def consume(self, approval_id, **kw):
            consumed.update(id=approval_id, **kw)

    monkeypatch.setattr("src.tool_approvals.tool_approval_store", Store())
    chunk = _sse({"type": "tool_output", "tool": "bash", "output": "",
                  "ask_user": {"kind": "tool_approval", "approval_id": "ap-1"}})
    out = json.loads(council_tools._approval_refused(chunk, "dinis")[6:])
    assert out["exit_code"] == 1 and "approval" in out["output"] and "ask_user" not in out
    assert consumed == {"id": "ap-1", "decision": "deny", "owner": "dinis", "session_id": None}


# ── Event translation ──

def test_claude_translator_emits_tool_events():
    tr = StreamTranslator("claude-opus-5-5", native_tools=True)
    out = []
    for obj in [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me check."}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1",
                                                       "name": "mcp__odysseus__search_history",
                                                       "input": {"query": "olive"}}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1",
                                                       "name": "mcp__odysseus__search_history",
                                                       "input": {"query": "olive"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                  "content": [{"type": "text", "text": "Farm plan"}]}]}},
    ]:
        out += tr.feed(obj)
    events = [json.loads(c[6:]) for c in out]
    assert [e["type"] for e in events] == ["tool_start", "tool_output"]  # duplicate tool_use ignored
    assert events[0]["tool"] == "search_history" and events[0]["command"] == "olive"
    assert events[1]["output"] == "Farm plan" and events[1]["exit_code"] == 0


def test_codex_translator_maps_items():
    tr = codex_cli.EventTranslator()
    lines = [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "thinking"}},
        {"type": "item.completed", "item": {"id": "a0", "type": "agent_message", "text": "Checking the web."}},
        {"type": "item.started", "item": {"id": "w", "type": "web_search", "query": "olive prices"}},
        {"type": "item.completed", "item": {"id": "w", "type": "web_search", "query": "olive prices"}},
        {"type": "item.started", "item": {"id": "c", "type": "command_execution", "command": "ls",
                                          "status": "in_progress"}},
        {"type": "item.completed", "item": {"id": "c", "type": "command_execution", "command": "ls",
                                            "aggregated_output": "a.txt\n", "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "m", "type": "mcp_tool_call", "server": "odysseus",
                                            "tool": "search_history", "arguments": {"query": "farm"},
                                            "result": {"content": [{"type": "text", "text": "Farm plan"}]},
                                            "status": "completed"}},
        {"type": "error", "message": "Reconnecting... 1/5"},
        {"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": "Final."}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20,
                                             "reasoning_output_tokens": 5}},
    ]
    out = []
    for obj in lines:
        out += tr.feed(obj)
    events = [json.loads(c[6:]) if c.startswith("data: {") else c for c in out]
    kinds = [e.get("type") or ("delta" if "delta" in e else e) if isinstance(e, dict) else e for e in events]
    assert kinds == ["delta", "delta", "tool_start", "tool_output", "tool_start", "tool_output",
                     "tool_start", "tool_output", "delta", "usage", "data: [DONE]\n\n"]
    assert events[0]["thinking"] is True
    assert events[5]["output"] == "a.txt\n" and events[5]["exit_code"] == 0
    assert events[6]["tool"] == "search_history" and events[7]["output"] == "Farm plan"
    assert events[9]["data"] == {"input_tokens": 100, "output_tokens": 25}


def test_codex_web_search_waits_for_its_query_and_lists_results():
    """Codex 0.16x starts a web search with an empty query and fills it in on
    completion (shape captured from a real run)."""
    tr = codex_cli.EventTranslator()
    out = tr.feed({"type": "item.started", "item": {"id": "exec-1", "type": "web_search", "query": "",
                                                    "action": {"type": "other"}}})
    assert out == []
    out = tr.feed({"type": "item.completed", "item": {
        "id": "exec-1", "type": "web_search", "query": "IANA example domains",
        "action": {"type": "search", "query": "IANA example domains"},
        "results": [{"type": "text_result", "domain": "www.iana.org", "title": "Example Domains",
                     "url": "https://www.iana.org/help/example-domains", "snippet": "..."}]}})
    start, done = (json.loads(c[6:]) for c in out)
    assert start == {"type": "tool_start", "tool": "web_search", "command": "IANA example domains", "id": "exec-1"}
    assert done["output"] == "Example Domains - https://www.iana.org/help/example-domains"


def test_codex_seat_runs_without_connectors_and_without_a_broken_sandbox(monkeypatch):
    seen = []

    async def fake_stream(run, *, timeout=None):
        seen.append(run)
        yield _sse({"delta": "ok"})

    monkeypatch.setattr(codex_cli, "stream", fake_stream)
    monkeypatch.setattr("src.llm_core._detect_provider", lambda url: "chatgpt-subscription")
    for mode, sandbox_ok in (("research", False), ("research", True), ("full", False)):
        monkeypatch.setattr(codex_cli, "sandbox_works", lambda ok=sandbox_ok: ok)
        _collect(council_tools.stream_fn_for(_settings(mode))(
            "https://chatgpt.com/backend-api/codex", "gpt-6-luna", [{"role": "user", "content": "q"}],
            headers={"Authorization": "Bearer t"}))
    research_docker, research_host, full = seen
    for run in seen:
        # The user's ChatGPT connectors (Gmail, Slack, ...) never sit on the council.
        assert {"features.apps=false", "features.plugins=false"} <= set(run.extra_config)
    # Research shells run in Codex's own sandbox: off where it cannot start.
    assert "features.shell_tool=false" in research_docker.extra_config
    assert "features.shell_tool=false" not in research_host.extra_config
    assert "features.shell_tool=false" not in full.extra_config and full.sandbox == codex_cli.SANDBOX_FULL


def test_claude_research_without_workspace_reads_no_files(tmp_path):
    bare = council_tools.claude_native_tools(_settings("research"))
    assert not {"Read", "Glob", "Grep"} & set(bare.builtin) and "WebFetch" in bare.builtin
    with_ws = council_tools.claude_native_tools(_settings("research", str(tmp_path)))
    assert {"Read", "Glob", "Grep"} <= set(with_ws.builtin)


def test_review_and_synthesis_briefs_allow_for_different_tools():
    review = council_tools.tools_brief(_settings(), "review")
    assert "FINAL RANKING" in review and "own separate session" in review
    assert "own separate session" in council_tools.tools_brief(_settings(), "synthesis")
    assert "own separate session" not in council_tools.tools_brief(_settings(), "opinions")


def test_codex_translator_reports_failures():
    tr = codex_cli.EventTranslator()
    out = tr.feed({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}})
    assert out[0].startswith("event: error") and "Reconnect" in out[0]
    tr2 = codex_cli.EventTranslator()
    assert "not installed" in tr2.finish(127, "")[0]


def test_codex_auth_json_has_no_refresh_token():
    import base64

    claims = base64.urlsafe_b64encode(json.dumps(
        {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-9"}}).encode()).decode().rstrip("=")
    token = f"h.{claims}.s"
    auth = codex_cli.auth_json(token)
    assert auth["tokens"] == {"id_token": token, "access_token": token, "refresh_token": "", "account_id": "acct-9"}
    assert auth["OPENAI_API_KEY"] is None and auth["last_refresh"].endswith("Z")


def test_codex_args(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_CODEX_CONFIG", 'model_provider="x"\n')
    run = codex_cli.CodexRun(model="gpt-5.6-sol", prompt="p", access_token="t", effort="high",
                             mcp_servers={"odysseus": {"command": "/py", "args": ["/s.py"], "env": {"A": "b\"c"}}})
    args = codex_cli.build_args("codex", run, "/work")
    assert args[:3] == ["codex", "--search", "exec"]
    assert args[args.index("-s") + 1] == "read-only" and args[args.index("-C") + 1] == "/work"
    assert 'model_reasoning_effort="high"' in args
    assert 'mcp_servers.odysseus.env={A="b\\"c"}' in args
    assert 'model_provider="x"' in args and args[-1] == "-"
    full = codex_cli.build_args("codex", codex_cli.CodexRun(model="m", prompt="p", access_token="t",
                                                            sandbox=codex_cli.SANDBOX_FULL), "/w")
    assert "--dangerously-bypass-approvals-and-sandbox" in full and "-s" not in full


# ── run_member: tool timeline, notes vs answer, billing ──

def test_run_member_keeps_notes_out_of_the_answer():
    events = []

    async def emit(e):
        events.append(e)

    async def engine(url, model, messages, **kw):
        assert kw.get("stage") == "opinions"
        yield _sse({"delta": "Let me search. "})
        yield _sse({"type": "tool_start", "tool": "web_search", "command": "olive prices", "id": "1"})
        yield _sse({"type": "tool_output", "tool": "web_search", "output": "10k/ha", "exit_code": 0, "id": "1"})
        yield _sse({"delta": "About 10k EUR per hectare."})
        yield "data: [DONE]\n\n"

    engine.accepts_stage = True
    sink = {}
    out = asyncio.run(council.run_member(engine, _member(), [], stage="opinions", index=0, emit=emit,
                                         timeout=30, sink=sink))
    assert out["text"] == "About 10k EUR per hectare."
    assert out["tools"] == [
        {"kind": "note", "text": "Let me search."},
        {"kind": "tool", "tool": "web_search", "command": "olive prices", "id": "1", "status": "done",
         "output": "10k/ha"},
    ]
    assert sink["tools"] == out["tools"]
    tool_events = [e for e in events if e["type"] == "tool"]
    assert [e["phase"] for e in tool_events] == ["start", "done"]
    assert tool_events[0]["note"] == "Let me search."


def test_run_member_falls_back_to_last_note_and_skips_self_billed():
    async def emit(e):
        pass

    async def engine(url, model, messages, **kw):
        yield _sse({"type": "self_billed"})
        yield _sse({"delta": "The answer is 42."})
        yield _sse({"type": "tool_start", "tool": "todowrite", "command": "", "id": "1"})
        yield "data: [DONE]\n\n"

    out = asyncio.run(council.run_member(engine, _member(), [], stage="opinions", index=0, emit=emit, timeout=30))
    assert out["text"] == "The answer is 42."
    assert out["tools"][-1]["status"] == "done"  # closed when the run ended
    assert out["self_billed"] is True
    member = _member()
    result = {"opinions": [{**out, "member": 0}]}
    assert council.billable_calls(result, [member], member) == []


# ── Route: who may use what ──

class _Req:
    class state:
        current_user = "dinis"


def _ask(**kw):
    from routes.council_routes import AskRequest, Seat

    seat = Seat(endpoint_id="ep", model="m")
    return AskRequest(question="q", members=[seat], chairman=seat, **kw)


def test_full_tools_and_workspace_need_admin(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from routes.council_routes import _tool_settings

    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda user: False)
    with pytest.raises(HTTPException) as exc:
        _tool_settings(_Req(), "dinis", _ask(tools="full"))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        _tool_settings(_Req(), "dinis", _ask(tools="research", workspace=str(tmp_path)))
    assert _tool_settings(_Req(), "dinis", _ask(tools="research")).mode == "research"
    assert _tool_settings(_Req(), "dinis", _ask()).mode == "off"

    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda user: True)
    settings = _tool_settings(_Req(), "dinis", _ask(tools="full", workspace=str(tmp_path)))
    assert settings.full and settings.workspace == str(tmp_path.resolve())
    with pytest.raises(HTTPException) as exc:
        _tool_settings(_Req(), "dinis", _ask(tools="research", workspace="/"))
    assert exc.value.status_code == 400
