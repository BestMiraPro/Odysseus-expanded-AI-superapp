"""Read-only analytics module for per-user study signals.

Aggregates review counts, question accuracy, and calibration inputs from
StudyReview and StudyAttempt.  All functions are pure reads — no writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.database import StudyAttempt, StudyCard, StudyFocusSession, StudyReview


def get_review_counts(db: Session, owner: Optional[str], since: datetime) -> Dict[str, Any]:
    """Return review totals in the given window."""
    q = db.query(StudyReview).filter(StudyReview.reviewed_at >= since)
    if owner is not None:
        q = q.filter(StudyReview.owner == owner)
    rows = q.all()
    total = len(rows)
    again = sum(1 for r in rows if r.rating == 1)
    return {"total": total, "again": again}


def get_question_accuracy(db: Session, owner: Optional[str], since: datetime) -> Dict[str, Any]:
    """Return question-attempt accuracy stats in the given window."""
    q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= since)
    if owner is not None:
        q = q.filter(StudyAttempt.owner == owner)
    rows = q.all()
    total = len(rows)
    correct = sum(1 for r in rows if r.correct is True)
    with_score = [r.score for r in rows if r.score is not None]
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else None,
        "avg_score": round(sum(with_score) / len(with_score), 1) if with_score else None,
    }


def get_calibration(
    db: Session, owner: Optional[str], since: datetime
) -> List[Dict[str, Any]]:
    """Return confidence buckets for calibration curve (20-point bands).

    Buckets: 0-19, 20-39, 40-59, 60-79, 80-100.
    Kept for backwards compatibility — get_calibration_curve() provides
    the finer-grained decile view.
    """
    q = db.query(StudyAttempt).filter(
        StudyAttempt.attempted_at >= since,
        StudyAttempt.confidence.isnot(None),
    )
    if owner is not None:
        q = q.filter(StudyAttempt.owner == owner)
    rows = q.all()
    buckets: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        conf = r.confidence or 0
        bucket = min(conf // 20, 4)
        if bucket not in buckets:
            buckets[bucket] = {
                "bucket": bucket,
                "label": f"{bucket * 20}-{(bucket + 1) * 20 - 1}",
                "total": 0,
                "correct": 0,
            }
        buckets[bucket]["total"] += 1
        if r.correct:
            buckets[bucket]["correct"] += 1
    result = []
    for b in sorted(buckets):
        d = buckets[b]
        d["accuracy"] = round(d["correct"] / d["total"], 3) if d["total"] else None
        result.append(d)
    return result


def get_calibration_curve(
    db: Session,
    owner: Optional[str],
    since: datetime,
    *,
    min_bin_n: int = 3,
) -> List[Dict[str, Any]]:
    """Return persistent, owner-scoped calibration curve on numeric confidence.

    Bins are deciles (0-9, 10-19, …, 90-100) using the NUMERIC 0-100
    confidence column.  Each bin reports:
      - predicted: midpoint confidence of the bin (e.g. 5 for 0-9)
      - accuracy:  actual fraction correct in that bin
      - total / n: number of attempts
      - low_n:     True when n < min_bin_n (caller may dim/hide)

    Empty data returns [].  Deterministic — bins ordered ascending.
    """
    q = db.query(StudyAttempt).filter(
        StudyAttempt.attempted_at >= since,
        StudyAttempt.confidence.isnot(None),
    )
    if owner is not None:
        q = q.filter(StudyAttempt.owner == owner)
    rows = q.all()
    raw: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        conf = r.confidence or 0
        # clamp to valid range, cap at max bin=9 (90-99) except 100 goes to bin 9
        conf = max(0, min(100, conf))
        bucket = min(conf // 10, 9)  # 0..9
        if bucket not in raw:
            start = bucket * 10
            end = start + 9 if bucket < 9 else 100
            raw[bucket] = {
                "predicted": start + 5,
                "label": f"{start}-{end}",
                "total": 0,
                "correct": 0,
            }
        raw[bucket]["total"] += 1
        if r.correct:
            raw[bucket]["correct"] += 1

    result: List[Dict[str, Any]] = []
    for b in sorted(raw):
        d = raw[b]
        n = d["total"]
        accuracy = round(d["correct"] / n, 3) if n else None
        result.append({
            "predicted": d["predicted"],
            "label": d["label"],
            "accuracy": accuracy,
            "total": n,
            "n": n,
            "low_n": n < min_bin_n,
        })
    return result


def get_daily_breakdown(
    db: Session,
    owner: Optional[str],
    since: datetime,
    days: int,
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    """Return daily stats and aggregated rollup totals.

    The returned list is sorted by ISO date ascending.
    """
    by_day: Dict[str, Dict[str, Any]] = {}
    for i in range(days + 1):
        d = (since + timedelta(days=i)).date().isoformat()
        by_day[d] = {
            "date": d,
            "reviews": 0,
            "again": 0,
            "focus_min": 0,
            "attempts": 0,
            "attempts_correct": 0,
        }

    rev_q = db.query(StudyReview).filter(StudyReview.reviewed_at >= since)
    foc_q = db.query(StudyFocusSession).filter(StudyFocusSession.started_at >= since)
    att_q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= since)
    if owner is not None:
        rev_q = rev_q.filter(StudyReview.owner == owner)
        foc_q = foc_q.filter(StudyFocusSession.owner == owner)
        att_q = att_q.filter(StudyAttempt.owner == owner)

    for r in rev_q.all():
        k = r.reviewed_at.date().isoformat() if r.reviewed_at else None
        if k in by_day:
            by_day[k]["reviews"] += 1
            if r.rating == 1:
                by_day[k]["again"] += 1

    for s in foc_q.all():
        k = s.started_at.date().isoformat() if s.started_at else None
        if k in by_day:
            by_day[k]["focus_min"] += s.actual_min or 0

    for a in att_q.all():
        k = a.attempted_at.date().isoformat() if a.attempted_at else None
        if k in by_day:
            by_day[k]["attempts"] += 1
            if a.correct:
                by_day[k]["attempts_correct"] += 1

    daily = sorted(by_day.values(), key=lambda v: v["date"])
    total_reviews = sum(v["reviews"] for v in daily)
    total_again = sum(v["again"] for v in daily)
    total_focus = sum(v["focus_min"] for v in daily)
    return daily, total_reviews, total_again, total_focus


def get_stats(
    db: Session,
    owner: Optional[str],
    days: int = 42,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Aggregate per-user study signals.

    Returns a dict with:
      - daily: per-day breakdown (reviews, again, focus_min, attempts, attempts_correct)
      - totals: rollup (reviews, success_rate, cards, focus_min, attempts, accuracy, avg_score)
      - calibration: legacy 20-point confidence buckets
      - calibration_curve: decile-based persistent calibration
    """
    days = max(7, min(180, days))
    if now is None:
        now = datetime.utcnow()
    since = now - timedelta(days=days)

    daily, total_reviews, total_again, total_focus = get_daily_breakdown(
        db, owner, since, days
    )

    card_q = db.query(StudyCard)
    if owner is not None:
        card_q = card_q.filter(StudyCard.owner == owner)
    cards = card_q.count()

    accuracy = get_question_accuracy(db, owner, since)
    calibration = get_calibration(db, owner, since)
    calibration_curve = get_calibration_curve(db, owner, since)

    return {
        "daily": daily,
        "totals": {
            "reviews": total_reviews,
            "success_rate": (
                round(1 - total_again / total_reviews, 3) if total_reviews else None
            ),
            "cards": cards,
            "focus_min": total_focus,
            "attempts": accuracy["total"],
            "accuracy": accuracy["accuracy"],
            "avg_score": accuracy["avg_score"],
        },
        "calibration": calibration,
        "calibration_curve": calibration_curve,
    }
