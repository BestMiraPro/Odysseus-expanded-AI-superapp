"""Golden-value regression tests for src/fsrs.py — the FSRS-4.5 scheduler.

The companion suite (`tests/test_fsrs_scheduler.py`) deliberately pins only
*structural* properties (state transitions, interval ordering, lapse handling)
so a parameter refit does not break it. That is the right call for the UI
contract, but it has a blind spot: a silent change to `DEFAULT_W` or to any of
the memory-model formulas would still satisfy every structural assertion while
quietly changing every interval a user sees.

This file closes that gap. For a fixed set of (rating, card-state) inputs it
asserts the *exact* resulting stability, difficulty and next interval against
values derived from the FSRS-4.5 reference algorithm
(https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm) with
the current `DEFAULT_W`. Any edit to the weights or the formulas flips these
numbers, so the diff is caught here even when it is invisible to the structural
suite.

Determinism notes:
- `schedule()` rounds stability/difficulty to 4 decimals, so the stored values
  are clean 4-dp floats; we assert against them with a tight absolute tolerance.
- All inputs use a fixed `NOW` and explicit `last_review`, so there is no
  wall-clock, RNG or ordering dependence.
- The expected numbers below were hand-verified against the reference formulas
  (e.g. review/Good: r=0.9 since elapsed==stability, grow≈2.5086 ⇒ S≈35.084;
  difficulty 0.031*D0(Easy)+0.969*5 ⇒ 4.9669). They are not merely a snapshot of
  the implementation's current output.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.fsrs import (
    AGAIN, HARD, GOOD, EASY,
    init_difficulty,
    init_stability,
    schedule,
)

# Tight tolerance: schedule() rounds to 4 dp, so 1e-4 pins the visible value
# while staying robust to binary-float representation noise.
TOL = 1e-4

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _new_card():
    return {"state": "new", "stability": 0.0, "difficulty": 0.0,
            "last_review": None, "reps": 0, "lapses": 0}


def _review_card(stability=10.0, difficulty=5.0, days_ago=10):
    """A graduated card reviewed exactly when due (elapsed == stability ⇒ R=0.9)."""
    return {"state": "review", "stability": stability, "difficulty": difficulty,
            "last_review": NOW - timedelta(days=days_ago), "reps": 3, "lapses": 0}


# --- initial S0 / D0 tables (the raw weights a refit would move) --------------

@pytest.mark.parametrize("rating, expected", [
    (AGAIN, 0.4872),   # w[0]
    (HARD, 1.4003),    # w[1]
    (GOOD, 3.7145),    # w[2]
    (EASY, 13.8206),   # w[3]
])
def test_init_stability_golden(rating, expected):
    assert init_stability(rating) == pytest.approx(expected, abs=TOL)


@pytest.mark.parametrize("rating, expected", [
    (AGAIN, 7.6214),   # w[4] - (1-3)*w[5]
    (HARD, 6.3916),
    (GOOD, 5.1618),    # == w[4]
    (EASY, 3.9320),    # D0(Easy), the mean-reversion target
])
def test_init_difficulty_golden(rating, expected):
    assert init_difficulty(rating) == pytest.approx(expected, abs=TOL)


# --- new card at each rating --------------------------------------------------
# New cards take S0/D0 directly; Good/Easy graduate with interval = round(S0).

def test_new_again_golden():
    res = schedule(_new_card(), AGAIN, NOW)
    assert res["state"] == "learning"
    assert res["stability"] == pytest.approx(0.4872, abs=TOL)
    assert res["difficulty"] == pytest.approx(7.6214, abs=TOL)
    assert res["interval_days"] == 0           # same-day re-drill
    assert res["elapsed_days"] == pytest.approx(0.0, abs=TOL)


def test_new_hard_golden():
    res = schedule(_new_card(), HARD, NOW)
    assert res["state"] == "learning"
    assert res["stability"] == pytest.approx(1.4003, abs=TOL)
    assert res["difficulty"] == pytest.approx(6.3916, abs=TOL)
    assert res["interval_days"] == 0


def test_new_good_golden():
    res = schedule(_new_card(), GOOD, NOW)
    assert res["state"] == "review"
    assert res["stability"] == pytest.approx(3.7145, abs=TOL)
    assert res["difficulty"] == pytest.approx(5.1618, abs=TOL)
    assert res["interval_days"] == 4           # round(3.7145)


def test_new_easy_golden():
    res = schedule(_new_card(), EASY, NOW)
    assert res["state"] == "review"
    assert res["stability"] == pytest.approx(13.8206, abs=TOL)
    assert res["difficulty"] == pytest.approx(3.9320, abs=TOL)
    assert res["interval_days"] == 14          # round(13.8206)


# --- review-state card (S=10, D=5, reviewed when due) at each rating ----------
# These exercise the recall/forget stability growth + difficulty mean-reversion
# formulas, which the structural suite never pins to a number.

def test_review_again_golden():
    res = schedule(_review_card(), AGAIN, NOW)
    assert res["state"] == "relearning"
    assert res["lapses"] == 1
    # forget stability: w11*D^-w12*((S+1)^w13-1)*exp((1-R)*w14), capped at S.
    assert res["stability"] == pytest.approx(2.5604, abs=TOL)
    assert res["difficulty"] == pytest.approx(6.7062, abs=TOL)
    assert res["interval_days"] == 0           # lapse -> minutes-scale relearning


def test_review_hard_golden():
    res = schedule(_review_card(), HARD, NOW)
    assert res["state"] == "review"
    assert res["stability"] == pytest.approx(15.6991, abs=TOL)
    assert res["difficulty"] == pytest.approx(5.8366, abs=TOL)
    assert res["interval_days"] == 16          # round(15.6991)


def test_review_good_golden():
    res = schedule(_review_card(), GOOD, NOW)
    assert res["state"] == "review"
    # recall stability: grow = e^w8*(11-D)*S^-w9*(e^((1-R)w10)-1) ⇒ S≈35.084.
    assert res["stability"] == pytest.approx(35.0839, abs=TOL)
    # difficulty: 0.031*D0(Easy) + 0.969*5 = 4.9669 (mean reversion).
    assert res["difficulty"] == pytest.approx(4.9669, abs=TOL)
    assert res["interval_days"] == 35          # round(35.0839)


def test_review_easy_golden():
    res = schedule(_review_card(), EASY, NOW)
    assert res["state"] == "review"
    # Easy applies the w[16] hard/easy bonus on top of recall growth.
    assert res["stability"] == pytest.approx(82.1287, abs=TOL)
    assert res["difficulty"] == pytest.approx(4.0972, abs=TOL)
    assert res["interval_days"] == 82          # round(82.1287)


# --- cross-check: ratings stay strictly ordered AND exact -------------------
# Belt-and-suspenders — if a refit nudged the weights just enough to preserve
# ordering (which the structural suite checks) the exact values above still
# fail, but this makes the intent explicit in one place.

def test_review_stability_exact_and_ordered():
    again = schedule(_review_card(), AGAIN, NOW)["stability"]
    hard = schedule(_review_card(), HARD, NOW)["stability"]
    good = schedule(_review_card(), GOOD, NOW)["stability"]
    easy = schedule(_review_card(), EASY, NOW)["stability"]
    assert (again, hard, good, easy) == pytest.approx(
        (2.5604, 15.6991, 35.0839, 82.1287), abs=TOL)
    assert again < hard < good < easy
