"""Tests for Phase 2.2: elaborative-interrogation prompt + JOL capture.

Covers:
- ASK_ELABORATE_SYSTEM exists and is a distinct prompt.
- AskIn.elaborate default False; when True + answered True → elaborate mode.
- When elaborate=False (default) or answered=False → existing behaviour.
- JOL (confidence numeric 0-100) flows through attempt and is persisted.
- Existing test battery unchanged (no regressions).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import Base, StudyAttempt, StudyCard, StudyDeck, StudyQuestion
from routes import study_routes
from src.study_ai import ASK_ELABORATE_SYSTEM, ASK_COACH_SYSTEM, ASK_TUTOR_SYSTEM

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
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(study_routes.RateLimiter, "check", lambda *_a, **_k: True)

    async def fake_llm_text(owner, system, prompt, **kwargs):
        # Echo the prompt back so we can inspect which system prompt was used.
        return f"[system_len={len(system)}] {prompt[:80]}"

    monkeypatch.setattr(study_routes, "_llm_text", fake_llm_text)

    def schedule(card, rating, **_kwargs):
        reps = (card.get("reps") or 0) + 1
        return {
            "state": "review",
            "stability": (card.get("stability") or 0) + 1,
            "difficulty": 4.5,
            "due": datetime(2026, 1, 3),
            "last_review": datetime(2026, 1, 2),
            "reps": reps,
            "lapses": card.get("lapses") or 0,
            "interval_days": 7,
        }

    monkeypatch.setattr(study_routes.fsrs, "schedule", schedule)

    return TestClient(app), TestSessionLocal


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


def _card(card_id="card-1"):
    return StudyCard(
        id=card_id,
        owner=OWNER,
        deck_id="deck-1",
        front="What is ATP?",
        back="Cellular energy currency.",
        tags="[]",
        suspended=False,
        state="review",
        stability="2",
        difficulty="5",
        due=datetime(2026, 1, 1),
        reps=4,
        lapses=0,
        created_at=datetime(2026, 1, 1),
    )


def _question(q_id="q-1", qtype="mcq", options='["ATP", "DNA"]', correct_index=0):
    return StudyQuestion(
        id=q_id,
        owner=OWNER,
        deck_id="deck-1",
        qtype=qtype,
        question="Which molecule stores energy?",
        options=options,
        correct_index=correct_index,
        reference="ATP stores readily usable cellular energy.",
        state="review",
        stability="2",
        fsrs_difficulty="5",
        due=datetime(2026, 1, 1),
        reps=4,
        lapses=0,
        created_at=datetime(2026, 1, 1),
    )


def _seed(session_local, *rows):
    db = session_local()
    try:
        for row in rows:
            db.add(row)
        db.commit()
    finally:
        db.close()


def _one(session_local, model):
    db = session_local()
    try:
        return db.query(model).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Prompt separation
# ---------------------------------------------------------------------------

def test_ask_elaborate_system_is_distinct():
    assert ASK_ELABORATE_SYSTEM is not None
    assert ASK_ELABORATE_SYSTEM != ASK_COACH_SYSTEM
    assert ASK_ELABORATE_SYSTEM != ASK_TUTOR_SYSTEM
    txt = ASK_ELABORATE_SYSTEM.lower()
    assert "why" in txt or "how" in txt
    assert "elaborative" in txt or "probe" in txt


def test_ask_elaborate_system_never_reveals():
    txt = ASK_ELABORATE_SYSTEM.lower()
    assert "do not reveal the answer" in txt
    assert "do not ask them to compute" in txt


# ---------------------------------------------------------------------------
# Route-level elaboration gating
# ---------------------------------------------------------------------------

def test_ask_defaults_to_coach_when_not_answered(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/ask",
        json={"message": "Help", "answered": False, "elaborate": False},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["mode"] == "coach"
    assert "[system_len=" in payload["reply"]


def test_ask_defaults_to_tutor_when_answered(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/ask",
        json={"message": "Why?", "answered": True, "elaborate": False},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["mode"] == "tutor"


def test_ask_switches_to_elaborate_when_answered_and_elaborate_true(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/ask",
        json={"message": "Why?", "answered": True, "elaborate": True},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["mode"] == "elaborate"


def test_ask_coach_ignores_elaborate_flag(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/ask",
        json={"message": "Help", "answered": False, "elaborate": True},
    )
    assert r.status_code == 200
    payload = r.json()
    # Before answering, elaborate never triggers.
    assert payload["mode"] == "coach"


# ---------------------------------------------------------------------------
# JOL capture on attempt
# ---------------------------------------------------------------------------

def test_attempt_numeric_confidence_is_persisted(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": 73},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["rating"] is not None
    att = _one(session_local, StudyAttempt)
    assert att is not None
    assert att.confidence == 73


def test_attempt_legacy_string_confidence_is_converted(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": "sure"},
    )
    assert r.status_code == 200
    att = _one(session_local, StudyAttempt)
    assert att.confidence == 85


def test_attempt_null_confidence_is_null_in_db(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    r = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": None},
    )
    assert r.status_code == 200
    att = _one(session_local, StudyAttempt)
    assert att.confidence is None


def test_attempt_jol_affects_rating(study_app):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    # High confidence + correct → Easy (rating 4)
    r_high = client.post(
        "/api/study/questions/q-1/attempt",
        json={"choice_index": 0, "confidence": 90},
    )
    _seed(session_local, _question(q_id="q-2"))
    r_low = client.post(
        "/api/study/questions/q-2/attempt",
        json={"choice_index": 0, "confidence": 30},
    )
    assert r_high.status_code == 200
    assert r_low.status_code == 200
    assert r_high.json()["rating"] == 4
    assert r_low.json()["rating"] == 3
