"""S5a/S5b: internal exception details must not reach shared app boundaries.

Failure injection per the S5 remediation plan: the actual transport / model /
tool / filesystem dependency raises ``RuntimeError(LEAK)`` (or an httpx error
carrying the same marker), and we assert the exact existing envelope keeps its
shape and status while every internal marker stays out of the serialized
response, the persisted event history, and the captured logs.

Family coverage (S5a):
  * shared LLM JSON transport   (sync + async ``llm_call``/``llm_call_async``)
  * shared LLM stream transport (``stream_llm`` connect failure + success)
  * Study agent tool dispatch   (``dispatch_tool``)
  * ordinary Study agent turn   (``run_study_agent`` tool failure + history)
  * protected practice turn     (#334 inner provider failure, buffered path)
  * Study agent chat SSE route  (#311 outer stream handler)

Family coverage (S5b — app-surface routes):
  * calendar quick-parse LLM failure (#71/#73)
  * email ai-reply endpoint failure  (#105)
  * email attachment-as-doc read failures (#98/#99)
  * chat rewrite SSE failure (#78) + terminal persistence logging (#76/#77)
  * auth integration connectivity test (#66/#67)

Every failure family also has one valid request and one legitimate validation
error. No real accounts or live providers are used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from unittest.mock import MagicMock

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


# ------------------------------------------------------- calendar quick-parse

@pytest.fixture
def calendar_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Patch and build routes from the SAME module object: test_calendar_owner_scope
    # re-executes routes.calendar_routes in a fresh namespace, so importing the
    # setup function and patching via the import system can land on different
    # module dicts and the route runs an unpatched _require_user.
    import routes.calendar_routes as cr

    monkeypatch.setattr(cr, "_require_user", lambda _request: OWNER)
    app = FastAPI()
    app.include_router(cr.setup_calendar_routes())
    return TestClient(app, raise_server_exceptions=False)


_SAMPLE_EVENT_JSON = json.dumps({
    "summary": "Lunch", "dtstart": "2026-09-22T12:00:00",
    "dtend": "2026-09-22T13:00:00", "all_day": False,
    "location": "", "description": "", "confidence": 0.9,
})


class TestCalendarQuickParse:
    def test_llm_internal_failure_is_fixed(self, calendar_client, monkeypatch, caplog):
        import src.endpoint_resolver as er
        import src.llm_core as lc

        monkeypatch.setattr(
            er, "resolve_endpoint",
            lambda kind, owner=None: ("http://model.invalid/v1", "m", {}),
        )

        async def boom(*args, **kwargs):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(lc, "llm_call_async", boom)

        with caplog.at_level(logging.WARNING):
            resp = calendar_client.post(
                "/api/calendar/quick-parse", json={"text": "lunch tomorrow"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "error": "Could not parse the event. Try again."}
        assert_no_internal_details(resp.text)
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_llm_http_exception_detail_is_controlled(self, calendar_client, monkeypatch):
        import src.endpoint_resolver as er
        import src.llm_core as lc

        monkeypatch.setattr(
            er, "resolve_endpoint",
            lambda kind, owner=None: ("http://model.invalid/v1", "m", {}),
        )

        async def denied(*args, **kwargs):
            raise HTTPException(401, "test-provider rejected the API key. Check Model Endpoints and re-paste the key.")

        monkeypatch.setattr(lc, "llm_call_async", denied)

        resp = calendar_client.post(
            "/api/calendar/quick-parse", json={"text": "lunch tomorrow"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "rejected the API key" in body["error"]
        assert_no_internal_details(resp.text)

    def test_valid_parse_returns_event(self, calendar_client, monkeypatch):
        import src.endpoint_resolver as er
        import src.llm_core as lc

        monkeypatch.setattr(
            er, "resolve_endpoint",
            lambda kind, owner=None: ("http://model.invalid/v1", "m", {}),
        )

        async def ok(*args, **kwargs):
            return _SAMPLE_EVENT_JSON

        monkeypatch.setattr(lc, "llm_call_async", ok)

        resp = calendar_client.post(
            "/api/calendar/quick-parse", json={"text": "lunch tomorrow"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["event"]["summary"] == "Lunch"

    def test_missing_text_is_validation_error(self, calendar_client):
        resp = calendar_client.post("/api/calendar/quick-parse", json={"text": "  "})
        assert resp.status_code == 400
        assert_no_internal_details(resp.text)


# ---------------------------------------------------------- email ai-reply

def _route_endpoint(router, path, method):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def _email_ai_reply(monkeypatch):
    """Resolve the /api/email/ai-reply handler with a scripted endpoint set."""
    from routes.email_routes import setup_email_routes

    import src.endpoint_resolver as er
    import src.llm_core as lc

    monkeypatch.setattr(
        er, "resolve_endpoint",
        lambda kind, owner=None: ("https://u:secret-marker@mail.model.example/v1", "m", {}),
    )
    monkeypatch.setattr(lc, "list_model_ids", lambda *a, **k: None)
    monkeypatch.setattr(er, "resolve_utility_fallback_candidates", lambda owner=None: [])

    import routes.email_routes as er_outes
    monkeypatch.setattr(er_outes, "_load_settings", lambda: {})

    router = setup_email_routes()
    return _route_endpoint(router, "/api/email/ai-reply", "POST")


class TestEmailAiReply:
    def test_all_endpoints_failure_is_fixed(self, monkeypatch, caplog):
        import src.llm_core as lc

        async def boom(candidates, messages, **kwargs):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(lc, "llm_call_async_with_fallback", boom)
        ai_reply = _email_ai_reply(monkeypatch)

        with caplog.at_level(logging.WARNING):
            result = _run(ai_reply({
                "to": "sender@example.com", "subject": "S",
                "original_body": "Body", "fast": True,
            }, owner=OWNER))

        assert result["success"] is False
        # The candidate host is shown, never its embedded credentials.
        assert "m@mail.model.example" in result["error"]
        assert result["error"].endswith(". Check your API keys in Settings \u2192 Services.")
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_http_exception_detail_stays_controlled(self, monkeypatch):
        import src.llm_core as lc

        async def denied(candidates, messages, **kwargs):
            raise HTTPException(502, "The model request could not be sent. Check the endpoint configuration and try again.")

        monkeypatch.setattr(lc, "llm_call_async_with_fallback", denied)
        ai_reply = _email_ai_reply(monkeypatch)

        result = _run(ai_reply({
            "to": "sender@example.com", "subject": "S",
            "original_body": "Body", "fast": True,
        }, owner=OWNER))

        assert result["success"] is False
        assert "The model request could not be sent." in result["error"]
        assert_no_internal_details(json.dumps(result))

    def test_valid_reply_returns_styled_reply(self, monkeypatch):
        import src.llm_core as lc

        async def ok(candidates, messages, **kwargs):
            return "<<<REPLY>>>\nThanks!\n<<<END>>>"

        monkeypatch.setattr(lc, "llm_call_async_with_fallback", ok)
        ai_reply = _email_ai_reply(monkeypatch)

        result = _run(ai_reply({
            "to": "sender@example.com", "subject": "S",
            "original_body": "Body", "model": "m", "fast": True,
        }, owner=OWNER))

        assert result == {"success": True, "reply": "Thanks!", "model_used": "m"}

    def test_missing_body_is_validation_error(self, monkeypatch):
        ai_reply = _email_ai_reply(monkeypatch)
        result = _run(ai_reply({"to": "sender@example.com"}, owner=OWNER))
        assert result == {"success": False, "error": "No email body provided"}


# ------------------------------------------------- email attachment-as-doc

class _FakeImapConn:
    def __init__(self, raw):
        self._raw = raw
        self.selected = None

    def select(self, folder, readonly=True):
        self.selected = folder
        return "OK", []

    def uid(self, command, uid_set, query):
        return "OK", [(b"1 (RFC822)", self._raw)]


class _FakeImapCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *args):
        pass


def _email_attachment_as_doc(monkeypatch, tmp_path, attachment_path):
    """Resolve /api/email/attachment-as-doc/{uid}/{index} with fake IMAP."""
    from routes.email_routes import setup_email_routes

    import routes.email_routes as er

    fake_conn = _FakeImapConn(b"From: someone@example.com\r\nSubject: hi\r\n\r\n")
    monkeypatch.setattr(er, "_imap", lambda account_id=None, owner="": _FakeImapCtx(fake_conn))
    monkeypatch.setattr(er, "attachment_extract_dir", lambda folder, uid: str(tmp_path))
    monkeypatch.setattr(er, "_extract_attachment_to_disk", lambda msg, index, target_dir: attachment_path)

    import src.auth_helpers as sah
    monkeypatch.setattr(sah, "get_current_user", lambda request: OWNER)

    class _NoDb:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None

            return _noop

        def query(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def add(self, *args, **kwargs):
            pass

        def update(self, *args, **kwargs):
            return 0

        def commit(self):
            pass

        def close(self):
            pass

    import src.database as sdb
    monkeypatch.setattr(sdb, "SessionLocal", _NoDb)

    router = setup_email_routes()
    return _route_endpoint(router, "/api/email/attachment-as-doc/{uid}/{index}", "POST")


class TestEmailAttachmentAsDoc:
    @pytest.mark.parametrize("suffix", [".txt", ".docx"])
    def test_read_failure_is_fixed(self, monkeypatch, caplog, tmp_path, suffix):
        filepath = tmp_path / f"notes{suffix}"
        filepath.write_text("content", encoding="utf-8")

        if suffix == ".txt":
            def boom_read(*args, **kwargs):
                raise RuntimeError(LEAK)
            from pathlib import Path
            monkeypatch.setattr(Path, "read_text", boom_read)
        else:
            docx_stub = types.ModuleType("docx")

            def boom_docx(*args, **kwargs):
                raise RuntimeError(LEAK)

            docx_stub.Document = boom_docx
            monkeypatch.setitem(sys.modules, "docx", docx_stub)

        endpoint = _email_attachment_as_doc(monkeypatch, tmp_path, filepath)

        with caplog.at_level(logging.WARNING):
            result = endpoint(uid="1", index=0, request=None,
                              folder="INBOX", account_id=None, owner=OWNER)

        expected = "Failed to read text file" if suffix == ".txt" else "Failed to read docx file"
        assert result == {"error": expected, "filename": f"notes{suffix}"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_valid_text_attachment_returns_doc(self, monkeypatch, tmp_path):
        filepath = tmp_path / "notes.txt"
        filepath.write_text("hello", encoding="utf-8")

        endpoint = _email_attachment_as_doc(monkeypatch, tmp_path, filepath)

        result = endpoint(uid="1", index=0, request=None,
                          folder="INBOX", account_id=None, owner=OWNER)

        assert result["filename"] == "notes.txt"
        assert result.get("doc_id")

    def test_unsupported_type_is_validation_error(self, monkeypatch, tmp_path):
        filepath = tmp_path / "photo.png"
        filepath.write_text("x", encoding="utf-8")

        endpoint = _email_attachment_as_doc(monkeypatch, tmp_path, filepath)

        result = endpoint(uid="1", index=0, request=None,
                          folder="INBOX", account_id=None, owner=OWNER)

        assert result == {"error": "Unsupported attachment type: .png", "filename": "photo.png"}


# ------------------------------------------------------------- chat rewrite

@pytest.fixture
def rewrite_app(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import chat_routes as cr
    from core.models import ChatMessage

    class _FakeSessionManager:
        def __init__(self):
            self.saved = 0
            self.sessions = {}

        def get_session(self, session_id):
            if session_id not in self.sessions:
                raise KeyError(session_id)
            return self.sessions[session_id]

        def save_sessions(self):
            self.saved += 1

    sm = _FakeSessionManager()
    sess = type("S", (), {
        "endpoint_url": "https://u:secret-marker@chat.model.example/v1",
        "model": "m",
        "headers": {},
        "history": [ChatMessage("assistant", "old answer")],
    })()
    sm.sessions["s-1"] = sess

    router = cr.setup_chat_routes(sm, None, None, None, None, None)
    monkeypatch.setattr(cr, "_verify_session_owner", lambda request, session_id: None)

    class _FakeDb:
        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def order_by(self, *a):
            return self

        def first(self):
            return None

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cr, "SessionLocal", lambda: _FakeDb())

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False), sm


class TestChatRewrite:
    def test_stream_failure_is_fixed_wellformed(self, rewrite_app, monkeypatch, caplog):
        from routes import chat_routes as cr

        async def broken(*args, **kwargs):
            raise RuntimeError(LEAK)
            yield  # pragma: no cover

        monkeypatch.setattr(cr, "stream_llm", broken)
        client, sm = rewrite_app

        with caplog.at_level(logging.ERROR):
            resp = client.post("/api/rewrite", json={
                "session_id": "s-1", "original_text": "old answer",
                "instruction": "make it shorter",
            })

        assert resp.status_code == 200
        assert_no_internal_details(resp.text)
        assert_no_internal_details(caplog.text)
        assert "Rewrite stream error error_type=RuntimeError" in caplog.text
        lines = [line for line in resp.text.splitlines() if line.startswith("data: ")]
        assert json.loads(lines[-1][len("data: "):]) == {
            "error": "The rewrite request failed. Try again.", "status": 500}

    def test_valid_rewrite_relays_and_persists(self, rewrite_app, monkeypatch):
        from routes import chat_routes as cr

        async def scripted(*args, **kwargs):
            yield 'data: {"delta": "shorter answer"}\n\n'
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(cr, "stream_llm", scripted)
        client, sm = rewrite_app

        resp = client.post("/api/rewrite", json={
            "session_id": "s-1", "original_text": "old answer",
            "instruction": "make it shorter",
        })

        assert resp.status_code == 200
        assert "shorter answer" in resp.text
        assert resp.text.rstrip().endswith("data: [DONE]")
        assert sm.saved >= 1

    def test_missing_fields_is_400(self, rewrite_app):
        client, _sm = rewrite_app
        resp = client.post("/api/rewrite", json={"instruction": "x"})
        assert resp.status_code == 400
        assert_no_internal_details(resp.text)

    def test_unknown_session_is_404(self, rewrite_app):
        client, _sm = rewrite_app
        resp = client.post("/api/rewrite", json={
            "session_id": "nope", "original_text": "o", "instruction": "i"})
        assert resp.status_code == 404
        assert_no_internal_details(resp.text)


# ------------------------------------------- auth integration connectivity

class _FakeAuthManager:
    def is_admin(self, user):
        return user == "admin"

    def get_username_for_token(self, token):
        return "admin"


def _auth_integration_test_endpoint():
    from routes.auth_routes import setup_auth_routes

    router = setup_auth_routes(_FakeAuthManager())
    return _route_endpoint(router, "/api/auth/integrations/{integration_id}/test", "POST")


class TestAuthIntegrationConnectivity:
    def test_ntfy_failure_never_serializes_exception(self, monkeypatch, caplog):
        import httpx as httpx_mod
        import routes.auth_routes as ar

        monkeypatch.setattr(
            ar, "get_integration",
            lambda integration_id: {"preset": "ntfy", "name": "ntfy",
                                    "base_url": "https://ntfy.example.com"},
        )
        monkeypatch.setattr(ar, "_load_settings", lambda: {"reminder_ntfy_topic": "reminders"})

        class _BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                raise RuntimeError(LEAK)

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _BoomClient())
        endpoint = _auth_integration_test_endpoint()

        class _Req:
            cookies = {}

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint("ntfy-int", _Req()))

        assert result["ok"] is False
        assert result["message"].startswith("ntfy publish to https://ntfy.example.com/reminders failed (RuntimeError).")
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_discord_failure_never_serializes_exception(self, monkeypatch, caplog):
        import httpx as httpx_mod
        import routes.auth_routes as ar

        monkeypatch.setattr(
            ar, "get_integration",
            lambda integration_id: {"preset": "discord_webhook", "name": "discord",
                                    "base_url": "https://discord.com/api/webhooks/x/y"},
        )

        class _BoomClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                raise RuntimeError(LEAK)

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _BoomClient())
        endpoint = _auth_integration_test_endpoint()

        class _Req:
            cookies = {}

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint("dc-int", _Req()))

        assert result["ok"] is False
        assert result["message"] == "Request failed (RuntimeError)."
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)

    def test_ntfy_success_keeps_owner_diagnostic(self, monkeypatch):
        import httpx as httpx_mod
        import routes.auth_routes as ar

        monkeypatch.setattr(
            ar, "get_integration",
            lambda integration_id: {"preset": "ntfy", "name": "ntfy",
                                    "base_url": "https://ntfy.example.com"},
        )
        monkeypatch.setattr(ar, "_load_settings", lambda: {"reminder_ntfy_topic": "reminders"})

        class _OkClient:
            is_success = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                return self

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _OkClient())
        endpoint = _auth_integration_test_endpoint()

        class _Req:
            cookies = {}

        result = _run(endpoint("ntfy-int", _Req()))

        assert result["ok"] is True
        assert "ntfy.example.com/reminders" in result["message"]

    def test_discord_missing_webhook_is_validation_error(self, monkeypatch):
        import routes.auth_routes as ar

        monkeypatch.setattr(
            ar, "get_integration",
            lambda integration_id: {"preset": "discord_webhook", "name": "discord",
                                    "base_url": ""},
        )
        endpoint = _auth_integration_test_endpoint()

        class _Req:
            cookies = {}

        result = _run(endpoint("dc-int", _Req()))

        assert result["ok"] is False
        assert "No webhook URL set" in result["message"]


# ------------------------------------------------- S5c: cookbook routes

def _cookbook_router(monkeypatch, tmp_path):
    import routes.cookbook_routes as cr

    monkeypatch.setattr(cr, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cr, "COOKBOOK_STATE_FILE", str(tmp_path / "cookbook_state.json"))
    router = cr.setup_cookbook_routes()
    return cr, router


def _cookbook_endpoint(router, path, method):
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class _NoAdminReq:
    """Stand-in Request for admin-gated cookbook routes (require_admin is patched)."""

    cookies = {}

    async def json(self):
        return self._body

    def __init__(self, body=None):
        self._body = body or {}


class TestCookbookSsh:
    def _endpoint(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        endpoint = _cookbook_endpoint(router, "/api/cookbook/test-ssh", "POST")
        return cr, lambda **kw: endpoint(None, SimpleNamespace(**kw))

    def test_ssh_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        cr, call = self._endpoint(monkeypatch, tmp_path)

        async def boom(host, port, cmd, **kw):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(cr, "run_ssh_command_async", boom)

        with caplog.at_level(logging.WARNING):
            result = _run(call(host="server", ssh_port=None))

        assert result == {"stdout": "", "stderr": "SSH test failed", "exit_code": -1}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_ssh_valid_request_keeps_process_output(self, monkeypatch, tmp_path):
        cr, call = self._endpoint(monkeypatch, tmp_path)

        async def ok(host, port, cmd, **kw):
            return 0, b"ok\n", b""

        monkeypatch.setattr(cr, "run_ssh_command_async", ok)

        result = _run(call(host="server", ssh_port=None))
        assert result == {"stdout": "ok\n", "stderr": "", "exit_code": 0}

    def test_ssh_bad_host_is_validation_error(self, monkeypatch, tmp_path):
        from fastapi import HTTPException

        _cr, call = self._endpoint(monkeypatch, tmp_path)
        with pytest.raises(HTTPException) as exc:
            _run(call(host="bad host!", ssh_port=None))
        assert exc.value.status_code == 400
        assert_no_internal_details(str(exc.value.detail))


class TestCookbookModelRoutes:
    def _router(self, monkeypatch, tmp_path):
        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        monkeypatch.setattr(cr, "_binary_available", lambda binary, remote, *a, **k: _yes())
        return cr, router

    def test_download_launch_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import types as _types

        from routes.cookbook_helpers import ModelDownloadRequest

        cr, router = self._router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "find_bash", lambda: _boom())
        endpoint = _cookbook_endpoint(router, "/api/model/download", "POST")

        req = ModelDownloadRequest(repo_id="org/some-model")
        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_NoAdminReq(), req))

        assert result["ok"] is False
        assert result["error"] == "Could not launch the download"
        assert result["session_id"]
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_serve_launch_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        from routes.cookbook_helpers import ServeRequest

        cr, router = self._router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "find_bash", lambda: _boom())
        endpoint = _cookbook_endpoint(router, "/api/model/serve", "POST")

        req = ServeRequest(repo_id="cached-model", cmd="python -m vllm.entrypoints.openai.api_server --model cached-model")
        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_NoAdminReq(), req))

        assert result["ok"] is False
        assert result["error"] == "Could not launch the serve session"
        assert result["session_id"]
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_download_bad_repo_is_validation_error(self, monkeypatch, tmp_path):
        from fastapi import HTTPException
        from routes.cookbook_helpers import ModelDownloadRequest

        cr, router = self._router(monkeypatch, tmp_path)
        endpoint = _cookbook_endpoint(router, "/api/model/download", "POST")

        with pytest.raises(HTTPException) as exc:
            _run(endpoint(_NoAdminReq(), ModelDownloadRequest(repo_id="bad repo")))
        assert exc.value.status_code == 400
        assert_no_internal_details(str(exc.value.detail))

    def test_download_valid_request_launches(self, monkeypatch, tmp_path):
        from routes.cookbook_helpers import ModelDownloadRequest

        cr, router = self._router(monkeypatch, tmp_path)
        endpoint = _cookbook_endpoint(router, "/api/model/download", "POST")

        result = _run(endpoint(_NoAdminReq(), ModelDownloadRequest(repo_id="org/some-model")))
        # Either the local detached launch (bash present) succeeded or, without
        # bash, the cmd.exe error-recording fallback ran — both are a valid 200.
        assert result["ok"] is True
        assert result["session_id"]


async def _yes():
    return True


def _boom(*args, **kwargs):
    raise RuntimeError(LEAK)


class TestCookbookCachedModels:
    def test_parse_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import asyncio as aio
        import types as _types

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

            def kill(self):
                pass

        async def fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cr, "json", _types.SimpleNamespace(loads=_boom))

        endpoint = _cookbook_endpoint(router, "/api/model/cached", "GET")
        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_NoAdminReq()))

        assert result == {"models": [], "host": "local",
                          "error": "Failed to parse cached models"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_cached_valid_request_returns_models(self, monkeypatch, tmp_path):
        import asyncio as aio

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        payload = json.dumps([{
            "repo_id": "org/model-x", "size_bytes": 1073741824,
            "nb_files": 2, "has_incomplete": False,
        }])

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return payload.encode(), b""

            def kill(self):
                pass

        async def fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)

        endpoint = _cookbook_endpoint(router, "/api/model/cached", "GET")
        result = _run(endpoint(_NoAdminReq()))

        assert result["host"] == "local"
        assert result["models"][0]["repo_id"] == "org/model-x"

    def test_cached_bad_host_is_validation_error(self, monkeypatch, tmp_path):
        from fastapi import HTTPException

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        endpoint = _cookbook_endpoint(router, "/api/model/cached", "GET")

        with pytest.raises(HTTPException) as exc:
            _run(endpoint(_NoAdminReq(), host="bad host!"))
        assert exc.value.status_code == 400
        assert_no_internal_details(str(exc.value.detail))


class TestCookbookSetup:
    def test_setup_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import asyncio as aio
        from types import SimpleNamespace

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

            def kill(self):
                pass

        calls = {"n": 0}

        async def fake_shell(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return _FakeProc()
            raise RuntimeError(LEAK)

        monkeypatch.setattr(aio, "create_subprocess_shell", fake_shell)

        endpoint = _cookbook_endpoint(router, "/api/cookbook/setup", "POST")
        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_NoAdminReq(), SimpleNamespace(host="server", ssh_port=None)))

        assert result["ok"] is False
        assert result["error"] == "Setup failed"
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_setup_valid_request_runs(self, monkeypatch, tmp_path):
        import asyncio as aio
        from types import SimpleNamespace

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"OK\n", b""

            def kill(self):
                pass

        async def fake_shell(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(aio, "create_subprocess_shell", fake_shell)

        endpoint = _cookbook_endpoint(router, "/api/cookbook/setup", "POST")
        result = _run(endpoint(_NoAdminReq(), SimpleNamespace(host="server", ssh_port=None)))

        assert result["ok"] is True
        assert "OK" in result["output"]

    def test_setup_missing_host_is_validation_error(self, monkeypatch, tmp_path):
        from fastapi import HTTPException
        from types import SimpleNamespace

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        endpoint = _cookbook_endpoint(router, "/api/cookbook/setup", "POST")

        with pytest.raises(HTTPException) as exc:
            _run(endpoint(_NoAdminReq(), SimpleNamespace(host="", ssh_port=None)))
        assert exc.value.status_code == 400
        assert "host is required" in str(exc.value.detail)


class TestCookbookGpus:
    def _endpoint(self, monkeypatch, tmp_path):
        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        return cr, _cookbook_endpoint(router, "/api/cookbook/gpus", "GET")

    def test_probe_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import asyncio as aio

        cr, endpoint = self._endpoint(monkeypatch, tmp_path)

        class _ErrProc:
            returncode = 1

            async def communicate(self):
                return b"", b"x"

            def kill(self):
                pass

        async def boom_exec(*args, **kwargs):
            raise RuntimeError(LEAK)

        async def fake_shell(*args, **kwargs):
            return _ErrProc()

        monkeypatch.setattr(aio, "create_subprocess_exec", boom_exec)
        monkeypatch.setattr(aio, "create_subprocess_shell", fake_shell)

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_NoAdminReq()))

        assert result == {"ok": False, "error": "nvidia-smi probe failed", "gpus": []}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_gpus_valid_request_lists_cards(self, monkeypatch, tmp_path):
        import asyncio as aio

        cr, endpoint = self._endpoint(monkeypatch, tmp_path)
        csv_line = b"0, GeForce RTX, 1000, 2000, 500, 50, GPU-abc\n"

        class _OutProc:
            returncode = 0

            async def communicate(self):
                return csv_line, b""

            def kill(self):
                pass

        class _ErrProc:
            returncode = 1

            async def communicate(self):
                return b"", b"x"

            def kill(self):
                pass

        async def fake_exec(*args, **kwargs):
            return _OutProc()

        async def fake_shell(*args, **kwargs):
            return _ErrProc()

        monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(aio, "create_subprocess_shell", fake_shell)

        result = _run(endpoint(_NoAdminReq()))

        assert result["ok"] is True
        assert result["gpus"][0]["name"] == "GeForce RTX"
        assert result["backend"] == "cuda"

    def test_gpus_bad_host_is_validation_error(self, monkeypatch, tmp_path):
        from fastapi import HTTPException

        _cr, endpoint = self._endpoint(monkeypatch, tmp_path)
        with pytest.raises(HTTPException) as exc:
            _run(endpoint(_NoAdminReq(), host="bad host!"))
        assert exc.value.status_code == 400
        assert_no_internal_details(str(exc.value.detail))


class TestCookbookKillPid:
    def _endpoint(self, monkeypatch, tmp_path):
        from types import SimpleNamespace

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        endpoint = _cookbook_endpoint(router, "/api/cookbook/kill-pid", "POST")
        return cr, lambda **kw: endpoint(_NoAdminReq(), SimpleNamespace(**kw))

    def test_kill_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import asyncio as aio

        cr, call = self._endpoint(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "pid_alive", lambda pid: True)
        monkeypatch.setattr(cr, "kill_process_tree", _boom)
        monkeypatch.setattr(aio, "create_subprocess_exec", _boom_async)

        with caplog.at_level(logging.WARNING):
            result = _run(call(pid=500, host=None, ssh_port=None, signal="TERM"))

        assert result == {"ok": False, "error": "kill command failed"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_kill_valid_request_returns_ok(self, monkeypatch, tmp_path):
        import asyncio as aio

        cr, call = self._endpoint(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "pid_alive", lambda pid: True)
        monkeypatch.setattr(cr, "kill_process_tree", lambda pid: None)

        class _OkProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def fake_exec(*args, **kwargs):
            return _OkProc()

        monkeypatch.setattr(aio, "create_subprocess_exec", fake_exec)

        result = _run(call(pid=500, host=None, ssh_port=None, signal="TERM"))
        assert result["ok"] is True
        assert result["pid"] == 500

    def test_kill_low_pid_is_validation_error(self, monkeypatch, tmp_path):
        from fastapi import HTTPException

        _cr, call = self._endpoint(monkeypatch, tmp_path)
        with pytest.raises(HTTPException) as exc:
            _run(call(pid=50, host=None, ssh_port=None, signal="TERM"))
        assert exc.value.status_code == 400
        assert_no_internal_details(str(exc.value.detail))


async def _boom_async(*args, **kwargs):
    raise RuntimeError(LEAK)


class TestCookbookState:
    def test_state_save_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import core.atomic_io as aio

        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)
        monkeypatch.setattr(aio, "atomic_write_json", _boom)

        endpoint = _cookbook_endpoint(router, "/api/cookbook/state", "POST")
        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_NoAdminReq(body={"tasks": [], "env": {}})))

        assert result == {"ok": False, "error": "Failed to save cookbook state"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_state_valid_save_returns_ok(self, monkeypatch, tmp_path):
        cr, router = _cookbook_router(monkeypatch, tmp_path)
        monkeypatch.setattr(cr, "require_admin", lambda request: None)

        endpoint = _cookbook_endpoint(router, "/api/cookbook/state", "POST")
        result = _run(endpoint(_NoAdminReq(body={"tasks": [], "env": {}})))

        assert result["ok"] is True


class TestCookbookHfLatest:
    def test_fetch_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import httpx as httpx_mod

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _RaisingCtxClient())
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/hf-latest", "GET")

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint())

        assert result == {"models": [], "error": "Failed to fetch HuggingFace models"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_hf_latest_valid_request_returns_models(self, monkeypatch, tmp_path):
        import httpx as httpx_mod

        monkeypatch.setattr(httpx_mod, "AsyncClient",
                            lambda **kw: _JsonCtxClient([{"modelId": "org/model-7b",
                                                          "pipeline_tag": "text-generation",
                                                          "tags": []}]))
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/hf-latest", "GET")
        result = _run(endpoint())

        assert result["models"]
        assert result["models"][0]["repo_id"] == "org/model-7b"


class _RaisingCtxClient:
    async def __aenter__(self):
        raise RuntimeError(LEAK)

    async def __aexit__(self, *args):
        return False


class _JsonCtxClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", args[0]),
                              json=self._payload)


# ------------------------------------------------- S5c: cookbook scrapes

class TestCookbookOllamaLibrary:
    def test_fetch_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import httpx as httpx_mod

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _RaisingCtxClient())
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/ollama/library", "GET")

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint())

        assert result["error"] == "Failed to fetch the Ollama library"
        assert result["models"]
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_valid_request_falls_back_to_curated_list(self, monkeypatch, tmp_path):
        import httpx as httpx_mod

        class _EmptyHtmlClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return httpx.Response(200, request=httpx.Request("GET", args[0]),
                                      text="")

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _EmptyHtmlClient())
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/ollama/library", "GET")

        result = _run(endpoint())

        # No cards parsed from the empty page; the curated fallback is always
        # merged in, and there is no error label on a successful fetch.
        assert result["models"]
        assert result["error"] is None

    def test_http_error_keeps_status_code_label(self, monkeypatch, tmp_path):
        import httpx as httpx_mod

        class _Http500Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return httpx.Response(500, request=httpx.Request("GET", args[0]))

        monkeypatch.setattr(httpx_mod, "AsyncClient", lambda **kw: _Http500Client())
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/ollama/library", "GET")

        result = _run(endpoint())

        assert result["error"] == "HTTP 500"
        assert result["models"]


class TestCookbookVllmRecipe:
    def test_yaml_parse_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import httpx as httpx_mod

        class _BadYamlClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, *args, **kwargs):
                # A NUL byte inside the LEAK string makes pyYAML's scan error
                # quote the offending content — the old f-string would have
                # serialized it. The new literal must not.
                return httpx.Response(200, request=httpx.Request("GET", args[0]),
                                      text="key: [" + LEAK + "\x00]")

        monkeypatch.setattr(httpx_mod, "Client", lambda **kw: _BadYamlClient())
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/vllm-recipe", "GET")

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(repo="org/some-model"))

        assert result == {"exists": False, "error": "Failed to parse recipe YAML"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=" in caplog.text

    def test_valid_recipe_returns_normalized_payload(self, monkeypatch, tmp_path):
        import httpx as httpx_mod

        class _OkYamlClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, *args, **kwargs):
                return httpx.Response(200, request=httpx.Request("GET", args[0]),
                                      text="meta: {}\nmodel: {}\nfeatures: {}\n")

        monkeypatch.setattr(httpx_mod, "Client", lambda **kw: _OkYamlClient())
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/vllm-recipe", "GET")

        result = _run(endpoint(repo="org/some-model"))

        assert result["exists"] is True
        assert "/recipes/main/models/org/some-model.yaml" in result["source_url"]

    def test_missing_slash_is_validation_error(self, monkeypatch, tmp_path):
        endpoint = _cookbook_endpoint(_cookbook_router(monkeypatch, tmp_path)[1],
                                      "/api/cookbook/vllm-recipe", "GET")
        result = _run(endpoint(repo="no-slash"))
        assert result == {"exists": False, "error": "repo must be <org>/<model>"}


# ------------------------------------------------------- S5c: mcp oauth

class _FakeMcpManager:
    def __init__(self):
        self.connect_result = True
        self.status = {"tool_count": 3}
        self.connected_args = None

    async def connect_server(self, **kwargs):
        self.connected_args = kwargs
        return self.connect_result

    def get_server_status(self, server_id):
        return self.status


class TestMcpOauthCallback:
    def _endpoint(self, monkeypatch, tmp_path):
        import routes.mcp.mcp_routes as mr

        manager = _FakeMcpManager()
        monkeypatch.setattr(mr, "require_admin", lambda request: None)
        # Path.resolve() yields 8.3 short names on Windows; resolve the base up
        # front so the sanitizer's confinement check matches its own resolve().
        monkeypatch.setattr(mr, "_mcp_oauth_base_dir", lambda: tmp_path.resolve())

        class FakeSrv:
            id = "srv-1"
            name = "test-server"
            transport = "stdio"
            command = "npx"
            args = "[]"
            env = "{}"
            url = None
            oauth_config = json.dumps({"keys_file": "keys.json", "token_file": "tokens.json"})

        class _FakeDb:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def first(self):
                return FakeSrv()

            def close(self):
                pass

        monkeypatch.setattr(mr, "SessionLocal", lambda: _FakeDb())

        keys_path = tmp_path / "keys.json"
        keys_path.write_text(json.dumps(
            {"installed": {"client_id": "cid", "client_secret": "csec"}}), encoding="utf-8")

        router = mr.setup_mcp_routes(manager)
        # mcp_routes reuses a module-level APIRouter: every setup call appends
        # to it, so pick the LAST registration (this fixture's closures).
        endpoint = None
        for route in router.routes:
            if route.path == "/api/mcp/oauth/callback":
                endpoint = route.endpoint
        assert endpoint is not None
        return mr, manager, endpoint

    def test_callback_failure_is_fixed(self, monkeypatch, caplog, tmp_path):
        import httpx as httpx_mod

        mr, _manager, endpoint = self._endpoint(monkeypatch, tmp_path)

        class _BoomClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                raise RuntimeError(LEAK)

            async def __aexit__(self, *args):
                return False

        monkeypatch.setattr(httpx_mod, "AsyncClient", _BoomClient)
        monkeypatch.setattr("src.mcp_oauth.resolve_pending",
                            lambda state, code: False)

        class _Req:
            cookies = {}

        with caplog.at_level(logging.WARNING):
            resp = _run(endpoint("code-1", "srv-1", _Req()))

        assert resp.status_code == 500
        assert "The MCP OAuth callback failed" in resp.body.decode()
        assert_no_internal_details(resp.body.decode())
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_callback_valid_exchange_connects_server(self, monkeypatch, tmp_path):
        import httpx as httpx_mod

        _mr, manager, endpoint = self._endpoint(monkeypatch, tmp_path)

        class _OkClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return httpx.Response(200, request=httpx.Request("POST", args[0]),
                                      json={"access_token": "t", "refresh_token": "r"})

        monkeypatch.setattr(httpx_mod, "AsyncClient", _OkClient)
        monkeypatch.setattr("src.mcp_oauth.resolve_pending",
                            lambda state, code: False)

        class _Req:
            cookies = {}

        resp = _run(endpoint("code-1", "srv-1", _Req()))
        assert "test-server connected with 3 tools" in resp.body.decode()
        assert manager.connected_args["server_id"] == "srv-1"

    def test_callback_missing_server_is_404(self, monkeypatch, tmp_path):
        mr, _manager, endpoint = self._endpoint(monkeypatch, tmp_path)

        class _NoServerDb:
            def query(self, *a, **k):
                return self

            def filter(self, *a, **k):
                return self

            def first(self):
                return None

            def close(self):
                pass

        monkeypatch.setattr(mr, "SessionLocal", lambda: _NoServerDb())
        monkeypatch.setattr("src.mcp_oauth.resolve_pending",
                            lambda state, code: False)

        class _Req:
            cookies = {}

        resp = _run(endpoint("code-1", "srv-missing", _Req()))
        assert resp.status_code == 404
        assert_no_internal_details(resp.body.decode())


# ------------------------------------------------- S5c: memory import

async def _read_async(file):
    return file.read()


class TestMemoryAuditSerialization:
    """audit_memories serializes failures into ``result["error"]``, which the
    /audit route re-emits — the source must only ever produce fixed codes."""

    def test_audit_failure_returns_fixed_code(self, monkeypatch, caplog):
        import services.memory.memory_extractor as me
        from src import llm_core as lc

        class _Store:
            def load(self, owner=None):
                return [{"id": "m1", "text": "fact", "category": "fact"}]

        store = _Store()
        monkeypatch.setattr(me, "_load_tidy_state", lambda mm: {})
        monkeypatch.setattr(me, "_save_tidy_state", lambda mm, owner, fp: None)

        async def boom(url, model, messages, **kw):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(lc, "llm_call_async", boom)

        with caplog.at_level(logging.WARNING):
            result = _run(me.audit_memories(store, None, "http://x", "m",
                                            headers={}))

        assert result == {"error": "audit_failed"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text


class TestMemoryImportExtraction:
    def _endpoint(self, monkeypatch):
        import routes.memory.memory_routes as mr

        monkeypatch.setattr(mr, "get_current_user", lambda request: OWNER)
        monkeypatch.setattr("src.auth_helpers.require_privilege",
                            lambda request, priv: None)
        monkeypatch.setattr(mr, "resolve_task_endpoint",
                            lambda *a, owner=None: ("http://model.example/v1", "m", {}))
        monkeypatch.setattr(mr, "resolve_endpoint",
                            lambda kind, owner=None: ("http://model.example/v1", "m", {}))
        monkeypatch.setattr(mr, "read_upload_limited",
                            lambda file, limit, what: _read_async(file))

        router = mr.setup_memory_routes(MagicMock(), MagicMock(), memory_vector=None)
        endpoint = _route_endpoint(router, "/api/memory/import", "POST")
        return mr, endpoint

    def _upload(self, filename, content):
        from io import BytesIO

        class _File:
            def __init__(self):
                self.filename = None

            def read(self):
                return content

        f = _File()
        f.filename = filename
        return f

    def test_llm_failure_is_fixed(self, monkeypatch, caplog):
        async def boom(url, model, messages, **kw):
            raise RuntimeError(LEAK)

        mr, endpoint = self._endpoint(monkeypatch)
        monkeypatch.setattr(mr, "llm_call_async", boom)

        class _Req:
            cookies = {}

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                _run(endpoint(_Req(), session=None,
                              file=self._upload("notes.txt", b"some content")))

        assert exc.value.status_code == 502
        assert exc.value.detail == "Could not extract memories from the uploaded document"
        assert_no_internal_details(str(exc.value.detail))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_valid_import_returns_suggestions(self, monkeypatch):
        async def ok(url, model, messages, **kw):
            return '[{"text": "Alice lives in Berlin", "category": "fact"}]'

        mr, endpoint = self._endpoint(monkeypatch)
        monkeypatch.setattr(mr, "llm_call_async", ok)

        class _Req:
            cookies = {}

        result = _run(endpoint(_Req(), session=None,
                               file=self._upload("notes.txt", b"some content")))

        assert result["filename"] == "notes.txt"
        assert result["suggestions"][0]["text"] == "Alice lives in Berlin"

    def test_unsupported_type_is_validation_error(self, monkeypatch):
        _mr, endpoint = self._endpoint(monkeypatch)

        class _Req:
            cookies = {}

        with pytest.raises(HTTPException) as exc:
            _run(endpoint(_Req(), session=None,
                          file=self._upload("photo.png", b"binary")))
        assert exc.value.status_code == 400
        assert "Unsupported file type" in str(exc.value.detail)


# ------------------------------------------------------- S5c: omnigent

class _FakeOmnigentManager:
    def __init__(self):
        self.start_calls = 0
        self.restart_calls = 0
        self.stop_calls = 0
        self.start_error = None
        self.restart_error = None
        self.stop_error = None

    def status(self):
        return {"status": "stopped"}

    def sessions(self):
        return [{"id": "s-1"}]

    def workers(self):
        return []

    def presets(self):
        return {}

    def worker_roster(self):
        return []

    def start(self, env_extra=None):
        self.start_calls += 1
        if self.start_error:
            raise self.start_error
        return {"status": "running"}

    def restart(self, env_extra=None):
        self.restart_calls += 1
        if self.restart_error:
            raise self.restart_error
        return {"status": "running"}

    def stop(self):
        self.stop_calls += 1
        if self.stop_error:
            raise self.stop_error
        return {"status": "stopped"}


def _omnigent_router_client(monkeypatch):
    import routes.omnigent_routes as og

    monkeypatch.setattr(og, "require_admin", lambda request: None)
    monkeypatch.setattr(og, "get_current_user", lambda request: OWNER)
    monkeypatch.setattr(og, "require_authenticated_request", lambda request: None)
    monkeypatch.setattr(og, "_builtin_agent_env", lambda: {})
    monkeypatch.setattr(og, "_gateway_credentials_env", lambda user: {})

    class _NoDb:
        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def all(self, *a, **k):
            return []

        def first(self, *a, **k):
            return None

        def close(self):
            pass

    monkeypatch.setattr(og, "SessionLocal", lambda: _NoDb())
    return og


def _omnigent_request():
    from starlette.requests import Request

    request = Request({"type": "http", "method": "POST",
                       "path": "/api/omnigent/server/start",
                       "headers": [], "state": {}})
    request.state.current_user = OWNER
    return request


class TestOmnigentServerRoutes:
    def test_start_install_failure_is_fixed(self, monkeypatch, caplog):
        import routes.omnigent_routes as og

        og = _omnigent_router_client(monkeypatch)
        monkeypatch.setattr(og, "_install_api_models", _boom)
        router = og.setup_omnigent_routes(manager=_FakeOmnigentManager(),
                                          native_manager=MagicMock())
        endpoint = _route_endpoint(router, "/api/omnigent/server/start", "POST")

        with caplog.at_level(logging.WARNING):
            result = endpoint(_omnigent_request())

        assert result["api_models"] == {"endpoints": 0, "models": 0,
                                        "error": "Could not install API model endpoints"}
        assert result["status"] == "running"
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_start_manager_failure_is_fixed_500(self, monkeypatch, caplog):
        import routes.omnigent_routes as og

        og = _omnigent_router_client(monkeypatch)
        mgr = _FakeOmnigentManager()
        mgr.start_error = RuntimeError(LEAK)
        router = og.setup_omnigent_routes(manager=mgr, native_manager=MagicMock())
        endpoint = _route_endpoint(router, "/api/omnigent/server/start", "POST")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                endpoint(_omnigent_request())

        assert exc.value.status_code == 500
        assert exc.value.detail == "Could not start the Omnigent server"
        assert_no_internal_details(str(exc.value.detail))
        assert_no_internal_details(caplog.text)

    def test_stop_manager_failure_is_fixed_500(self, monkeypatch, caplog):
        import routes.omnigent_routes as og

        og = _omnigent_router_client(monkeypatch)
        mgr = _FakeOmnigentManager()
        mgr.stop_error = RuntimeError(LEAK)
        router = og.setup_omnigent_routes(manager=mgr, native_manager=MagicMock())
        endpoint = _route_endpoint(router, "/api/omnigent/server/stop", "POST")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc:
                endpoint(_omnigent_request())

        assert exc.value.status_code == 500
        assert exc.value.detail == "Could not stop the Omnigent server"
        assert_no_internal_details(str(exc.value.detail))
        assert_no_internal_details(caplog.text)

    def test_start_valid_request_runs(self, monkeypatch):
        import routes.omnigent_routes as og

        og = _omnigent_router_client(monkeypatch)
        router = og.setup_omnigent_routes(manager=_FakeOmnigentManager(),
                                          native_manager=MagicMock())
        endpoint = _route_endpoint(router, "/api/omnigent/server/start", "POST")

        result = endpoint(_omnigent_request())
        assert result["status"] == "running"
        assert result["api_models"]["endpoints"] == 0

    def test_launch_restart_failure_keeps_status_envelope(self, monkeypatch, caplog):
        import routes.omnigent_routes as og

        og = _omnigent_router_client(monkeypatch)
        mgr = _FakeOmnigentManager()
        mgr.restart_error = RuntimeError(LEAK)
        router = og.setup_omnigent_routes(manager=mgr, native_manager=MagicMock())
        endpoint = _route_endpoint(router, "/api/omnigent/launch", "POST")

        with caplog.at_level(logging.WARNING):
            result = endpoint(_omnigent_request())

        assert result["status"] == "stopped"
        assert result["error"] == "Could not restart the Omnigent server"
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_sessions_passthrough_is_preserved(self, monkeypatch):
        import routes.omnigent_routes as og

        og = _omnigent_router_client(monkeypatch)
        router = og.setup_omnigent_routes(manager=_FakeOmnigentManager(),
                                          native_manager=MagicMock())
        endpoint = _route_endpoint(router, "/api/omnigent/sessions", "GET")

        result = endpoint(None)
        assert result == [{"id": "s-1"}]


# ------------------------------------------------------- S5c: preset expand

class TestPresetExpand:
    def _endpoint(self, monkeypatch):
        import routes.preset_routes as pr

        monkeypatch.setattr(pr, "effective_user", lambda request: OWNER)
        router = pr.setup_preset_routes(MagicMock())
        return _route_endpoint(router, "/api/presets/expand", "POST")

    def test_expansion_failure_is_fixed(self, monkeypatch, caplog):
        import src.ai_interaction as ai

        def boom(spec, owner=None):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(ai, "_resolve_model", boom)
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"name": "Pirate", "prompt": "rough notes"}

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_Req()))

        assert result == {"success": False, "message": "Could not expand the prompt"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_http_exception_detail_stays_controlled(self, monkeypatch):
        import src.ai_interaction as ai
        from src import llm_core as lc

        monkeypatch.setattr(ai, "_resolve_model",
                            lambda spec, owner=None: ("http://x", "m", {}))

        async def denied(url, model, messages, **kw):
            raise HTTPException(401, "The endpoint rejected the API key. Re-paste it.")

        monkeypatch.setattr(lc, "llm_call_async", denied)
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"name": "Pirate"}

        result = _run(endpoint(_Req()))
        assert result["success"] is False
        assert "rejected the API key" in result["message"]
        assert_no_internal_details(json.dumps(result))

    def test_valid_expansion_returns_prompt(self, monkeypatch):
        import src.ai_interaction as ai
        from src import llm_core as lc

        monkeypatch.setattr(ai, "_resolve_model",
                            lambda spec, owner=None: ("http://x", "m", {}))

        async def ok(url, model, messages, **kw):
            return "  expanded prompt  "

        monkeypatch.setattr(lc, "llm_call_async", ok)
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"name": "Pirate"}

        result = _run(endpoint(_Req()))
        assert result == {"success": True, "prompt": "expanded prompt"}

    def test_empty_input_is_validation_error(self, monkeypatch):
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"name": "", "prompt": ""}

        result = _run(endpoint(_Req()))
        assert result == {"success": False, "message": "Nothing to expand"}


# ------------------------------------------------- S5c: search routes

class TestSearchRoutes:
    def _router(self):
        import routes.search.search_routes as sr

        return sr.setup_search_routes(MagicMock())

    class _Req:
        headers = {}
        query_params = {}

        def __init__(self, body=None):
            self._body = body or {}

        async def json(self):
            return self._body

        async def form(self):
            return self._body

    def test_web_search_failure_is_fixed(self, monkeypatch, caplog):
        import routes.search.search_routes as sr

        router = self._router()
        monkeypatch.setattr(sr, "comprehensive_web_search", _boom)
        endpoint = _route_endpoint(router, "/api/search", "POST")

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(self._Req({"query": "news"})))

        assert result == {"context": "", "sources": [],
                          "error": "The web search failed. Try again."}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_web_search_valid_request_returns_context(self, monkeypatch):
        import routes.search.search_routes as sr

        router = self._router()
        monkeypatch.setattr(sr, "comprehensive_web_search",
                            lambda query, return_sources=False, time_filter=None:
                            ("context text", [{"title": "T", "url": "https://e"}]))

        endpoint = _route_endpoint(router, "/api/search", "POST")

        result = _run(endpoint(self._Req({"query": "news"})))
        assert result["context"] == "context text"
        assert result["sources"][0]["title"] == "T"

    def test_web_search_missing_query_is_validation_error(self, monkeypatch):
        router = self._router()
        endpoint = _route_endpoint(router, "/api/search", "POST")

        result = _run(endpoint(self._Req({"query": "  "})))
        assert result["error"] == "query is required"

    def test_provider_failure_is_fixed(self, monkeypatch, caplog):
        import routes.search.search_routes as sr

        router = self._router()
        monkeypatch.setattr(sr, "_call_provider", _boom)
        endpoint = _route_endpoint(router, "/api/search/query", "POST")

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(self._Req({"query": "news", "provider": "searxng"})))

        assert result["error"] == "The search request failed. Try again."
        assert result["provider"] == "searxng"
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)

    def test_unknown_provider_is_validation_error(self, monkeypatch):
        router = self._router()
        endpoint = _route_endpoint(router, "/api/search/query", "POST")

        result = _run(endpoint(self._Req({"query": "news", "provider": "nope"})))
        assert result["error"] == "Unknown provider"


# ------------------------------------------------- S5c: skills builtin

class TestSkillsBuiltin:
    def test_import_failure_is_fixed(self, monkeypatch, caplog):
        import builtins as _builtins
        import routes.skills_routes as skr

        router = skr.setup_skills_routes(MagicMock())
        endpoint = _route_endpoint(router, "/api/skills/builtin", "GET")
        real_import = _builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "src.agent_loop":
                raise RuntimeError(LEAK)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(_builtins, "__import__", guarded_import)

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(None))

        assert result == {"builtin": [], "count": 0,
                          "error": "Could not list built-in skills"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_valid_list_runs(self, monkeypatch):
        import routes.skills_routes as skr

        router = skr.setup_skills_routes(MagicMock())
        endpoint = _route_endpoint(router, "/api/skills/builtin", "GET")

        result = _run(endpoint(None))
        assert result["count"] == len(result["builtin"])


# ------------------------------------------------- S5c: task parse

class TestTaskParse:
    def _endpoint(self, monkeypatch):
        import routes.task.task_routes as tr

        monkeypatch.setattr(tr, "get_current_user", lambda request: OWNER)
        router = tr.setup_task_routes(MagicMock())
        return _route_endpoint(router, "/api/tasks/parse", "POST")

    def test_parse_failure_is_fixed(self, monkeypatch, caplog):
        from src import llm_core as lc
        from src import endpoint_resolver as er

        monkeypatch.setattr(er, "resolve_endpoint",
                            lambda kind, owner=None: ("http://x", "m", {}))

        async def boom(url, model, messages, **kw):
            raise RuntimeError(LEAK)

        monkeypatch.setattr(lc, "llm_call_async", boom)
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"description": "daily digest at 7am"}

        with caplog.at_level(logging.WARNING):
            result = _run(endpoint(_Req()))

        assert result == {"success": False, "message": "Could not parse the task. Try again."}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_valid_parse_returns_draft(self, monkeypatch):
        from src import llm_core as lc
        from src import endpoint_resolver as er

        monkeypatch.setattr(er, "resolve_endpoint",
                            lambda kind, owner=None: ("http://x", "m", {}))

        async def ok(url, model, messages, **kw):
            return '{"name": "Digest", "prompt": "Summarize today" , "task_type": "research"}'

        monkeypatch.setattr(lc, "llm_call_async", ok)
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"description": "daily digest"}

        result = _run(endpoint(_Req()))
        assert result["success"] is True
        assert result["draft"]["name"] == "Digest"

    def test_empty_description_is_validation_error(self, monkeypatch):
        endpoint = self._endpoint(monkeypatch)

        class _Req:
            async def json(self):
                return {"description": "  "}

        result = _run(endpoint(_Req()))
        assert result == {"success": False, "message": "Nothing to parse"}


# ------------------------------------------------- S5c: gallery exif

class TestGalleryExif:
    def test_exif_failure_is_fixed_label(self, monkeypatch, caplog):
        import sys as _sys
        import types as _types
        from routes.gallery.gallery_helpers import _extract_exif

        pil_stub = _types.ModuleType("PIL")

        class _ImageStub:
            @staticmethod
            def open(*args, **kwargs):
                raise RuntimeError(LEAK)

        pil_stub.Image = _ImageStub()
        monkeypatch.setitem(_sys.modules, "PIL", pil_stub)

        with caplog.at_level(logging.WARNING):
            result = _extract_exif(b"not really an image")

        assert result == {"width": None, "height": None,
                          "exif_error": "Could not read image metadata"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_exif_valid_image_returns_dimensions(self, monkeypatch):
        import sys as _sys
        import types as _types
        from routes.gallery.gallery_helpers import _extract_exif

        pil_stub = _types.ModuleType("PIL")
        pil_stub.Image = _types.SimpleNamespace(
            open=lambda *a, **k: _FakeImg(640, 480, None))
        monkeypatch.setitem(_sys.modules, "PIL", pil_stub)

        result = _extract_exif(b"png-bytes")

        assert result["width"] == 640
        assert result["height"] == 480
        assert "exif_error" not in result


class _FakeImg:
    def __init__(self, width, height, exif):
        self.width = width
        self.height = height
        self._exif = exif

    def _getexif(self):
        return self._exif


# ------------------------------------------------- S5c: hwfit models

class TestHwfitModels:
    def test_catalog_refresh_failure_is_fixed(self, monkeypatch, caplog):
        import services.hwfit.models as hm
        import services.hwfit.fit as hf
        import services.hwfit.hardware as hh

        import routes.hwfit_routes as hr

        monkeypatch.setattr(hh, "detect_system",
                            lambda host=None, ssh_port=None, platform="", fresh=False:
                            {"has_gpu": False, "gpu_count": 0, "gpu_vram_gb": 0,
                             "gpus": [], "gpu_groups": [], "gpu_name": None,
                             "backend": "cpu_x86", "available_ram_gb": 16,
                             "total_ram_gb": 16, "cpu_name": "test"})
        monkeypatch.setattr(hm, "get_models", lambda: [{"name": "m1"}])
        monkeypatch.setattr(hm, "refresh_dynamic_catalogs", _boom)
        monkeypatch.setattr(hf, "rank_models", lambda system, **kw: [])

        router = hr.setup_hwfit_routes()
        endpoint = _route_endpoint(router, "/api/hwfit/models", "GET")

        with caplog.at_level(logging.WARNING):
            result = endpoint(refresh_catalog=True)

        assert result["models"] == []
        assert result["catalog_refresh"] == {"error": "Failed to refresh dynamic catalogs"}
        assert_no_internal_details(json.dumps(result))
        assert_no_internal_details(caplog.text)
        assert "error_type=RuntimeError" in caplog.text

    def test_valid_request_ranks_models(self, monkeypatch):
        import services.hwfit.models as hm
        import services.hwfit.fit as hf
        import services.hwfit.hardware as hh

        import routes.hwfit_routes as hr

        monkeypatch.setattr(hh, "detect_system",
                            lambda host=None, ssh_port=None, platform="", fresh=False:
                            {"has_gpu": False, "gpu_count": 0, "gpu_vram_gb": 0,
                             "gpus": [], "gpu_groups": [], "gpu_name": None,
                             "backend": "cpu_x86", "available_ram_gb": 16,
                             "total_ram_gb": 16, "cpu_name": "test"})
        monkeypatch.setattr(hm, "get_models", lambda: [{"name": "m1"}])
        monkeypatch.setattr(hf, "rank_models", lambda system, **kw: [{"name": "m1"}])

        router = hr.setup_hwfit_routes()
        endpoint = _route_endpoint(router, "/api/hwfit/models", "GET")

        result = endpoint()
        assert result["models"][0]["name"] == "m1"

    def test_ssh_port_without_host_is_validation_error(self, monkeypatch):
        from fastapi import HTTPException

        import routes.hwfit_routes as hr

        router = hr.setup_hwfit_routes()
        endpoint = _route_endpoint(router, "/api/hwfit/models", "GET")

        with pytest.raises(HTTPException) as exc:
            endpoint(host="", ssh_port="22")
        assert exc.value.status_code == 400
        assert "ssh_port requires host" in str(exc.value.detail)