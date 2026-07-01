"""Rate-limit tests for the 11 study AI endpoints.

Assertions:
  - After the configured limit is exceeded, the next request returns 429.
  - A representative endpoint (/ai/grade) is used as the canary.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import StudyDeck, StudyQuestion
from routes import study_routes

OWNER = "alice"


class _Query:
    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.rows = session.rows.setdefault(model, [])

    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, n):
        clone = _Query(self.session, self.model)
        clone.rows = self.rows[:n]
        return clone

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)

    def delete(self):
        count = len(self.rows)
        self.session.rows[self.model] = []
        self.rows = self.session.rows[self.model]
        return count


class _Session:
    def __init__(self):
        from core.database import (
            StudyDeck,
            StudyCard,
            StudyExam,
            StudyMaterial,
            StudyQuestion,
            StudyReview,
            StudyAttempt,
        )

        self.rows = {
            StudyDeck: [],
            StudyCard: [],
            StudyExam: [],
            StudyMaterial: [],
            StudyQuestion: [],
            StudyReview: [],
            StudyAttempt: [],
        }
        self.commits = 0
        self.closed = False

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


@pytest.fixture
def rl_client(monkeypatch):
    session = _Session()
    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", lambda: session)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)
    return TestClient(app), session


def _deck(**overrides):
    data = {
        "id": "deck-1",
        "owner": OWNER,
        "name": "Biology",
        "description": "Cell biology",
        "color": "#268bd2",
        "archived": False,
        "new_per_day": 15,
        "retention": "0.9",
        "overview": None,
        "created_at": datetime(2026, 1, 1),
    }
    data.update(overrides)
    return StudyDeck(**data)


def _question(**overrides):
    data = {
        "id": "q-1",
        "owner": OWNER,
        "deck_id": "deck-1",
        "material_id": None,
        "qtype": "open",
        "question": "What is ATP?",
        "context": None,
        "options": None,
        "correct_index": None,
        "reference": "Cellular energy currency.",
        "explanation": None,
        "deep_explanation": None,
        "number": None,
        "source_page": None,
        "prereq_ids": None,
        "topic": "Cells",
        "difficulty": "medium",
        "origin": "user",
        "suspended": False,
        "state": "new",
        "stability": "0",
        "fsrs_difficulty": "0",
        "due": datetime(2026, 1, 1),
        "last_review": None,
        "reps": 0,
        "lapses": 0,
        "created_at": datetime(2026, 1, 1),
    }
    data.update(overrides)
    return StudyQuestion(**data)


def test_ai_grade_returns_429_after_limit(rl_client):
    client, session = rl_client

    # Seed enough state for the endpoint to reach the rate-check
    session.rows[StudyDeck].append(_deck())
    session.rows[StudyQuestion].append(_question(qtype="open"))

    # Exceed the limit (20 requests in 60 seconds) on /ai/grade
    for _ in range(21):
        resp = client.post(
            "/api/study/ai/grade",
            json={
                "question": "What is ATP?",
                "answer": "It stores energy.",
                "reference": "Cellular energy currency.",
            },
        )

    # The last request must be rate-limited
    assert resp.status_code == 429
    assert "Too many requests" in resp.json()["detail"]
