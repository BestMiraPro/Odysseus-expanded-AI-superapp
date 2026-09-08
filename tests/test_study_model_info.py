"""Study must be able to say which model its AI passes will use.

The model is not hardcoded — _resolve_study_model falls back study -> utility
-> default — but nothing in the UI ever reported which tier answered, so a
subject-view AI pass looked like it ran on a fixed model the user could not
choose. This endpoint makes the resolution visible, including *which* tier it
came from, so "Same as chat" can stop being a guess.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import study_routes


OWNER = "alice"


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "get_current_user", lambda _r: OWNER)
    monkeypatch.setattr(study_routes.RateLimiter, "check", lambda *_a, **_k: True)
    return TestClient(app)


def _resolver(mapping):
    """Stand in for endpoint_resolver.resolve_endpoint."""
    def resolve(kind, owner=None):
        return mapping.get(kind, (None, None, {}))
    return resolve


def _patch(monkeypatch, mapping, text_model=None):
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_endpoint", _resolver(mapping))
    monkeypatch.setattr(study_routes, "_study_text_model", lambda owner: text_model)


def test_reports_the_study_model_when_one_is_configured(monkeypatch, client):
    _patch(monkeypatch, {"study": ("http://ep/v1", "qwen-72b", {})})

    body = client.get("/api/study/model").json()

    assert body["configured"] is True
    assert body["model"] == "qwen-72b"
    assert body["source"] == "study"


def test_reports_the_utility_fallback(monkeypatch, client):
    """'Same as chat' actually means the utility model — say so."""
    _patch(monkeypatch, {"utility": ("http://ep/v1", "small-fast", {})})

    body = client.get("/api/study/model").json()

    assert body["configured"] is True
    assert body["model"] == "small-fast"
    assert body["source"] == "utility"


def test_reports_the_default_fallback(monkeypatch, client):
    _patch(monkeypatch, {"default": ("http://ep/v1", "big-default", {})})

    body = client.get("/api/study/model").json()

    assert body["model"] == "big-default"
    assert body["source"] == "default"


def test_prefers_study_over_the_fallbacks(monkeypatch, client):
    _patch(monkeypatch, {
        "study": ("http://ep/v1", "chosen", {}),
        "utility": ("http://ep/v1", "not-this", {}),
        "default": ("http://ep/v1", "nor-this", {}),
    })
    assert client.get("/api/study/model").json()["model"] == "chosen"


def test_reports_the_text_model_when_one_is_set(monkeypatch, client):
    """A vision model can stay selected while text calls use a cheaper one."""
    _patch(monkeypatch, {"study": ("http://ep/v1", "vision-model", {})},
           text_model="text-only-model")

    body = client.get("/api/study/model").json()

    assert body["model"] == "vision-model"
    assert body["text_model"] == "text-only-model"


def test_says_so_when_nothing_is_configured(monkeypatch, client):
    """No model must be a plain answer, not a 503 the panel cannot render."""
    _patch(monkeypatch, {})

    res = client.get("/api/study/model")

    assert res.status_code == 200
    body = res.json()
    assert body["configured"] is False
    assert body["model"] is None
    assert body["source"] is None


def test_never_leaks_the_endpoint_url_or_headers(monkeypatch, client):
    """Headers carry API keys; the URL is infrastructure. Neither belongs here."""
    _patch(monkeypatch, {"study": ("http://secret-host/v1", "m", {"Authorization": "Bearer sk-x"})})

    body = client.get("/api/study/model").json()

    flat = str(body)
    assert "secret-host" not in flat
    assert "sk-x" not in flat
    assert "Authorization" not in flat
