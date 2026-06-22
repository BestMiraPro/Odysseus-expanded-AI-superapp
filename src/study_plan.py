# src/study_plan.py
"""Deterministic, evidence-based study plan generator.

Builds a day-by-day revision schedule backwards from an exam date following
the best-replicated findings in learning science:

- retrieval practice over re-reading (every contact ends in a recall demand)
- distributed practice: expanding review offsets (1, 3, 7, 14, 30 days),
  compressed proportionally when the exam is close
- interleaving: study blocks rotate 2-4 topics instead of blocking one
- time allocation driven by importance x (low) mastery
- mock exams under real conditions at ~60% of the runway and near the end
- taper: the final day is light review only — consolidation happens in sleep,
  so the plan never schedules heavy acquisition the night before

The generator is pure and deterministic (no LLM, no randomness) so it is
testable and instantly regenerable when inputs change.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Optional

BASE_OFFSETS = [1, 3, 7, 14, 30]   # expanding review cadence (days after first contact)
BASE_HORIZON = 40                  # the cadence above assumes ~40 days of runway
MIN_SESSION_MIN = 20
MOCK_MIN = 60


def _priority(topic: Dict) -> float:
    """importance (1-5) x knowledge gap (1-5). Drives time allocation."""
    importance = max(1, min(5, int(topic.get("importance") or 3)))
    mastery = max(1, min(5, int(topic.get("mastery") or 2)))
    return importance * (6 - mastery)


def _compress_offsets(days_until: int) -> List[int]:
    """Scale the expanding cadence to the actual runway, dedupe, keep order."""
    if days_until >= BASE_HORIZON:
        return list(BASE_OFFSETS)
    scale = days_until / float(BASE_HORIZON)
    seen, out = set(), []
    for off in BASE_OFFSETS:
        d = max(1, round(off * scale))
        if d < days_until and d not in seen:
            seen.add(d)
            out.append(d)
    return out or [1]


def generate_plan(
    exam_date: date,
    topics: List[Dict],
    *,
    start_date: Optional[date] = None,
    hours_per_week: float = 7.0,
    rest_days: Optional[List[int]] = None,  # weekday() ints, e.g. [6] = Sunday
) -> Dict:
    """Return {days: [{date, blocks: [...]}, ...], meta: {...}}.

    Each block: {type, topics, minutes, note}
      type ∈ first_contact | retrieval | interleaved | mock | light_review | cram
    """
    start = start_date or date.today()
    days_until = (exam_date - start).days
    if days_until <= 0:
        raise ValueError("exam_date must be after start_date")
    topics = [t for t in topics if (t.get("name") or "").strip()]
    if not topics:
        raise ValueError("at least one topic is required")

    rest = set(rest_days or [])
    daily_min = max(MIN_SESSION_MIN, round(hours_per_week * 60 / 7 / 5) * 5)

    ranked = sorted(topics, key=_priority, reverse=True)
    names = [t["name"] for t in ranked]

    # study days = every day from start to the day before the exam, minus rest
    # days (but never remove so many that topics can't fit).
    all_days = [start + timedelta(days=i) for i in range(days_until)]
    study_days = [d for d in all_days if d.weekday() not in rest] or all_days

    # ---- cram mode: too short for real spacing — triage honestly ----
    if days_until <= 3:
        top = names[: max(2, min(4, len(names)))]
        days_out = []
        for i, d in enumerate(study_days):
            last = i == len(study_days) - 1
            blocks = []
            if last:
                blocks.append({
                    "type": "light_review",
                    "topics": top,
                    "minutes": min(daily_min, 45),
                    "note": "Light cue-sheet recall only. Protect a full night of sleep — "
                            "recall degrades sharply without it.",
                })
            else:
                blocks.append({
                    "type": "cram",
                    "topics": top,
                    "minutes": daily_min,
                    "note": "Straight to practice questions on the highest-weight topics. "
                            "Skip note-making entirely.",
                })
                if i == 0 and len(study_days) > 1:
                    blocks.append({
                        "type": "mock",
                        "topics": names,
                        "minutes": MOCK_MIN,
                        "note": "One timed full-format pass so the format isn't a surprise.",
                    })
            days_out.append({"date": d.isoformat(), "blocks": blocks})
        return {
            "days": days_out,
            "meta": {
                "mode": "cram",
                "daily_minutes": daily_min,
                "warning": "Runway too short for spaced repetition. This buys the exam, "
                           "not the knowledge — schedule real spacing afterwards if the "
                           "material recurs.",
            },
        }

    # ---- normal mode: spacing backbone ----
    offsets = _compress_offsets(days_until)
    day_topics: Dict[date, Dict[str, set]] = {
        d: {"first": set(), "review": set()} for d in study_days
    }

    # First contact: spread topics over the first ~25% of study days,
    # highest priority first, round-robin so no day is overloaded.
    fc_window = study_days[: max(1, len(study_days) // 4)]
    for i, name in enumerate(names):
        fc_day = fc_window[i % len(fc_window)]
        day_topics[fc_day]["first"].add(name)
        # Expanding reviews from first contact.
        for off in offsets:
            target = fc_day + timedelta(days=off)
            if target >= exam_date:
                continue
            candidates = [d for d in study_days if d >= target]
            if candidates:
                day_topics[candidates[0]]["review"].add(name)

    # Mock exams: one at ~60% of the runway, one 2-3 days out (if room).
    mock_days: List[date] = []
    if days_until >= 7:
        mid = study_days[min(len(study_days) - 1, int(len(study_days) * 0.6))]
        mock_days.append(mid)
        late_target = exam_date - timedelta(days=3)
        late = max((d for d in study_days if d <= late_target), default=None)
        if late and late != mid:
            mock_days.append(late)
    elif days_until >= 5:
        mock_days.append(study_days[-2])

    taper_day = study_days[-1]  # day before exam (or last study day)

    days_out = []
    for d in study_days:
        info = day_topics[d]
        blocks = []
        remaining = daily_min

        if d == taper_day:
            blocks.append({
                "type": "light_review",
                "topics": names[:4],
                "minutes": min(remaining, 45),
                "note": "Light retrieval of cue sheets and prior misses only — no new "
                        "material. Finish early and sleep a full night.",
            })
            days_out.append({"date": d.isoformat(), "blocks": blocks})
            continue

        if d in mock_days:
            blocks.append({
                "type": "mock",
                "topics": names,
                "minutes": min(MOCK_MIN, remaining),
                "note": "Timed, closed-book, full exam format. Predict your score before "
                        "marking; the prediction gap is your calibration data.",
            })
            remaining -= blocks[-1]["minutes"]

        firsts = [n for n in names if n in info["first"]]
        if firsts and remaining >= MIN_SESSION_MIN:
            m = min(remaining, max(MIN_SESSION_MIN, int(daily_min * 0.5)))
            blocks.append({
                "type": "first_contact",
                "topics": firsts,
                "minutes": m,
                "note": "Fast skeleton pass (one read / one worked example), then "
                        "immediately close the source and recall it onto a blank page.",
            })
            remaining -= m

        reviews = [n for n in names if n in info["review"]]
        if reviews and remaining >= MIN_SESSION_MIN:
            m = min(remaining, max(MIN_SESSION_MIN, int(daily_min * 0.4)))
            blocks.append({
                "type": "retrieval",
                "topics": reviews[:4],
                "minutes": m,
                "note": "Closed-book recall and flashcards before re-reading anything. "
                        "Check answers, log why each miss happened.",
            })
            remaining -= m

        if remaining >= MIN_SESSION_MIN:
            # Interleaved practice across 2-4 topics, rotated by day index so
            # pairings vary instead of always drilling the same neighbors.
            k = min(len(names), max(2, min(4, len(names))))
            rot = study_days.index(d) % max(1, len(names))
            mix = [names[(rot + j) % len(names)] for j in range(k)]
            blocks.append({
                "type": "interleaved",
                "topics": mix,
                "minutes": remaining,
                "note": "Mixed practice questions across these topics, attempted "
                        "closed-book. Feels worse than blocking; tests better.",
            })

        days_out.append({"date": d.isoformat(), "blocks": blocks})

    return {
        "days": days_out,
        "meta": {
            "mode": "spaced",
            "daily_minutes": daily_min,
            "offsets": offsets,
            "mock_dates": [d.isoformat() for d in mock_days],
            "taper_date": taper_day.isoformat(),
        },
    }
