"""Model roster: recommendation, cost sources, and the delegation tools built on it."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as dbmod
from core.database import Base, ModelEndpoint
from src import model_roster as mr


class _NoPrices(mr.PriceCatalog):
    def __init__(self, index=None):
        super().__init__()
        self._fixed = index or {}

    def index(self):
        return self._fixed


def _entries(rows, declared=None, index=None):
    return mr.build_entries(rows, declared=declared or {}, prices=_NoPrices(index))


def _row(model, kind="api", endpoint="e1", provider="openai"):
    return (endpoint, "Gateway", model, kind, provider)


# ---------------------------------------------------------------------------
# parsing + recommendation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,line,family,version", [
    ("claude-opus-5-5", "claude", "claude-opus", (5, 5)),
    ("claude-3-5-sonnet-20241022", "claude", "claude-sonnet", (3, 5)),
    ("gpt-5.5-mini", "gpt", "gpt-mini", (5, 5)),
    ("o4-mini", "o", "o-mini", (4,)),
    ("Qwen/Qwen3-Coder-480B-A35B-Instruct", "qwen", "qwen-coder@frontier", (3,)),
    ("qwen3.8-27b", "qwen", "qwen@mid", (3, 8)),
    ("deepseek-v4-flash", "deepseek", "deepseek-flash", (4,)),
    ("zai-org/GLM-5.2", "glm", "glm", (5, 2)),
    ("meta-llama/Llama-3.3-70B-Instruct", "llama", "llama@mid", (3, 3)),
    ("claude-sonnet-5-5[1m]", "claude", "claude-sonnet", (5, 5)),
])
def test_parse_model_id(model, line, family, version):
    ident = mr.parse_model_id(model)
    assert (ident.line, ident.family, ident.version) == (line, family, version)


def test_newest_of_each_family_is_recommended():
    entries = _entries([_row(m) for m in (
        "claude-opus-5-5", "claude-opus-4-1", "claude-sonnet-5-5", "claude-haiku-4-5-20251001",
        "glm-5.2", "glm-4.6", "deepseek-chat",
    )])
    rec = {e.model for e in entries if e.recommended}
    # Haiku 4.5 is the newest Haiku even though Claude's line is at 5.
    assert rec == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001", "glm-5.2"}


def test_a_family_two_generations_behind_is_not_recommended():
    entries = _entries([_row("llama-4-scout"), _row("llama-2-chat")])
    rec = {e.model for e in entries if e.recommended}
    assert "llama-2-chat" not in rec and "llama-4-scout" in rec


def test_release_dates_retire_stale_families_when_known():
    index = {
        "gpt-5-5": {"input_per_mtok": 1.0, "output_per_mtok": 8.0, "created": 1_780_000_000},
        "gpt-4o": {"input_per_mtok": 2.5, "output_per_mtok": 10.0, "created": 1_715_000_000},
    }
    entries = _entries([_row("gpt-5.5"), _row("gpt-4o")], index=index)
    assert {e.model: e.recommended for e in entries} == {"gpt-5.5": True, "gpt-4o": False}


def test_declared_overrides_and_deprecation_win():
    declared = {"glm-4.6": {"recommended": True}, "glm-5.2": {"deprecated": True}}
    entries = _entries([_row("glm-5.2"), _row("glm-4.6")], declared=declared)
    assert {e.model: e.recommended for e in entries} == {"glm-5.2": False, "glm-4.6": True}


# ---------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------

OPENROUTER = {"data": [
    {"id": "anthropic/claude-haiku-4.5", "created": 1_760_000_000, "context_length": 200000,
     "architecture": {"input_modalities": ["text", "image"]},
     "pricing": {"prompt": "0.000001", "completion": "0.000005", "input_cache_read": "0.0000001"}},
    {"id": "deepseek/deepseek-v4-flash:free", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "deepseek/deepseek-v4-flash", "context_length": 128000,
     "pricing": {"prompt": "0.00000027", "completion": "0.0000011"}},
    {"id": "broken/no-pricing"},
]}


def test_parse_catalog_prefers_paid_listing_and_converts_to_per_million():
    index = mr.parse_catalog(OPENROUTER)
    haiku = index["claude-haiku-4-5"]
    assert (haiku["input_per_mtok"], haiku["output_per_mtok"], haiku["cached_per_mtok"]) == (1.0, 5.0, 0.1)
    assert haiku["context_k"] == 200 and haiku["vision"] is True
    assert index["deepseek-v4-flash"]["input_per_mtok"] == 0.27      # not the :free zero
    assert "no-pricing" not in index


def test_catalog_matching_tolerates_dates_dots_and_vendor_prefixes():
    cat = _NoPrices(mr.parse_catalog(OPENROUTER))
    assert cat.lookup("claude-haiku-4-5-20251001")["catalog_id"] == "anthropic/claude-haiku-4.5"
    assert cat.lookup("deepseek-ai/DeepSeek-V4-Flash")["output_per_mtok"] == 1.1
    assert cat.lookup("unknown-model") is None


def test_cost_sources_and_labels():
    index = mr.parse_catalog(OPENROUTER)
    declared = {"glm-5.2": {"input_per_mtok": 0.6, "output_per_mtok": 2.2, "notes": "strong at refactors"}}
    entries = {e.model: e for e in _entries([
        _row("glm-5.2"),
        _row("deepseek-v4-flash"),
        _row("claude-haiku-4-5-20251001", kind="subscription", provider="claude-subscription"),
        _row("qwen3:8b", kind="local", provider="ollama"),
        _row("mystery-model"),
    ], declared=declared, index=index)}
    assert entries["glm-5.2"].price_source == "declared"
    assert entries["glm-5.2"].cost_label() == "$0.6 in, $2.2 out per 1M tokens"
    assert entries["glm-5.2"].cost_band() == "$$"
    assert entries["deepseek-v4-flash"].cost_label() == "~$0.27 in, $1.1 out per 1M tokens"
    assert entries["deepseek-v4-flash"].cost_band() == "$"          # blended (3:1) ~ $0.48
    # Subscriptions never show a metered price, even when the catalog knows one.
    assert entries["claude-haiku-4-5-20251001"].billing == "subscription"
    assert entries["claude-haiku-4-5-20251001"].input_per_mtok is None
    assert entries["claude-haiku-4-5-20251001"].context_k == 200
    assert entries["qwen3:8b"].cost_label() == "free (local hardware)"
    assert entries["mystery-model"].cost_label() == "price unknown"
    assert entries["mystery-model"].cost_band() == "?"


def test_tiers_and_traits():
    entries = {e.model: e for e in _entries([
        _row("claude-opus-5-5"), _row("gpt-5.5-mini"), _row("Qwen/Qwen3-Coder-480B-A35B"), _row("qwen3.8-27b"),
    ])}
    assert entries["claude-opus-5-5"].tier == "flagship"
    assert entries["gpt-5.5-mini"].tier == "fast"
    assert entries["Qwen/Qwen3-Coder-480B-A35B"].tier == "flagship"
    assert "code" in entries["Qwen/Qwen3-Coder-480B-A35B"].traits
    assert entries["qwen3.8-27b"].tier == "fast"


def test_price_fetch_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_MODEL_PRICES", "off")
    cat = mr.PriceCatalog()
    with pytest.raises(RuntimeError, match="turned off"):
        cat.refresh()
    monkeypatch.setattr(mr, "_cache_path", lambda: "/nonexistent/model-prices.json")
    assert cat.index() == {}                  # no background fetch started either
    assert cat.status()["enabled"] is False


def test_refresh_writes_cache_and_index(monkeypatch, tmp_path):
    import httpx

    path = tmp_path / "model-prices.json"
    monkeypatch.setattr(mr, "_cache_path", lambda: str(path))
    body = json.dumps(OPENROUTER).encode()

    def handler(request):
        return httpx.Response(200, content=body)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    cat = mr.PriceCatalog()
    assert cat.refresh() == 2
    assert json.loads(path.read_text())["payload"]["data"][0]["id"] == "anthropic/claude-haiku-4.5"
    fresh = mr.PriceCatalog()                 # a new process reads the cache
    assert fresh.lookup("claude-haiku-4-5")["input_per_mtok"] == 1.0


# ---------------------------------------------------------------------------
# roster from the database + tools
# ---------------------------------------------------------------------------

@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr("src.database.SessionLocal", Session)    # re-export used by ai_interaction
    monkeypatch.setattr(mr, "PRICES", _NoPrices())
    monkeypatch.setattr("src.omnigent_catalog.load_declared", lambda path=None: {})
    s = Session()
    s.add_all([
        ModelEndpoint(id="ep1", name="Early", base_url="https://api.early.example/v1", owner="alice",
                      is_enabled=True, model_type="llm", api_key="k1",
                      cached_models=json.dumps(["glm-5.2-air", "nomic-embed-text"])),
        ModelEndpoint(id="ep2", name="Late", base_url="https://api.late.example/v1", owner="alice",
                      is_enabled=True, model_type="llm", api_key="k2",
                      cached_models=json.dumps(["glm-5.2", "glm-4.6"])),
        ModelEndpoint(id="ep3", name="Local", base_url="http://127.0.0.1:11434/v1", owner="alice",
                      is_enabled=True, model_type="llm", cached_models=json.dumps(["qwen3:8b"])),
        ModelEndpoint(id="bob", name="Bob", base_url="https://api.bob.example/v1", owner="bob",
                      is_enabled=True, model_type="llm", api_key="kb", cached_models=json.dumps(["gpt-5.5"])),
    ])
    s.commit()
    s.close()
    return Session


def test_roster_is_owner_scoped_sorted_and_skips_embeddings(db):
    entries = mr.roster("alice")
    models = [e.model for e in entries]
    assert "gpt-5.5" not in models and "nomic-embed-text" not in models
    assert set(models) == {"glm-5.2-air", "glm-5.2", "glm-4.6", "qwen3:8b"}
    assert entries[-1].kind == "local"                       # api before local
    assert entries[0].recommended                             # recommended first within a kind
    assert next(e for e in entries if e.model == "glm-5.2").key == "ep2::glm-5.2"


def test_list_models_tool_shows_keys_recommendation_and_guidance(db):
    from src.agent_tools.model_interaction_tools import list_models

    out = asyncio.run(list_models("", owner="alice"))["results"]
    assert "[ep2::glm-5.2]" in out and "RECOMMENDED" in out
    assert "free (local hardware)" in out
    assert "Choosing a model" in out
    filtered = asyncio.run(list_models("local", owner="alice"))["results"]
    assert "qwen3:8b" in filtered and "glm-5.2" not in filtered


def test_resolve_model_prefers_exact_match_on_a_later_endpoint(db):
    from src.ai_interaction import _resolve_model

    # "glm-5.2" partially matches ep1's "glm-5.2-air"; the exact model lives on ep2.
    url, model, headers = _resolve_model("glm-5.2", owner="alice")
    assert model == "glm-5.2" and "api.late.example" in url
    assert headers["Authorization"] == "Bearer k2"


def test_resolve_model_accepts_roster_keys_and_respects_owner(db):
    from src.ai_interaction import _resolve_model

    assert _resolve_model("ep2::glm-4.6", owner="alice")[1] == "glm-4.6"
    with pytest.raises(ValueError):
        _resolve_model("bob::gpt-5.5", owner="alice")


def test_chat_with_model_passes_instructions_and_reports_cost(db, monkeypatch):
    from src.agent_tools import model_interaction_tools as mit

    seen = {}

    async def fake_call(url, model, messages, **kwargs):
        seen["model"], seen["messages"] = model, messages
        return "delegate answer"

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_call)
    content = json.dumps({"model": "ep3::qwen3:8b", "message": "Summarise X", "instructions": "Be terse."})
    out = asyncio.run(mit.chat_with_model(content, owner="alice"))
    assert out["response"] == "delegate answer"
    assert out["cost"] == "free (local hardware)"
    assert seen["model"] == "qwen3:8b"
    assert seen["messages"] == [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Summarise X"}]

    plain = asyncio.run(mit.chat_with_model("glm-4.6\nhello", owner="alice"))
    assert plain["model"] == "glm-4.6"
    missing = asyncio.run(mit.chat_with_model("no-such-model\nhello", owner="alice"))
    assert "list_models" in missing["error"]


def test_ask_teacher_auto_picks_a_recommended_model(db, monkeypatch):
    from src.agent_tools import model_interaction_tools as mit

    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: "")
    picked = {}

    async def fake_call(url, model, messages, **kwargs):
        picked["model"] = model
        return "guidance"

    monkeypatch.setattr("src.llm_core.llm_call_async", fake_call)
    out = asyncio.run(mit.ask_teacher("auto\nI am stuck", owner="alice"))
    assert out["teacher"] is True
    assert picked["model"] in {"glm-5.2", "glm-5.2-air", "qwen3:8b"}


def test_native_chat_with_model_args_carry_instructions():
    from src.tool_schemas import function_call_to_tool_block

    block = function_call_to_tool_block("chat_with_model", {"model": "m", "message": "x", "instructions": "be brief"})
    text = json.dumps(block) if not isinstance(block, str) else block
    assert "be brief" in text


def test_roster_route(db):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from routes.model_roster_routes import setup_model_roster_routes

    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    app.include_router(setup_model_roster_routes())
    data = TestClient(app).get("/api/models/roster").json()
    assert {m["model"] for m in data["models"]} == {"glm-5.2-air", "glm-5.2", "glm-4.6", "qwen3:8b"}
    first = data["models"][0]
    assert {"key", "recommended", "cost_label", "cost_band", "tier", "kind"} <= set(first)
    assert "prices" in data and "guidance" in data
