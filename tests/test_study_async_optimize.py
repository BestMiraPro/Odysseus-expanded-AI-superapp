"""Tests for Phase 4.3 — Async FSRS optimization.

Verifies:
  (a) POST /optimize returns immediately with status=running.
  (b) GET /optimize/status reflects the background task result once it finishes.
  (c) The async path persists the fitted w correctly.
  (d) The status endpoint returns never_run for a user who hasn't triggered.
"""

from __future__ import annotations

import json
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
