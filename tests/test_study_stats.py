"""Tests for Phase 0.5: study_stats.py analytics substrate + endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.study_stats import get_daily_breakdown, get_question_accuracy, get_review_counts, get_stats
import core.database as db_mod


OWNER = "alice"
NOW = datetime(2026, 2, 1, 12, 0, 0)
SINCE = NOW - timedelta(days=7)


class _FakeRow:
    """Minimal fake DB row with attribute access."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeDb:
    def __init__(self, *, reviews=None, attempts=None, focus=None, cards=None):
        self._reviews = reviews or []
        self._attempts = attempts or []
        self._focus = focus or []
        self._cards = cards or []

    def query(self, model):
        if model.__name__ == "StudyReview":
            return _FakeQuery(self._reviews)
        if model.__name__ == "StudyAttempt":
            return _FakeQuery(self._attempts)
        if model.__name__ == "StudyFocusSession":
            return _FakeQuery(self._focus)
        if model.__name__ == "StudyCard":
            return _FakeQuery(self._cards)
        return _FakeQuery([])

    def close(self):
        pass


# ---------------------------------------------------------------------------
# 1. unit tests for study_stats helpers
# ---------------------------------------------------------------------------

class TestReviewCounts:
    def test_empty(self):
        db = _FakeDb()
        out = get_review_counts(db, OWNER, SINCE)
        assert out == {"total": 0, "again": 0}

    def test_counts(self):
        db = _FakeDb(reviews=[
            _FakeRow(rating=3, reviewed_at=NOW),
            _FakeRow(rating=1, reviewed_at=NOW),
            _FakeRow(rating=4, reviewed_at=SINCE),
        ])
        out = get_review_counts(db, OWNER, SINCE)
        assert out["total"] == 3
        assert out["again"] == 1

    def test_none_owner_no_filter(self):
        db = _FakeDb(reviews=[
            _FakeRow(rating=1, reviewed_at=NOW, owner=OWNER),
            _FakeRow(rating=1, reviewed_at=NOW, owner="bob"),
        ])
        out = get_review_counts(db, None, SINCE)
        assert out["total"] == 2


class TestQuestionAccuracy:
    def test_empty(self):
        db = _FakeDb()
        out = get_question_accuracy(db, OWNER, SINCE)
        assert out["accuracy"] is None
        assert out["avg_score"] is None

    def test_accuracy(self):
        db = _FakeDb(attempts=[
            _FakeRow(correct=True, score=90),
            _FakeRow(correct=True, score=80),
            _FakeRow(correct=False, score=30),
            _FakeRow(correct=None, score=None),
        ])
        out = get_question_accuracy(db, OWNER, SINCE)
        assert out["total"] == 4
        assert out["correct"] == 2
        assert out["accuracy"] == pytest.approx(0.5)
        assert out["avg_score"] == pytest.approx(66.7, rel=1e-2)


class TestCalibration:
    def test_empty(self):
        db = _FakeDb()
        from src.study_stats import get_calibration
        assert get_calibration(db, OWNER, SINCE) == []

    def test_buckets(self):
        from src.study_stats import get_calibration
        db = _FakeDb(attempts=[
            _FakeRow(confidence=10, correct=True),
            _FakeRow(confidence=25, correct=False),
            _FakeRow(confidence=55, correct=True),
            _FakeRow(confidence=85, correct=True),
            _FakeRow(confidence=95, correct=False),
        ])
        cal = get_calibration(db, OWNER, SINCE)
        assert len(cal) == 4
        labels = {c["label"]: c["total"] for c in cal}
        assert labels["0-19"] == 1
        assert labels["20-39"] == 1
        assert labels["40-59"] == 1
        assert labels["80-99"] == 2


class TestDailyBreakdown:
    def test_empty(self):
        db = _FakeDb()
        daily, reviews, again, focus = get_daily_breakdown(db, OWNER, SINCE, 7)
        assert len(daily) == 8
        d0 = daily[0]
        assert d0["date"] == SINCE.date().isoformat()
        assert d0["reviews"] == 0
        assert reviews == 0

    def test_rolls_up(self):
        db = _FakeDb(
            reviews=[
                _FakeRow(rating=3, reviewed_at=NOW),
                _FakeRow(rating=1, reviewed_at=NOW),
            ],
            focus=[_FakeRow(started_at=NOW, actual_min=30)],
            attempts=[
                _FakeRow(attempted_at=NOW, correct=True),
                _FakeRow(attempted_at=NOW, correct=False),
            ],
        )
        daily, reviews, again, focus_min = get_daily_breakdown(db, OWNER, SINCE, 7)
        today = daily[-1]
        assert today["reviews"] == 2
        assert today["again"] == 1
        assert today["focus_min"] == 30
        assert today["attempts"] == 2
        assert today["attempts_correct"] == 1
        assert reviews == 2
        assert again == 1
        assert focus_min == 30


class TestGetStats:
    def test_shape(self):
        db = _FakeDb(
            reviews=[_FakeRow(rating=3, reviewed_at=NOW)],
            focus=[_FakeRow(started_at=NOW, actual_min=15)],
            attempts=[_FakeRow(attempted_at=NOW, correct=True, score=85, confidence=85)],
            cards=[_FakeRow(id="c1")],
        )
        out = get_stats(db, OWNER, days=7, now=NOW)
        assert "daily" in out
        assert "totals" in out
        assert "calibration" in out
        totals = out["totals"]
        assert totals["reviews"] == 1
        assert totals["success_rate"] == 1.0
        assert totals["cards"] == 1
        assert totals["focus_min"] == 15
        assert totals["attempts"] == 1
        assert totals["accuracy"] == 1.0
        assert totals["avg_score"] == 85.0
        assert len(out["calibration"]) == 1

    def test_days_clamped(self):
        db = _FakeDb(cards=[])
        out = get_stats(db, OWNER, days=3, now=NOW)
        assert len(out["daily"]) == 8  # clamped to 7


# ---------------------------------------------------------------------------
# 2. endpoint contract test
# ---------------------------------------------------------------------------

class TestEndpoint:
    def test_stats_endpoint_json(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routes import study_routes

        app = FastAPI()
        app.include_router(study_routes.setup_study_routes())
        client = TestClient(app)

        captured = {}

        def _fake_get_stats(db, owner, **kwargs):
            captured["owner"] = owner
            return {"daily": [], "totals": {"stub": True}, "calibration": []}

        # _get_stats is imported into study_routes at load time; patch the
        # module-level alias so the route handler sees the replacement.
        monkeypatch.setattr(study_routes, "_get_stats", _fake_get_stats)
        monkeypatch.setattr(study_routes, "get_current_user", lambda _request: OWNER)
        # Provide a fake SessionLocal that yields a trivial session stand-in
        monkeypatch.setattr(study_routes, "SessionLocal", lambda: _FakeDb())

        response = client.get("/api/study/stats?days=30")
        assert response.status_code == 200
        payload = response.json()
        assert "daily" in payload
        assert "totals" in payload
        assert "calibration" in payload
        assert payload["totals"]["stub"] is True

