"""Claude Subscription provider: CLI transport, credentials, and provider wiring."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as dbmod
from core.database import Base, ModelEndpoint, ProviderAuthSession
from src import claude_subscription as cs

TOKEN = "sk-ant-oat01-" + "a" * 40


def _mem_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


# ---------------------------------------------------------------------------
# URLs, ids, prompt building
# ---------------------------------------------------------------------------

def test_sentinel_url_round_trip_and_lookalikes():
    url = cs.endpoint_base_url("abc123")
    assert url == "https://claude-subscription.invalid/abc123"
    assert cs.is_claude_subscription_base(url)
    assert cs.auth_id_from_url(url) == "abc123"
    assert cs.auth_id_from_url(url + "/v1/messages") == "abc123"
    assert not cs.is_claude_subscription_base("https://claude-subscription.invalid.evil.com/abc")
    assert not cs.is_claude_subscription_base("https://evil.com/claude-subscription.invalid")
    assert cs.auth_id_from_url("https://claude-subscription.invalid/../etc") == ""
    assert cs.auth_id_from_url("https://api.anthropic.com/abc123") == ""


@pytest.mark.parametrize("model,ok", [
    ("claude-opus-5-5", True),
    ("sonnet", True),
    ("claude-sonnet-5-5[1m]", True),
    ("claude-haiku-4-5-20251001", True),
    ("--dangerously-skip-permissions", False),
    ("-p", False),
    ("opus; rm -rf /", False),
    ("", False),
])
def test_model_ids_cannot_smuggle_cli_flags(model, ok):
    assert cs.valid_model_id(model) is ok


def test_token_validation():
    assert cs.valid_token(TOKEN)
    assert not cs.valid_token("short")
    assert not cs.valid_token("has spaces " + "a" * 30)
    assert not cs.valid_token(None)


def test_normalize_token_rejoins_a_terminal_wrapped_paste():
    wrapped = " " + TOKEN[:20] + "\r\n " + TOKEN[20:40] + "\n " + TOKEN[40:] + "\n"
    assert cs.normalize_token(wrapped) == TOKEN
    assert cs.valid_token(cs.normalize_token(wrapped))
    assert cs.normalize_token("  \n ") is None
    assert cs.normalize_token(None) is None


def test_build_prompt_single_user_turn_is_sent_verbatim():
    system, prompt = cs.build_prompt([
        {"role": "system", "content": "Be terse."},
        {"role": "system", "content": "Use metric."},
        {"role": "user", "content": "How far is the moon?"},
    ])
    assert system == "Be terse.\n\nUse metric."
    assert prompt == "How far is the moon?"


def test_build_prompt_replays_history_as_transcript():
    _system, prompt = cs.build_prompt([
        {"role": "user", "content": "My number is 41."},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": [{"type": "text", "text": "Plus one?"},
                                     {"type": "image_url", "image_url": {"url": "data:..."}}]},
    ])
    assert '<turn role="user">\nMy number is 41.\n</turn>' in prompt
    assert '<turn role="assistant">\nNoted.\n</turn>' in prompt
    assert prompt.rstrip().endswith("Plus one?\n[image omitted]")


def test_build_prompt_ending_on_assistant_asks_to_continue():
    _system, prompt = cs.build_prompt([
        {"role": "user", "content": "Write a poem."},
        {"role": "assistant", "content": "Roses are"},
    ])
    assert "Continue from where the conversation above left off." in prompt


def test_cli_args_never_carry_conversation_text(tmp_path):
    args = cs.cli_args("/bin/claude", "claude-opus-5-5", str(tmp_path / "system.md"))
    assert args[:2] == ["/bin/claude", "-p"]
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--system-prompt-file") + 1].endswith("system.md")
    assert "--no-session-persistence" in args
    assert "--dangerously-skip-permissions" not in args


def test_cli_env_keeps_subscription_billing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-token")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")

    token_env = cs.cli_env(cs.AUTH_MODE_TOKEN, TOKEN)
    assert "ANTHROPIC_API_KEY" not in token_env
    assert token_env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "CLAUDE_CODE_USE_BEDROCK" not in token_env

    host_env = cs.cli_env(cs.AUTH_MODE_HOST, None)
    assert "ANTHROPIC_API_KEY" not in host_env
    # Host mode is the machine's own Claude login, ambient settings included.
    assert host_env["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient-token"


# ---------------------------------------------------------------------------
# stream-json translation
# ---------------------------------------------------------------------------

def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("event: error"):
            out.append(("error", json.loads(chunk.split("data: ", 1)[1])))
        elif chunk.strip() == "data: [DONE]":
            out.append(("done", None))
        else:
            out.append(("data", json.loads(chunk[len("data: "):])))
    return out


def _delta(text, kind="text_delta", key="text"):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": kind, key: text}}}


def test_translator_streams_text_thinking_usage_and_done():
    t = cs.StreamTranslator("opus")
    chunks = []
    chunks += t.feed({"type": "system", "subtype": "init", "model": "claude-opus-5-5"})
    chunks += t.feed(_delta("hmm", "thinking_delta", "thinking"))
    chunks += t.feed(_delta("Hello "))
    chunks += t.feed(_delta("world"))
    chunks += t.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello world"}]}})
    chunks += t.feed({"type": "result", "subtype": "success", "is_error": False, "result": "Hello world",
                      "usage": {"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 2}})
    ev = _events(chunks)
    assert ev[0] == ("data", {"type": "model_actual", "requested_model": "opus", "model": "claude-opus-5-5"})
    assert ev[1] == ("data", {"delta": "hmm", "thinking": True})
    assert "".join(cs.iter_text(chunks)) == "Hello world"
    assert ("data", {"type": "usage", "data": {"input_tokens": 10, "output_tokens": 2}}) in ev
    assert ev[-1] == ("done", None)
    assert t.done and not t.failed


def test_translator_uses_full_message_when_cli_sends_no_partials():
    t = cs.StreamTranslator("claude-sonnet-5-5")
    chunks = t.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "Whole reply"}]}})
    chunks += t.feed({"type": "result", "subtype": "success", "is_error": False, "result": "Whole reply"})
    assert "".join(cs.iter_text(chunks)) == "Whole reply"


def test_translator_maps_login_failure_to_401_without_echoing_raw_text():
    t = cs.StreamTranslator("opus")
    chunks = t.feed({"type": "result", "subtype": "success", "is_error": True,
                     "result": "Invalid API key · Please run /login"})
    (kind, payload), = _events(chunks)
    assert kind == "error"
    assert payload["status"] == 401
    assert "Reconnect" in payload["text"]
    assert "Invalid API key" not in payload["text"]


def test_translator_usage_limit_is_429():
    t = cs.StreamTranslator("opus")
    chunks = t.feed({"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "result": "Claude AI usage limit reached|1791243600"})
    assert _events(chunks)[0][1]["status"] == 429


def test_translator_finish_without_result_is_an_error():
    t = cs.StreamTranslator("opus")
    (kind, payload), = _events(t.finish(1, "boom"))
    assert kind == "error" and payload["status"] == 502


# ---------------------------------------------------------------------------
# provisioning + runtime credentials
# ---------------------------------------------------------------------------

def test_provision_token_mode_creates_owner_scoped_endpoint(monkeypatch):
    Session = _mem_db(monkeypatch)
    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)
    assert res["models"] == cs.default_models()
    db = Session()
    try:
        auth = db.query(ProviderAuthSession).one()
        ep = db.query(ModelEndpoint).one()
        assert auth.provider == cs.CLAUDE_SUBSCRIPTION_PROVIDER
        assert auth.owner == "alice" and auth.auth_mode == "token"
        assert auth.access_token == TOKEN
        assert ep.owner == "alice"
        assert ep.base_url == cs.endpoint_base_url(auth.id)
        assert ep.api_key is None
        assert ep.provider_auth_id == auth.id
        assert ep.supports_tools is False
        assert ep.model_refresh_mode == "manual"
    finally:
        db.close()
    # The token is never part of the runtime credentials handed to callers.
    creds = cs.resolve_runtime_credentials(res["auth_id"], owner="alice")
    assert creds["api_key"] is None
    assert cs.load_credentials(res["auth_id"]) == ("token", TOKEN)


def test_provision_switching_to_host_mode_drops_the_token_and_reuses_rows(monkeypatch):
    Session = _mem_db(monkeypatch)
    first = cs.provision("bob", cs.AUTH_MODE_TOKEN, TOKEN)
    second = cs.provision("bob", cs.AUTH_MODE_HOST, None)
    assert first["id"] == second["id"]
    db = Session()
    try:
        assert db.query(ProviderAuthSession).count() == 1
        assert db.query(ProviderAuthSession).one().access_token is None
    finally:
        db.close()
    assert cs.load_credentials(second["auth_id"]) == ("host", None)


def test_provision_rejects_bad_token(monkeypatch):
    _mem_db(monkeypatch)
    with pytest.raises(ValueError):
        cs.provision("alice", cs.AUTH_MODE_TOKEN, "nope")


def test_runtime_credentials_are_owner_scoped(monkeypatch):
    _mem_db(monkeypatch)
    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)
    with pytest.raises(cs.ClaudeSubscriptionError):
        cs.resolve_runtime_credentials(res["auth_id"], owner="mallory")


def test_disconnect_removes_endpoint_and_token(monkeypatch):
    Session = _mem_db(monkeypatch)
    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)
    cs.provision("carol", cs.AUTH_MODE_TOKEN, TOKEN)
    out = cs.disconnect("alice")
    assert out == {"removed_endpoints": 1, "removed_auth": 1}
    assert cs.connection("alice") is None
    assert cs.connection("carol") is not None
    with pytest.raises(cs.ClaudeSubscriptionError):
        cs.load_credentials(res["auth_id"])
    db = Session()
    try:
        assert db.query(ModelEndpoint).count() == 1
    finally:
        db.close()


def test_resolver_and_headers_route_claude_endpoints_without_a_bearer(monkeypatch):
    _mem_db(monkeypatch)
    from src.endpoint_resolver import build_chat_url, build_headers, build_models_url, resolve_endpoint_runtime
    from src.llm_core import _detect_provider, _provider_label

    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)
    db = dbmod.SessionLocal()
    try:
        ep = db.query(ModelEndpoint).one()
        base, key = resolve_endpoint_runtime(ep, owner="alice")
    finally:
        db.close()
    assert base == res["base_url"] and key is None
    assert _detect_provider(base) == "claude-subscription"
    assert _provider_label(base) == "Claude Subscription"
    assert build_chat_url(base) == base
    assert build_headers(None, base) == {}
    assert build_models_url(base) is None


def test_chatgpt_endpoints_still_resolve_through_chatgpt(monkeypatch):
    from src import endpoint_resolver

    calls = []

    def fake_chatgpt(auth_id, owner=None):
        calls.append(auth_id)
        return {"base_url": "https://chatgpt.com/backend-api/codex", "api_key": "AT"}

    monkeypatch.setattr("src.chatgpt_subscription.resolve_runtime_credentials", fake_chatgpt)

    class Ep:
        base_url = "https://chatgpt.com/backend-api/codex"
        api_key = None
        provider_auth_id = "gpt1"

    assert endpoint_resolver.resolve_endpoint_runtime(Ep(), owner="a") == ("https://chatgpt.com/backend-api/codex", "AT")
    assert calls == ["gpt1"]


# ---------------------------------------------------------------------------
# end to end through a fake `claude` executable
# ---------------------------------------------------------------------------

FAKE_CLI = textwrap.dedent('''\
    #!{python}
    import json, sys
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
    model = args[args.index("--model") + 1]
    import os
    who = "token" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "{token}" else "host"
    if "HANG" in prompt:
        import time
        time.sleep(30)
    if "SLOW" in prompt:
        import time
        for part in ("one ", "two ", "three"):
            print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
                              "delta": {{"type": "text_delta", "text": part}}}}}}), flush=True)
            time.sleep(0.6)
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "x"}}))
        sys.exit(0)
    if "FAIL" in prompt:
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                          "result": "Invalid API key · Please run /login"}}))
        sys.exit(1)
    print(json.dumps({{"type": "system", "subtype": "init", "model": model}}))
    reply = "model=%s auth=%s system=%s prompt=%s" % (model, who, system, prompt)
    for i in range(0, len(reply), 7):
        print(json.dumps({{"type": "stream_event", "event": {{"type": "content_block_delta",
                          "delta": {{"type": "text_delta", "text": reply[i:i+7]}}}}}}), flush=True)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": reply,
                      "usage": {{"input_tokens": 5, "output_tokens": 9}}}}))
''')


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("shebang executables are POSIX-only")
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.format(python=sys.executable, token=TOKEN))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(script))
    return script


def test_stream_llm_runs_the_cli_with_the_stored_token(monkeypatch, fake_cli):
    _mem_db(monkeypatch)
    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)
    from src.llm_core import stream_llm

    async def run():
        chunks = []
        async for chunk in stream_llm(res["base_url"], "claude-opus-5-5", [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hello"},
        ], tools=[{"type": "function"}]):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    text = "".join(cs.iter_text(chunks))
    assert text == "model=claude-opus-5-5 auth=token system=SYS prompt=hello"
    assert chunks[-1].strip() == "data: [DONE]"


def test_llm_call_async_collects_and_surfaces_auth_errors(monkeypatch, fake_cli):
    _mem_db(monkeypatch)
    from fastapi import HTTPException
    from src.llm_core import llm_call, llm_call_async

    res = cs.provision("alice", cs.AUTH_MODE_HOST, None)
    text = asyncio.run(llm_call_async(res["base_url"], "sonnet", [{"role": "user", "content": "hi"}]))
    assert text == "model=sonnet auth=host system=%s prompt=hi" % cs.DEFAULT_SYSTEM_PROMPT
    assert llm_call(res["base_url"], "haiku", [{"role": "user", "content": "sync"}]).endswith("prompt=sync")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(llm_call_async(res["base_url"], "sonnet", [{"role": "user", "content": "FAIL please"}]))
    assert exc.value.status_code == 401


def test_stream_chat_reports_missing_cli(monkeypatch):
    _mem_db(monkeypatch)
    monkeypatch.setenv("CLAUDE_CLI_PATH", "/definitely/not/here/claude")
    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)

    async def run():
        return [c async for c in cs.stream_chat(res["base_url"], "opus", [{"role": "user", "content": "x"}])]

    (kind, payload), = _events(asyncio.run(run()))
    assert kind == "error" and payload["status"] == 503


def test_stream_chat_after_disconnect_is_401(monkeypatch, fake_cli):
    _mem_db(monkeypatch)
    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)
    cs.disconnect("alice")

    async def run():
        return [c async for c in cs.stream_chat(res["base_url"], "opus", [{"role": "user", "content": "x"}])]

    (kind, payload), = _events(asyncio.run(run()))
    assert kind == "error" and payload["status"] == 401


def test_timeout_is_idle_not_total(monkeypatch, fake_cli):
    _mem_db(monkeypatch)
    res = cs.provision("alice", cs.AUTH_MODE_HOST, None)

    async def run(prompt, timeout):
        return [c async for c in cs.stream_chat(res["base_url"], "opus", [{"role": "user", "content": prompt}],
                                                timeout=timeout)]

    # 1.8 s of output with 0.6 s gaps finishes under a 1 s idle timeout...
    slow = asyncio.run(run("SLOW", 1))
    assert "".join(cs.iter_text(slow)) == "one two three"
    assert slow[-1].strip() == "data: [DONE]"
    # ...while a CLI that goes silent is cut off and killed.
    (kind, payload), = _events(asyncio.run(run("HANG", 1)))
    assert kind == "error" and payload["status"] == 504


def test_short_legacy_auth_ids_are_reissued_keeping_the_endpoint(monkeypatch):
    Session = _mem_db(monkeypatch)
    db = Session()
    db.add(ProviderAuthSession(id="abc123def456", provider=cs.CLAUDE_SUBSCRIPTION_PROVIDER, owner="alice",
                               label="Claude Subscription", base_url=cs.endpoint_base_url("abc123def456"),
                               access_token=TOKEN, auth_mode="token"))
    db.add(ModelEndpoint(id="ep-old", name="Claude Subscription", base_url=cs.endpoint_base_url("abc123def456"),
                         owner="alice", provider_auth_id="abc123def456", model_type="llm"))
    db.commit()
    db.close()

    res = cs.provision("alice", cs.AUTH_MODE_TOKEN, TOKEN)

    assert res["id"] == "ep-old"                      # settings that point at it keep working
    assert len(res["auth_id"]) == 32 and res["auth_id"] != "abc123def456"
    assert res["base_url"] == cs.endpoint_base_url(res["auth_id"])
    with pytest.raises(cs.ClaudeSubscriptionError):
        cs.load_credentials("abc123def456")


def test_shared_legacy_connection_resolves_for_a_signed_in_user(monkeypatch):
    _mem_db(monkeypatch)
    res = cs.provision(None, cs.AUTH_MODE_HOST, None)
    assert cs.resolve_runtime_credentials(res["auth_id"], owner="alice")["base_url"] == res["base_url"]


def test_cli_workdir_is_private_and_stable():
    first = cs._workdir()
    assert first == cs._workdir()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(first).st_mode) == 0o700


def test_llm_call_async_drops_thinking_deltas(monkeypatch):
    from src import llm_core

    async def fake_stream(url, model, messages, **kwargs):
        yield 'data: {"delta": "let me think", "thinking": true}\n\n'
        yield 'data: {"delta": "answer"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    url = cs.endpoint_base_url("f" * 32)
    assert asyncio.run(llm_core.llm_call_async(url, "opus", [{"role": "user", "content": "thinking-test"}])) == "answer"


TOOL_SYSTEM = (
    "To use a tool, write a fenced code block with the tool name as the language tag.\n"
    "- ```list_models``` — list models\n- ```chat_with_model``` — ask a model\n"
)


def test_text_tool_names_only_in_tool_mode():
    assert cs.text_tool_names(TOOL_SYSTEM) == {"list_models", "chat_with_model"}
    assert cs.text_tool_names("You are a helpful assistant.") is None


def _feed_text(t, *parts):
    chunks = []
    for part in parts:
        chunks += t.feed(_delta(part))
    return chunks


def test_stream_stops_after_a_complete_xml_tool_call():
    t = cs.StreamTranslator("haiku", stop_tools={"list_models"})
    chunks = _feed_text(t, "Let me check.\n<function_calls>\n<invoke name=\"list_models\">\n</inv",
                        "oke>\n</function_calls>\n\nPerfect! The models are", " gpt and claude.")
    text = "".join(cs.iter_text(chunks))
    assert text.endswith("</function_calls>")
    assert "Perfect" not in text                      # no invented tool result
    assert t.done and t.stopped_at_tool_call
    assert chunks[-1].strip() == "data: [DONE]"
    assert t.feed(_delta("more")) == []


def test_stream_stops_after_an_empty_body_tool_fence():
    t = cs.StreamTranslator("haiku", stop_tools={"list_models"})
    chunks = _feed_text(t, "```list_models\n``", "`\n\nI'll get the list first, then", " ask the model.")
    text = "".join(cs.iter_text(chunks))
    assert text == "```list_models\n```"
    assert t.done and t.stopped_at_tool_call


def test_stream_stops_after_a_fenced_tool_block_but_not_ordinary_code():
    t = cs.StreamTranslator("haiku", stop_tools={"chat_with_model"})
    chunks = _feed_text(t, "Example:\n```python\nprint(1)\n```\nNow asking:\n```chat_with_model\nep::m\nhi\n",
                        "```\nThe model said 42.")
    text = "".join(cs.iter_text(chunks))
    assert "```python\nprint(1)\n```" in text         # a plain code block does not stop the stream
    assert text.endswith("```chat_with_model\nep::m\nhi\n```")
    assert "42" not in text


def test_plain_chat_is_never_cut():
    t = cs.StreamTranslator("haiku")                  # no tool instructions in the prompt
    chunks = _feed_text(t, "Here is XML: <function_calls></function_calls> and more text.")
    assert "".join(cs.iter_text(chunks)).endswith("and more text.")
    assert not t.done
