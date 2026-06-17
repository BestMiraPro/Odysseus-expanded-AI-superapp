# src/fsrs.py
"""FSRS-4.5 spaced-repetition scheduler (pure Python, no dependencies).

Implements the Free Spaced Repetition Scheduler memory model:
    https://github.com/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm

Cards are plain dicts so callers (SQLAlchemy rows, JSON APIs) don't need a
class hierarchy. The scheduler is deliberately "long-term": every successful
graduation lands on a >= 1 day interval; same-day learning steps are handled
with short fixed delays (minutes) instead of modelling sub-day memory decay,
which FSRS-4.5 does not support anyway.

Ratings (Anki convention):
    1 = Again   (forgot)
    2 = Hard    (recalled with serious difficulty)
    3 = Good    (recalled with some effort — the calibration target)
    4 = Easy    (trivial; interval gets a bonus)

Card states:
    "new"         never studied
    "learning"    seen, not yet graduated to a day-scale interval
    "review"      graduated; scheduled by the FSRS memory model
    "relearning"  lapsed from review; must re-graduate
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

# Default FSRS-4.5 parameters (fit on the open Anki review dataset).
DEFAULT_W = [
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.0310,
    1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.5870, 0.2272,
    2.8755,
]

DECAY = -0.5
FACTOR = 19.0 / 81.0  # 0.9 ** (1 / DECAY) - 1 → R(S, S) == 0.9

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4

# Same-day learning delays (minutes). Sub-day memory isn't modelled by
# FSRS-4.5, so these are pragmatic re-drill delays, not predictions.
LEARN_AGAIN_MIN = 5
LEARN_HARD_MIN = 12

DEFAULT_RETENTION = 0.9
DEFAULT_MAX_INTERVAL = 730  # days

MIN_STABILITY = 0.05
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (DB rows store ISO strings without tz)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def retrievability(elapsed_days: float, stability: float) -> float:
    """Probability of recall after `elapsed_days` given memory `stability`."""
    if stability <= 0:
        return 0.0
    elapsed_days = max(0.0, elapsed_days)
    return (1.0 + FACTOR * elapsed_days / stability) ** DECAY


def interval_for_retention(stability: float, retention: float = DEFAULT_RETENTION) -> float:
    """Days until predicted recall probability drops to `retention`.

    At retention=0.9 this equals stability exactly (by FACTOR construction).
    """
    retention = _clamp(retention, 0.70, 0.99)
    return (stability / FACTOR) * (retention ** (1.0 / DECAY) - 1.0)


def init_stability(rating: int, w=DEFAULT_W) -> float:
    return max(MIN_STABILITY, w[rating - 1])


def init_difficulty(rating: int, w=DEFAULT_W) -> float:
    return _clamp(w[4] - (rating - 3) * w[5], MIN_DIFFICULTY, MAX_DIFFICULTY)


def next_difficulty(difficulty: float, rating: int, w=DEFAULT_W) -> float:
    """Difficulty update with mean reversion toward D0(Easy)."""
    d = difficulty - w[6] * (rating - 3)
    d = w[7] * init_difficulty(EASY, w) + (1.0 - w[7]) * d
    return _clamp(d, MIN_DIFFICULTY, MAX_DIFFICULTY)


def next_recall_stability(difficulty: float, stability: float, r: float,
                          rating: int, w=DEFAULT_W) -> float:
    """Stability after a successful review (Hard/Good/Easy)."""
    hard_penalty = w[15] if rating == HARD else 1.0
    easy_bonus = w[16] if rating == EASY else 1.0
    grow = (
        math.exp(w[8])
        * (11.0 - difficulty)
        * stability ** (-w[9])
        * (math.exp((1.0 - r) * w[10]) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return max(MIN_STABILITY, stability * (1.0 + grow))


def next_forget_stability(difficulty: float, stability: float, r: float,
                          w=DEFAULT_W) -> float:
    """Stability after a lapse (Again on a review card). Capped at current S."""
    s = (
        w[11]
        * difficulty ** (-w[12])
        * ((stability + 1.0) ** w[13] - 1.0)
        * math.exp((1.0 - r) * w[14])
    )
    return max(MIN_STABILITY, min(s, stability))


def _next_interval_days(stability: float, retention: float,
                        maximum_interval: int) -> int:
    days = round(interval_for_retention(stability, retention))
    return int(_clamp(days, 1, maximum_interval))


def schedule(card: Dict, rating: int, now: Optional[datetime] = None, *,
             desired_retention: float = DEFAULT_RETENTION,
             maximum_interval: int = DEFAULT_MAX_INTERVAL,
             w=DEFAULT_W) -> Dict:
    """Apply one review to a card; return the updated scheduling fields.

    `card` needs: state, stability, difficulty, last_review (datetime|None),
    reps (int), lapses (int). Returns a new dict:
    {state, stability, difficulty, due, last_review, reps, lapses,
     interval_days, elapsed_days}
    where `interval_days` is the day-scale interval granted (0 for same-day
    learning steps) and `due` is an aware UTC datetime.
    """
    if rating not in (AGAIN, HARD, GOOD, EASY):
        raise ValueError(f"rating must be 1-4, got {rating!r}")
    now = _ensure_aware(now or utcnow())

    state = card.get("state") or "new"
    stability = float(card.get("stability") or 0.0)
    difficulty = float(card.get("difficulty") or 0.0)
    reps = int(card.get("reps") or 0)
    lapses = int(card.get("lapses") or 0)
    last_review = card.get("last_review")
    if isinstance(last_review, str):
        last_review = datetime.fromisoformat(last_review)
    if last_review is not None:
        last_review = _ensure_aware(last_review)

    elapsed_days = 0.0
    if last_review is not None:
        elapsed_days = max(0.0, (now - last_review).total_seconds() / 86400.0)

    if state == "new":
        stability = init_stability(rating, w)
        difficulty = init_difficulty(rating, w)
    elif state in ("learning", "relearning"):
        # Same-day step: elapsed is ~0 so R≈1; recall growth is tiny, which
        # is the desired "don't reward cramming the same minute" behavior.
        r = retrievability(elapsed_days, stability) if stability > 0 else 0.0
        if rating == AGAIN:
            stability = init_stability(AGAIN, w) if stability <= 0 else \
                next_forget_stability(difficulty, stability, r, w)
        else:
            stability = next_recall_stability(difficulty, max(stability, MIN_STABILITY), r, rating, w)
        difficulty = next_difficulty(difficulty or init_difficulty(rating, w), rating, w)
    else:  # review
        r = retrievability(elapsed_days, stability)
        if rating == AGAIN:
            stability = next_forget_stability(difficulty, stability, r, w)
        else:
            stability = next_recall_stability(difficulty, stability, r, rating, w)
        difficulty = next_difficulty(difficulty, rating, w)

    # --- state transitions + due date ---
    interval_days = 0
    if state in ("new", "learning", "relearning"):
        if rating == AGAIN:
            new_state = "relearning" if state == "relearning" else "learning"
            due = now + timedelta(minutes=LEARN_AGAIN_MIN)
        elif rating == HARD:
            new_state = "relearning" if state == "relearning" else "learning"
            due = now + timedelta(minutes=LEARN_HARD_MIN)
        else:  # Good/Easy graduate to day-scale scheduling
            new_state = "review"
            interval_days = _next_interval_days(stability, desired_retention, maximum_interval)
            if rating == EASY and interval_days == 1:
                interval_days = min(2, maximum_interval)  # Easy must beat Good
            due = now + timedelta(days=interval_days)
    else:  # review
        if rating == AGAIN:
            new_state = "relearning"
            lapses += 1
            due = now + timedelta(minutes=LEARN_AGAIN_MIN)
        else:
            new_state = "review"
            interval_days = _next_interval_days(stability, desired_retention, maximum_interval)
            due = now + timedelta(days=interval_days)

    return {
        "state": new_state,
        "stability": round(stability, 4),
        "difficulty": round(difficulty, 4),
        "due": due,
        "last_review": now,
        "reps": reps + 1,
        "lapses": lapses,
        "interval_days": interval_days,
        "elapsed_days": round(elapsed_days, 4),
    }


def preview_intervals(card: Dict, now: Optional[datetime] = None, *,
                      desired_retention: float = DEFAULT_RETENTION,
                      maximum_interval: int = DEFAULT_MAX_INTERVAL,
                      w=DEFAULT_W) -> Dict[int, str]:
    """Human-readable next-due preview for each rating (for review buttons)."""
    out: Dict[int, str] = {}
    for rating in (AGAIN, HARD, GOOD, EASY):
        res = schedule(dict(card), rating, now,
                       desired_retention=desired_retention,
                       maximum_interval=maximum_interval, w=w)
        if res["interval_days"] <= 0:
            mins = LEARN_AGAIN_MIN if rating == AGAIN else LEARN_HARD_MIN
            out[rating] = f"{mins}m"
        elif res["interval_days"] < 30:
            out[rating] = f"{res['interval_days']}d"
        elif res["interval_days"] < 365:
            out[rating] = f"{res['interval_days'] / 30.44:.1f}mo"
        else:
            out[rating] = f"{res['interval_days'] / 365.25:.1f}y"
    return out
