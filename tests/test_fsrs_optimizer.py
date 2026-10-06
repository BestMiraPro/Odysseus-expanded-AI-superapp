"""Tests for src/fsrs_optimizer.py

ACs exercised:
1. Gate: <400 reviews => fit_w returns None.
2. Determinism: same inputs => identical w.
3. Owner-scope: data built from a single user's rows.
4. Train on memory tests only - reviews of a card in the review state after
   a real gap - and count lapses among them (gate works on those rows).
5. Warm-start fallback: schedule() behavior-identical when no w supplied.
"""

from datetime import datetime, timedelta, timezone

import math

import pytest

from src import fsrs
from src.fsrs import schedule
from src.fsrs_optimizer import fit_w, MIN_REVIEWS
from src import fsrs_optimizer

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


# --- 6. Lapses are training data ---

def _history(card_id: str, outcomes):
    """A card that graduates, then is reviewed every 5 days with the given
    outcomes (True = recalled). Each lapse is followed by a relearning step
    10 minutes later, recorded as the scheduler would: Again in review grants
    0 days, which is why the old interval_days > 0 mask dropped every lapse."""
    t = NOW - timedelta(days=5 * (len(outcomes) + 2))
    rows = [{"id": f"{card_id}-0", "card_id": card_id, "rating": 3, "interval_days": 4,
             "state_before": "new", "reviewed_at": t.isoformat()}]
    for i, ok in enumerate(outcomes, start=1):
        t += timedelta(days=5)
        rows.append({"id": f"{card_id}-{i}", "card_id": card_id,
                     "rating": 3 if ok else 1, "interval_days": 5 if ok else 0,
                     "state_before": "review", "reviewed_at": t.isoformat()})
        if not ok:
            rows.append({"id": f"{card_id}-{i}r", "card_id": card_id, "rating": 3,
                         "interval_days": 2, "state_before": "relearning",
                         "reviewed_at": (t + timedelta(minutes=10)).isoformat()})
    return rows


def test_training_rows_include_lapses():
    """Observed recall must reflect the failures. Under the old mask every
    training row read as a success (observed == 1), inflating stability."""
    import numpy as np
    outcomes = [True, False, True, False]
    grouped, _ = fsrs_optimizer._parse_reviews([], _history("c1", outcomes))
    s_pre, elapsed, observed = fsrs_optimizer._training_rows(
        grouped, np.array(fsrs.DEFAULT_W))
    # One row per review-state memory test; the graduation step and the
    # same-day relearning steps are not memory tests.
    assert list(observed) == [1.0, 0.0, 1.0, 0.0]
    assert all(e >= 4.9 for e in elapsed), "a relearning step leaked into training"
    assert all(s > 0 for s in s_pre)


def test_gate_counts_memory_tests_including_lapses():
    """The gate counts exactly the memory tests - lapses included, the
    graduation and relearning steps not."""
    outcomes = [i % 4 == 0 for i in range(MIN_REVIEWS)]   # 75% lapses
    rows = _history("c1", outcomes)
    assert len(rows) > MIN_REVIEWS
    grouped, _ = fsrs_optimizer._parse_reviews([], rows)
    assert fsrs_optimizer._count_trainable(grouped) == MIN_REVIEWS
    grouped, _ = fsrs_optimizer._parse_reviews([], _history("c1", outcomes[:-1]))
    assert fit_w([], [r for revs in grouped.values() for r in revs]) is None


def test_frequent_lapses_lower_the_loss_of_weaker_memory():
    """The loss now sees lapses, so a history with many of them prefers
    parameters that predict lower recall than the defaults do. With the old
    all-success rows, longer stability would always have looked better."""
    import numpy as np
    histories = []
    for c in range(40):
        histories += _history(f"c{c}", [(c + i) % 2 == 0 for i in range(6)])
    grouped, _ = fsrs_optimizer._parse_reviews([], histories)
    base = np.array(fsrs.DEFAULT_W)
    weaker = base.copy()
    weaker[8] -= 1.0       # slower stability growth after a recall
    stronger = base.copy()
    stronger[8] += 1.0
    assert fsrs_optimizer._loss_for_w(grouped, weaker) < \
        fsrs_optimizer._loss_for_w(grouped, base) < \
        fsrs_optimizer._loss_for_w(grouped, stronger)
