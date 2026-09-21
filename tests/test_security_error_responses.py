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