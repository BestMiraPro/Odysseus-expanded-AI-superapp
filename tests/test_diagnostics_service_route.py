"""Route-level regression tests for GET /api/diagnostics/services.

The reviewer asked for explicit coverage of unauthenticated / non-admin / admin
access to this admin diagnostics route, beyond the unit tests for the collector.

These need a real FastAPI + TestClient (the conftest only stubs FastAPI when it
is *not* installed). When the full app deps aren't present we skip rather than
fail, so the suite stays green in minimal environments; CI installs
requirements, so the tests run there.
"""
import logging

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from fastapi import FastAPI, HTTPException, Request
from starlette.testclient import TestClient

# Importing the route module pulls a few app deps; skip cleanly if unavailable.
diag = pytest.importorskip("routes.diagnostics_routes")


def _client_with_admin_gate(monkeypatch, gate):
    """Mount the diagnostics router with `require_admin` and the collector
    patched (via monkeypatch so the module globals are restored afterwards),
    and return a TestClient. `gate` plays the role of require_admin."""
    import src.service_health as sh

    async def _fake_collect(_rag, _mem):
        return {"overall": "ok", "services": [], "timestamp": "t"}

    # monkeypatch.setattr restores these after the test — a plain assignment
    # would leak the fakes into every later test in the session.
    monkeypatch.setattr(diag, "require_admin", gate)
    monkeypatch.setattr(sh, "collect_service_health", _fake_collect)

    app = FastAPI()
    app.include_router(diag.setup_diagnostics_routes(
        rag_manager=None, rag_available=False, research_handler=None,
        memory_vector=None))
    return TestClient(app, raise_server_exceptions=False)


def test_unauthenticated_is_rejected(monkeypatch):
    def gate(_request: Request):
        raise HTTPException(401, "Not authenticated")
    client = _client_with_admin_gate(monkeypatch, gate)
    r = client.get("/api/diagnostics/services")
    assert r.status_code == 401


def test_non_admin_is_forbidden(monkeypatch):
    def gate(_request: Request):
        raise HTTPException(403, "Admin only")
    client = _client_with_admin_gate(monkeypatch, gate)
    r = client.get("/api/diagnostics/services")
    assert r.status_code == 403


def test_admin_gets_report(monkeypatch):
    def gate(_request: Request):
        return None  # admin allowed
    client = _client_with_admin_gate(monkeypatch, gate)
    r = client.get("/api/diagnostics/services")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"overall", "services", "timestamp"}
    assert body["overall"] == "ok"


# -- S5c: /api/test/youtube and /api/test-research must not echo exception text

_LEAK = "private-marker /srv/private/auth.json postgresql://u:secret-marker@db/app"


def _assert_no_internal(serialized):
    for marker in ("private-marker", "/srv/private/auth.json", "secret-marker"):
        assert marker not in serialized


def _client(monkeypatch, research_handler):
    monkeypatch.setattr(diag, "require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(diag.setup_diagnostics_routes(
        rag_manager=None, rag_available=False,
        research_handler=research_handler, memory_vector=None))
    return TestClient(app, raise_server_exceptions=False)


class _ResearchHandler:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = 0

    async def call_research_service(self, query, endpoint, model):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def test_youtube_transcript_failure_hides_exception(monkeypatch, caplog):
    async def boom(url, video_id, max_retries=3):
        raise RuntimeError(_LEAK)

    monkeypatch.setattr(diag, "extract_transcript_async", boom)
    client = _client(monkeypatch, _ResearchHandler())

    with caplog.at_level(logging.WARNING):
        r = client.get("/api/test/youtube", params={"url": "https://youtu.be/abc123"})

    assert r.status_code == 200
    assert r.json() == {"error": "Could not test YouTube transcript extraction"}
    _assert_no_internal(r.text)
    _assert_no_internal(caplog.text)
    assert "error_type=RuntimeError" in caplog.text


def test_youtube_valid_request_returns_transcript(monkeypatch):
    async def ok(url, video_id, max_retries=3):
        return {"success": True, "transcript": "hello world"}

    monkeypatch.setattr(diag, "extract_transcript_async", ok)
    client = _client(monkeypatch, _ResearchHandler())

    r = client.get("/api/test/youtube", params={"url": "https://youtu.be/abc123"})
    assert r.status_code == 200
    body = r.json()
    assert body["video_id"] == "abc123"
    assert body["transcript_success"] is True
    assert body["transcript_preview"] == "hello world"


def test_youtube_invalid_url_is_validation_error(monkeypatch):
    client = _client(monkeypatch, _ResearchHandler())

    r = client.get("/api/test/youtube", params={"url": "https://example.com/page"})
    assert r.status_code == 200
    assert r.json() == {"error": "Invalid YouTube URL"}


def test_research_failure_hides_exception(monkeypatch, caplog):
    client = _client(monkeypatch, _ResearchHandler(error=RuntimeError(_LEAK)))

    with caplog.at_level(logging.WARNING):
        r = client.post("/api/test-research", data={"query": "quantum cats"})

    assert r.status_code == 200
    assert r.json() == {"status": "error",
                        "error": "The research service test failed",
                        "query": "quantum cats"}
    _assert_no_internal(r.text)
    _assert_no_internal(caplog.text)
    assert "error_type=RuntimeError" in caplog.text


def test_research_valid_request_returns_preview(monkeypatch):
    client = _client(monkeypatch, _ResearchHandler(result="RESULT TEXT"))
    r = client.post("/api/test-research", data={"query": "quantum cats"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert body["result_preview"] == "RESULT TEXT"
    assert body["result_length"] == 11


def test_research_admin_gate_is_validation_error(monkeypatch):
    monkeypatch.setattr(diag, "require_admin",
                        lambda request: (_ for _ in ()).throw(HTTPException(403, "Admin only")))
    app = FastAPI()
    app.include_router(diag.setup_diagnostics_routes(
        rag_manager=None, rag_available=False,
        research_handler=_ResearchHandler(), memory_vector=None))
    client = TestClient(app, raise_server_exceptions=False)

    r = client.post("/api/test-research", data={"query": "x"})
    assert r.status_code == 403
    _assert_no_internal(r.text)
