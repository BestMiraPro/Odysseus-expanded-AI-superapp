"""Regression tests for deep-research search error reporting (issue #344).

When every configured search provider returns no results *without raising*
(e.g. SearXNG is reachable but all of its engines fail), ``_search`` used to
leave ``_last_search_error`` unset. The caller then surfaced a useless
"Search unavailable ... Error: unknown error" message, which is what the
reporter in #344 was confused by ("is this a model issue or deep research
issue?").

These tests pin that the empty-but-no-exception path now records an
actionable reason, while the existing raise path keeps surfacing the
provider's own error.
"""
import asyncio
import sys
import types


def _make_researcher():
    # Build the object without running the heavy __init__ (which wires up an
    # LLM caller etc.); _search only touches the attributes set below.
    from src.deep_research import DeepResearcher
    r = DeepResearcher.__new__(DeepResearcher)
    r.search_provider_override = None
    r.providers_used = []
    return r


def _install_search_fakes(monkeypatch, *, chain, call_provider, failures=None):
    providers_mod = types.ModuleType("src.search.providers")
    providers_mod._get_search_settings = lambda: {"search_provider": chain[0]}
    providers_mod.last_failure = lambda prov, since=0.0: (failures or {}).get(prov)
    from services.search.providers import describe_failure
    providers_mod.describe_failure = describe_failure
    core_mod = types.ModuleType("src.search.core")
    core_mod._build_provider_chain = lambda provider: list(chain)
    core_mod._call_provider = call_provider
    monkeypatch.setitem(sys.modules, "src.search.providers", providers_mod)
    monkeypatch.setitem(sys.modules, "src.search.core", core_mod)


def test_empty_results_without_exception_record_reason(monkeypatch):
    # Both providers are reachable but return nothing, and neither raises.
    _install_search_fakes(
        monkeypatch,
        chain=["searxng", "duckduckgo"],
        call_provider=lambda prov, query, n: [],
    )
    r = _make_researcher()
    results = asyncio.run(r._search("anything"))

    assert results == []
    # Before the fix this stayed unset, so the caller reported "unknown error".
    err = getattr(r, "_last_search_error", None)
    assert err, "an empty search must record a reason, not leave it unset"
    assert "no results" in err
    # Names the provider(s) that were actually tried, so the message is useful.
    assert "searxng" in err


def test_provider_exception_is_still_surfaced(monkeypatch):
    # A provider that raises must keep surfacing its own error unchanged.
    def _boom(prov, query, n):
        raise RuntimeError("connection refused")

    _install_search_fakes(monkeypatch, chain=["searxng"], call_provider=_boom)
    r = _make_researcher()
    results = asyncio.run(r._search("anything"))

    assert results == []
    err = getattr(r, "_last_search_error", None)
    assert err and "connection refused" in err
    # The raise path, not the empty-results path.
    assert "no results" not in err


def test_results_are_returned_and_provider_recorded(monkeypatch):
    # Sanity: a provider with results returns them and is recorded.
    hits = [{"url": "https://example.com", "title": "x"}]
    _install_search_fakes(
        monkeypatch, chain=["brave"], call_provider=lambda p, q, n: hits
    )
    r = _make_researcher()
    results = asyncio.run(r._search("anything"))

    assert results == hits
    assert r.providers_used == ["brave"]


def test_swallowed_provider_failures_are_named(monkeypatch):
    # Real providers catch their own errors and return []. The reasons they
    # noted must reach the report instead of "no results" (which reads as
    # "rephrase the question").
    _install_search_fakes(
        monkeypatch,
        chain=["searxng", "duckduckgo"],
        call_provider=lambda prov, query, n: [],
        failures={"searxng": "[Errno 111] Connection refused (http://localhost:8080)",
                  "duckduckgo": "HTTP 403 Forbidden"},
    )
    r = _make_researcher()
    assert asyncio.run(r._search("anything")) == []

    assert "Connection refused" in r._last_search_error
    assert "duckduckgo: HTTP 403 Forbidden" in r._last_search_error
    assert "no results" not in r._last_search_error
    assert r.search_failure == r._last_search_error


def _full_researcher(monkeypatch, *, search_failure):
    from src.deep_research import DeepResearcher
    r = DeepResearcher(llm_endpoint="http://x/v1/chat/completions", llm_model="m",
                       max_rounds=1, min_rounds=1)

    async def _plan(q):
        return "plan"

    async def _category(q):
        return None

    async def _queries(q, report, n):
        return ["q1", "q2"]

    async def _search_and_extract(queries, q):
        r.search_failure = search_failure
        return []

    async def _stop(*a, **k):
        return False

    monkeypatch.setattr(r, "_create_plan", _plan)
    monkeypatch.setattr(r, "_classify_category", _category)
    monkeypatch.setattr(r, "_generate_queries", _queries)
    monkeypatch.setattr(r, "_search_and_extract", _search_and_extract)
    monkeypatch.setattr(r, "_should_stop", _stop)
    return r


def test_round_limit_with_failed_search_reports_the_failure(monkeypatch):
    # One round (below max_empty_rounds), every search failed: the report and
    # stats must name the failure, not claim nothing could be gathered.
    r = _full_researcher(monkeypatch, search_failure="searxng: Connection refused")
    out = asyncio.run(r.research("What is the Krebs cycle?"))

    assert out.startswith("**Search unavailable**")
    assert "searxng: Connection refused" in out
    assert r.get_stats()["Search error"] == "searxng: Connection refused"


def test_round_limit_with_working_search_keeps_no_information(monkeypatch):
    r = _full_researcher(monkeypatch, search_failure="")
    out = asyncio.run(r.research("What is the Krebs cycle?"))

    assert out == "No information could be gathered for this question."
    assert "Search error" not in r.get_stats()


def test_raised_http_error_does_not_leak_the_request_url(monkeypatch):
    # Google PSE carries its API key in the query string; an HTTP error's
    # message includes that URL. Only the status may reach the report/UI.
    import httpx

    url = "https://www.googleapis.com/customsearch/v1?key=AIzaSECRET&cx=1&q=x"

    def _forbidden(prov, query, n):
        resp = httpx.Response(403, request=httpx.Request("GET", url))
        resp.raise_for_status()

    _install_search_fakes(monkeypatch, chain=["google_pse"], call_provider=_forbidden)
    r = _make_researcher()
    assert asyncio.run(r._search("anything")) == []
    assert r.search_failure == "google_pse: HTTP 403 Forbidden"
    assert "AIzaSECRET" not in r._last_search_error
