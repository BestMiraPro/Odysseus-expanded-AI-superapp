"""Unit tests for src/fsrs.py — the FSRS-4.5 spaced-repetition scheduler.

Pure unit tests: no DB, no network, no FastAPI. They pin the structural
properties the review UI depends on (state transitions, interval ordering,
lapse handling) rather than exact float values, so a parameter refit doesn't
break the suite.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.fsrs import (
    AGAIN, HARD, GOOD, EASY,
    DEFAULT_RETENTION,
    init_difficulty,
    init_stability,
    interval_for_retention,
    preview_intervals,
    retrievability,
    schedule,
)

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _new_card():
    return {"state": "new", "stability": 0.0, "difficulty": 0.0,
            "last_review": None, "reps": 0, "lapses": 0}


# --- memory model basics ------------------------------------------------------

def test_retrievability_at_stability_is_90_percent():
    assert retrievability(10.0, 10.0) == pytest.approx(0.9, abs=1e-9)


def test_retrievability_decreases_with_time():
    assert retrievability(1, 5) > retrievability(10, 5) > retrievability(100, 5)


def test_interval_at_default_retention_equals_stability():
    assert interval_for_retention(7.0, 0.9) == pytest.approx(7.0, abs=1e-9)


def test_lower_retention_gives_longer_intervals():
    assert interval_for_retention(10.0, 0.8) > interval_for_retention(10.0, 0.9)


def test_init_stability_monotonic_in_rating():
    s = [init_stability(r) for r in (AGAIN, HARD, GOOD, EASY)]
    assert s == sorted(s)


def test_init_difficulty_higher_for_again_than_easy():
    assert init_difficulty(AGAIN) > init_difficulty(EASY)


# --- new card transitions -----------------------------------------------------

def test_new_card_again_stays_learning_with_short_delay():
    res = schedule(_new_card(), AGAIN, NOW)
    assert res["state"] == "learning"
    assert res["interval_days"] == 0
    assert NOW < res["due"] <= NOW + timedelta(minutes=30)
    assert res["lapses"] == 0  # learning failures are not lapses


def test_new_card_good_graduates_to_review():
    res = schedule(_new_card(), GOOD, NOW)
    assert res["state"] == "review"
    assert res["interval_days"] >= 1
    assert res["reps"] == 1


def test_new_card_easy_interval_beats_good():
    good = schedule(_new_card(), GOOD, NOW)
    easy = schedule(_new_card(), EASY, NOW)
    assert easy["interval_days"] > good["interval_days"]


# --- review card transitions --------------------------------------------------

def _review_card(stability=10.0, difficulty=5.0, days_ago=10):
    return {"state": "review", "stability": stability, "difficulty": difficulty,
            "last_review": NOW - timedelta(days=days_ago), "reps": 3, "lapses": 0}


def test_review_again_lapses_to_relearning():
    res = schedule(_review_card(), AGAIN, NOW)
    assert res["state"] == "relearning"
    assert res["lapses"] == 1
    assert res["interval_days"] == 0
    assert res["stability"] < 10.0  # forgetting shrinks stability


def test_review_success_grows_stability_and_interval():
    res = schedule(_review_card(), GOOD, NOW)
    assert res["state"] == "review"
    assert res["stability"] > 10.0
    assert res["interval_days"] > 10


def test_review_intervals_ordered_by_rating():
    hard = schedule(_review_card(), HARD, NOW)
    good = schedule(_review_card(), GOOD, NOW)
    easy = schedule(_review_card(), EASY, NOW)
    assert hard["interval_days"] <= good["interval_days"] <= easy["interval_days"]
    assert hard["interval_days"] < easy["interval_days"]


def test_repeated_good_reviews_expand_intervals():
    card = _new_card()
    res = schedule(card, GOOD, NOW)
    intervals = [res["interval_days"]]
    t = NOW
    for _ in range(4):
        t = res["due"]  # review exactly when due
        card = {**card, **res}
        res = schedule(card, GOOD, t)
        intervals.append(res["interval_days"])
    assert intervals == sorted(intervals)
    assert intervals[-1] > intervals[0]


def test_again_then_relearn_interval_shorter_than_before_lapse():
    pre = schedule(_review_card(), GOOD, NOW)["interval_days"]
    lapsed = schedule(_review_card(), AGAIN, NOW)
    relearned = schedule({**_review_card(), **lapsed},
                         GOOD, NOW + timedelta(minutes=5))
    assert relearned["state"] == "review"
    assert relearned["interval_days"] < pre


def test_maximum_interval_is_respected():
    card = _review_card(stability=5000.0, days_ago=300)
    res = schedule(card, EASY, NOW, maximum_interval=365)
    assert res["interval_days"] <= 365


def test_difficulty_stays_clamped():
    card = _new_card()
    res = schedule(card, AGAIN, NOW)
    for _ in range(30):
        res = schedule({**card, **res}, AGAIN, res["due"])
    assert 1.0 <= res["difficulty"] <= 10.0


def test_invalid_rating_raises():
    with pytest.raises(ValueError):
        schedule(_new_card(), 5, NOW)


def test_naive_last_review_treated_as_utc():
    card = _review_card()
    card["last_review"] = (NOW - timedelta(days=10)).replace(tzinfo=None)
    res = schedule(card, GOOD, NOW)
    assert res["state"] == "review"


def test_iso_string_last_review_accepted():
    card = _review_card()
    card["last_review"] = (NOW - timedelta(days=10)).isoformat()
    res = schedule(card, GOOD, NOW)
    assert res["interval_days"] >= 1


# --- preview ------------------------------------------------------------------

def test_preview_returns_all_four_ratings():
    out = preview_intervals(_review_card(), NOW)
    assert set(out) == {AGAIN, HARD, GOOD, EASY}
    assert out[AGAIN].endswith("m")  # lapse goes to minutes-scale relearning


def test_preview_does_not_mutate_card():
    card = _review_card()
    snapshot = dict(card)
    preview_intervals(card, NOW)
    assert card == snapshot
