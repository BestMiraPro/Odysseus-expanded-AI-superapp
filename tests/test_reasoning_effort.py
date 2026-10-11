"""Per-model reasoning effort: level tables, clamping, payloads and the API."""
import asyncio

import pytest

from src import llm_core, reasoning_effort as re_


# ── Which levels each provider/model accepts ──

@pytest.mark.parametrize("provider,model,url,expected", [
    ("anthropic", "claude-opus-5-5", "", re_.LEVELS),
    ("anthropic", "claude-sonnet-5-5", "", re_.LEVELS),
    ("anthropic", "claude-opus-4-8", "", re_.LEVELS),
    ("anthropic", "claude-opus-4-6", "", ("low", "medium", "high", "max")),
    ("anthropic", "claude-sonnet-4-6", "", ("low", "medium", "high", "max")),
    ("anthropic", "claude-opus-4-5-20251101", "", ("low", "medium", "high")),
    ("anthropic", "claude-haiku-4-5-20251001", "", ()),
    ("anthropic", "claude-sonnet-4-5", "", ()),
    ("claude-subscription", "claude-fable-5-1", "", re_.LEVELS),
    ("claude-subscription", "opus", "", re_.LEVELS),
    ("claude-subscription", "claude-haiku-4-5-20251001", "", ()),
    ("chatgpt-subscription", "gpt-5.6-sol", "", ("low", "medium", "high", "xhigh")),
    ("chatgpt-subscription", "gpt-5.1-codex-max", "", ("low", "medium", "high", "xhigh")),
    ("chatgpt-subscription", "gpt-5-codex", "", ("low", "medium", "high")),
    ("openai", "gpt-5.6-luna", "https://api.openai.com/v1", ("low", "medium", "high", "xhigh")),
    ("openai", "o4-mini", "https://api.openai.com/v1", ("low", "medium", "high")),
    ("openai", "gpt-4.1", "https://api.openai.com/v1", ()),
    ("openai", "gemini-3-pro", "https://generativelanguage.googleapis.com/v1beta/openai", ("low", "medium", "high")),
    ("openai", "gpt-oss-120b", "https://api.inference.wandb.ai/v1", ("low", "medium", "high")),
    ("openai", "deepseek-v4", "https://api.inference.wandb.ai/v1", ()),
    ("openai", "gpt-5.6-luna", "https://example.com/v1", ()),
    ("groq", "openai/gpt-oss-20b", "https://api.groq.com/openai/v1", ("low", "medium", "high")),
    ("ollama", "gpt-oss:20b", "http://localhost:11434", ("low", "medium", "high")),
    ("ollama", "qwen3:8b", "http://localhost:11434", ()),
    ("openrouter", "anthropic/claude-opus-5-5", "", ("low", "medium", "high")),
    ("openrouter", "openai/gpt-4o", "", ()),
    ("mistral", "magistral-medium-latest", "", ("low", "medium", "high")),
    ("mistral", "codestral-latest", "", ()),
])
def test_supported_levels(provider, model, url, expected):
    assert re_.supported_levels(provider, model, url) == expected


def test_clamp_picks_highest_supported_at_or_below():
    four_six = ("low", "medium", "high", "max")
    assert re_.clamp("xhigh", four_six) == "high"
    assert re_.clamp("max", ("low", "medium", "high")) == "high"
    assert re_.clamp("max", four_six) == "max"
    assert re_.clamp("low", ("medium", "high")) == "medium"
    assert re_.clamp("high", ()) is None
    assert re_.clamp("bogus", four_six) is None


def test_normalize_level_aliases():
    assert re_.normalize_level("Extra high") == "xhigh"
    assert re_.normalize_level("x-high") == "xhigh"
    assert re_.normalize_level("MAX") == "max"
    assert re_.normalize_level("turbo") is None


def test_nothing_applies_outside_a_user_scope():
    payload = {}
    assert re_.apply_to_payload("anthropic", "claude-opus-5-5", payload) is None
    assert payload == {}


# ── What actually goes on the wire (through stream_llm) ──

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
        self.payloads = []

    def stream(self, method, url, **kw):
        self.payloads.append(kw.get("json"))
        return _Ctx()


def _sent_payload(monkeypatch, url, model, levels, tools=None):
    client = _CaptureClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(re_, "user_levels", lambda owner: dict(levels))

    async def run():
        with re_.for_owner("alice"):
            async for _ in llm_core.stream_llm(
                url, model, [{"role": "user", "content": "hi"}],
                headers={"Authorization": "Bearer k"}, tools=tools,
            ):
                pass

    asyncio.run(run())
    assert client.payloads, "no request was sent"
    return client.payloads[0]


def test_anthropic_gets_output_config_effort(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.anthropic.com/v1", "claude-opus-5-5",
                            {"claude-opus-5-5": "xhigh"})
    assert payload["output_config"] == {"effort": "xhigh"}
    assert "thinking" not in payload


def test_anthropic_clamps_to_model_range(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.anthropic.com/v1", "claude-opus-4-6",
                            {"claude-opus-4-6": "xhigh"})
    assert payload["output_config"] == {"effort": "high"}


def test_anthropic_model_without_effort_sends_nothing(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.anthropic.com/v1", "claude-haiku-4-5-20251001",
                            {"claude-haiku-4-5-20251001": "high"})
    assert "output_config" not in payload


def test_effort_is_per_model(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.anthropic.com/v1", "claude-sonnet-5-5",
                            {"claude-opus-5-5": "max"})
    assert "output_config" not in payload


def test_openai_gets_reasoning_effort(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.openai.com/v1", "gpt-5.6-luna",
                            {"gpt-5.6-luna": "max"})
    assert payload["reasoning_effort"] == "xhigh"


def test_openai_tools_restriction_still_wins(monkeypatch):
    tool = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
    payload = _sent_payload(monkeypatch, "https://api.openai.com/v1", "gpt-5.6-luna",
                            {"gpt-5.6-luna": "high"}, tools=[tool])
    assert payload["reasoning_effort"] == "none"


def test_openrouter_gets_reasoning_object(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://openrouter.ai/api/v1", "anthropic/claude-opus-5-5",
                            {"anthropic/claude-opus-5-5": "max"})
    assert payload["reasoning"] == {"effort": "high"}


def test_mistral_choice_overrides_env_default(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.mistral.ai/v1", "magistral-medium-latest",
                            {"magistral-medium-latest": "low"})
    assert payload["reasoning_effort"] == "low"


def test_no_saved_level_leaves_openai_payload_alone(monkeypatch):
    payload = _sent_payload(monkeypatch, "https://api.openai.com/v1", "gpt-5.6-luna", {})
    assert "reasoning_effort" not in payload


def test_chatgpt_subscription_gets_reasoning_object(monkeypatch):
    monkeypatch.setattr(llm_core, "_detect_provider", lambda url: "chatgpt-subscription")
    monkeypatch.setattr(llm_core, "_normalize_chatgpt_subscription_url", lambda url: url)
    payload = _sent_payload(monkeypatch, "https://chatgpt.com/backend-api/codex", "gpt-5.6-sol",
                            {"gpt-5.6-sol": "xhigh"})
    assert payload["reasoning"] == {"effort": "xhigh"}
    assert "temperature" not in payload


def test_claude_subscription_passes_cli_effort(monkeypatch):
    seen = {}

    async def fake_stream_chat(url, model, messages, *, timeout=None, effort=None, credentials=None):
        seen["effort"] = effort
        yield 'data: {"delta": "ok"}\n\n'

    import src.claude_subscription as cs
    monkeypatch.setattr(cs, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(llm_core, "_detect_provider", lambda url: "claude-subscription")
    monkeypatch.setattr(re_, "user_levels", lambda owner: {"claude-opus-5-5": "max"})

    async def run():
        with re_.for_owner("alice"):
            async for _ in llm_core.stream_llm("https://x.claude-subscription.invalid", "claude-opus-5-5",
                                               [{"role": "user", "content": "hi"}]):
                pass

    asyncio.run(run())
    assert seen["effort"] == "max"


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
