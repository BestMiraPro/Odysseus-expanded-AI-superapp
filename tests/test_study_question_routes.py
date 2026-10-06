"""Question bank routes against a temp SQLite DB.

- PUT /questions/{id}: shrinking an MCQ's options used to leave
  ``correct_index`` pointing past the end, so the question could never be
  answered right.
- POST /questions/{id}/attempt: a picked MCQ option is graded locally, so it
  must not spend the AI rate limit (open answers still do).
- Practice attempts schedule with the deck's desired retention, like card
  reviews, instead of the 0.9 default.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers.sqlite_db import make_temp_sqlite

OWNER = "alice"


@pytest.fixture
def env(monkeypatch):
    import core.database as cd
    from routes import study_routes as sr

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)
    monkeypatch.setattr(sr, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(sr, "_read_pref", lambda *_a, **_k: None)
    app = FastAPI()
    app.include_router(sr.setup_study_routes())
    yield TestClient(app), SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _seed(SessionLocal, *, retention="0.9", **question):
    from core.database import StudyDeck, StudyQuestion
    s = SessionLocal()
    s.add(StudyDeck(id="d1", owner=OWNER, name="Micro", new_per_day=15, retention=retention))
    fields = dict(id="q1", owner=OWNER, deck_id="d1", qtype="mcq", question="Which?",
                  options=json.dumps(["a", "b", "c"]), correct_index=2, reference="",
                  difficulty="medium", state="new", due=datetime(2026, 1, 1))
    fields.update(question)
    s.add(StudyQuestion(**fields))
    s.commit()
    s.close()


def _row(SessionLocal, qid="q1"):
    from core.database import StudyQuestion
    s = SessionLocal()
    try:
        r = s.query(StudyQuestion).filter(StudyQuestion.id == qid).first()
        return {"options": json.loads(r.options) if r.options else None,
                "correct_index": r.correct_index, "stability": r.stability,
                "state": r.state, "due": r.due, "last_review": r.last_review}
    finally:
        s.close()


# ---------------------------------------------------------------- PUT options

def test_shrinking_options_past_the_answer_is_rejected(env):
    client, db = env
    _seed(db)
    res = client.put("/api/study/questions/q1", json={"options": ["a", "b"]})
    assert res.status_code == 400
    assert "correct_index" in res.json()["detail"]
    # Nothing was written: the question is still answerable.
    row = _row(db)
    assert row["options"] == ["a", "b", "c"] and row["correct_index"] == 2


def test_shrinking_options_with_a_new_answer_is_accepted(env):
    client, db = env
    _seed(db)
    res = client.put("/api/study/questions/q1",
                     json={"options": ["a", "b"], "correct_index": 1})
    assert res.status_code == 200, res.text
    assert res.json()["options"] == ["a", "b"] and res.json()["correct_index"] == 1


def test_shrinking_options_that_keep_the_answer_in_range_is_fine(env):
    client, db = env
    _seed(db, correct_index=0)
    res = client.put("/api/study/questions/q1", json={"options": ["a", "b"]})
    assert res.status_code == 200, res.text
    assert _row(db)["correct_index"] == 0


# ---------------------------------------------------------------- AI rate limit

@pytest.fixture
def limiter_exhausted(monkeypatch):
    # Patch the class, as the other study tests do: patching the shared
    # instance leaves a bound method pinned on it after teardown, shadowing
    # their class-level patches.
    from routes import study_routes
    monkeypatch.setattr(study_routes.RateLimiter, "check", lambda *_a, **_k: False)


def test_mcq_attempt_is_not_ai_rate_limited(env, limiter_exhausted):
    client, db = env
    _seed(db)
    res = client.post("/api/study/questions/q1/attempt", json={"choice_index": 2})
    assert res.status_code == 200, res.text
    assert res.json()["correct"] is True


def test_typed_recall_mcq_attempt_is_ai_rate_limited(env, limiter_exhausted):
    client, db = env
    _seed(db)
    res = client.post("/api/study/questions/q1/attempt",
                      json={"answer": "c", "typed_recall": True})
    assert res.status_code == 429


def test_open_attempt_is_ai_rate_limited(env, limiter_exhausted):
    client, db = env
    _seed(db, qtype="open", options=None, correct_index=None, reference="r")
    res = client.post("/api/study/questions/q1/attempt", json={"answer": "x"})
    assert res.status_code == 429


# ---------------------------------------------------------------- retention

def _review_state(**kw):
    """A question in review state, last seen 10 days ago."""
    now = datetime.utcnow()
    return dict(state="review", stability="10", fsrs_difficulty="5", reps=3,
                last_review=now - timedelta(days=10), due=now - timedelta(days=1), **kw)


@pytest.mark.parametrize("retention", ["0.8", "0.95"])
def test_practice_attempt_schedules_with_the_deck_retention(env, retention):
    from src import fsrs
    client, db = env
    _seed(db, retention=retention, **_review_state())
    before = _row(db)
    res = client.post("/api/study/questions/q1/attempt", json={"choice_index": 2})
    assert res.status_code == 200, res.text
    expected = fsrs.schedule({"state": "review", "stability": 10.0, "difficulty": 5.0,
                              "last_review": before["last_review"], "reps": 3, "lapses": 0},
                             res.json()["rating"], desired_retention=float(retention))
    default = fsrs.schedule({"state": "review", "stability": 10.0, "difficulty": 5.0,
                             "last_review": before["last_review"], "reps": 3, "lapses": 0},
                            res.json()["rating"])
    assert expected["interval_days"] != default["interval_days"], "test needs a retention that matters"
    assert res.json()["interval_days"] == expected["interval_days"]
