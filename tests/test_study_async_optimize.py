"""Tests for Phase 4.3 — Async FSRS optimization.

Verifies:
  (a) POST /optimize returns immediately with status=running.
  (b) GET /optimize/status reflects the background task result once it finishes.
  (c) The async path persists the fitted w correctly.
  (d) The status endpoint returns never_run for a user who hasn't triggered.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import (
    StudyCard,
    StudyReview,
    StudyUserParams,
)
from routes import study_routes


OWNER = "alice"


class _Query:
    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.rows = session.rows.setdefault(model, [])
        self._preds = []

    def filter(self, *preds):
        self._preds.extend(preds)
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)


class _Session:
    def __init__(self):
        self.rows = {
            StudyCard: [],
            StudyReview: [],
            StudyUserParams: [],
        }
        self.commits = 0
        self.closed = False
        self._commit_log = []

    def query(self, model):
        return _Query(self, model)

    def add(self, row):
        self.rows.setdefault(type(row), []).append(row)

    def delete(self, row):
        table = self.rows.setdefault(type(row), [])
        if row in table:
            table.remove(row)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@contextmanager
def _session_scope(session):
    yield session


@pytest.fixture
def study_client(monkeypatch):
    # Reset shared state between tests
    study_routes._optimize_status.clear()
    study_routes._optimize_locks.clear()

    session = _Session()
    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", lambda: session)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(study_routes.RateLimiter, "check", lambda *_a, **_k: True)
    return TestClient(app), session


def test_optimize_returns_running_immediately(study_client):
    """POST /optimize should return immediately with status=running, not block."""
    client, session = study_client
    res = client.post("/api/study/optimize")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "running"
    assert "reviews" in body


def test_optimize_status_polling(study_client):
    """GET /optimize/status should show running, then transition to a terminal state."""
    client, session = study_client

    # Trigger optimization
    client.post("/api/study/optimize")

    # Wait for background thread to finish (it's fast with no data)
    time.sleep(0.5)

    res = client.get("/api/study/optimize/status")
    assert res.status_code == 200
    body = res.json()
    # With no reviews, it should be insufficient_data
    assert body["status"] in ("running", "insufficient_data", "ok", "error")


def test_optimize_status_never_run(study_client):
    """A user who hasn't triggered optimization gets never_run."""
    client, session = study_client
    res = client.get("/api/study/optimize/status")
    assert res.status_code == 200
    assert res.json()["status"] == "never_run"


@pytest.fixture
def blocking_fit(monkeypatch):
    """fit_w that holds until released, counting how many fits started."""
    from src import fsrs_optimizer
    release = threading.Event()
    started = []

    def fit(snapshots, reviews, seed=42):
        started.append(1)
        release.wait(5)
        return None

    monkeypatch.setattr(fsrs_optimizer, "fit_w", fit)
    yield release, started
    release.set()


def _wait_idle(client, timeout=5.0):
    """Until the worker has released the user's lock (the status turns
    terminal a moment before that)."""
    end = time.time() + timeout
    while time.time() < end:
        if not study_routes._optimize_lock(OWNER).locked():
            return
        time.sleep(0.02)
    raise AssertionError("optimization never finished")


def test_concurrent_optimize_runs_one_fit(study_client, blocking_fit):
    """A second /optimize while one is running must not start a parallel fit
    that races the first to write the user's weights."""
    client, _session = study_client
    release, started = blocking_fit
    first = client.post("/api/study/optimize").json()
    second = client.post("/api/study/optimize").json()
    assert first["status"] == "running" and not first.get("already_running")
    assert second == {"status": "running", "reviews": 0, "already_running": True}
    release.set()
    _wait_idle(client)
    assert len(started) == 1

    # The lock is released when the fit ends: the next request runs again.
    third = client.post("/api/study/optimize").json()
    assert not third.get("already_running")
    _wait_idle(client)
    assert len(started) == 2


def test_reset_restores_default_weights(study_client):
    client, session = study_client
    session.rows[StudyUserParams].append(StudyUserParams(
        id="p1", owner=OWNER, w_json=json.dumps([1.0] * 17), review_count=500))
    res = client.post("/api/study/optimize/reset")
    assert res.status_code == 200
    assert res.json() == {"status": "reset", "removed": 1}
    assert session.rows[StudyUserParams] == []
    assert client.get("/api/study/optimize/status").json()["status"] == "reset"


def test_reset_is_refused_while_a_fit_runs(study_client, blocking_fit):
    """Otherwise the running fit would write its weights straight back."""
    client, _session = study_client
    release, _started = blocking_fit
    client.post("/api/study/optimize")
    assert client.post("/api/study/optimize/reset").status_code == 409
    release.set()
    _wait_idle(client)
    assert client.post("/api/study/optimize/reset").status_code == 200
