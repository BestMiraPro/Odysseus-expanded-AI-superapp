"""Tests for Phase 2.5: typed-recall + wrong-MCQ gate.

These exercise:
1. typed_recall flag on an MCQ question — free-typed answer is graded via the
   open-answer AI path and verdict=="correct" maps to correct==True.
2. wrong_mcq_gate flag — when on, backend returns require_reengage=True for a
   wrong MCQ answer.
3. Default (flags off/MCQ) — existing choice_index path is untouched.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, StudyAttempt, StudyDeck, StudyQuestion
from routes import study_routes


OWNER = "alice"


@pytest.fixture
def study_app(tmp_path, monkeypatch):
    db_path = tmp_path / "study.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_study_attempts_idempotency_key ON study_attempts(idempotency_key)"))
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)

    # Patch out the rate limiter so tests aren't throttled
    monkeypatch.setattr(
        study_routes.RateLimiter, "check", lambda *_args, **_kwargs: True
    )

    return TestClient(app), TestSessionLocal


def _seed(session_local, *rows):
    db = session_local()
    try:
        for row in rows:
            db.add(row)
        db.commit()
    finally:
        db.close()


def _deck():
    return StudyDeck(
        id="deck-1",
        owner=OWNER,
        name="Biology",
        retention="0.9",
        new_per_day=15,
        archived=False,
        created_at=datetime(2026, 1, 1),
    )


def _question(qtype="mcq", **overrides):
    data = {
        "id": "q-1",
        "owner": OWNER,
        "deck_id": "deck-1",
        "qtype": qtype,
        "question": "Which molecule stores energy?",
        "options": '["ATP", "DNA", "RNA", "Protein"]',
        "correct_index": 0,
        "reference": "ATP stores readily usable cellular energy.",
        "state": "review",
        "stability": "2",
        "fsrs_difficulty": "5",
        "due": datetime(2026, 1, 1),
        "reps": 4,
        "lapses": 0,
        "created_at": datetime(2026, 1, 1),
    }
    data.update(overrides)
    return StudyQuestion(**data)


def _count(session_local, model):
    db = session_local()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _one(session_local, model):
    db = session_local()
    try:
        return db.query(model).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Default (flags off) — existing MCQ path untouched
# ---------------------------------------------------------------------------

def test_mcq_choice_index_default_no_flags(study_app, monkeypatch):
    """With typed_recall=False (default) and wrong_mcq_gate=False,
    a correct MCQ choice_index behaves exactly as before."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 3.0,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 0,
            "interval_days": 7,
        },
    )

    res = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": "sure"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["qtype"] == "mcq"
    assert data["correct"] is True
    assert data["score"] is None         # MCQ doesn't set score today
    assert data["grading"] is None       # no AI grading for plain MCQ
    assert data["rating"] == 4           # Easy (correct + sure + no hints)
    assert data["require_reengage"] is False
    assert _count(session_local, StudyAttempt) == 1


def test_mcq_choice_index_wrong_no_gate_default_off(study_app, monkeypatch):
    """A wrong MCQ answer with wrong_mcq_gate=False (default) should NOT set
    require_reengage=True."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 3.0,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 1,
            "interval_days": 7,
        },
    )

    res = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 1, "confidence": "sure"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["correct"] is False
    assert data["require_reengage"] is False


# ---------------------------------------------------------------------------
# Wrong-MCQ gate
# ---------------------------------------------------------------------------

def test_wrong_mcq_gate_signals_reengage_on_wrong_answer(study_app, monkeypatch):
    """When wrong_mcq_gate=True, a wrong MCQ answer returns
    require_reengage=True. A correct answer still returns False."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 3.0,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 1,
            "interval_days": 7,
        },
    )

    wrong = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 1, "confidence": "sure", "wrong_mcq_gate": True},
    )
    assert wrong.status_code == 200
    assert wrong.json()["correct"] is False
    assert wrong.json()["require_reengage"] is True

    right = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": "sure", "wrong_mcq_gate": True},
    )
    assert right.status_code == 200
    assert right.json()["correct"] is True
    assert right.json()["require_reengage"] is False


# ---------------------------------------------------------------------------
# Typed-recall: MCQ answered via free-typed recall using open grading path
# ---------------------------------------------------------------------------

def test_typed_recall_on_mcq_uses_ai_grading(study_app, monkeypatch):
    """typed_recall=True on an MCQ skips choice_index and uses open answer
    grading. The AI verdict drives correct==True."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    grade_calls = []

    async def mock_llm_json(user, system, prompt, **_kwargs):
        grade_calls.append(prompt)
        return {
            "score": 92,
            "verdict": "correct",
            "feedback": "Accurate and complete.",
            "followup": "Can you explain why ATP is better than GTP?",
        }

    monkeypatch.setattr(study_routes, "_llm_json", mock_llm_json)
    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 3.0,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 0,
            "interval_days": 7,
        },
    )

    res = client.post(
        "/api/study/questions/q-1/attempt",
        json={
            "answer": "Adenosine triphosphate (ATP) stores energy.",
            "confidence": "sure",
            "typed_recall": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["qtype"] == "mcq"
    assert data["correct"] is True
    assert data["score"] == 92
    assert data["grading"]["verdict"] == "correct"
    assert data["grading"]["score"] == 92
    assert data["require_reengage"] is False
    assert len(grade_calls) == 1
    assert "LEARNER'S ANSWER" in grade_calls[0]
    assert _count(session_local, StudyAttempt) == 1
    att = _one(session_local, StudyAttempt)
    assert att.answer == "Adenosine triphosphate (ATP) stores energy."


def test_typed_recall_partial_verdict_counts_as_incorrect(study_app, monkeypatch):
    """When typed_recall AI verdict is 'partial', correct maps to False
    (same as open grading behavior)."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    async def mock_llm_json(*_args, **_kwargs):
        return {"score": 55, "verdict": "partial", "feedback": "Half there.", "followup": None}

    monkeypatch.setattr(study_routes, "_llm_json", mock_llm_json)
    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 2.5,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 0,
            "interval_days": 3,
        },
    )

    res = client.post(
        "/api/study/questions/q-1/attempt",
        json={"answer": "DNA", "confidence": "unsure", "typed_recall": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["correct"] is False
    assert data["score"] == 55
    assert data["grading"]["verdict"] == "partial"


def test_typed_recall_combined_with_wrong_mcq_gate(study_app, monkeypatch):
    """typed_recall + wrong_mcq_gate both on: a wrong typed recall triggers
    require_reengage=True because the question is MCQ."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    async def mock_llm_json(*_args, **_kwargs):
        return {"score": 30, "verdict": "incorrect", "feedback": "Wrong.", "followup": None}

    monkeypatch.setattr(study_routes, "_llm_json", mock_llm_json)
    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 2.0,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 1,
            "interval_days": 1,
        },
    )

    res = client.post(
        "/api/study/questions/q-1/attempt",
        json={
            "answer": "Glucose",
            "confidence": "guess",
            "typed_recall": True,
            "wrong_mcq_gate": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["correct"] is False
    assert data["require_reengage"] is True
    assert data["grading"]["verdict"] == "incorrect"


def test_typed_recall_empty_body_choice_index_ok_if_flag_off(study_app, monkeypatch):
    """typed_recall default is False: supplying only choice_index still works."""
    client, session_local = study_app
    _seed(session_local, _deck(), _question())

    monkeypatch.setattr(
        study_routes.fsrs,
        "schedule",
        lambda *_args, **_kwargs: {
            "state": "review",
            "stability": 3.0,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": 5,
            "lapses": 0,
            "interval_days": 7,
        },
    )

    res = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": "sure"},  # typed_recall omitted => False
    )
    assert res.status_code == 200
    assert res.json()["correct"] is True
    assert res.json()["grading"] is None  # no AI grading path taken
