"""Unit tests for src/study_plan.py — the deterministic study plan generator.

Pins the structural guarantees the Plan UI and the learning-science rationale
depend on: spacing, interleaving caps, mock placement, taper, cram triage.
"""
from datetime import date, timedelta

import pytest

from src.study_plan import generate_plan, _compress_offsets

START = date(2026, 6, 11)

TOPICS = [
    {"name": "Microeconomics", "importance": 5, "mastery": 2},
    {"name": "Statistics", "importance": 4, "mastery": 3},
    {"name": "Linear Algebra", "importance": 3, "mastery": 1},
    {"name": "Econometrics", "importance": 5, "mastery": 4},
]


def _plan(days=28, **kw):
    return generate_plan(START + timedelta(days=days), TOPICS,
                         start_date=START, **kw)


# --- input validation ---------------------------------------------------------

def test_exam_in_past_raises():
    with pytest.raises(ValueError):
        generate_plan(START - timedelta(days=1), TOPICS, start_date=START)


def test_no_topics_raises():
    with pytest.raises(ValueError):
        generate_plan(START + timedelta(days=10), [], start_date=START)


def test_blank_topic_names_filtered():
    with pytest.raises(ValueError):
        generate_plan(START + timedelta(days=10), [{"name": "  "}], start_date=START)


# --- structure ----------------------------------------------------------------

def test_plan_never_schedules_on_or_after_exam_day():
    exam = START + timedelta(days=28)
    plan = _plan(28)
    assert all(date.fromisoformat(d["date"]) < exam for d in plan["days"])


def test_every_topic_gets_first_contact():
    plan = _plan(28)
    touched = set()
    for d in plan["days"]:
        for b in d["blocks"]:
            if b["type"] == "first_contact":
                touched.update(b["topics"])
    assert touched == {t["name"] for t in TOPICS}


def test_first_contacts_land_in_first_quarter():
    plan = _plan(28)
    fc_dates = [date.fromisoformat(d["date"]) for d in plan["days"]
                if any(b["type"] == "first_contact" for b in d["blocks"])]
    assert fc_dates
    assert max(fc_dates) <= START + timedelta(days=7)


def test_topics_get_multiple_spaced_retrieval_contacts():
    plan = _plan(28)
    per_topic = {t["name"]: [] for t in TOPICS}
    for d in plan["days"]:
        for b in d["blocks"]:
            if b["type"] == "retrieval":
                for t in b["topics"]:
                    per_topic[t].append(date.fromisoformat(d["date"]))
    # Highest priority topic must be re-tested at least twice, spaced apart.
    days = sorted(per_topic["Linear Algebra"])
    assert len(days) >= 2
    assert (days[-1] - days[0]).days >= 3


def test_interleaved_blocks_cap_at_four_topics():
    plan = _plan(28)
    for d in plan["days"]:
        for b in d["blocks"]:
            if b["type"] == "interleaved":
                assert 2 <= len(b["topics"]) <= 4


def test_mocks_present_and_one_lands_late():
    exam = START + timedelta(days=28)
    plan = _plan(28)
    mock_dates = [date.fromisoformat(d["date"]) for d in plan["days"]
                  if any(b["type"] == "mock" for b in d["blocks"])]
    assert len(mock_dates) >= 2
    assert any(exam - md <= timedelta(days=4) for md in mock_dates)


def test_last_study_day_is_light_taper_only():
    plan = _plan(28)
    last = plan["days"][-1]
    assert [b["type"] for b in last["blocks"]] == ["light_review"]


def test_rest_days_respected():
    plan = _plan(28, rest_days=[6])  # no Sundays
    for d in plan["days"]:
        assert date.fromisoformat(d["date"]).weekday() != 6


def test_daily_minutes_scale_with_hours_per_week():
    lo = _plan(28, hours_per_week=3.5)["meta"]["daily_minutes"]
    hi = _plan(28, hours_per_week=14)["meta"]["daily_minutes"]
    assert hi > lo


def test_deterministic_for_same_inputs():
    assert _plan(28) == _plan(28)


# --- offset compression -------------------------------------------------------

def test_offsets_full_cadence_for_long_runway():
    assert _compress_offsets(60) == [1, 3, 7, 14, 30]


def test_offsets_compress_below_runway():
    offs = _compress_offsets(10)
    assert offs and all(o < 10 for o in offs)
    assert offs == sorted(set(offs))


# --- cram mode ----------------------------------------------------------------

def test_short_runway_enters_cram_mode_with_warning():
    plan = _plan(2)
    assert plan["meta"]["mode"] == "cram"
    assert "warning" in plan["meta"]
    types = {b["type"] for d in plan["days"] for b in d["blocks"]}
    assert "cram" in types
    # Final day still tapers — sleep is protected even when cramming.
    assert plan["days"][-1]["blocks"][0]["type"] == "light_review"


def test_cram_mode_triages_to_top_topics():
    plan = _plan(2)
    for d in plan["days"]:
        for b in d["blocks"]:
            if b["type"] == "cram":
                assert len(b["topics"]) <= 4
