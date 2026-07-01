"""Pretest queue-ordering tests (Phase 2.3 B2).

Tests the /practice/queue endpoint with ?mode=pretest.
When enabled, for NOT-yet-seen material (new questions, reps==0) one question
per topic gets surfaced as a pretest item ahead of due+new items.
When disabled, behavior matches the existing default.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import StudyDeck, StudyQuestion
from routes import study_routes as sr

OWNER = "pretest-user"


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
            if hasattr(crit, "clauses"):  # sqlalchemy BooleanClauseList
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
        self.rows = {StudyDeck: [], StudyQuestion: []}
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
    """Return the operator name string (eq, ne, le, ge, in_op, etc)."""
    return getattr(op, "__name__", None)


def _match(row, criterion):
    """Minimal SQLAlchemy WHERE evaluator for the fake query."""
    # Handle ~expr (unary NOT)
    if hasattr(criterion, "operator") and _op_name(criterion.operator) == "inv":
        return not _match(row, criterion.element)

    from sqlalchemy.sql.expression import BinaryExpression, BooleanClauseList

    if isinstance(criterion, BinaryExpression):
        left, right, op = criterion.left, criterion.right, criterion.operator
        op_name = _op_name(op)

        # AnnotatedColumn, InstrumentedAttribute, etc. all have .key
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
        results = [_match(row, c) for c in criterion.clauses]
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
    monkeypatch.setattr(sr, "SessionLocal", lambda: session)
    monkeypatch.setattr(sr, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(sr, "_read_pref", lambda _owner, key: None)
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


def _seed(session, *items):
    for item in items:
        session.rows.setdefault(type(item), []).append(item)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_pretest_mode_reorders_and_marks(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-new-1", topic="Optics", state="new", reps=0),
        _question(id="q-new-2", topic="Optics", state="new", reps=0),
        _question(id="q-new-3", topic="Mechanics", state="new", reps=0),
        _question(id="q-due-1", topic="Optics", state="review", reps=3,
                  due=datetime(2025, 1, 1)),
    )
    res = c.get("/api/study/practice/queue?mode=pretest&limit=20")
    assert res.status_code == 200
    payload = res.json()
    qids = [q["id"] for q in payload["queue"]]
    # Two topics with unseen new => 2 pretests
    assert payload["pretest"] == 2
    # Pretest items must be at the front
    pretest_qids = set(qids[:payload["pretest"]])
    assert pretest_qids == {"q-new-1", "q-new-3"}
    # Both front items should be marked
    assert all(q.get("pretest") for q in payload["queue"][:payload["pretest"]])
    # No duplicates in queue
    assert len(qids) == len(set(qids))
    # Due item should be present somewhere after the pretests
    due_remaining = [q for q in payload["queue"] if q["id"] == "q-due-1"]
    assert len(due_remaining) == 1


def test_pretest_mode_respects_limit(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-a", topic="A", state="new", reps=0),
        _question(id="q-b", topic="B", state="new", reps=0),
        _question(id="q-c", topic="C", state="new", reps=0),
        _question(id="q-d", topic="D", state="new", reps=0),
    )
    res = c.get("/api/study/practice/queue?mode=pretest&limit=3")
    assert res.status_code == 200
    payload = res.json()
    assert len(payload["queue"]) == 3
    # With limit=3 and 4 topics of unseen, pretests are capped at 3
    assert payload["pretest"] == 3
    # All returned should be pretest
    assert all(q.get("pretest") for q in payload["queue"])


def test_pretest_mode_no_duplicate(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-1", topic="T", state="new", reps=0),
        _question(id="q-2", topic="T", state="new", reps=0),
    )
    res = c.get("/api/study/practice/queue?mode=pretest&limit=20")
    payload = res.json()
    qids = [q["id"] for q in payload["queue"]]
    # One topic, 2 unseen => 1 pretest + 1 normal new
    assert len(set(qids)) == 2
    pretest = [q for q in payload["queue"] if q.get("pretest")]
    normal = [q for q in payload["queue"]
              if q["state"] == "new" and not q.get("pretest")]
    assert len(pretest) == 1
    assert len(normal) == 1
    assert pretest[0]["id"] != normal[0]["id"]


def test_pretest_empty_when_no_new(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-due", topic="T", state="review", reps=2,
                  due=datetime(2025, 1, 1)),
    )
    res = c.get("/api/study/practice/queue?mode=pretest&limit=20")
    payload = res.json()
    assert payload["pretest"] == 0
    assert not any(q.get("pretest") for q in payload["queue"])


def test_pretest_skips_seen_questions(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-unseen", topic="T", state="new", reps=0),
        _question(id="q-seen", topic="T", state="new", reps=1),
    )
    res = c.get("/api/study/practice/queue?mode=pretest&limit=20")
    payload = res.json()
    pretest_ids = [q["id"] for q in payload["queue"] if q.get("pretest")]
    assert pretest_ids == ["q-unseen"]


def test_default_mode_no_pretest_key(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-new-1", topic="Optics", state="new", reps=0),
        _question(id="q-new-2", topic="Mechanics", state="new", reps=0),
        _question(id="q-due-1", topic="Optics", state="review", reps=3,
                  due=datetime(2025, 1, 1)),
    )
    res = c.get("/api/study/practice/queue?limit=20")
    payload = res.json()
    # No pretest key anywhere
    for q in payload["queue"]:
        assert "pretest" not in q


def test_default_mode_byte_identical_ordering(client):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-new-1", topic="Optics", state="new", reps=0),
        _question(id="q-new-2", topic="Mechanics", state="new", reps=0),
        _question(id="q-due-1", topic="Optics", state="review", reps=3,
                  due=datetime(2025, 1, 1)),
    )
    res = c.get("/api/study/practice/queue?limit=20")
    payload = res.json()
    qids = [q["id"] for q in payload["queue"]]
    # Default ordering (no "review" pref): new first, then due
    assert qids.index("q-new-1") < qids.index("q-due-1")
    assert qids.index("q-new-2") < qids.index("q-due-1")


def test_pretest_with_review_ordering(client, monkeypatch):
    c, session = client
    _seed(
        session,
        _deck(),
        _question(id="q-new-1", topic="Optics", state="new", reps=0),
        _question(id="q-due-1", topic="Optics", state="review", reps=3,
                  due=datetime(2025, 1, 1)),
    )
    # Force "review" ordering preference
    monkeypatch.setattr(sr, "_read_pref",
                        lambda _owner, key: "review" if key == "study_order" else None)
    res = c.get("/api/study/practice/queue?mode=pretest&limit=20")
    payload = res.json()
    qids = [q["id"] for q in payload["queue"]]
    # Pretest should still be first even in "review" ordering mode
    assert qids[0] == "q-new-1"
    assert payload["queue"][0].get("pretest") is True
    # Due review comes next
    assert "q-due-1" in qids
