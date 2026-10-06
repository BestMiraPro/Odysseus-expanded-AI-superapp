"""Budget metering scopes: research and Study calls made straight through
llm_core are billed to their owner and stopped by a blocking monthly cap."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as dbmod
from core.database import Base, SpendEntry
from src import budget, llm_core

PRICED = budget.Price("metered", 2.0, 8.0, "declared")
URL = "https://api.example.com/v1"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    budget.clear_price_cache()
    monkeypatch.setattr(budget, "price_for", lambda url, model, kind=None:
                        PRICED if "example" in (url or "") else budget.Price("local"))
    llm_core._response_cache.clear() if hasattr(llm_core, "_response_cache") else None
    return Session


def _rows(db):
    s = db()
    try:
        return [(r.owner, r.source, r.model, r.input_tokens, r.output_tokens, r.estimated, r.cost_usd)
                for r in s.query(SpendEntry).order_by(SpendEntry.id)]
    finally:
        s.close()


@pytest.fixture
def fake_http(monkeypatch):
    calls = []

    async def post(client, url, headers, **kwargs):
        calls.append(kwargs.get("json"))
        body = {"choices": [{"message": {"role": "assistant", "content": "the answer"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 250}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", post)
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda key: None)
    return calls


def _call(model="gpt-x", content="question one"):
    return llm_core.llm_call_async(URL, model, [{"role": "user", "content": content}], max_retries=1)


def test_scoped_llm_call_async_is_billed_from_provider_usage(db, fake_http):
    async def go():
        with budget.metering("alice", "research", "s1"):
            return await _call()

    assert asyncio.run(go()) == "the answer"
    assert _rows(db) == [("alice", "research", "gpt-x", 1000, 250, False, (1000 * 2 + 250 * 8) / 1e6)]


def test_unscoped_calls_are_not_billed_here(db, fake_http):
    assert asyncio.run(_call(content="question two")) == "the answer"
    assert _rows(db) == []


def test_blocking_cap_refuses_scoped_metered_calls(db, fake_http):
    budget.save_settings("alice", {"monthly_cap_usd": 0.001})
    budget.record("alice", source="chat", model="gpt-x", usage={"input_tokens": 1000, "output_tokens": 0},
                  price=PRICED)

    async def go():
        with budget.metering("alice", "study"):
            return await _call(content="question three")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(go())
    assert exc.value.status_code == 402 and "Monthly budget reached" in exc.value.detail
    assert fake_http == []                       # never reached the provider


def _fake_stream(usage=True):
    async def stream(url, model, messages, *args, **kwargs):
        yield 'data: {"delta": "hello "}\n\n'
        yield 'data: {"delta": "world"}\n\n'
        if usage:
            yield 'data: {"type": "usage", "data": {"input_tokens": 300, "output_tokens": 40}}\n\n'
        yield "data: [DONE]\n\n"
    return stream


def test_scoped_stream_llm_is_billed(db, monkeypatch):
    monkeypatch.setattr(llm_core, "_stream_llm_unmetered", _fake_stream())

    async def go():
        with budget.metering("bob", "study", "t1"):
            return [c async for c in llm_core.stream_llm(URL, "gpt-x", [{"role": "user", "content": "hi"}])]

    chunks = asyncio.run(go())
    assert chunks[-1] == "data: [DONE]\n\n"
    assert _rows(db) == [("bob", "study", "gpt-x", 300, 40, False, (300 * 2 + 40 * 8) / 1e6)]


def test_stream_without_usage_is_estimated_and_local_is_free(db, monkeypatch):
    monkeypatch.setattr(llm_core, "_stream_llm_unmetered", _fake_stream(usage=False))

    async def go(url):
        with budget.metering("bob", "study"):
            return [c async for c in llm_core.stream_llm(url, "m", [{"role": "user", "content": "hi " * 40}])]

    asyncio.run(go(URL))
    asyncio.run(go("http://127.0.0.1:11434/v1"))
    rows = _rows(db)
    assert len(rows) == 1 and rows[0][5] is True and rows[0][3] > 0 and rows[0][4] > 0


def test_blocked_scoped_stream_yields_a_402_error_event(db, monkeypatch):
    monkeypatch.setattr(llm_core, "_stream_llm_unmetered", _fake_stream())
    budget.save_settings("bob", {"monthly_cap_usd": 0.0001})
    budget.record("bob", source="chat", model="gpt-x", usage={"input_tokens": 1000}, price=PRICED)

    async def go():
        with budget.metering("bob", "study"):
            return [c async for c in llm_core.stream_llm(URL, "gpt-x", [])]

    (chunk,) = asyncio.run(go())
    assert chunk.startswith("event: error") and '"status": 402' in chunk


def test_metered_stream_scope_does_not_leak_while_suspended(db, monkeypatch):
    seen = []

    async def inner():
        seen.append(budget.current_scope())
        yield "a"
        seen.append(budget.current_scope())
        yield "b"

    async def consumer():
        out = []
        async for chunk in budget.metered_stream(inner(), "carol", "study", "t9"):
            out.append((chunk, budget.current_scope()))
        return out

    out = asyncio.run(consumer())
    assert [c for c, _ in out] == ["a", "b"]
    assert all(scope is None for _, scope in out)            # consumer side: no scope
    assert all(s and s.owner == "carol" and s.source == "study" for s in seen)


def test_metered_decorator_and_study_helpers_scope_their_owner(db, monkeypatch):
    seen = {}

    @budget.metered("study")
    async def helper(owner, x):
        seen["scope"] = budget.current_scope()
        return x * 2

    assert asyncio.run(helper("dana", 21)) == 42
    assert (seen["scope"].owner, seen["scope"].source) == ("dana", "study")
    assert budget.current_scope() is None

    from routes.study import _common
    for name in ("_llm_json", "_llm_text", "_llm_json_vision", "_llm_text_vision"):
        assert getattr(_common, name).__wrapped__, name          # wrapped by budget.metered


def test_research_job_runs_inside_a_research_scope(monkeypatch):
    from src.research_handler import ResearchHandler

    handler = ResearchHandler()
    seen = {}

    async def fake_service(self, *args, **kwargs):
        seen["scope"] = budget.current_scope()
        return "report"

    monkeypatch.setattr(ResearchHandler, "call_research_service", fake_service)

    async def go():
        handler.start_research("sess-r", "why is the sky blue", URL, "gpt-x", owner="erin")
        task = handler._active_tasks["sess-r"]["task"]
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert seen["scope"] is not None
    assert (seen["scope"].owner, seen["scope"].source, seen["scope"].session_id) == ("erin", "research", "sess-r")
