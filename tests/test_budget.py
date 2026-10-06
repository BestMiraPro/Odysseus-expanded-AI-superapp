"""Budget guardrails: spend ledger, monthly cap, single-action limit, and the
places that enforce them (chat, Council, agent delegation)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as dbmod
from core.database import Base, ModelEndpoint, SpendEntry
from src import budget, council

PRICED = budget.Price("metered", 2.0, 8.0, "declared")      # $2 in / $8 out per 1M tokens


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    budget.clear_price_cache()
    return Session


@pytest.fixture
def prices(monkeypatch):
    """Price by model name: 'paid-*' metered, 'local-*' free, 'plan-*' flat, else unpriced."""
    def fake(base_url, model):
        if model.startswith("paid"):
            return PRICED
        if model.startswith("local"):
            return budget.Price("local")
        if model.startswith("plan"):
            return budget.Price("subscription")
        return budget.Price("metered")
    monkeypatch.setattr(budget, "price_for", fake)
    return fake


def _spend(db, owner, cost, when=None, model="paid-x", source="chat"):
    s = db()
    s.add(SpendEntry(owner=owner, model=model, source=source, input_tokens=10, output_tokens=10,
                     cost_usd=cost, created_at=when or budget._utcnow()))
    s.commit()
    s.close()


# --- pricing ---------------------------------------------------------------

def test_price_cost_counts_cache_tokens_at_cache_rates():
    p = budget.Price("metered", 10.0, 50.0)
    # fresh 1000*10 + read 8000*1 + write 1000*12.5 + out 100*50 = 35500 -> $0.0355
    assert p.cost(10000, 100, cache_read=8000, cache_write=1000) == 0.0355
    assert p.usage_cost({"input_tokens": 1000, "output_tokens": 500}) == round((1000 * 10 + 500 * 50) / 1e6, 6)


def test_free_flat_and_unpriced_models_have_no_cost():
    assert budget.Price("local").cost(1000, 1000) is None
    assert budget.Price("subscription").cost(1000, 1000) is None
    assert budget.Price("metered").cost(1000, 1000) is None
    assert budget.Price("metered", 1.0).cost(1_000_000, 0) == 1.0     # output falls back to input price


def test_price_for_uses_the_roster_classification(monkeypatch):
    budget.clear_price_cache()
    from src import model_roster

    class _Cat:
        def lookup(self, model):
            return {"input_per_mtok": 3.0, "output_per_mtok": 15.0} if model == "gpt-test" else None

    monkeypatch.setattr(model_roster, "PRICES", _Cat())
    monkeypatch.setattr("src.omnigent_catalog.load_declared", lambda: {})
    api = budget.price_for("https://api.openai.com/v1", "gpt-test")
    assert (api.billing, api.input_per_mtok, api.output_per_mtok) == ("metered", 3.0, 15.0)
    assert budget.price_for("http://127.0.0.1:11434/v1", "gpt-test").billing == "local"
    assert budget.price_for("https://claude-subscription.invalid/abc", "claude-opus-5-5").billing == "subscription"


# --- ledger ----------------------------------------------------------------

def test_record_bills_metered_calls_only(db, prices):
    usage = {"input_tokens": 1000, "output_tokens": 500}
    assert budget.record("alice", source="chat", model="paid-a", usage=usage) == 0.006
    assert budget.record("alice", source="chat", model="local-a", usage=usage) is None
    assert budget.record("alice", source="chat", model="plan-a", usage=usage) is None
    assert budget.record("alice", source="chat", model="mystery", usage=usage) is None   # unpriced but kept
    assert budget.record("alice", source="chat", model="paid-a", usage={}) is None          # nothing to bill
    s = db()
    rows = s.query(SpendEntry).order_by(SpendEntry.id).all()
    s.close()
    assert [(r.model, r.cost_usd) for r in rows] == [("paid-a", 0.006), ("mystery", None)]


def test_month_spend_is_per_owner_and_per_calendar_month(db):
    now = datetime(2026, 10, 15, 12)
    _spend(db, "alice", 1.5, datetime(2026, 10, 1, 0, 0, 1))
    _spend(db, "alice", 2.0, datetime(2026, 10, 31, 23, 59))
    _spend(db, "alice", 9.0, datetime(2026, 9, 30, 23, 59))     # last month
    _spend(db, "bob", 4.0, datetime(2026, 10, 3))
    _spend(db, None, 0.25, datetime(2026, 10, 3))                # single-user install
    assert budget.month_spend("alice", now) == 3.5
    assert budget.month_spend("bob", now) == 4.0
    assert budget.month_spend(None, now) == 0.25
    assert budget.month_window(datetime(2026, 12, 9)) == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def test_settings_validate_and_persist(db):
    assert budget.get_settings("alice") == budget.DEFAULTS
    saved = budget.save_settings("alice", {"monthly_cap_usd": 20, "cap_action": "warn"})
    assert saved == {"monthly_cap_usd": 20.0, "cap_action": "warn", "action_limit_usd": 0.5}
    assert budget.get_settings("alice")["cap_action"] == "warn"
    assert budget.get_settings("bob") == budget.DEFAULTS
    for bad in ({"monthly_cap_usd": -1}, {"action_limit_usd": float("nan")}, {"cap_action": "explode"},
                {"monthly_cap_usd": "lots"}):
        with pytest.raises(ValueError):
            budget.save_settings("alice", bad)


# --- decisions -------------------------------------------------------------

def test_check_cap_block_warn_and_action_limit(db):
    budget.save_settings("alice", {"monthly_cap_usd": 10, "cap_action": "block", "action_limit_usd": 0.5})
    _spend(db, "alice", 9.0)
    assert budget.check("alice", 0.4).allowed is True
    over = budget.check("alice", 1.5, what="This council turn")
    assert over.allowed is False and "past your $10.00 cap" in over.reason
    confirm = budget.check("alice", 0.8)
    assert confirm.allowed is True and confirm.confirm is True and "$0.50" in confirm.reason
    _spend(db, "alice", 1.0)
    reached = budget.check("alice", None)
    assert reached.allowed is False and reached.reason.startswith("Monthly budget reached")
    assert budget.check("alice", 5.0, metered=False).allowed is True        # free seats never blocked
    budget.save_settings("alice", {"cap_action": "warn"})
    assert budget.check("alice", 5.0).allowed is True                        # warn mode lets it run
    assert budget.status("alice")["state"] == "over"


def test_check_fails_open_when_the_store_is_broken(monkeypatch):
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(budget, "_session", boom)
    assert budget.check("alice", 99.0).allowed is True


def test_status_warns_from_eighty_percent(db):
    budget.save_settings("alice", {"monthly_cap_usd": 10})
    _spend(db, "alice", 7.9)
    assert budget.status("alice")["state"] == "ok"
    _spend(db, "alice", 0.2)
    st = budget.status("alice")
    assert st["state"] == "warn" and st["remaining_usd"] == pytest.approx(1.9)


def test_summary_breaks_down_by_source_and_model(db):
    now = datetime(2026, 10, 10, 12)
    _spend(db, "alice", 1.0, datetime(2026, 10, 2), model="paid-a", source="chat")
    _spend(db, "alice", 2.0, datetime(2026, 10, 3), model="paid-b", source="council")
    _spend(db, "alice", None, datetime(2026, 10, 4), model="mystery", source="council")
    out = budget.summary("alice", now)
    assert out["spent_usd"] == 3.0 and out["calls"] == 3 and out["unpriced_calls"] == 1
    assert [r["name"] for r in out["by_source"]] == ["council", "chat"]
    assert out["by_model"][0]["name"] == "paid-b"
    # 3.0 over ~9.5 days, projected over 31 days
    assert out["projected_usd"] == pytest.approx(3.0 / 9.5 * 31, rel=1e-3)
    assert out["month"] == "2026-10" and len(out["recent"]) == 3


# --- chat turns ------------------------------------------------------------

def test_record_turn_bills_agent_buckets_and_skips_untracked_routes(db, prices):
    s = db()
    s.add(ModelEndpoint(id="ep1", name="Paid API", base_url="https://api.example.com/v1", owner="alice",
                        is_enabled=True, model_type="llm"))
    s.commit()
    s.close()
    metrics = {"model": "paid-a", "input_tokens": 3000, "output_tokens": 300, "usage_buckets": [
        {"round": 1, "model": "paid-a", "endpoint_id": "ep1", "input_tokens": 1000, "output_tokens": 100,
         "usage_source": "real"},
        {"round": 2, "model": "paid-b", "endpoint_id": "ep1", "input_tokens": 2000, "output_tokens": 200,
         "usage_source": "estimated"},
        {"round": 3, "model": "paid-c", "endpoint_id": "ep1", "input_tokens": 5000, "output_tokens": 5000,
         "endpoint_cost_tracked": False},
    ]}
    total = budget.record_turn("alice", "sess1", metrics)
    assert total == pytest.approx((1000 * 2 + 100 * 8 + 2000 * 2 + 200 * 8) / 1e6)
    s = db()
    rows = s.query(SpendEntry).order_by(SpendEntry.id).all()
    s.close()
    assert [(r.source, r.model, r.estimated, r.endpoint_name) for r in rows] == [
        ("agent", "paid-a", False, "Paid API"), ("agent", "paid-b", True, "Paid API")]


def test_record_turn_plain_chat_and_teacher(db, prices):
    budget.record_turn("alice", "s", {"model": "paid-a", "input_tokens": 1000, "output_tokens": 0})
    budget.record_turn("alice", "s", {"model": "paid-a", "input_tokens": 1000, "output_tokens": 0, "teacher": True})
    budget.record_turn("alice", "s", {"model": "local-a", "input_tokens": 1000, "output_tokens": 10})
    s = db()
    assert [r.source for r in s.query(SpendEntry).order_by(SpendEntry.id)] == ["chat", "teacher"]
    s.close()


def test_accumulate_token_usage_records_the_session_owners_spend(db, prices, monkeypatch):
    import routes.chat_helpers as ch

    monkeypatch.setattr(ch, "SessionLocal", db)
    s = db()
    s.add(dbmod.Session(id="s1", name="t", endpoint_url="https://api.example.com/v1", model="paid-a", owner="alice"))
    s.commit()
    s.close()
    ch.accumulate_token_usage("s1", {"model": "paid-a", "input_tokens": 1000, "output_tokens": 500})
    assert budget.month_spend("alice") == 0.006


def test_chat_gate_refuses_metered_models_at_the_cap(db, prices):
    from routes.chat_helpers import _enforce_budget_cap
    from types import SimpleNamespace

    budget.save_settings("alice", {"monthly_cap_usd": 1})
    _spend(db, "alice", 1.0)
    with pytest.raises(HTTPException) as exc:
        _enforce_budget_cap("alice", SimpleNamespace(endpoint_url="https://api.example.com/v1", model="paid-a"))
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "budget_blocked"
    # The chat UI turns errors mentioning these words into a mode switch.
    assert "tool" not in exc.value.detail["message"] and "auto" not in exc.value.detail["message"]
    _enforce_budget_cap("alice", SimpleNamespace(endpoint_url="http://127.0.0.1:11434/v1", model="local-a"))
    _enforce_budget_cap("bob", SimpleNamespace(endpoint_url="https://api.example.com/v1", model="paid-a"))


def test_subscription_routes_are_not_cost_tracked():
    from src.endpoint_resolver import endpoint_cost_tracked

    assert endpoint_cost_tracked("https://claude-subscription.invalid/abc", "api") is False
    assert endpoint_cost_tracked("https://api.githubcopilot.com", "api") is False
    assert endpoint_cost_tracked("https://api.openai.com/v1", "api") is True


# --- Council ---------------------------------------------------------------

def _member(model, billing="metered", inp=2.0, out=8.0):
    return council.Member(endpoint_id="e", model=model, endpoint_name="E", kind="api", provider="openai",
                          billing=billing, input_per_mtok=inp if billing == "metered" else None,
                          output_per_mtok=out if billing == "metered" else None)


def test_estimate_turn_counts_every_stage():
    members = [_member("paid-a"), _member("free", billing="local"), _member("mystery", inp=None, out=None)]
    full = council.estimate_turn(members, members[0], council.MODE_FULL, "What is 6 x 7?", [])
    assert [c["stage"] for c in full["calls"]] == ["opinions"] * 3 + ["review"] * 3 + ["synthesis"]
    assert full["metered"] is True and full["unpriced_calls"] == 2 and full["priced_calls"] == 3
    quick = council.estimate_turn(members, members[0], council.MODE_QUICK, "What is 6 x 7?", [])
    assert [c["stage"] for c in quick["calls"]] == ["opinions"] * 3 + ["synthesis"]
    assert 0 < quick["total_usd"] < full["total_usd"]
    chair = quick["calls"][-1]
    assert chair["input_tokens"] > 3 * budget.EXPECTED_OUTPUT_TOKENS["opinions"]   # reads every answer
    free = council.estimate_turn([_member("l", billing="local")], _member("l", billing="local"), "full", "q", [])
    assert free["metered"] is False and free["total_usd"] == 0.0


def test_estimate_uses_observed_reply_lengths():
    turns = [{"opinions": [{"usage": {"output_tokens": 100}}, {"usage": {"output_tokens": 300}}],
              "reviews": [{"usage": {"output_tokens": 50}}, {"usage": {"output_tokens": 70}}],
              "usage": {"output_tokens": 1000}}] * 2
    observed = council.observed_output_tokens(turns)
    assert observed == {"opinions": 200, "review": 60, "synthesis": 480}
    est = council.estimate_turn([_member("paid-a")], _member("paid-a"), "quick", "q", [], expected=observed)
    assert est["calls"][0]["output_tokens"] == 200


def test_billable_calls_use_real_usage_or_estimates():
    a, b = _member("paid-a"), _member("paid-b")
    result = {
        "opinions": [{"member": 0, "text": "x", "usage": {"input_tokens": 100, "output_tokens": 10},
                      "cost_usd": 0.1, "input_estimate": 90},
                     {"member": 1, "text": "partial answer", "usage": None, "input_estimate": 120}],
        "reviews": [{"member": 1, "text": "", "error": "failed", "usage": None, "input_estimate": 500}],
        "chair": {"member": "chair", "text": "", "pending": True, "input_estimate": 900},
    }
    calls = council.billable_calls(result, [a, b], a)
    assert [(m.model, stage, est) for m, stage, _u, _c, est in calls] == [
        ("paid-a", "opinions", False), ("paid-b", "opinions", True)]
    assert calls[1][2]["input_tokens"] == 120 and calls[1][2]["output_tokens"] > 0


@pytest.fixture
def council_client(db, monkeypatch, prices):
    import routes.council_routes as cr

    monkeypatch.setattr(cr, "SessionLocal", db)
    s = db()
    s.add(ModelEndpoint(id="api1", name="Paid", base_url="https://api.example.com/v1", owner="alice",
                        is_enabled=True, model_type="llm", api_key="sk-1",
                        cached_models=json.dumps(["paid-a", "paid-b"])))
    s.commit()
    s.close()
    monkeypatch.setattr(cr, "_seat_pricing", lambda ep, model, kind, provider: {
        "billing": "metered", "input_per_mtok": 2.0, "output_per_mtok": 8.0})

    async def fake_stream(url, model, messages, **kwargs):
        system = messages[0]["content"]
        text = ("ok\nFINAL RANKING:\n1. Response A\n2. Response B" if system == council.REVIEW_SYSTEM
                else "42")
        yield f"data: {json.dumps({'delta': text})}\n\n"
        yield f"data: {json.dumps({'type': 'usage', 'data': {'input_tokens': 1000, 'output_tokens': 100}})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr("src.llm_core.stream_llm", fake_stream)
    cr._RUNNING.clear()
    cr._ACTIVE_SESSIONS.clear()
    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    app.include_router(cr.setup_council_routes())
    return TestClient(app)


SEATS = {"members": [{"endpoint_id": "api1", "model": "paid-a"}, {"endpoint_id": "api1", "model": "paid-b"}],
         "chairman": {"endpoint_id": "api1", "model": "paid-a"}, "mode": "full"}


def test_council_estimate_route(council_client):
    out = council_client.post("/api/council/estimate", json={**SEATS, "question": "Why?"}).json()
    assert out["estimate"]["metered"] is True and out["estimate"]["total_usd"] > 0
    assert out["budget"]["allowed"] is True and out["budget"]["confirm"] is False


def test_council_asks_first_above_the_limit_then_bills_the_turn(council_client, db):
    budget.save_settings("alice", {"action_limit_usd": 0.0001})
    sid = council_client.post("/api/council/sessions", json={}).json()["id"]
    first = council_client.post(f"/api/council/sessions/{sid}/ask", json={**SEATS, "question": "Why?"})
    assert first.status_code == 402
    assert first.json()["detail"]["code"] == "budget_confirm"
    s = db()
    assert s.query(dbmod.CouncilTurn).count() == 0            # nothing ran, nothing saved
    s.close()
    ok = council_client.post(f"/api/council/sessions/{sid}/ask",
                             json={**SEATS, "question": "Why?", "budget_confirmed": True})
    assert ok.status_code == 200 and '"type": "done"' in ok.text
    # 2 opinions + 2 reviews + chair, 1000 in / 100 out each at $2 / $8 per 1M
    assert budget.month_spend("alice") == pytest.approx(5 * (1000 * 2 + 100 * 8) / 1e6)
    s = db()
    assert {r.source for r in s.query(SpendEntry)} == {"council"}
    s.close()


def test_council_cap_cannot_be_confirmed_away(council_client, db):
    budget.save_settings("alice", {"monthly_cap_usd": 1.0, "action_limit_usd": 0})
    _spend(db, "alice", 1.0)
    sid = council_client.post("/api/council/sessions", json={}).json()["id"]
    res = council_client.post(f"/api/council/sessions/{sid}/ask",
                              json={**SEATS, "question": "Why?", "budget_confirmed": True})
    assert res.status_code == 402 and res.json()["detail"]["code"] == "budget_blocked"
    # The session is free for the next try.
    import routes.council_routes as cr
    assert sid not in cr._ACTIVE_SESSIONS


# --- delegation ------------------------------------------------------------

def test_delegation_over_the_limit_is_refused_with_cheaper_options(db, prices, monkeypatch):
    from src import ai_interaction, llm_core, model_roster
    from src.agent_tools import model_interaction_tools as mit

    budget.save_settings(None, {"action_limit_usd": 0.0001})
    monkeypatch.setattr(ai_interaction, "_resolve_model", lambda spec, owner=None: ("https://api.x.com/v1", "paid-big", {}))
    called = []

    async def fake_call(*a, **k):
        called.append(1)
        return "answer"
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    local = model_roster.RosterEntry(endpoint_id="l", endpoint_name="Local", model="local-q", kind="local",
                                     provider="ollama", line="q", family="q", version=[1], billing="local")
    monkeypatch.setattr(model_roster, "roster", lambda owner: [local])
    out = asyncio.run(mit.chat_with_model("paid-big\nsummarise this", owner=None))
    assert out.get("budget_blocked") is True and not called
    assert "single-action limit" in out["error"] and "l::local-q (free)" in out["error"]


def test_delegation_within_budget_is_billed(db, prices, monkeypatch):
    from src import ai_interaction, llm_core
    from src.agent_tools import model_interaction_tools as mit

    monkeypatch.setattr(ai_interaction, "_resolve_model", lambda spec, owner=None: ("https://api.x.com/v1", "paid-a", {}))

    async def fake_call(url, model, messages, headers=None, timeout=None):
        return "the answer is 42"
    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    out = asyncio.run(mit.chat_with_model("paid-a\nwhat is 6 x 7?", owner="alice"))
    assert out["response"] == "the answer is 42" and out["spent_usd"] > 0
    s = db()
    row = s.query(SpendEntry).one()
    s.close()
    assert (row.owner, row.source, row.estimated) == ("alice", "delegation", True)


# --- API -------------------------------------------------------------------

def test_budget_routes(db, prices):
    from routes.budget_routes import setup_budget_routes

    app = FastAPI()

    @app.middleware("http")
    async def _user(request: Request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    app.include_router(setup_budget_routes())
    c = TestClient(app)
    assert c.get("/api/budget/status").json()["state"] == "ok"
    out = c.put("/api/budget/settings", json={"monthly_cap_usd": 5, "action_limit_usd": 0}).json()
    assert out["monthly_cap_usd"] == 5 and out["action_limit_usd"] == 0 and "by_model" in out
    assert c.put("/api/budget/settings", json={"cap_action": "nope"}).status_code == 400
