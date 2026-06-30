"""Tests for Phase 0.3: numeric confidence capture + calibration prerequisite.

Covers:
- pinned mapping confidence_to_numeric / CONFIDENCE_ENUM_TO_NUMERIC
- rating_from_outcome numeric gate preserves transitional (legacy) behaviour
- StudyAttempt.confidence column accepts numeric values
- AttemptIn accepts Optional[int]
"""

from __future__ import annotations

import pytest

from src.study_ai import (
    CONFIDENCE_ENUM_TO_NUMERIC,
    CONFIDENCE_EASY_THRESHOLD,
    confidence_to_numeric,
    rating_from_outcome,
)


# ---------------------------------------------------------------------------
# 7.1 Pinned mapping (unit)
# ---------------------------------------------------------------------------

class TestConfidenceToNumeric:
    def test_enum_labels(self):
        assert confidence_to_numeric("sure") == 85
        assert confidence_to_numeric("unsure") == 55
        assert confidence_to_numeric("guess") == 25

    def test_none_returns_none(self):
        assert confidence_to_numeric(None) is None

    def test_unrecognized_string_returns_none(self):
        assert confidence_to_numeric("bogus") is None

    def test_numeric_pass_through(self):
        assert confidence_to_numeric(50) == 50
        assert confidence_to_numeric(0) == 0
        assert confidence_to_numeric(100) == 100

    def test_clamping(self):
        assert confidence_to_numeric(150) == 100
        assert confidence_to_numeric(-5) == 0

    def test_rounding(self):
        assert confidence_to_numeric(85.4) == 85
        assert confidence_to_numeric(84.6) == 85
        assert confidence_to_numeric(84.4) == 84

    def test_pinning_regression(self):
        assert CONFIDENCE_ENUM_TO_NUMERIC == {"sure": 85, "unsure": 55, "guess": 25}


# ---------------------------------------------------------------------------
# 7.2 rating_from_outcome Easy-gate (transitional 0.3 behaviour)
# ---------------------------------------------------------------------------

class TestRatingFromOutcome:
    # AC-RG1 (MCQ, numeric)
    def test_mcq_numeric_easy(self):
        assert rating_from_outcome("mcq", correct=True, confidence=85) == 4
        assert rating_from_outcome("mcq", correct=True, confidence=100) == 4
        assert rating_from_outcome("mcq", correct=True, confidence=84) == 3
        assert rating_from_outcome("mcq", correct=True, confidence=25) == 3
        assert rating_from_outcome("mcq", correct=True, confidence=None) == 3

    # AC-RG2 (MCQ, legacy string back-compat)
    def test_mcq_legacy_string_easy(self):
        assert rating_from_outcome("mcq", correct=True, confidence="sure") == 4
        assert rating_from_outcome("mcq", correct=True, confidence="unsure") == 3
        assert rating_from_outcome("mcq", correct=True, confidence="guess") == 3
        assert rating_from_outcome("mcq", correct=True, confidence="bogus") == 3

    # AC-RG3 (open, numeric)
    def test_open_numeric_easy(self):
        assert rating_from_outcome("open", score=95, confidence=85) == 4
        assert rating_from_outcome("open", score=95, confidence=84) == 3
        assert rating_from_outcome("open", score=94, confidence=100) == 3
        assert rating_from_outcome("open", score=95, confidence="sure") == 4

    # AC-RG4 (failure dominates)
    def test_failure_dominates(self):
        assert rating_from_outcome("mcq", correct=False, confidence=100) == 1
        assert rating_from_outcome("open", score=10, confidence=100) == 1

    # AC-RG5 (hint dominates)
    def test_hint_dominates(self):
        assert rating_from_outcome("mcq", correct=True, hints_used=1, confidence=100) == 2
        assert rating_from_outcome("open", score=95, hints_used=1, confidence=100) == 2

    def test_easy_threshold_pin(self):
        assert CONFIDENCE_EASY_THRESHOLD == 85


# ---------------------------------------------------------------------------
# Schema / model persistence (minimal in-memory smoke)
# ---------------------------------------------------------------------------

def test_study_attempt_confidence_column_is_int():
    from core.database import StudyAttempt
    # SQLAlchemy column type
    from sqlalchemy import Integer
    col = StudyAttempt.__table__.columns["confidence"]
    assert isinstance(col.type, Integer)


def test_attempt_in_accepts_numeric():
    from routes.study_routes import AttemptIn
    m = AttemptIn(confidence=73, choice_index=0)
    assert m.confidence == 73
    m2 = AttemptIn(confidence=None)
    assert m2.confidence is None


# ---------------------------------------------------------------------------
# Back-compat: mapping not silently changed
# ---------------------------------------------------------------------------

def test_threshold_equals_sure_mapping():
    # The Easy gate is pinned to the exact same value as the "sure" mapping
    assert CONFIDENCE_EASY_THRESHOLD == CONFIDENCE_ENUM_TO_NUMERIC["sure"]
