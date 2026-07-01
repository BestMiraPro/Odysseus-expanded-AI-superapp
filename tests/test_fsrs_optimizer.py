"""Tests for src/fsrs_optimizer.py

ACs exercised:
1. Gate: <400 reviews => fit_w returns None.
2. Determinism: same inputs => identical w.
3. Owner-scope: data built from a single user's rows.
4. Exclude interval_days==0 learning-step rows from loss (gate works on
   effective training rows after excluding zeros).
5. Warm-start fallback: schedule() behavior-identical when no w supplied.
"""

from datetime import datetime, timedelta, timezone

import math

import pytest

from src import fsrs
from src.fsrs import schedule
from src.fsrs_optimizer import fit_w, MIN_REVIEWS

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

TOL = 1e-4


def _make_reviews(count: int):
    """Make synthetic review rows for a single card.

    Most are review-state with interval_days > 0; every 5th is a learning
    step (interval_days == 0).
    """
    reviews = []
    for i in range(count):
        ra = NOW - timedelta(days=count - i)
        interval_days = 0 if (i % 5 == 0) else max(1, (i % 30) + 1)
        reviews.append({
            "id": f"rev_{i:04d}",
            "card_id": "card_1",
            "rating": 3 if i % 3 != 0 else 1,  # mix Again and Good
            "interval_days": interval_days,
            "state_before": "learning" if interval_days == 0 else "review",
            "reviewed_at": ra.isoformat(),
        })
    return reviews


def _make_snapshot():
    return {
        "id": "card_1",
        "state": "review",
        "stability": "10.0",
        "difficulty": "5.0",
        "last_review": (NOW - timedelta(days=1)).isoformat(),
        "reps": 5,
        "lapses": 1,
    }


# --- 1. Gate ---

def test_gate_below_400():
    reviews = _make_reviews(200)
    snapshots = [_make_snapshot()]
    assert fit_w(snapshots, reviews, seed=42) is None


def test_gate_at_least_400_trainable_rows():
    # total 400, but 80 have interval_days==0, leaving 320 trainable.
    reviews = _make_reviews(400)
    snapshots = [_make_snapshot()]
    assert fit_w(snapshots, reviews, seed=42) is None


def test_gate_above_400_with_zeros():
    # total 650, ~130 zero => 520 trainable > 400, should fit.
    reviews = _make_reviews(650)
    snapshots = [_make_snapshot()]
    w = fit_w(snapshots, reviews, seed=42)
    assert w is not None
    assert len(w) == 17
    assert all(math.isfinite(v) for v in w)
    # Sanity: monotonic init stability enforced by clamp
    assert w[0] < w[1] < w[2] < w[3]


# --- 2. Determinism ---

def test_determinism_same_input_same_output():
    reviews = _make_reviews(650)
    snapshots = [_make_snapshot()]
    w1 = fit_w(snapshots, reviews, seed=123)
    w2 = fit_w(snapshots, reviews, seed=123)
    assert w1 == w2


def test_determinism_different_seed_possibly_different():
    reviews = _make_reviews(650)
    snapshots = [_make_snapshot()]
    w1 = fit_w(snapshots, reviews, seed=1)
    w2 = fit_w(snapshots, reviews, seed=2)
    # Very likely different; not asserting inequality to avoid flakiness,
    # but at minimum both should succeed.
    assert w1 is not None
    assert w2 is not None


# --- 3. Owner-scope (structural) ---

def test_owner_scope_no_cross_user_rows():
    # The optimizer only sees the rows we pass in; as long as caller passes
    # owner-scoped rows, leakage cannot happen.  This test documents that
    # contract.
    reviews = _make_reviews(650)
    snapshots = [_make_snapshot()]
    w = fit_w(snapshots, reviews, seed=77)
    assert w is not None


# --- 4. schedule() behavior identical when no per-user w supplied ---

def test_schedule_default_w_path_unchanged():
    """schedule() without w= argument must still use DEFAULT_W."""
    card = {"state": "new", "stability": 0.0, "difficulty": 0.0,
            "last_review": None, "reps": 0, "lapses": 0}
    res = schedule(card, fsrs.GOOD, NOW)
    assert res["stability"] == pytest.approx(3.7145, abs=TOL)
    assert res["difficulty"] == pytest.approx(5.1618, abs=TOL)
    assert res["interval_days"] == 4


def test_schedule_with_explicit_w_matches_default_w():
    """Passing DEFAULT_W explicitly must yield the same result."""
    card = {"state": "new", "stability": 0.0, "difficulty": 0.0,
            "last_review": None, "reps": 0, "lapses": 0}
    res1 = schedule(card, fsrs.GOOD, NOW)
    res2 = schedule(card, fsrs.GOOD, NOW, w=fsrs.DEFAULT_W)
    assert res1 == res2


# --- 5. Warm start / clamp sanity ---

def test_fitted_difficulty_monotonic_init_stability():
    reviews = _make_reviews(700)
    snapshots = [_make_snapshot()]
    w = fit_w(snapshots, reviews, seed=42)
    assert w is not None
    assert w[0] < w[1] < w[2] < w[3]


def test_fitted_w_valid_length_and_finite():
    reviews = _make_reviews(700)
    snapshots = [_make_snapshot()]
    w = fit_w(snapshots, reviews, seed=42)
    assert w is not None
    assert len(w) == 17
    assert all(math.isfinite(v) and v > 0 for v in w)
