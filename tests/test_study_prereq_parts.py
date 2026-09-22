"""The "Earlier in this problem" box must show the WHOLE problem.

``prereq_ids`` stores every earlier part of a multi-part problem
(_link_deck_parts), but the route used to ``break`` after the first candidate —
so a 4-part question showed only its immediately preceding part and the rest
of the problem was invisible. These tests drive the real route against a
temporary database and pin: full chain, stored order, per-part answers, and
the still-active self/later/duplicate/owner filters.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import Base, StudyAttempt, StudyDeck, StudyQuestion
from routes import study_routes
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

OWNER = "alice"


@pytest.fixture
def study_app(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'prereqs.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False,
                                    autoflush=False)

    def _question(qid, number, **overrides):
        data = {
            "id": qid, "owner": OWNER, "deck_id": "deck-1", "qtype": "open",
            "question": f"Part {number}: continue the problem.",
            "number": number, "reference": f"Answer {number}",
            "origin": "extracted", "state": "new", "stability": "0",
            "fsrs_difficulty": "0", "created_at": datetime(2026, 1, 1),
        }
        data.update(overrides)
        return StudyQuestion(**data)

    session = TestSessionLocal()
    session.add(StudyDeck(id="deck-1", owner=OWNER, name="Maths"))
    session.add(_question("q-a", "16a"))
    session.add(_question("q-b", "16b"))
    session.add(_question(
        "q-c", "16c", qtype="mcq", options=json.dumps(["C1", "C2"]),
        correct_index=1, reference=""))
    session.add(_question(
        "q-d", "16d",
        prereq_ids=json.dumps(["q-a", "q-b", "q-c", "q-d", "q-e",
                               "q-b", "q-foreign"])))
    session.add(_question("q-e", "16e"))
    session.add(_question("q-foreign", "16a", owner="bob"))
    session.add(StudyAttempt(
        id="att-b", owner=OWNER, question_id="q-b", deck_id="deck-1",
        answer="my answer b", correct=False, score=40, rating=1,
        attempted_at=datetime(2026, 1, 2)))
    session.commit()
    session.close()

    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(study_routes, "get_current_user",
                        lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_a, **_k: None)
    monkeypatch.setattr(study_routes.RateLimiter, "check",
                        lambda *_a, **_k: True)
    return TestClient(app), TestSessionLocal


def _prereqs(client, qid):
    resp = client.get(f"/api/study/questions/{qid}/prereqs")
    assert resp.status_code == 200
    return resp.json()["prereqs"]


def test_the_whole_problem_chain_is_returned_in_order(study_app):
    client, _ = study_app
    parts = _prereqs(client, "q-d")

    assert [p["id"] for p in parts] == ["q-a", "q-b", "q-c"], (
        "the box must show every earlier part of the problem, earliest first"
    )
    assert [p["number"] for p in parts] == ["16a", "16b", "16c"]


def test_each_part_carries_the_stored_answer_and_its_own_correct_answer(study_app):
    client, _ = study_app
    parts = {p["id"]: p for p in _prereqs(client, "q-d")}

    assert parts["q-a"]["your_answer"] is None
    assert parts["q-a"]["correct"] == "Answer 16a"
    assert parts["q-b"]["your_answer"] == "my answer b", (
        "an earlier part with an attempt must surface the learner's answer"
    )
    assert parts["q-c"]["correct"] == "C2", (
        "an MCQ part's correct answer is its keyed option text"
    )


def test_self_later_duplicate_and_foreign_entries_stay_filtered(study_app):
    client, _ = study_app
    ids = [p["id"] for p in _prereqs(client, "q-d")]

    assert "q-d" not in ids, "the question is not its own prerequisite"
    assert "q-e" not in ids, "a later part is not earlier context"
    assert "q-foreign" not in ids, "another owner's question must not leak"
    assert ids.count("q-b") == 1, "a repeated id must appear once"


def test_a_question_without_prereqs_returns_nothing(study_app):
    client, _ = study_app
    assert _prereqs(client, "q-e") == []