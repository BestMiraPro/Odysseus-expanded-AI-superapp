"""Reasoning effort: level tables, who chose the level, payloads and the API.

A level comes from the place that picked the model (a Council seat, a Compare
column, Study, a Settings role), carried in the request headers as an internal
marker that llm_core must strip before anything leaves the process, or else
from the level the user saved for the model in the chat composer. Either is
clamped to what the model accepts and sent in the provider's own shape.
"""

import asyncio
import json
import time

import pytest

from src import chatgpt_subscription as cg
from src import llm_core
from src import reasoning_effort as re_

CLAUDE_SUB = "https://claude-subscription.invalid/abc123"
CHATGPT = "https://chatgpt.com/backend-api/codex"
WANDB = "https://api.inference.wandb.ai/v1"
ANTHROPIC = "https://api.anthropic.com/v1/messages"
OPENROUTER = "https://openrouter.ai/api/v1"
CLAUDE_ALL = ("low", "medium", "high", "xhigh", "max")


@pytest.fixture(autouse=True)
def _no_codex_lookup(monkeypatch):
    """Codex levels come from a fresh, empty cache unless a test fills it."""
    monkeypatch.setattr(cg, "_reasoning_levels_checked_at", time.time())
    monkeypatch.setattr(cg, "_reasoning_levels", {})


# ── Which levels each provider/model accepts ──

@pytest.mark.parametrize("model,levels,default", [
    ("claude-opus-5-5", CLAUDE_ALL, "medium"),
    ("claude-haiku-5-5", CLAUDE_ALL, "medium"),
    ("claude-sonnet-5-5", CLAUDE_ALL, "high"),
    ("claude-fable-5-1", CLAUDE_ALL, "high"),
    ("claude-opus-4-8", CLAUDE_ALL, "high"),
    ("claude-opus-4-6", ("low", "medium", "high", "max"), "high"),
    ("claude-sonnet-4-6", ("low", "medium", "high", "max"), "high"),
    ("claude-opus-4-5-20251101", ("low", "medium", "high"), "high"),
    ("claude-sonnet-4-5", (), None),
    ("claude-haiku-4-5-20251001", (), None),
])
def test_claude_levels_follow_the_model_table(model, levels, default):
    for provider, url in (("claude-subscription", CLAUDE_SUB), ("anthropic", ANTHROPIC)):
        assert re_.levels_for(provider, url, model) == (levels, default)


def test_claude_cli_aliases_take_every_level():
    assert re_.levels_for("claude-subscription", CLAUDE_SUB, "opus")[0] == CLAUDE_ALL


@pytest.mark.parametrize("provider,model,url,expected", [
    ("chatgpt-subscription", "gpt-5.6-sol", CHATGPT, ("low", "medium", "high", "xhigh")),
    ("chatgpt-subscription", "gpt-5.1-codex-max", CHATGPT, ("low", "medium", "high", "xhigh")),
    ("chatgpt-subscription", "gpt-5-codex", CHATGPT, ("low", "medium", "high")),
    ("chatgpt-subscription", "gpt-new", CHATGPT, ("low", "medium", "high")),
    ("openai", "gpt-5.6-luna", "https://api.openai.com/v1", ("low", "medium", "high", "xhigh")),
    ("openai", "gpt-5.1", "https://api.openai.com/v1", ("low", "medium", "high")),
    ("openai", "o4-mini", "https://api.openai.com/v1", ("low", "medium", "high")),
    ("openai", "gpt-4.1", "https://api.openai.com/v1", ()),
    ("openai", "gpt-5.6-luna", "https://example.com/v1", ()),  # same name, unknown server
    ("openai", "gemini-3-pro", "https://generativelanguage.googleapis.com/v1beta/openai", ("low", "medium", "high")),
    ("openai", "openai/gpt-oss-120b", WANDB, ("low", "medium", "high")),
    ("openai", "deepseek-ai/DeepSeek-V3.1", WANDB, ()),
    ("openai", "Qwen/Qwen3.8-27B", WANDB, ()),
    ("groq", "openai/gpt-oss-20b", "https://api.groq.com/openai/v1", ("low", "medium", "high")),
    ("ollama", "gpt-oss:20b", "http://localhost:11434", ("low", "medium", "high")),
    ("ollama", "qwen3:8b", "http://localhost:11434", ()),
    ("openrouter", "anthropic/claude-opus-5-5", OPENROUTER, ("low", "medium", "high")),
    ("openrouter", "openai/gpt-4o", OPENROUTER, ()),
    ("openrouter", "meta-llama/llama-3.3-70b", OPENROUTER, ()),
    ("mistral", "magistral-medium-latest", "", ("none", "low", "medium", "high")),
    ("mistral", "codestral-latest", "", ()),
])
def test_levels_only_where_documented(provider, model, url, expected):
    assert re_.levels_for(provider, url, model)[0] == expected


def test_chatgpt_levels_come_from_the_codex_models_endpoint():
    cg._record_reasoning_levels([
        {"slug": "gpt-6.1-sol", "default_reasoning_level": "low",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
        {"slug": "gpt-5.5", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
    ])
    assert re_.levels_for("chatgpt-subscription", CHATGPT, "gpt-6.1-sol") == (CLAUDE_ALL, "low")
    assert re_.levels_for("chatgpt-subscription", CHATGPT, "gpt-5.5") == (
        ("low", "medium", "high", "xhigh"), "medium")


def test_clamp_picks_highest_supported_at_or_below():
    four_six = ("low", "medium", "high", "max")
    assert re_.clamp("xhigh", four_six) == "high"
    assert re_.clamp("max", ("low", "medium", "high")) == "high"
    assert re_.clamp("max", four_six) == "max"
    assert re_.clamp("low", ("medium", "high")) == "medium"
    assert re_.clamp("none", CLAUDE_ALL) == "low"
    assert re_.clamp("high", ()) is None
    assert re_.clamp("bogus", four_six) is None


def test_effective_clamps_to_the_model():
    assert re_.effective("claude-subscription", CLAUDE_SUB, "claude-sonnet-4-6", "xhigh") == "high"
    assert re_.effective("claude-subscription", CLAUDE_SUB, "claude-sonnet-5-5", "xhigh") == "xhigh"
    assert re_.effective("claude-subscription", CLAUDE_SUB, "claude-haiku-4-5-20251001", "low") is None
    assert re_.effective("claude-subscription", CLAUDE_SUB, "claude-sonnet-5-5", None) is None


def test_normalize_aliases():
    assert re_.normalize("Extra high") == "xhigh"
    assert re_.normalize("x-high") == "xhigh"
    assert re_.normalize("MAX") == "max"
    assert re_.normalize("turbo") is None


# ── A place's own level: the header marker ──

def test_effort_round_trips_through_headers_and_is_removed():
    headers = re_.with_effort({"Authorization": "Bearer k"}, "HIGH")
    assert headers == {"Authorization": "Bearer k", re_.EFFORT_HEADER: "high"}
    effort, rest = re_.take_effort(headers)
    assert effort == "high" and rest == {"Authorization": "Bearer k"}


def test_take_effort_is_case_insensitive_and_reads_json_headers():
    effort, rest = re_.take_effort(json.dumps({"x-odysseus-reasoning-effort": "max", "A": "1"}))
    assert effort == "max" and rest == {"A": "1"}


def test_unset_or_unknown_effort_leaves_no_marker():
    assert re_.with_effort({"A": "1"}, "") == {"A": "1"}
    assert re_.with_effort({"A": "1", re_.EFFORT_HEADER: "low"}, "turbo") == {"A": "1"}
    assert re_.with_effort(None, None) == {}


def test_session_headers_carry_only_the_sessions_own_level():
    class Sess:
        headers = {"A": "1"}
        reasoning_effort = None

    assert re_.session_headers(Sess()) == {"A": "1"}
    Sess.reasoning_effort = "max"
    assert re_.session_headers(Sess())[re_.EFFORT_HEADER] == "max"


def test_a_places_level_wins_over_the_saved_one(monkeypatch):
    monkeypatch.setattr(re_, "user_levels", lambda owner: {"claude-opus-5-5": "max"})
    with re_.for_owner("alice"):
        assert re_.resolve("anthropic", ANTHROPIC, "claude-opus-5-5", {})[0] == "max"
        effort, rest = re_.resolve("anthropic", ANTHROPIC, "claude-opus-5-5",
                                   re_.with_effort({"A": "1"}, "low"))
        assert effort == "low" and rest == {"A": "1"}
    # Outside a user's scope only the place's own level applies.
    assert re_.resolve("anthropic", ANTHROPIC, "claude-opus-5-5", {})[0] is None


def _resolve_role(monkeypatch, prefix, stored):
    """resolve_endpoint for ``prefix`` against ``stored`` settings and one endpoint."""
    import src.settings as settings
    from src import endpoint_resolver

    monkeypatch.setattr(settings, "load_settings", lambda: {})
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner="", default=None: stored.get(key, default))

    class _Query:
        def filter(self, *a):
            return self

        def first(self):
            return object()

    class _Db:
        def query(self, *a):
            return _Query()

        def close(self):
            pass

    monkeypatch.setattr(endpoint_resolver, "SessionLocal", _Db)
    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint_runtime", lambda ep, owner=None: (ANTHROPIC, "k"))
    monkeypatch.setattr(endpoint_resolver, "_endpoint_hidden_models", lambda ep: set())
    return endpoint_resolver.resolve_endpoint(prefix)


def test_a_role_effort_rides_in_the_resolved_headers(monkeypatch):
    _, model, headers = _resolve_role(monkeypatch, "study", {
        "study_endpoint_id": "ep1", "study_model": "claude-opus-5-5", "study_effort": "high"})
    assert model == "claude-opus-5-5"
    assert re_.take_effort(headers)[0] == "high"


def test_study_keeps_its_effort_while_borrowing_the_utility_model(monkeypatch):
    _, model, headers = _resolve_role(monkeypatch, "study", {
        "utility_endpoint_id": "ep1", "utility_model": "claude-sonnet-5-5",
        "utility_effort": "low", "study_effort": "max"})
    assert model == "claude-sonnet-5-5"
    assert re_.take_effort(headers)[0] == "max"


def test_the_default_role_has_no_effort_of_its_own(monkeypatch):
    # Chat effort is set per model in the composer; a stray default_effort
    # (from an earlier build) must not reach background work.
    _, _, headers = _resolve_role(monkeypatch, "default", {
        "default_endpoint_id": "ep1", "default_model": "claude-opus-5-5", "default_effort": "max"})
    assert re_.take_effort(headers)[0] is None


# ── What actually goes on the wire ──

class _Resp:
    status_code = 200

    async def aiter_lines(self):
        for ln in ('data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"):
            yield ln

    async def aread(self):
        return b""


class _Ctx:
    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *a):
        return False


class _CaptureClient:
    def __init__(self):
        self.calls = []

    def stream(self, method, url, **kw):
        self.calls.append(kw)
        return _Ctx()


@pytest.fixture
def capture(monkeypatch):
    client = _CaptureClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    return client


def _sent(capture, monkeypatch, url, model, saved=None, headers=None, tools=None):
    """The request body stream_llm sends with ``saved`` levels in scope."""
    monkeypatch.setattr(re_, "user_levels", lambda owner: dict(saved or {}))

    async def run():
        with re_.for_owner("alice"):
            async for _ in llm_core.stream_llm(
                url, model, [{"role": "user", "content": "hi"}],
                headers=headers if headers is not None else {"Authorization": "Bearer k"}, tools=tools,
            ):
                pass

    asyncio.run(run())
    assert capture.calls, "no request was sent"
    return capture.calls[0]


def test_anthropic_gets_output_config_effort(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.anthropic.com/v1", "claude-opus-5-5",
                    {"claude-opus-5-5": "xhigh"})["json"]
    assert payload["output_config"] == {"effort": "xhigh"}
    assert "thinking" not in payload


def test_anthropic_clamps_to_model_range(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.anthropic.com/v1", "claude-opus-4-6",
                    {"claude-opus-4-6": "xhigh"})["json"]
    assert payload["output_config"] == {"effort": "high"}


def test_anthropic_model_without_effort_sends_nothing(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001",
                    {"claude-haiku-4-5-20251001": "high"})["json"]
    assert "output_config" not in payload


def test_saved_effort_is_per_model(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.anthropic.com/v1", "claude-sonnet-5-5",
                    {"claude-opus-5-5": "max"})["json"]
    assert "output_config" not in payload


def test_a_seat_level_beats_the_saved_one_on_the_wire(capture, monkeypatch):
    sent = _sent(capture, monkeypatch, "https://api.anthropic.com/v1", "claude-opus-5-5",
                 {"claude-opus-5-5": "max"}, headers=re_.with_effort({"x-api-key": "k"}, "low"))
    assert sent["json"]["output_config"] == {"effort": "low"}
    assert not any(k.lower() == re_.EFFORT_HEADER.lower() for k in sent["headers"])


def test_openai_gets_reasoning_effort(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.openai.com/v1", "gpt-5.6-luna",
                    {"gpt-5.6-luna": "max"})["json"]
    assert payload["reasoning_effort"] == "xhigh"


def test_openai_tools_restriction_still_wins(capture, monkeypatch):
    tool = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
    payload = _sent(capture, monkeypatch, "https://api.openai.com/v1", "gpt-5.6-luna",
                    {"gpt-5.6-luna": "high"}, tools=[tool])["json"]
    assert payload["reasoning_effort"] == "none"


def test_openai_compatible_stream_strips_the_marker(capture, monkeypatch):
    sent = _sent(capture, monkeypatch, WANDB, "openai/gpt-oss-120b",
                 headers=re_.with_effort({"Authorization": "Bearer k"}, "high"))
    assert sent["json"]["reasoning_effort"] == "high"
    assert not any(k.lower() == re_.EFFORT_HEADER.lower() for k in sent["headers"])


def test_unsupported_model_gets_no_effort_field(capture, monkeypatch):
    sent = _sent(capture, monkeypatch, WANDB, "deepseek-ai/DeepSeek-V3.1", headers=re_.with_effort({}, "high"))
    assert "reasoning_effort" not in sent["json"]


def test_openrouter_gets_reasoning_object(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, OPENROUTER, "anthropic/claude-opus-5-5",
                    {"anthropic/claude-opus-5-5": "max"})["json"]
    assert payload["reasoning"] == {"effort": "high"}


def test_mistral_choice_overrides_env_default(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.mistral.ai/v1", "magistral-medium-latest",
                    {"magistral-medium-latest": "low"})["json"]
    assert payload["reasoning_effort"] == "low"


def test_no_level_leaves_openai_payload_alone(capture, monkeypatch):
    payload = _sent(capture, monkeypatch, "https://api.openai.com/v1", "gpt-5.6-luna", {})["json"]
    assert "reasoning_effort" not in payload


def test_chatgpt_subscription_gets_reasoning_object(capture, monkeypatch):
    monkeypatch.setattr(cg, "_reasoning_levels",
                        {"gpt-6.1-sol": {"levels": list(CLAUDE_ALL), "default": "low"}})
    sent = _sent(capture, monkeypatch, CHATGPT, "gpt-6.1-sol",
                 headers=re_.with_effort({"Authorization": "Bearer k"}, "max"))
    assert sent["json"]["reasoning"] == {"effort": "max"}
    assert "temperature" not in sent["json"]
    assert not any(k.lower() == re_.EFFORT_HEADER.lower() for k in sent["headers"])


def _fake_claude_cli(monkeypatch):
    import src.claude_subscription as cs

    seen = []

    async def fake_stream_chat(url, model, messages, *, timeout=None, effort=None, credentials=None, tools=None):
        seen.append(effort)
        yield 'data: {"delta": "ok"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(cs, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(llm_core, "_detect_provider", lambda url: "claude-subscription")
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    return seen


def test_claude_subscription_passes_cli_effort(monkeypatch):
    seen = _fake_claude_cli(monkeypatch)
    monkeypatch.setattr(re_, "user_levels", lambda owner: {"claude-opus-5-5": "max"})

    async def run():
        with re_.for_owner("alice"):
            async for _ in llm_core.stream_llm(CLAUDE_SUB, "claude-opus-5-5",
                                               [{"role": "user", "content": "hi"}]):
                pass

    asyncio.run(run())
    assert seen == ["max"]


def test_one_shot_calls_keep_the_provider_default(monkeypatch):
    """Titles, memory extraction, ...: a saved chat level must not make them
    slow, even on a subscription that only streams; a caller's level still applies."""
    seen = _fake_claude_cli(monkeypatch)
    monkeypatch.setattr(re_, "user_levels", lambda owner: {"claude-opus-5-5": "max"})

    async def run():
        with re_.for_owner("alice"):
            await llm_core.llm_call_async(CLAUDE_SUB, "claude-opus-5-5",
                                          [{"role": "user", "content": "title this"}])
            await llm_core.llm_call_async(CLAUDE_SUB, "claude-opus-5-5",
                                          [{"role": "user", "content": "grade this"}],
                                          headers=re_.with_effort({}, "low"))

    asyncio.run(run())
    assert seen == [None, "low"]


def test_claude_cli_args_carry_effort():
    from src.claude_subscription import cli_args

    assert cli_args("claude", "claude-opus-5-5", "/tmp/s", "xhigh")[-2:] == ["--effort", "xhigh"]
    assert "--effort" not in cli_args("claude", "claude-opus-5-5", "/tmp/s", None)


# ── Per-user storage and API ──

@pytest.fixture
def prefs_file(tmp_path, monkeypatch):
    from routes import prefs_routes

    path = tmp_path / "prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(path))
    return path


def test_levels_are_saved_per_user(prefs_file):
    re_.save_user_level("alice", "claude-opus-5-5", "max")
    re_.save_user_level("bob", "claude-opus-5-5", "low")
    assert re_.user_levels("alice") == {"claude-opus-5-5": "max"}
    assert re_.user_levels("bob") == {"claude-opus-5-5": "low"}
    re_.save_user_level("alice", "claude-opus-5-5", None)
    assert re_.user_levels("alice") == {}


def _app():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes.model_roster_routes import setup_model_roster_routes

    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    app.include_router(setup_model_roster_routes())
    return TestClient(app)


def test_effort_api_round_trip(prefs_file):
    client = _app()
    q = {"model": "claude-opus-4-6", "url": "https://api.anthropic.com/v1"}

    state = client.get("/api/models/effort", params=q).json()
    assert [lvl["id"] for lvl in state["levels"]] == ["low", "medium", "high", "max"]
    assert state["default"] == "high"
    assert state["effort"] is None and state["applied"] is None

    state = client.put("/api/models/effort", json={**q, "effort": "xhigh"}).json()
    assert state["effort"] == "xhigh"
    assert state["applied"] == "high"
    assert re_.user_levels("alice") == {"claude-opus-4-6": "xhigh"}

    state = client.put("/api/models/effort", json={**q, "effort": None}).json()
    assert state["effort"] is None
    assert re_.user_levels("alice") == {}


def test_effort_api_rejects_unknown_level(prefs_file):
    r = _app().put("/api/models/effort", json={"model": "claude-opus-5-5", "effort": "turbo"})
    assert r.status_code == 400


def test_effort_api_hides_control_for_models_without_effort(prefs_file):
    state = _app().get("/api/models/effort", params={"model": "gpt-4.1", "url": "https://api.openai.com/v1"}).json()
    assert state["levels"] == []
