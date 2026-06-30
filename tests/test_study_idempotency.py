from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database as cdb
from core.database import Base, StudyAttempt, StudyCard, StudyDeck, StudyQuestion, StudyReview
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
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_study_reviews_idempotency_key ON study_reviews(idempotency_key)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_study_attempts_idempotency_key ON study_attempts(idempotency_key)"))
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_args, **_kwargs: None)
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


def _question(question_id="q-1", **overrides):
    data = {
        "id": question_id,
        "owner": OWNER,
        "deck_id": "deck-1",
        "qtype": "mcq",
        "question": "Which molecule stores energy?",
        "options": '["ATP", "DNA"]',
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


def _schedule_spy(monkeypatch, *, barrier=None):
    calls = []

    def schedule(card, rating, **_kwargs):
        calls.append((dict(card), rating))
        if barrier is not None:
            # Generous timeout: under CPU contention (full suite run) both
            # threads may be slow to reach the rendezvous. Kept below the
            # 10s thread-join so a genuine non-rendezvous still surfaces.
            barrier.wait(timeout=8)
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
    return calls


def _one(session_local, model):
    db = session_local()
    try:
        return db.query(model).first()
    finally:
        db.close()


def _count(session_local, model):
    db = session_local()
    try:
        return db.query(model).count()
    finally:
        db.close()


def test_review_retry_same_key_returns_prior_result_without_rescheduling(study_app, monkeypatch):
    client, session_local = study_app
    _seed(session_local, _deck(), _card())
    calls = _schedule_spy(monkeypatch)

    body = {"rating": 3, "duration_ms": 1200, "idempotency_key": "rv:test:1"}
    first = client.post("/api/study/cards/card-1/review", json=body)
    assert first.status_code == 200
    after_first = _one(session_local, StudyCard)

    second = client.post("/api/study/cards/card-1/review", json=body)
    assert second.status_code == 200
    after_second = _one(session_local, StudyCard)

    assert second.json()["interval_days"] == first.json()["interval_days"]
    assert len(calls) == 1
    assert _count(session_local, StudyReview) == 1
    assert after_second.reps == after_first.reps
    assert after_second.stability == after_first.stability
    assert after_second.due == after_first.due


def test_review_without_key_preserves_legacy_double_advance(study_app, monkeypatch):
    client, session_local = study_app
    _seed(session_local, _deck(), _card())
    calls = _schedule_spy(monkeypatch)

    assert client.post("/api/study/cards/card-1/review", json={"rating": 3}).status_code == 200
    assert client.post("/api/study/cards/card-1/review", json={"rating": 3}).status_code == 200

    assert len(calls) == 2
    assert _count(session_local, StudyReview) == 2
    assert _one(session_local, StudyCard).reps == 6


def test_attempt_retry_same_key_returns_prior_result_without_rescheduling(study_app, monkeypatch):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    calls = _schedule_spy(monkeypatch)

    body = {"choice_index": 0, "confidence": "sure", "idempotency_key": "att:test:1"}
    first = client.post("/api/study/questions/q-1/attempt", json=body)
    assert first.status_code == 200
    after_first = _one(session_local, StudyQuestion)

    second = client.post("/api/study/questions/q-1/attempt", json=body)
    assert second.status_code == 200
    after_second = _one(session_local, StudyQuestion)

    assert second.json()["correct"] == first.json()["correct"]
    assert second.json()["score"] == first.json()["score"]
    assert second.json()["rating"] == first.json()["rating"]
    assert len(calls) == 1
    assert _count(session_local, StudyAttempt) == 1
    assert after_second.reps == after_first.reps
    assert after_second.stability == after_first.stability
    assert after_second.due == after_first.due


def test_open_attempt_retry_same_key_skips_regrading(study_app, monkeypatch):
    client, session_local = study_app
    _seed(session_local, _deck(), _question(qtype="open", options=None, correct_index=None))
    _schedule_spy(monkeypatch)
    grade_calls = []

    async def grade(*_args, **_kwargs):
        grade_calls.append(True)
        return {"score": 88, "verdict": "correct", "feedback": "Good.", "followup": None}

    monkeypatch.setattr(study_routes, "_llm_json", grade)

    body = {"answer": "ATP stores energy.", "confidence": "sure", "idempotency_key": "att:open:1"}
    first = client.post("/api/study/questions/q-1/attempt", json=body)
    second = client.post("/api/study/questions/q-1/attempt", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["grading"] == first.json()["grading"]
    assert len(grade_calls) == 1
    assert _count(session_local, StudyAttempt) == 1


def test_attempt_without_key_preserves_legacy_double_advance(study_app, monkeypatch):
    client, session_local = study_app
    _seed(session_local, _deck(), _question())
    calls = _schedule_spy(monkeypatch)

    body = {"choice_index": 0, "confidence": "sure"}
    assert client.post("/api/study/questions/q-1/attempt", json=body).status_code == 200
    assert client.post("/api/study/questions/q-1/attempt", json=body).status_code == 200

    assert len(calls) == 2
    assert _count(session_local, StudyAttempt) == 2
    assert _one(session_local, StudyQuestion).reps == 6


def test_concurrent_review_same_key_uses_unique_backstop(study_app, monkeypatch):
    client, session_local = study_app
    _seed(session_local, _deck(), _card())
    calls = _schedule_spy(monkeypatch, barrier=threading.Barrier(2))
    body = {"rating": 3, "idempotency_key": "rv:race:1"}
    responses = []

    def post_review():
        responses.append(client.post("/api/study/cards/card-1/review", json=body))

    t1 = threading.Thread(target=post_review)
    t2 = threading.Thread(target=post_review)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert [r.status_code for r in responses] == [200, 200]
    assert {r.json()["interval_days"] for r in responses} == {7}
    assert len(calls) == 2
    assert _count(session_local, StudyReview) == 1
    assert _one(session_local, StudyCard).reps == 5


def test_study_idempotency_migration_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "old-study.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE study_reviews (id VARCHAR PRIMARY KEY, reviewed_at DATETIME)")
    conn.execute("CREATE TABLE study_attempts (id VARCHAR PRIMARY KEY, attempted_at DATETIME)")
    conn.execute("INSERT INTO study_reviews (id, reviewed_at) VALUES ('r1', '2026-01-01')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")
    cdb._migrate_add_study_idempotency_keys()
    cdb._migrate_add_study_idempotency_keys()

    conn = sqlite3.connect(db_path)
    try:
        for table_name in ("study_reviews", "study_attempts"):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")]
            assert "idempotency_key" in columns
            indexes = conn.execute(f"PRAGMA index_list({table_name})").fetchall()
            assert sum(1 for row in indexes if row[1] == f"ux_{table_name}_idempotency_key") == 1
            assert any(row[1] == f"ux_{table_name}_idempotency_key" and row[2] for row in indexes)
        conn.execute("INSERT INTO study_reviews (id) VALUES ('r2')")
        conn.execute("INSERT INTO study_reviews (id) VALUES ('r3')")
        conn.execute("INSERT INTO study_reviews (id, idempotency_key) VALUES ('r4', 'same')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO study_reviews (id, idempotency_key) VALUES ('r5', 'same')")
    finally:
        conn.close()
