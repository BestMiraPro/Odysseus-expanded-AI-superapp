"""Tests for the flag-gated, seeded anti-clustering fuzz in src/fsrs.py.

Phase 2.1 (A7): optional ±25% jitter on the computed next *review-state*
interval so reviews don't clump on the same calendar day.

What these tests pin (all four are A7 requirements):

1. FLAG-GATED / DEFAULT OFF
   `schedule(... fuzz=False)` (the default) is byte-identical to the
   pre-fuzz scheduler. Golden + structural suites already cover exactness
   for the default path; here we also directly assert that the unfuzzed
   interval equals the fuzzed path's *base* interval for the same card.

2. SEEDED / DETERMINISTIC
   `fuzz=True` with the same explicit `fuzz_seed` (or the same derivable
   card fields) always yields the same interval — across repeated calls and
   independent of process. No wall-clock RNG.

3. REVIEW-STATE CARDS ONLY
   Learning / relearning steps (`interval_days == 0`) are never fuzzed even
   when `fuzz=True` with a seed. The graduation interval (new → Good/Easy)
   is also not fuzzed — only established review cards on Hard/Good/Easy.

4. CLAMPED TO SANE BOUNDS
   The fuzzed interval stays within [1, maximum_interval] and never below
   1 day for review cards.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.fsrs import (
    AGAIN, HARD, GOOD, EASY,
    DEFAULT_MAX_INTERVAL,
    FUZZ_DEFAULT,
    FUZZ_FRACTION,
    _derive_fuzz_seed,
    _fuzz_interval,
    _next_interval_days,
    _seeded_jitter,
    schedule,
)

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _new_card():
    return {"id": "card_new", "state": "new", "stability": 0.0,
            "difficulty": 0.0, "last_review": None, "reps": 0, "lapses": 0}


def _review_card(stability=10.0, difficulty=5.0, days_ago=10, card_id="card_r1"):
    return {"id": card_id, "state": "review", "stability": stability,
            "difficulty": difficulty,
            "last_review": NOW - timedelta(days=days_ago),
            "reps": 3, "lapses": 0}


# --- 0. gate defaults --------------------------------------------------------

def test_fuzz_is_off_by_default_constant():
    assert FUZZ_DEFAULT is False


def test_fuzz_fraction_is_25_percent():
    assert FUZZ_FRACTION == pytest.approx(0.25, abs=1e-9)


def test_schedule_signature_default_fuzz_false():
    # Calling schedule() without fuzz= must behave as fuzz=False.
    res_default = schedule(_review_card(), GOOD, NOW)
    res_off = schedule(_review_card(), GOOD, NOW, fuzz=False)
    assert res_default["interval_days"] == res_off["interval_days"]


# --- 1. flag-gated / default off → identical to today ------------------------

def test_fuzz_off_matches_unfuzzed_interval():
    card = _review_card()
    res = schedule(card, GOOD, NOW, fuzz=False)
    # The unfuzzed interval must equal the raw helper applied to the
    # *post-review* stability (schedule grows S before computing days).
    base = _next_interval_days(res["stability"], 0.9, DEFAULT_MAX_INTERVAL)
    assert res["interval_days"] == base


def test_fuzz_off_ignores_seed_argument():
    card = _review_card()
    a = schedule(card, GOOD, NOW, fuzz=False, fuzz_seed=12345)
    b = schedule(card, GOOD, NOW, fuzz=False, fuzz_seed=99999)
    # identical: when fuzz is off, seed is never consulted
    assert a == b


def test_default_call_path_is_unchanged_for_review_good():
    # The exact golden value for review/Good (round(35.0839) = 35).
    res = schedule(_review_card(), GOOD, NOW)
    assert res["interval_days"] == 35


# --- 2. seeded / deterministic ------------------------------------------------

def test_seeded_jitter_is_in_range():
    for s in range(0, 4096, 17):
        j = _seeded_jitter(s)
        assert -1.0 <= j < 1.0


def test_seeded_jitter_is_deterministic_for_same_seed():
    assert _seeded_jitter(42) == _seeded_jitter(42)
    # And distinct seeds give different values (statistically expected; we
    # check a few pairs rather than assert strict uniqueness to avoid the
    # rare collision case).
    vals = {_seeded_jitter(s) for s in range(0, 256)}
    assert len(vals) > 200  # overwhelmingly distinct


def test_fuzz_on_same_seed_is_deterministic():
    card = _review_card()
    a = schedule(card, GOOD, NOW, fuzz=True, fuzz_seed=7)
    b = schedule(card, GOOD, NOW, fuzz=True, fuzz_seed=7)
    assert a == b
    assert a["interval_days"] == b["interval_days"]


def test_fuzz_on_explicit_seed_does_not_depend_on_card_id():
    # Same explicit seed → same interval regardless of card identity, because
    # explicit seed overrides _derive_fuzz_seed.
    c1 = _review_card(card_id="aaa")
    c2 = _review_card(card_id="zzz")
    a = schedule(c1, GOOD, NOW, fuzz=True, fuzz_seed=100)
    b = schedule(c2, GOOD, NOW, fuzz=True, fuzz_seed=100)
    assert a["interval_days"] == b["interval_days"]


def test_fuzz_on_derived_seed_is_stable_for_same_card():
    # No explicit seed: seed derived from card id + last_review. Same card →
    # same fuzzed interval across calls (deterministic).
    card = _review_card(card_id="card_X")
    a = schedule(card, GOOD, NOW, fuzz=True)
    b = schedule(card, GOOD, NOW, fuzz=True)
    assert a["interval_days"] == b["interval_days"]


def test_fuzz_on_derived_seed_varies_across_distinct_cards():
    # Different cards should (over a reasonable sample) get different fuzzed
    # intervals — that's the whole point of anti-clustering.
    base = schedule(_review_card(card_id="c0"), GOOD, NOW, fuzz=False)["interval_days"]
    vals = set()
    for i in range(64):
        card = _review_card(card_id=f"c{i}")
        vals.add(schedule(card, GOOD, NOW, fuzz=True)["interval_days"])
    # base is a single integer; the fuzzed set should be noticeably larger
    # than 1 element (it can include base when jitter rounds back to base).
    assert len(vals) > 1
    # And every value must be within ±25% of base (with rounding slack).
    for v in vals:
        assert base * (1 - FUZZ_FRACTION) - 1 <= v <= base * (1 + FUZZ_FRACTION) + 1


# --- 3. review-state cards only ----------------------------------------------

@pytest.mark.parametrize("rating", [HARD, GOOD, EASY])
def test_learning_steps_never_fuzzed(rating):
    # New card rated Again/Hard stays in learning with interval_days == 0.
    # Even with fuzz=True + explicit seed, the learning step must NOT move.
    if rating == AGAIN:
        res = schedule(_new_card(), AGAIN, NOW, fuzz=True, fuzz_seed=1)
        assert res["state"] == "learning"
        assert res["interval_days"] == 0
    elif rating == HARD:
        res = schedule(_new_card(), HARD, NOW, fuzz=True, fuzz_seed=1)
        assert res["state"] == "learning"
        assert res["interval_days"] == 0


def test_new_card_graduation_interval_not_fuzzed():
    # Graduation (new → Good) is NOT a review-state interval per the A7 spec,
    # so it must match the unfuzzed value even with fuzz=True.
    card = _new_card()
    a = schedule(card, GOOD, NOW, fuzz=False)
    b = schedule(card, GOOD, NOW, fuzz=True, fuzz_seed=1)
    assert a["interval_days"] == b["interval_days"]  # both == round(3.7145) == 4
    assert a["interval_days"] == 4


def test_relearning_step_not_fuzzed():
    # Review card lapses (Again) → relearning with a minutes-scale delay,
    # interval_days == 0, NOT fuzzed even with fuzz=True.
    res = schedule(_review_card(), AGAIN, NOW, fuzz=True, fuzz_seed=1)
    assert res["state"] == "relearning"
    assert res["interval_days"] == 0


def test_only_review_state_day_interval_is_fuzzed():
    # For an established review card, Hard/Good/Easy go through the fuzzed
    # path. Different seeds should be able to perturb the interval.
    card = _review_card(stability=10.0)
    base = schedule(card, GOOD, NOW, fuzz=False)["interval_days"]
    perturbed = {
        schedule(card, GOOD, NOW, fuzz=True, fuzz_seed=s)["interval_days"]
        for s in range(0, 512)
    }
    assert len(perturbed) > 1
    for v in perturbed:
        assert base * (1 - FUZZ_FRACTION) - 1 <= v <= base * (1 + FUZZ_FRACTION) + 1


# --- 4. clamped to sane bounds -----------------------------------------------

def test_fuzzed_interval_never_below_one_day():
    # Tiny review interval (stability ~1.0 → base ≈ 1). Fuzz must not push
    # it below 1 day.
    card = _review_card(stability=1.0, days_ago=1)
    for s in range(0, 1024):
        v = schedule(card, GOOD, NOW, fuzz=True, fuzz_seed=s)["interval_days"]
        assert v >= 1


def test_fuzzed_interval_respects_maximum_interval():
    # Very large stability would normally exceed max_interval; fuzz must not
    # lift the result above the cap.
    card = _review_card(stability=5000.0, days_ago=300)
    cap = 365
    for s in range(0, 1024):
        v = schedule(card, EASY, NOW, fuzz=True, fuzz_seed=s,
                     maximum_interval=cap)["interval_days"]
        assert 1 <= v <= cap


def test_fuzz_interval_helper_clamps():
    # Direct unit check on the helper (pre-clamp). schedule() does the clamp.
    base = 100
    # Worst-case negative and positive seeds:
    for s in range(0, 4096):
        v = _fuzz_interval(base, s)
        # raw (pre-clamp) range is [75, 125]; always within ±25%.
        assert int(base * (1 - FUZZ_FRACTION)) - 1 <= v <= int(base * (1 + FUZZ_FRACTION)) + 1


def test_fuzz_on_with_no_card_id_returns_unfuzzed():
    # No 'id' field → _derive_fuzz_seed returns None → no fuzz even when on.
    card = _review_card()
    del card["id"]
    a = schedule(card, GOOD, NOW, fuzz=False)
    b = schedule(card, GOOD, NOW, fuzz=True)  # seed auto-derivation fails
    assert a["interval_days"] == b["interval_days"]


def test_derive_fuzz_seed_returns_int_or_none():
    assert _derive_fuzz_seed({"id": "abc"}) is not None
    assert _derive_fuzz_seed({"id": 42}) is not None
    assert _derive_fuzz_seed({"id": "abc", "last_review": NOW}) is not None
    # Missing id entirely:
    assert _derive_fuzz_seed({"state": "review"}) is None


# --- integration: fuzz shifts due date too ------------------------------------

def test_fuzz_changes_due_date_consistently():
    card = _review_card()
    off = schedule(card, GOOD, NOW, fuzz=False)
    on = schedule(card, GOOD, NOW, fuzz=True, fuzz_seed=314159)
    # Due date offset equals interval_days, so a fuzzed interval shows up
    # as a (possibly equal) due-date difference. At minimum the relationship
    # holds: due == now + interval_days days.
    assert on["due"] == NOW + timedelta(days=on["interval_days"])
    assert off["due"] == NOW + timedelta(days=off["interval_days"])
    # And the fuzzed interval is within the allowed band of the base.
    base = off["interval_days"]
    assert base * (1 - FUZZ_FRACTION) - 1 <= on["interval_days"] <= base * (1 + FUZZ_FRACTION) + 1
