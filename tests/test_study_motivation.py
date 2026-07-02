"""Tests for Phase 3.3 — Non-coercive motivation.

Verifies:
  (a) build_intention_cues generates one cue per study day with an if-then
      structure anchored to the day's topics.
  (b) generate_plan includes intention_cues in the returned meta.
  (c) The flex-day streak survives one missed day per week (non-coercive).
  (d) Nothing punitive: a missed day never *reduces* prior effort.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.study_plan import generate_plan, build_intention_cues


START = date(2026, 7, 1)
TOPICS = [{"name": "Cells"}, {"name": "Membranes"}, {"name": "Genetics"}]


def _plan(days, **kw):
    return generate_plan(START + timedelta(days=days), TOPICS,
                         start_date=START, **kw)


# --------------------------------------------------------------------------- intention cues

def test_intention_cues_one_per_day():
    days = [{"date": "2026-07-01", "blocks": [{"topics": ["Cells"], "type": "retrieval"}]},
            {"date": "2026-07-02", "blocks": [{"topics": ["Membranes"], "type": "retrieval"}]}]
    cues = build_intention_cues(days)
    assert len(cues) == 2
    assert cues[0]["date"] == "2026-07-01"
    assert "If " in cues[0]["cue"]
    assert "then " in cues[0]["cue"]
    assert "Cells" in cues[0]["cue"]
    assert cues[1]["topics"] == ["Membranes"]


def test_intention_cues_empty_plan():
    assert build_intention_cues([]) == []


def test_intention_cues_in_generated_plan_meta():
    plan = _plan(21)
    assert "intention_cues" in plan["meta"]
    cues = plan["meta"]["intention_cues"]
    assert len(cues) == len(plan["days"])
    # every cue has the if-then structure
    for c in cues:
        assert c["cue"].startswith("If ")
        assert " then I'll spend" in c["cue"]


def test_intention_cues_in_cram_meta():
    plan = _plan(2)  # cram mode
    assert plan["meta"]["mode"] == "cram"
    assert "intention_cues" in plan["meta"]
    assert len(plan["meta"]["intention_cues"]) == len(plan["days"])


def test_intention_cues_deduplicate_topics():
    days = [{"date": "2026-07-01", "blocks": [
        {"topics": ["Cells", "Cells"], "type": "retrieval"},
        {"topics": ["Membranes"], "type": "interleaved"},
    ]}]
    cues = build_intention_cues(days)
    assert cues[0]["topics"] == ["Cells", "Membranes"]  # deduped, order preserved


# --------------------------------------------------------------------------- non-punitive streak (unit test of the algorithm)

def test_flex_day_streak_survives_one_missed_day():
    """The flex-day streak logic should forgive one missed day per week of
    streak — a non-coercive design. We test the algorithm directly since the
    route reads the DB; the logic is: if a gap day is found and the next prior
    day was active, skip it (up to max(1, streak//7) flex days).
    """
    # Simulate: active on Mon, Tue, Wed, off Thu (flex), active Fri, Sat.
    # A naive streak from Sat backward would break at Thu. The flex logic
    # should skip Thu and continue counting Wed-Tue-Mon.
    active_days = {
        date(2026, 7, 6),   # Mon
        date(2026, 7, 7),   # Tue
        date(2026, 7, 8),   # Wed
        # Thu 7/9 — missed (flex)
        date(2026, 7, 10),  # Fri
        date(2026, 7, 11),  # Sat
    }

    # Replicate the flex logic from the overview endpoint
    streak = 0
    flex_used = 0
    probe = max(active_days)
    while True:
        if probe in active_days:
            streak += 1
            probe -= timedelta(days=1)
        else:
            if flex_used < max(1, streak // 7) and (probe - timedelta(days=1)) in active_days:
                flex_used += 1
                probe -= timedelta(days=1)
                continue
            break

    # Should count Fri, Sat, skip Thu (flex), count Wed, Tue, Mon = 5
    assert streak == 5
    assert flex_used == 1


def test_flex_day_streak_breaks_on_two_consecutive_misses():
    """Two consecutive missed days should break the streak (flex forgives one,
    not two)."""
    active_days = {
        date(2026, 7, 6),   # Mon
        date(2026, 7, 7),   # Tue
        # Wed 7/8 — missed
        # Thu 7/9 — missed
        date(2026, 7, 10),  # Fri
    }

    streak = 0
    flex_used = 0
    probe = max(active_days)
    while True:
        if probe in active_days:
            streak += 1
            probe -= timedelta(days=1)
        else:
            if flex_used < max(1, streak // 7) and (probe - timedelta(days=1)) in active_days:
                flex_used += 1
                probe -= timedelta(days=1)
                continue
            break

    # Fri (1), skip Wed? No — Wed is missed, but Thu is also missed, so the
    # "next prior day" check fails → streak stops at 1 (just Fri).
    assert streak == 1


def test_no_punitive_reset_on_missed_today():
    """If today is missed but yesterday was active, the streak should still
    count yesterday backward (the overview starts probing from today, and if
    today isn't active, it steps back one day — non-punitive)."""
    # The overview logic: if probe not in days_with, probe -= 1 day, then count.
    # So missing today alone doesn't reset — it just doesn't add today.
    active_days = {
        date(2026, 7, 9),   # yesterday
        date(2026, 7, 8),
        date(2026, 7, 7),
    }
    today = date(2026, 7, 10)  # not active

    streak = 0
    probe = today
    if probe not in active_days:
        probe -= timedelta(days=1)
    while probe in active_days:
        streak += 1
        probe -= timedelta(days=1)

    assert streak == 3  # yesterday + 2 before, today just not counted yet
