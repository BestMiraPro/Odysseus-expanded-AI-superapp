"""Tests for Phase 3.2 — Focus<->Plan link.

Verifies:
  (a) GET /focus/today-blocks returns the correct shape for a user whose
      plan contains today's blocks.
  (b) POST /focus/start persists deck_id/exam_id/block_key.
  (c) Sessions started without the optional fields still work (back-compat).
  (d) _focus_to_dict surfaces the link fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import (
    StudyDeck,
    StudyExam,
    StudyFocusSession,
)
from routes import study_routes


OWNER = "alice"


# --------------------------------------------------------------------------- fake session

class _Query:
    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.rows = session.rows.setdefault(model, [])
        self._filters = []

    def filter(self, *preds):
        # The focus handlers filter by .archived == False and by .owner == user.
        # For the fake we just record predicates but return all rows — the
        # tests seed exactly the rows they expect.
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


class _Session:
    def __init__(self):
        self.rows = {
            StudyDeck: [],
            StudyExam: [],
            StudyFocusSession: [],
        }
        self.commits = 0
        self.closed = False

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
    session = _Session()
    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", lambda: session)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)
    return TestClient(app), session


# --------------------------------------------------------------------------- builders

TODAY_ISO = datetime.now(timezone.utc).date().isoformat()


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


def _exam_with_today_blocks(**overrides):
    """An exam whose plan has a day == today with two blocks."""
    plan = {
        "days": [
            {
                "date": TODAY_ISO,
                "blocks": [
                    {"type": "review", "topics": ["Cells"], "minutes": 25},
                    {"type": "practice", "topics": ["Membranes"], "minutes": 50},
                ],
            },
            {
                "date": "2099-12-31",
                "blocks": [{"type": "review", "topics": ["Future"], "minutes": 25}],
            },
        ]
    }
    data = {
        "id": "exam-1",
        "owner": OWNER,
        "title": "Final",
        "exam_date": "2099-12-31",
        "exam_format": "MCQ",
        "hours_per_week": "7",
        "rest_days": "[6]",
        "topics": '[{"name": "Cells"}]',
        "plan": json.dumps(plan),
        "done_blocks": None,
        "archived": False,
    }
    data.update(overrides)
    return StudyExam(**data)


def _exam_no_plan(**overrides):
    data = {
        "id": "exam-2",
        "owner": OWNER,
        "title": "Quiz",
        "exam_date": "2099-12-31",
        "exam_format": "MCQ",
        "hours_per_week": "7",
        "rest_days": "[6]",
        "topics": "[]",
        "plan": None,
        "done_blocks": None,
        "archived": False,
    }
    data.update(overrides)
    return StudyExam(**data)


# --------------------------------------------------------------------------- tests

def test_today_blocks_returns_shape_for_user_with_plan(study_client):
    client, session = study_client
    session.rows[StudyDeck] = [_deck()]
    session.rows[StudyExam] = [_exam_with_today_blocks(), _exam_no_plan()]

    res = client.get("/api/study/focus/today-blocks")
    assert res.status_code == 200
    body = res.json()

    # decks present
    assert any(d["id"] == "deck-1" for d in body["decks"])

    # exactly the two blocks from today's day, not the future day
    blocks = body["blocks"]
    assert len(blocks) == 2
    assert blocks[0]["exam_id"] == "exam-1"
    assert blocks[0]["exam_title"] == "Final"
    assert blocks[0]["block_key"] == f"{TODAY_ISO}:0"
    assert blocks[0]["type"] == "review"
    assert blocks[0]["topics"] == ["Cells"]
    assert blocks[0]["minutes"] == 25
    assert blocks[1]["block_key"] == f"{TODAY_ISO}:1"
    assert blocks[1]["type"] == "practice"
    assert blocks[1]["minutes"] == 50


def test_today_blocks_empty_when_no_plan(study_client):
    client, session = study_client
    session.rows[StudyDeck] = []
    session.rows[StudyExam] = [_exam_no_plan()]

    res = client.get("/api/study/focus/today-blocks")
    assert res.status_code == 200
    body = res.json()
    assert body == {"decks": [], "blocks": []}


def test_focus_start_persists_link_fields(study_client):
    client, session = study_client

    res = client.post("/api/study/focus/start", json={
        "label": "Cell bio review",
        "planned_min": 25,
        "deck_id": "deck-1",
        "exam_id": "exam-1",
        "block_key": f"{TODAY_ISO}:0",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["deck_id"] == "deck-1"
    assert body["exam_id"] == "exam-1"
    assert body["block_key"] == f"{TODAY_ISO}:0"

    # row persisted in the fake session
    rows = session.rows[StudyFocusSession]
    assert len(rows) == 1
    assert rows[0].deck_id == "deck-1"
    assert rows[0].exam_id == "exam-1"
    assert rows[0].block_key == f"{TODAY_ISO}:0"
    assert rows[0].label == "Cell bio review"


def test_focus_start_without_link_fields_back_compat(study_client):
    """Sessions started with no attribution must still work (back-compat)."""
    client, session = study_client

    res = client.post("/api/study/focus/start", json={
        "label": "Free study",
        "planned_min": 50,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["deck_id"] is None
    assert body["exam_id"] is None
    assert body["block_key"] is None
    assert body["label"] == "Free study"

    rows = session.rows[StudyFocusSession]
    assert len(rows) == 1
    assert rows[0].deck_id is None
    assert rows[0].exam_id is None
    assert rows[0].block_key is None


def test_focus_finish_preserves_link_fields(study_client):
    """Finishing a session must not clear the attribution."""
    client, session = study_client

    # start with link
    start = client.post("/api/study/focus/start", json={
        "label": "Plan block",
        "planned_min": 25,
        "exam_id": "exam-1",
        "block_key": f"{TODAY_ISO}:0",
    })
    sid = start.json()["id"]

    res = client.post(f"/api/study/focus/{sid}/finish", json={
        "actual_min": 25,
        "completed": True,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["completed"] is True
    assert body["actual_min"] == 25
    # link fields survive the finish
    assert body["exam_id"] == "exam-1"
    assert body["block_key"] == f"{TODAY_ISO}:0"
