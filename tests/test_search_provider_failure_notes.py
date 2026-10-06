"""Search providers return [] on failure so the fallback chain can move on.
They also note why they failed, so deep research can say "SearXNG refused the
connection" instead of telling the user to rephrase the question."""
import sys

import httpx

from services.search import core, providers


def _reset():
    providers._LAST_FAILURE.clear()


def test_searxng_connection_refused_is_noted(monkeypatch):
    _reset()

    def _refuse(*a, **k):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(providers.httpx, "get", _refuse)
    monkeypatch.setattr(providers, "_get_search_instance", lambda: "http://localhost:8080")

    assert providers.searxng_search_api("krebs cycle", 5) == []
    why = providers.last_failure("searxng")
    assert "Connection refused" in why
    assert "localhost:8080" in why


def test_duckduckgo_http_error_is_noted(monkeypatch):
    _reset()
    monkeypatch.setitem(sys.modules, "ddgs", None)  # force the HTML path

    def _forbidden(url, *a, **k):
        return httpx.Response(403, request=httpx.Request("GET", url))

    monkeypatch.setattr(providers.httpx, "get", _forbidden)

    assert providers.duckduckgo_search("krebs cycle", 5) == []
    assert providers.last_failure("duckduckgo") == "HTTP 403 Forbidden"


def test_missing_key_is_noted(monkeypatch):
    _reset()
    monkeypatch.setattr(providers, "_get_provider_key", lambda p: "")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    assert providers.tavily_search("krebs cycle", 5) == []
    assert providers.last_failure("tavily") == "no API key set"


def test_old_failures_are_ignored_and_success_clears():
    _reset()
    providers._note_failure("brave", "rate limited")
    noted_at = providers._LAST_FAILURE["brave"][0]
    assert providers.last_failure("brave", since=noted_at + 1) is None
    assert providers.last_failure("brave", since=noted_at) == "rate limited"


def test_call_provider_clears_note_on_results(monkeypatch):
    _reset()
    providers._note_failure("brave", "rate limited")
    monkeypatch.setattr(core, "brave_search", lambda q, n, t=None: [{"url": "https://a"}])

    assert core._call_provider("brave", "q", 5) == [{"url": "https://a"}]
    assert providers.last_failure("brave") is None


def test_failure_notes_never_carry_credentials():
    _reset()
    providers._note_failure("searxng", "Connection refused (http://admin:hunter2@search.lan:8080)")
    providers._note_failure("google_pse", "bad URL https://www.googleapis.com/customsearch/v1?key=AIzaSECRET&cx=1")
    assert providers.last_failure("searxng") == "Connection refused (http://***@search.lan:8080)"
    assert "AIzaSECRET" not in providers.last_failure("google_pse")
    assert "key=***" in providers.last_failure("google_pse")
