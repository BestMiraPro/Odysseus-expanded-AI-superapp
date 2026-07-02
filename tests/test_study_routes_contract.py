"""Contract tests for core Study route handlers.

These pin current request -> status + JSON shape before the thin service
extraction. The DB is a small in-memory fake; route bodies still execute.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import (
    StudyAttempt,
    StudyCard,
    StudyDeck,
    StudyExam,
    StudyMaterial,
    StudyQuestion,
    StudyReview,
)
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


@contextmanager
def _session_scope(session):
    yield session


@pytest.fixture
def study_client(monkeypatch):
    session = _Session()
    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", lambda: session)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)
    # Patch the rate limiter so AI-heavy endpoints (attempt, ask, hint) aren't
    # throttled by the global singleton accumulating across test functions.
    monkeypatch.setattr(study_routes.RateLimiter, "check", lambda *_a, **_k: True)
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


def _card(**overrides):
    data = {
        "id": "card-1",
        "owner": OWNER,
        "deck_id": "deck-1",
        "front": "What is ATP?",
        "back": "Cellular energy currency.",
        "notes": None,
        "tags": '["bio"]',
        "suspended": False,
        "source": "user",
        "state": "new",
        "stability": "0",
        "difficulty": "0",
        "due": datetime(2026, 1, 1),
        "last_review": None,
        "reps": 0,
        "lapses": 0,
        "created_at": datetime(2026, 1, 1),
    }
    data.update(overrides)
    return StudyCard(**data)


def _exam(**overrides):
    data = {
        "id": "exam-1",
        "owner": OWNER,
        "title": "Final",
        "exam_date": "2026-07-15",
        "exam_format": "MCQ",
        "hours_per_week": "7",
        "rest_days": "[6]",
        "topics": '[{"name": "Cells"}]',
        "plan": None,
        "done_blocks": None,
        "archived": False,
    }
    data.update(overrides)
    return StudyExam(**data)


def _material(**overrides):
    data = {
        "id": "mat-1",
        "owner": OWNER,
        "deck_id": "deck-1",
        "name": "Chapter 1",
        "kind": "text",
        "file_id": None,
        "content": "A long enough paragraph about cells and membranes.",
        "char_count": 48,
        "question_count": 0,
        "summary": None,
        "category": "theory",
        "created_at": datetime(2026, 1, 1),
    }
    data.update(overrides)
    return StudyMaterial(**data)


def _question(**overrides):
    data = {
        "id": "q-1",
        "owner": OWNER,
        "deck_id": "deck-1",
        "material_id": None,
        "qtype": "mcq",
        "question": "Which molecule stores energy?",
        "context": None,
        "options": '["ATP", "DNA"]',
        "correct_index": 0,
        "reference": "ATP stores readily usable cellular energy.",
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


def _seed(session, *rows):
    for row in rows:
        session.rows[type(row)].append(row)


def _assert_keys(payload, keys):
    assert set(keys).issubset(payload.keys())


def test_deck_crud_contracts(study_client):
    client, session = study_client
    _seed(session, _deck())

    listed = client.get("/api/study/decks")
    assert listed.status_code == 200
    _assert_keys(listed.json(), {"decks"})
    _assert_keys(listed.json()["decks"][0], {"id", "name", "due_count", "q_total"})

    created = client.post("/api/study/decks", json={"name": " Chemistry "})
    assert created.status_code == 200
    _assert_keys(created.json(), {"id", "name"})

    updated = client.put("/api/study/decks/deck-1", json={"name": "Biochem", "archived": True})
    assert updated.status_code == 200
    assert updated.json() == {"ok": True}

    deleted = client.delete("/api/study/decks/deck-1")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_card_crud_and_review_contracts(study_client, monkeypatch):
    client, session = study_client
    _seed(session, _deck(), _card())
    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "learning",
            "stability": 1.2,
            "difficulty": 5.5,
            "due": datetime(2026, 1, 2),
            "last_review": datetime(2026, 1, 1),
            "reps": 1,
            "lapses": 0,
            "interval_days": 1,
        },
    )

    listed = client.get("/api/study/decks/deck-1/cards")
    assert listed.status_code == 200
    _assert_keys(listed.json(), {"cards"})
    _assert_keys(listed.json()["cards"][0], {"id", "deck_id", "front", "back", "state"})

    created = client.post(
        "/api/study/decks/deck-1/cards",
        json={"cards": [{"front": "F", "back": "B", "tags": ["x"]}], "source": "user"},
    )
    assert created.status_code == 200
    _assert_keys(created.json(), {"created", "ids"})

    updated = client.put("/api/study/cards/card-1", json={"front": "Updated", "tags": ["bio", "x"]})
    assert updated.status_code == 200
    _assert_keys(updated.json(), {"id", "front", "tags", "due", "reps"})

    reviewed = client.post("/api/study/cards/card-1/review", json={"rating": 3, "duration_ms": 1200})
    assert reviewed.status_code == 200
    _assert_keys(reviewed.json(), {"card", "interval_days"})
    _assert_keys(reviewed.json()["card"], {"id", "state", "due", "last_review"})

    deleted = client.delete("/api/study/cards/card-1")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_exam_crud_contracts(study_client):
    client, session = study_client
    _seed(session, _exam())

    listed = client.get("/api/study/exams")
    assert listed.status_code == 200
    _assert_keys(listed.json(), {"exams"})
    _assert_keys(listed.json()["exams"][0], {"id", "title", "topics", "done_blocks"})

    created = client.post(
        "/api/study/exams",
        json={"title": "Midterm", "exam_date": "2026-08-01", "topics": [{"name": "Cells"}]},
    )
    assert created.status_code == 200
    _assert_keys(created.json(), {"id", "title", "exam_date", "topics", "archived"})

    updated = client.put("/api/study/exams/exam-1", json={"hours_per_week": 5, "archived": True})
    assert updated.status_code == 200
    _assert_keys(updated.json(), {"id", "hours_per_week", "archived"})

    deleted = client.delete("/api/study/exams/exam-1")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_material_crud_contracts(study_client):
    client, session = study_client
    _seed(session, _deck(), _material())

    listed = client.get("/api/study/decks/deck-1/materials")
    assert listed.status_code == 200
    _assert_keys(listed.json(), {"materials"})
    _assert_keys(listed.json()["materials"][0], {"id", "deck_id", "name", "category"})

    created = client.post(
        "/api/study/decks/deck-1/materials",
        json={"name": "Notes", "text": "This paragraph has enough text to be accepted as study material."},
    )
    assert created.status_code == 200
    _assert_keys(created.json(), {"id", "deck_id", "name", "kind", "category"})

    categorized = client.put("/api/study/materials/mat-1/category", json={"category": "exam"})
    assert categorized.status_code == 200
    assert categorized.json() == {"ok": True, "category": "exam"}

    deleted = client.delete("/api/study/materials/mat-1")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}


def test_question_crud_and_attempt_contracts(study_client, monkeypatch):
    client, session = study_client
    _seed(session, _deck(), _question())
    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "learning",
            "stability": 1.2,
            "difficulty": 5.5,
            "due": datetime(2026, 1, 2),
            "last_review": datetime(2026, 1, 1),
            "reps": 1,
            "lapses": 0,
            "interval_days": 1,
        },
    )

    listed = client.get("/api/study/decks/deck-1/questions")
    assert listed.status_code == 200
    _assert_keys(listed.json(), {"questions"})
    _assert_keys(listed.json()["questions"][0], {"id", "qtype", "question", "options", "reference"})

    updated = client.put("/api/study/questions/q-1", json={"topic": "Energy", "difficulty": "hard"})
    assert updated.status_code == 200
    _assert_keys(updated.json(), {"id", "topic", "difficulty", "state"})

    attempted = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": "sure", "duration_ms": 900},
    )
    assert attempted.status_code == 200
    _assert_keys(
        attempted.json(),
        {"qtype", "correct", "score", "grading", "correct_index", "reference", "rating", "interval_days", "next_due"},
    )

    deleted = client.delete("/api/study/questions/q-1")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
