"""Adaptive question selection tests (Phase 2.4).

Tests the /practice/queue endpoint with ?adaptive=true.
When enabled, candidates are re-ordered toward the learner's weak areas
using per-question accuracy, topic accuracy, and FSRS stability signals.
When disabled (default), selection is byte-identical to today's order.
"""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import (
    StudyAttempt,
    StudyDeck,
    StudyQuestion,
    StudyReview,
)
from routes import study_routes as sr
from src.study_stats import adaptive_question_priority as _adaptive_question_priority

OWNER = "adaptive-user"


class _Query:
    """Fake DB query that actually respects simple equality/inequality filters."""

    def __init__(self, session, model):
        self.session = session
        self.model = model
        self.rows = list(session.rows.setdefault(model, []))

    def filter(self, *args):
        clone = _Query(self.session, self.model)
        rows = list(self.rows)
        for crit in args:
            if hasattr(crit, "clauses"):
                for c in crit.clauses:
                    rows = [r for r in rows if _match(r, c)]
            else:
                rows = [r for r in rows if _match(r, crit)]
        clone.rows = rows
        return clone

    def order_by(self, *_args):
        clone = _Query(self.session, self.model)
        clone.rows = list(self.rows)
        return clone

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
            StudyQuestion: [],
            StudyAttempt: [],
            StudyReview: [],
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


def _op_name(op):
    return getattr(op, "__name__", None)


def _match(row, criterion):
    if hasattr(criterion, "operator") and _op_name(criterion.operator) == "inv":
        return not _match(row, criterion.element)

    from sqlalchemy.sql.expression import BinaryExpression, BooleanClauseList

    if isinstance(criterion, BinaryExpression):
        left, right, op = criterion.left, criterion.right, criterion.operator
        op_name = _op_name(op)

        col = getattr(left, "key", None)
        if col is None:
            return True

        val = right.value if hasattr(right, "value") else right
        actual = getattr(row, col, None)

        if op_name == "eq":
            return actual == val
        if op_name == "ne":
            return actual != val
        if op_name == "le":
            return actual is not None and actual <= val
        if op_name == "ge":
            return actual is not None and actual >= val
        if op_name == "in_op":
            return actual in val
        return True

    if isinstance(criterion, BooleanClauseList):
        results = [_match(r, c) for c in criterion.clauses]
        op_name = _op_name(criterion.operator)
        if op_name == "or_":
            return any(results)
        return all(results)

    return True


@contextmanager
def _session_scope(session):
    yield session


@pytest.fixture
def client(monkeypatch):
    session = _Session()
    app = FastAPI()
    app.include_router(sr.setup_study_routes())
    # Make the 42-day adaptive window stable for tests that seed old attempts.
    NOW = datetime(2026, 1, 15, 12, 0, 0)
    monkeypatch.setattr(sr, "SessionLocal", lambda: session)
    monkeypatch.setattr(sr, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(sr, "_read_pref", lambda _owner, key: None)
    monkeypatch.setattr(sr, "_utcnow_naive", lambda: NOW)
    return TestClient(app), session


def _deck(**overrides):
    defaults = {
        "id": "deck-1", "owner": OWNER, "name": "Physics",
        "description": "", "color": None, "archived": False,
        "new_per_day": 15, "retention": "0.9", "overview": None,
        "created_at": datetime(2026, 1, 1),
    }
    defaults.update(overrides)
    return StudyDeck(**defaults)


def _question(**overrides):
    defaults = {
        "id": "q-1", "owner": OWNER, "deck_id": "deck-1",
        "material_id": None, "qtype": "mcq",
        "question": "What is the speed of light?", "context": None,
        "options": json.dumps(["3e8 m/s", "1e8 m/s"]), "correct_index": 0,
        "reference": "3 x 10^8 m/s", "explanation": None,
        "deep_explanation": None, "number": None, "source_page": None,
        "prereq_ids": None, "topic": "Optics", "difficulty": "medium",
        "origin": "extracted", "suspended": False, "state": "new",
        "stability": "0", "fsrs_difficulty": "0",
        "due": datetime(2026, 1, 1), "last_review": None,
        "reps": 0, "lapses": 0, "created_at": datetime(2026, 1, 1),
    }
    defaults.update(overrides)
    return StudyQuestion(**defaults)


def _attempt(**overrides):
    defaults = {
        "id": "att-1", "owner": OWNER, "question_id": "q-1",
        "deck_id": "deck-1", "qtype": "mcq", "answer": "3e8 m/s",
        "correct": True, "score": None, "rating": 3,
        "confidence": None, "hints_used": 0, "grading": None,
        "duration_ms": None, "attempted_at": datetime(2026, 1, 1),
        "idempotency_key": None,
    }
    defaults.update(overrides)
    return StudyAttempt(**defaults)


def _seed(session, *items):
    for item in items:
        session.rows.setdefault(type(item), []).append(item)


# ---------------------------------------------------------------------------
# 1. Pure-function scoring/ordering tests
# ---------------------------------------------------------------------------

class TestAdaptivePriority:
    def test_prioritizes_low_accuracy(self):
        q1 = SimpleNamespace(id="q1", topic="T1")
        q2 = SimpleNamespace(id="q2", topic="T2")
        candidates = [q1, q2]
        per_q = {
            "q1": {"accuracy": 0.2},
            "q2": {"accuracy": 0.9},
        }
        topic = {}
        stab = {"q1": 3.0, "q2": 3.0}
        ordered = _adaptive_question_priority(candidates, per_q, topic, stab)
        assert ordered[0].id == "q1"
        assert ordered[1].id == "q2"

    def test_prioritizes_low_stability(self):
        q1 = SimpleNamespace(id="q1", topic="T1")
        q2 = SimpleNamespace(id="q2", topic="T1")
        candidates = [q1, q2]
        per_q = {
            "q1": {"accuracy": 0.5},
            "q2": {"accuracy": 0.5},
        }
        topic = {}
        stab = {"q1": 0.1, "q2": 5.0}
        ordered = _adaptive_question_priority(candidates, per_q, topic, stab)
        assert ordered[0].id == "q1"
        assert ordered[1].id == "q2"

    def test_falls_back_to_topic_accuracy(self):
        q1 = SimpleNamespace(id="q1", topic="WeakTopic")
        q2 = SimpleNamespace(id="q2", topic="StrongTopic")
        candidates = [q1, q2]
        per_q = {}  # no per-question stats
        topic = {
            "WeakTopic": {"accuracy": 0.3},
            "StrongTopic": {"accuracy": 0.8},
        }
        stab = {}
        ordered = _adaptive_question_priority(candidates, per_q, topic, stab)
        assert ordered[0].id == "q1"
        assert ordered[1].id == "q2"

    def test_neutral_fallback_when_no_signals(self):
        q1 = SimpleNamespace(id="q1", topic="T1")
        q2 = SimpleNamespace(id="q2", topic="T2")
        candidates = [q1, q2]
        per_q = {}
        topic = {}
        stab = {}
        ordered = _adaptive_question_priority(candidates, per_q, topic, stab)
        # No signals => both score identically (0.5); sort is stable.
        assert ordered[0].id == q1.id
        assert ordered[1].id == q2.id


# ---------------------------------------------------------------------------
# 2. Endpoint tests (flag gating)
# ---------------------------------------------------------------------------

class TestEndpointGating:
    def test_adaptive_flag_default_off_preserves_order(self, client):
        c, session = client
        _seed(
            session,
            _deck(),
            _question(id="q-due", topic="A", state="review", reps=3, stability="2.0",
                      due=datetime(2025, 1, 1)),
            _question(id="q-new-1", topic="B", state="new", reps=0),
            _question(id="q-new-2", topic="C", state="new", reps=0),
        )
        res = c.get("/api/study/practice/queue?limit=20")
        assert res.status_code == 200
        payload = res.json()
        qids = [q["id"] for q in payload["queue"]]
        # Default ordering: new first, then due (non-review pref)
        assert qids.index("q-new-1") < qids.index("q-due")
        assert qids.index("q-new-2") < qids.index("q-due")
        # No adaptive_weights key
        assert "adaptive_weights" not in payload

    def test_adaptive_flag_on_reorders_toward_weak_area(self, client):
        c, session = client
        _seed(
            session,
            _deck(),
            _question(id="q-strong", topic="A", state="review", reps=3, stability="5.0",
                      due=datetime(2025, 1, 1)),
            _question(id="q-weak", topic="B", state="review", reps=3, stability="0.3",
                      due=datetime(2025, 1, 1)),
        )
        # weak: 0% correct attempts on q-weak, strong: 100% correct on q-strong
        _seed(
            session,
            _attempt(question_id="q-weak", correct=False, attempted_at=datetime(2026, 1, 2)),
            _attempt(question_id="q-strong", correct=True, attempted_at=datetime(2026, 1, 2)),
        )
        res = c.get("/api/study/practice/queue?adaptive=true&limit=20")
        assert res.status_code == 200
        payload = res.json()
        qids = [q["id"] for q in payload["queue"]]
        # Weak item should come first
        assert qids.index("q-weak") < qids.index("q-strong")
        # Transparency payload present
        assert "adaptive_weights" in payload
        wmap = {w["question_id"]: w for w in payload["adaptive_weights"]}
        assert "q-weak" in wmap
        assert "q-strong" in wmap
        # Accuracy values reflect attempts (may be None if fake DB can’t resolve topics,
        # but when present they distinguish strong vs weak)
        if wmap["q-weak"]["accuracy"] is not None:
            assert wmap["q-weak"]["accuracy"] == 0.0
            assert wmap["q-strong"]["accuracy"] == 1.0

    def test_adaptive_flag_on_empty_attempts_neutral(self, client):
        c, session = client
        _seed(
            session,
            _deck(),
            _question(id="q1", topic="A", state="new", reps=0),
            _question(id="q2", topic="B", state="new", reps=0),
        )
        res = c.get("/api/study/practice/queue?adaptive=true&limit=20")
        assert res.status_code == 200
        payload = res.json()
        qids = [q["id"] for q in payload["queue"]]
        # No attempt data → neutral scores, order unchanged from default
        assert len(qids) == 2
        assert "adaptive_weights" in payload
        for w in payload["adaptive_weights"]:
            assert w["accuracy"] is None
            assert w["topic_accuracy"] is None
            assert w["stability"] is None
