"""S5a: internal exception details must not reach shared LLM/Study boundaries.

Failure injection per the S5 remediation plan: the actual transport / model /
tool dependency raises ``RuntimeError(LEAK)`` (or an httpx error carrying the
same marker), and we assert the exact existing envelope keeps its shape and
status while every internal marker stays out of the serialized response, the
persisted event history, and the captured logs.

Family coverage:
  * shared LLM JSON transport   (sync + async ``llm_call``/``llm_call_async``)
  * shared LLM stream transport (``stream_llm`` connect failure + success)
  * Study agent tool dispatch   (``dispatch_tool``)
  * ordinary Study agent turn   (``run_study_agent`` tool failure + history)
  * protected practice turn     (#334 inner provider failure, buffered path)
  * Study agent chat SSE route  (#311 outer stream handler)

Every family also has one valid request and one legitimate validation error.
No real accounts or live providers are used.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src import llm_core as lc
from src import study_agent as sa

OWNER = "alice"
LEAK = "private-marker /srv/private/auth.json postgresql://u:secret-marker@db/app"


def assert_no_internal_details(serialized_response):
    for marker in ("private-marker", "/srv/private/auth.json", "secret-marker"):
        assert marker not in serialized_response


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def db(monkeypatch):
    import os

    import core.database as cdb
    from routes import study_routes as sr
    from tests.helpers.sqlite_db import make_temp_sqlite

    SessionLocal, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)  # forwards to _common
    monkeypatch.setattr(sa, "SessionLocal", SessionLocal)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)
    yield SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _deck_question(session, qid="q-1", qtype="open", **extra):
    from core.database import StudyDeck, StudyQuestion
    if not session.query(StudyDeck).filter(StudyDeck.id == "d-1").first():
        session.add(StudyDeck(id="d-1", owner=OWNER, name="History"))
    kwargs = dict(id=qid, owner=OWNER, deck_id="d-1", qtype=qtype,
                  question="A practice question?", origin="extracted", state="new",
                  stability="0", fsrs_difficulty="0", difficulty="medium")
    kwargs.update(extra)
    session.add(StudyQuestion(**kwargs))
    session.commit()


class _FakeStreamCtx:
    def __init__(self, error=None, status_code=200, lines=()):
        self.error = error
        self.status_code = status_code
        self._lines = list(lines)

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    def aiter_lines(self):
        async def _gen():
            for line in self._lines:
                yield line
        return _gen()


class _FakeClient:
    def __init__(self, error=None, status_code=200, lines=()):
        self._ctx = _FakeStreamCtx(error=error, status_code=status_code, lines=lines)

    def stream(self, *args, **kwargs):
        return self._ctx


# ---------------------------------------------------------- shared transport

class TestSharedTransportFailure:
    def test_async_connect_failure_is_fixed_503(self, monkeypatch, caplog):
        async def transport(client, url, headers=None, **kw):
            raise httpx.ConnectError(LEAK)

        monkeypatch.setattr(lc, "httpx_post_kimi_aware_async", transport)
        monkeypatch.setattr(lc, "_get_http_client", lambda: object())
        monkeypatch.setattr(lc, "_dead_hosts", {})
        monkeypatch.setattr(lc, "_host_fails", {})
        monkeypatch.setattr(lc, "DEAD_HOST_COOLDOWN", 1)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                _run(lc.llm_call_async(
                    "https://u:secret-marker@model.example/v1", "m",
                    [{"role": "user", "content": "hi"}], max_retries=1))

        assert exc.value.status_code == 503
        assert_no_internal_details(str(exc.value.detail))
        assert exc.value.detail == ("Cannot reach the model endpoint after "
                                    "repeated attempts. Try again later.")
        assert_no_internal_details(caplog.text)

    def test_async_unexpected_internal_error_is_fixed_502(self, monkeypatch, caplog):
        async def transport(client, url, headers=None, **kw):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(lc, "httpx_post_kimi_aware_async", transport)
        monkeypatch.setattr(lc, "_get_http_client", lambda: object())

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                _run(lc.llm_call_async(
                    "https://model.example/v1", "m",
                    [{"role": "user", "content": "hi"}], max_retries=1))

        assert exc.value.status_code == 502
        assert_no_internal_details(str(exc.value.detail))
        assert exc.value.detail == "The model request failed unexpectedly. Try again."
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_sync_transform_failure_is_fixed_502(self, monkeypatch, caplog):
        def transport(url, headers, **kw):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(lc, "httpx_post_kimi_aware", transport)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                lc.llm_call("https://model.example/v1", "m",
                            [{"role": "user", "content": "hi"}])

        assert exc.value.status_code == 502
        assert_no_internal_details(str(exc.value.detail))
        assert_no_internal_details(caplog.text)

    def test_provider_401_status_mapping_survives_without_body(self, monkeypatch, caplog):
        async def transport(client, url, headers=None, **kw):
            return httpx.Response(
                401, request=httpx.Request("POST", url),
                text='{"error": {"message": "' + LEAK + '"}}',
            )

        monkeypatch.setattr(lc, "httpx_post_kimi_aware_async", transport)
        monkeypatch.setattr(lc, "_get_http_client", lambda: object())
        monkeypatch.setattr(lc, "_is_host_dead", lambda url: False)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                _run(lc.llm_call_async(
                    "https://api.openai.com/v1", "m",
                    [{"role": "user", "content": "hi"}], max_retries=1))

        assert exc.value.status_code == 401
        assert "rejected the API key" in str(exc.value.detail)
        assert_no_internal_details(str(exc.value.detail))
        assert_no_internal_details(caplog.text)

    def test_valid_request_returns_content(self, monkeypatch):
        async def transport(client, url, headers=None, **kw):
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"choices": [{"message": {"role": "assistant",
                                               "content": "the answer"}}]},
            )

        monkeypatch.setattr(lc, "httpx_post_kimi_aware_async", transport)
        monkeypatch.setattr(lc, "_get_http_client", lambda: object())
        monkeypatch.setattr(lc, "_is_host_dead", lambda url: False)

        out = _run(lc.llm_call_async(
            "https://model.example/v1", "m",
            [{"role": "user", "content": "hi"}], max_retries=1))
        assert out == "the answer"


class TestSharedStreamFailure:
    def test_connect_failure_stream_event_is_fixed(self, monkeypatch, caplog):
        monkeypatch.setattr(lc, "_get_http_client",
                            lambda: _FakeClient(error=httpx.ConnectError(LEAK)))
        monkeypatch.setattr(lc, "_dead_hosts", {})
        monkeypatch.setattr(lc, "_host_fails", {})
        monkeypatch.setattr(lc, "DEAD_HOST_COOLDOWN", 1)

        with caplog.at_level(logging.WARNING):
            chunks = _run(_collect(lc.stream_llm(
                "https://model.example/v1", "m",
                [{"role": "user", "content": "hi"}], timeout=5)))

        assert len(chunks) == 1
        assert chunks[0].startswith("event: error\n")
        payload = json.loads(chunks[0].split("data: ", 1)[1])
        assert payload == {"error": "Cannot reach the model endpoint.",
                           "status": 503}
        assert_no_internal_details(chunks[0])
        assert_no_internal_details(caplog.text)

    def test_valid_stream_request_relays_deltas(self, monkeypatch):
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "hel"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
            "data: [DONE]",
        ]
        monkeypatch.setattr(lc, "_get_http_client",
                            lambda: _FakeClient(lines=lines))
        monkeypatch.setattr(lc, "_is_host_dead", lambda url: False)

        chunks = _run(_collect(lc.stream_llm(
            "https://model.example/v1", "m",
            [{"role": "user", "content": "hi"}], timeout=5)))
        deltas = [json.loads(c.split("data: ", 1)[1])["delta"]
                  for c in chunks if c.startswith("data: ") and "[DONE]" not in c]
        assert "".join(deltas) == "hello"
        assert chunks[-1] == "data: [DONE]\n\n"


async def _collect(agen):
    return [c async for c in agen]


# ------------------------------------------------------- study agent tools

class TestDispatchTool:
    def test_internal_tool_failure_is_fixed(self, db, monkeypatch, caplog):
        captured = {}

        async def broken(owner, args):
            captured["owner"] = owner
            raise RuntimeError(LEAK)

        monkeypatch.setattr(sa.TOOLS["list_subjects"], "handler", broken)

        with caplog.at_level(logging.WARNING):
            res = _run(sa.dispatch_tool("list_subjects", OWNER, {}))

        assert res == {"ok": False, "error": "The tool could not run. Try again."}
        assert_no_internal_details(json.dumps(res))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text
        assert captured["owner"] == OWNER

    def test_tool_validation_error_detail_is_kept(self, db):
        res = _run(sa.dispatch_tool("create_subject", OWNER, {}))
        assert res["ok"] is False
        assert "missing required argument(s): name" in res["error"]
        assert_no_internal_details(json.dumps(res))

    def test_valid_tool_returns_ok(self, db):
        res = _run(sa.dispatch_tool("create_subject", OWNER, {"name": "Math"}))
        assert res["ok"] and res["result"]["name"] == "Math"


# ---------------------------------------------------- ordinary agent turn

def _scripted_llm(script):
    calls = {"n": 0}

    async def fake_stream(url, model, messages, **kw):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(script):
            raise AssertionError("scripted generator ran out of rounds")
        for chunk in script[i]:
            yield chunk

    return fake_stream


class TestOrdinaryAgentTurnToolFailure:
    def test_tool_error_never_enters_events_or_history(
            self, db, monkeypatch, caplog):
        monkeypatch.setattr(
            "routes.study_routes._resolve_study_model",
            lambda owner, prefer_text=False: ("http://x", "scripted-model", {}),
        )

        async def broken_handler(owner, args):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(sa.TOOLS["list_subjects"], "handler", broken_handler)
        monkeypatch.setattr(lc, "stream_llm", _scripted_llm([
            ["data: " + json.dumps({"type": "tool_calls", "calls": [
                {"id": "c1", "name": "list_subjects", "arguments": "{}"}]}) + "\n\n",
             "data: [DONE]\n\n"],
            ['data: {"delta": "All your subjects are listed above."}\n\n',
             "data: [DONE]\n\n"],
        ]))

        thread = sa.create_thread(OWNER)
        with caplog.at_level(logging.WARNING):
            chunks = _run(_collect(sa.run_study_agent(OWNER, thread["id"],
                                                      "list my subjects")))
        assert_no_internal_details("".join(chunks))
        assert_no_internal_details(caplog.text)

        event_dump = "".join(c for c in chunks if c.startswith("data: "))
        assert '"tool_output"' in event_dump
        persisted = sa._load_rows(OWNER, thread["id"], 50)
        assert persisted, "expected persisted history rows"
        for row in persisted:
            assert_no_internal_details(str(row.content or "") + str(row.name or ""))
        tool_rows = [r for r in persisted if r.name == "list_subjects"]
        assert tool_rows
        assert "The tool could not run. Try again." in tool_rows[0].content


# ------------------------------------------------------- protected practice

class TestProtectedPracticeProviderFailure:
    def test_provider_failure_yields_generic_error_retryable(
            self, db, monkeypatch, caplog):
        from routes.study._common import AskIn
        from src.study_practice_coach import build_practice_context, \
            resolve_practice_thread

        monkeypatch.setattr(
            "routes.study_routes._resolve_study_model",
            lambda owner, prefer_text=False: ("http://x", "scripted-model", {}),
        )

        async def broken_stream(url, model, messages, **kw):
            raise RuntimeError(LEAK)
            yield  # pragma: no cover

        monkeypatch.setattr(lc, "stream_llm", broken_stream)

        session = db()
        _deck_question(session, qid="q-1")
        session.close()
        body = AskIn(message="help me")
        thread_id = resolve_practice_thread(OWNER, "q-1", None)
        context = build_practice_context(OWNER, "q-1", body)

        with caplog.at_level(logging.WARNING):
            chunks = _run(_collect(sa.run_study_agent(
                OWNER, thread_id, "help me", practice_context=context)))

        assert_no_internal_details("".join(chunks))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

        events = [json.loads(c.split("data: ", 1)[1]) for c in chunks
                  if c.startswith("data: ") and "[DONE]" not in c]
        kinds = [e.get("type") for e in events]
        assert "model_info" in kinds and "status" in kinds
        error = next(e for e in events if e.get("type") == "error")
        assert error == {"type": "error", "retryable": True,
                         "message": sa._GENERIC_PRACTICE_ERROR}
        assert chunks[-1] == "data: [DONE]\n\n"

        persisted = sa._load_rows(OWNER, thread_id, 50)
        for row in persisted:
            assert_no_internal_details(str(row.content or "") + str(row.name or ""))

    def test_practice_missing_question_validation_is_fixed_404(self, db):
        from src.study_practice_coach import resolve_practice_thread
        with pytest.raises(HTTPException) as exc:
            resolve_practice_thread(OWNER, "missing-q", None)
        assert exc.value.status_code == 404
        assert_no_internal_details(str(exc.value.detail))


# ------------------------------------------------------- agent chat SSE route

@pytest.fixture
def agent_client(monkeypatch):
    from routes import study_agent_routes as sar

    app = FastAPI()
    app.include_router(sar.setup_study_agent_routes())
    monkeypatch.setattr(sar, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(sar.study_agent, "get_thread",
                        lambda user, tid: type("T", (), {"question_id": None})())
    client = TestClient(app, raise_server_exceptions=False)
    return client


class TestStudyAgentChatRoute:
    def test_stream_failure_is_fixed_wellformed(self, agent_client, monkeypatch, caplog):
        from routes import study_agent_routes as sar

        async def broken(owner, thread_id, text, **kw):
            raise RuntimeError(LEAK)
            yield  # pragma: no cover

        monkeypatch.setattr(sar.study_agent, "run_study_agent", broken)

        with caplog.at_level(logging.WARNING):
            resp = agent_client.post("/api/study/agent/chat",
                                     json={"message": "hi", "thread_id": "t-1"})

        assert resp.status_code == 200
        assert_no_internal_details(resp.text)
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text
        lines = resp.text.splitlines()
        assert lines[0].startswith("data: ")
        assert json.loads(lines[0][len("data: "):])["type"] == "thread"
        events = [json.loads(line[len("data: "):]) for line in lines
                  if line.startswith("data: ") and "[DONE]" not in line]
        assert [e for e in events if e.get("type") == "error"] == [
            {"type": "error", "message": "The reply request failed. Try again."}]
        assert resp.text.rstrip().endswith("data: [DONE]")

    def test_valid_chat_forwards_events(self, agent_client, monkeypatch):
        from routes import study_agent_routes as sar

        async def scripted(owner, thread_id, text, **kw):
            yield sa._sse({"type": "model_info", "model": "m"})
            yield sa._sse({"delta": "hi"})
            yield sa.DONE

        monkeypatch.setattr(sar.study_agent, "run_study_agent", scripted)

        resp = agent_client.post("/api/study/agent/chat",
                                 json={"message": "hi", "thread_id": "t-1"})
        assert resp.status_code == 200
        assert '"type": "model_info"' in resp.text
        assert resp.text.rstrip().endswith("data: [DONE]")

    def test_missing_message_is_400(self, agent_client):
        resp = agent_client.post("/api/study/agent/chat", json={"message": "  "})
        assert resp.status_code == 400
        assert_no_internal_details(resp.text)