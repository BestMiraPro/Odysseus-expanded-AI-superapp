"""Per-user FSRS weight (w) optimizer.

Lightweight, numpy-only implementation (no scipy).

Optimization strategy: warm-start from DEFAULT_W, then deterministic
coordinate-wise refinement with fixed fractional steps.  For each candidate
w we re-simulate the full per-card review history from the initial "new"
state so that pre-review stability/difficulty are consistent with the
candidate parameters.

Key design choices:
- Training rows are memory tests: reviews of a card that was in the
  "review" state, after a real gap (elapsed days since the previous review
  > 0). The observed outcome is whether that review succeeded (rating !=
  Again), so lapses count. Learning/relearning steps are fixed same-day
  re-drills, not a stability signal, and are excluded.
  (Selecting rows by the interval a review *granted* was wrong: Again always
  grants 0 days, so every lapse was dropped and every row read as a success,
  which pushed stability up.)
- Gate: >= 400 trainable reviews before fitting.
- Deterministic: inputs sorted by (card_id, reviewed_at, id); RNG seeded.
- Owner-scoped: only caller-passed rows feed the loss.
- No per-row gradient — loss is binned MSE over predicted vs observed recall.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from src import fsrs

MIN_REVIEWS = 400
BINS = 20
SEED = 42
MAX_ITERS = 4


def _ensure_aware(dt):
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_reviews(card_snapshots: List[Dict], reviews: List[Dict]):
    """Group reviews by card and ensure deterministic chronological order.

    Returns {card_id: [reviews]}, plus a mapping of card_id -> snapshot dict.
    """
    snapshots = {}
    for s in card_snapshots:
        cid = s.get("id")
        if cid:
            snapshots[cid] = s

    grouped: Dict[str, List[Dict]] = {}
    for r in reviews:
        cid = r.get("card_id")
        if cid is None:
            continue
        grouped.setdefault(cid, []).append(r)

    for cid in grouped:
        grouped[cid].sort(key=lambda r: (
            str(r.get("reviewed_at", "")),
            str(r.get("id", "")),
        ))

    return grouped, snapshots


def _is_memory_test(state_before: str, elapsed_days: float) -> bool:
    """A review that tests recall after a real gap: the card was in the
    review state and time passed since its previous review."""
    return state_before == "review" and elapsed_days > 0


def _count_trainable(grouped: Dict[str, List[Dict]]) -> int:
    """Training rows per _is_memory_test, from the recorded history alone
    (elapsed days do not depend on w)."""
    n = 0
    for revs in grouped.values():
        prev = None
        for r in revs:
            ra = _ensure_aware(r.get("reviewed_at"))
            elapsed = ((ra - prev).total_seconds() / 86400.0
                       if prev is not None and ra is not None else 0.0)
            if _is_memory_test(r.get("state_before") or "new", elapsed):
                n += 1
            prev = ra
    return n


def _simulate_card(reviews: List[Dict], w: np.ndarray):
    """Simulate a single card's history with a given w.

    Returns numpy arrays (pre-review S, pre-review D, elapsed_days,
    rating, interval_days, state_before).
    """
    n = len(reviews)
    s_pre = np.zeros(n, dtype=np.float64)
    d_pre = np.zeros(n, dtype=np.float64)
    elapsed = np.zeros(n, dtype=np.float64)
    ratings = np.zeros(n, dtype=np.int32)
    ivals = np.zeros(n, dtype=np.int32)
    states = [""] * n

    card = {
        "state": "new",
        "stability": 0.0,
        "difficulty": 0.0,
        "last_review": None,
        "reps": 0,
        "lapses": 0,
    }

    for i, rev in enumerate(reviews):
        ra = _ensure_aware(rev.get("reviewed_at"))
        states[i] = rev.get("state_before") or "new"
        ratings[i] = int(rev.get("rating") or 3)
        ivals[i] = int(rev.get("interval_days") or 0)

        if card["last_review"] is not None and ra is not None:
            elapsed[i] = max(0.0, (ra - card["last_review"]).total_seconds() / 86400.0)
        else:
            elapsed[i] = 0.0

        s_pre[i] = float(card.get("stability") or 0.0)
        d_pre[i] = float(card.get("difficulty") or 0.0)

        # Advance card state
        res = fsrs.schedule(card, ratings[i], now=ra, w=w.tolist())
        card["state"] = res["state"]
        card["stability"] = float(res["stability"])
        card["difficulty"] = float(res["difficulty"])
        card["last_review"] = ra
        card["reps"] = res["reps"]
        card["lapses"] = res["lapses"]

    return s_pre, d_pre, elapsed, ratings, ivals, states


def _training_rows(grouped: Dict, w: np.ndarray):
    """(pre-review S, elapsed days, observed recall) for every memory test
    (see _is_memory_test), simulated under ``w``; None when there are none.
    ``observed`` is 1.0 for a successful review and 0.0 for Again."""
    all_s = []
    all_elapsed = []
    all_rating = []

    for cid, reviews in grouped.items():
        if not reviews:
            continue
        s_pre, _d_pre, elapsed, rating, _ivals, states = _simulate_card(reviews, w)
        in_review = np.array([st == "review" for st in states], dtype=bool)
        # s_pre > 0 drops a history that starts mid-review (no simulated
        # memory state to predict from).
        mask = in_review & (elapsed > 0) & (s_pre > 0)
        if not np.any(mask):
            continue
        all_s.append(s_pre[mask])
        all_elapsed.append(elapsed[mask])
        all_rating.append(rating[mask])

    if not all_s:
        return None
    rating = np.concatenate(all_rating)
    observed = (rating != fsrs.AGAIN).astype(np.float64)
    return np.concatenate(all_s), np.concatenate(all_elapsed), observed


def _loss_for_w(grouped: Dict, w: np.ndarray) -> float:
    """Binned MSE between predicted and observed recall over memory tests,
    binned by (log) elapsed days."""
    FACTOR = fsrs.FACTOR
    DECAY = fsrs.DECAY

    rows = _training_rows(grouped, w)
    if rows is None:
        return 1e9
    s_pre, elapsed, observed = rows
    n = len(s_pre)

    # Predicted R
    r_pred = np.empty(n, dtype=np.float64)
    for i in range(n):
        if s_pre[i] <= 0:
            r_pred[i] = 0.0
        else:
            r_pred[i] = (1.0 + FACTOR * elapsed[i] / s_pre[i]) ** DECAY

    log_min, log_max = math.log(1.0), math.log(730.0)
    bidx = np.empty(n, dtype=np.int32)
    for i in range(n):
        ld = math.log(max(1.0, float(elapsed[i])))
        b = int((ld - log_min) / (log_max - log_min) * (BINS - 1))
        b = max(0, min(BINS - 1, b))
        bidx[i] = b

    bin_pred = np.zeros(BINS, dtype=np.float64)
    bin_obs = np.zeros(BINS, dtype=np.float64)
    bin_cnt = np.zeros(BINS, dtype=np.int32)

    for i in range(n):
        b = bidx[i]
        bin_pred[b] += r_pred[i]
        bin_obs[b] += observed[i]
        bin_cnt[b] += 1

    pop = bin_cnt > 0
    if not np.any(pop):
        return 1e9

    p_mean = bin_pred[pop] / bin_cnt[pop]
    o_mean = bin_obs[pop] / bin_cnt[pop]
    loss = float(np.average((p_mean - o_mean) ** 2, weights=bin_cnt[pop]))
    if not math.isfinite(loss):
        return 1e9
    return loss


def fit_w(card_snapshots: List[Dict], reviews: List[Dict],
          seed: int = SEED) -> Optional[List[float]]:
    """Fit per-user FSRS weights.

    Returns a list of 17 floats or None if the gate (< MIN_REVIEWS memory
    tests, see _is_memory_test) isn't met.
    """
    grouped, _snapshots = _parse_reviews(card_snapshots, reviews)
    if _count_trainable(grouped) < MIN_REVIEWS:
        return None

    rng = np.random.default_rng(seed)

    w = np.array(fsrs.DEFAULT_W, dtype=np.float64)
    best = np.copy(w)
    best_loss = _loss_for_w(grouped, best)

    scales = np.array([
        2.0, 2.0, 2.0, 4.0,      # init S
        1.0, 1.0, 0.5, 0.5,      # difficulty
        1.0, 0.5, 2.0,           # recall S
        2.0, 0.5, 0.5, 1.0,      # forget S
        0.5, 1.0,                # bonuses
    ], dtype=np.float64)

    step_f = 0.02

    for _ in range(MAX_ITERS):
        improved = False
        order = np.arange(len(w))
        rng.shuffle(order)
        for i in order:
            step = step_f * scales[i]
            for sign in (1.0, -1.0):
                cand = np.copy(best)
                cand[i] += sign * step
                cand[i] = max(1e-6, cand[i])
                loss = _loss_for_w(grouped, cand)
                if loss < best_loss:
                    best_loss = loss
                    best = cand
                    improved = True
        if not improved:
            break

    # finer pass
    for _ in range(2):
        improved = False
        for i in range(len(w)):
            step = step_f * 0.5 * scales[i]
            for sign in (1.0, -1.0):
                cand = np.copy(best)
                cand[i] += sign * step
                cand[i] = max(1e-6, cand[i])
                loss = _loss_for_w(grouped, cand)
                if loss < best_loss:
                    best_loss = loss
                    best = cand
                    improved = True
        if not improved:
            break

    # clamp difficulty related
    best[4] = max(1.0, min(10.0, best[4]))
    best[5] = max(0.1, min(5.0, best[5]))
    best[6] = max(0.0, min(2.0, best[6]))
    best[7] = max(0.0, min(1.0, best[7]))

    # ensure monotonic init stability
    for i in range(1, 4):
        if best[i] < best[i - 1]:
            best[i] = best[i - 1] + 0.01

    return [float(v) for v in best]
