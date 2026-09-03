"""Study route sub-module: insights handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def overview_payload(user):
    """Dashboard payload: per-subject due/new counts, today's retrievals, streak,
    focus minutes and upcoming exams with today's plan blocks. Shared by the
    /overview route and the Study agent."""
    db = _common.SessionLocal()
    try:
        now = _common._utcnow_naive()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        deck_q = db.query(StudyDeck).filter(StudyDeck.archived == False)  # noqa: E712
        if user is not None:
            deck_q = deck_q.filter(StudyDeck.owner == user)
        decks = deck_q.all()
        deck_rows = []
        due_total = new_total = q_due_total = q_new_total = 0
        for d in decks:
            counts = _deck_counts(db, user, d)
            due_total += counts["due_count"]
            new_total += counts["new_available"]
            q_due_total += counts["q_due"]
            q_new_total += counts["q_new"]
            deck_rows.append({"id": d.id, "name": d.name, "color": d.color,
                              **counts})

        rev_q = db.query(StudyReview)
        if user is not None:
            rev_q = rev_q.filter(StudyReview.owner == user)
        today_reviews = rev_q.filter(StudyReview.reviewed_at >= day_start).all()
        today_again = sum(1 for r in today_reviews if r.rating == 1)
        success = (1 - today_again / len(today_reviews)) if today_reviews else None

        # Streak: consecutive days with >= 1 review, counting back from
        # today (or yesterday if today has none yet).
        recent = rev_q.filter(
            StudyReview.reviewed_at >= now - timedelta(days=120)).all()
        days_with = {r.reviewed_at.date() for r in recent if r.reviewed_at}
        recent_att_q = db.query(StudyAttempt).filter(
            StudyAttempt.attempted_at >= now - timedelta(days=120))
        if user is not None:
            recent_att_q = recent_att_q.filter(StudyAttempt.owner == user)
        days_with |= {t.attempted_at.date() for t in recent_att_q.all()
                      if t.attempted_at}
        streak = 0
        flex_used = 0
        probe = now.date()
        if probe not in days_with:
            probe -= timedelta(days=1)
        while True:
            if probe in days_with:
                streak += 1
                probe -= timedelta(days=1)
            else:
                # Flex day: skip one missed day per week of streak (non-coercive).
                # The streak survives a single missed day if the next prior
                # day was active — i.e. one "off" day per week is forgiven.
                if flex_used < max(1, streak // 7) and (probe - timedelta(days=1)) in days_with:
                    flex_used += 1
                    probe -= timedelta(days=1)
                    continue
                break

        focus_q = db.query(StudyFocusSession).filter(
            StudyFocusSession.started_at >= day_start)
        if user is not None:
            focus_q = focus_q.filter(StudyFocusSession.owner == user)
        focus_min = sum(s.actual_min or 0 for s in focus_q.all())

        exam_q = db.query(StudyExam).filter(StudyExam.archived == False)  # noqa: E712
        if user is not None:
            exam_q = exam_q.filter(StudyExam.owner == user)
        exams = []
        today_iso = now.date().isoformat()
        for e in exam_q.order_by(StudyExam.exam_date.asc()).all():
            try:
                days_left = (date.fromisoformat(e.exam_date) - now.date()).days
            except ValueError:
                days_left = None
            if days_left is not None and days_left < 0:
                continue
            plan = json.loads(e.plan) if e.plan else None
            done = set(json.loads(e.done_blocks) if e.done_blocks else [])
            today_blocks = []
            if plan:
                for d in plan.get("days", []):
                    if d.get("date") == today_iso:
                        for i, b in enumerate(d.get("blocks", [])):
                            today_blocks.append({
                                **b, "key": f"{today_iso}:{i}",
                                "done": f"{today_iso}:{i}" in done,
                            })
            exams.append({"id": e.id, "title": e.title, "exam_date": e.exam_date,
                          "days_left": days_left, "has_plan": bool(plan),
                          "today_blocks": today_blocks})

        att_q = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= day_start)
        if user is not None:
            att_q = att_q.filter(StudyAttempt.owner == user)
        today_attempts = att_q.all()
        att_ok = sum(1 for t in today_attempts
                     if (t.correct is True) or ((t.score or 0) >= 60))

        return {
            "decks": deck_rows,
            "due_total": due_total,
            "new_total": new_total,
            "q_due_total": q_due_total,
            "q_new_total": q_new_total,
            "today": {
                "attempts": len(today_attempts),
                "attempts_ok": att_ok,
                "reviews": len(today_reviews),
                "success_rate": round(success, 3) if success is not None else None,
                "focus_min": focus_min,
                "streak_days": streak,
                "flex_used": flex_used,
            },
            "exams": exams,
        }
    finally:
        db.close()


def history_entries(user, limit: int = 100):
    """Answered-question + card-review log, newest first. Shared by the /history
    route and the Study agent."""
    limit = max(1, min(500, limit))
    db = _common.SessionLocal()
    try:
        aq = db.query(StudyAttempt)
        if user is not None:
            aq = aq.filter(StudyAttempt.owner == user)
        attempts = aq.order_by(StudyAttempt.attempted_at.desc()).limit(limit).all()
        q_ids = {a.question_id for a in attempts}
        qmap = {qq.id: qq for qq in db.query(StudyQuestion).filter(
            StudyQuestion.id.in_(q_ids)).all()} if q_ids else {}

        entries = []
        for a in attempts:
            q = qmap.get(a.question_id)
            grading = None
            if a.grading:
                try:
                    grading = json.loads(a.grading)
                except Exception:
                    grading = None
            entries.append({
                "kind": "question",
                "when": _iso(a.attempted_at),
                "deck_id": a.deck_id,
                "qtype": a.qtype,
                "title": q.question if q else "(question deleted)",
                "answer": a.answer,
                "correct": a.correct,
                "score": a.score,
                "rating": a.rating,
                "confidence": a.confidence,
                "hints_used": a.hints_used or 0,
                "feedback": (grading or {}).get("feedback"),
                "followup": (grading or {}).get("followup"),
                "reference": q.reference if q else None,
            })

        rq = db.query(StudyReview)
        if user is not None:
            rq = rq.filter(StudyReview.owner == user)
        reviews = rq.order_by(StudyReview.reviewed_at.desc()).limit(limit).all()
        c_ids = {r.card_id for r in reviews}
        cmap = {cc.id: cc for cc in db.query(StudyCard).filter(
            StudyCard.id.in_(c_ids)).all()} if c_ids else {}
        for r in reviews:
            c = cmap.get(r.card_id)
            entries.append({
                "kind": "card",
                "when": _iso(r.reviewed_at),
                "deck_id": r.deck_id,
                "title": c.front if c else "(card deleted)",
                "back": c.back if c else None,
                "rating": r.rating,
            })

        entries.sort(key=lambda e: e["when"] or "", reverse=True)
        return {"entries": entries[:limit]}
    finally:
        db.close()


def stats_payload(user, days: int = 42) -> Dict:
    """Daily retrieval chart + totals for the last ``days`` days.

    Counts both kinds of retrieval — card reviews *and* practice-question
    attempts — because practice is where most retrieval happens and a chart fed
    by card reviews alone under-reports the work. Card success and question
    accuracy stay separate signals rather than being blended into one number.
    Shared by the /stats route and the Study agent."""
    db = _common.SessionLocal()
    try:
        return _common._get_stats(db, user, days=days,
                                  now=_common._utcnow_naive())
    finally:
        db.close()


# A stated confidence at or above this counts as "sure", so a miss above it is
# the calibration alarm worth re-testing first.
SURE_THRESHOLD = 80


def _attempt_ok(a) -> bool:
    """Did a practice attempt count as a successful retrieval? MCQs are graded
    right/wrong; open answers pass at the same 60% mark the dashboard uses."""
    return (a.correct is True) or ((a.score or 0) >= 60)


def calibration_payload(user, days: int = 90) -> Dict:
    """Confidence calibration over the last ``days`` days.

    Every practice answer carries a 0-100 confidence, so each attempt is a
    probability forecast that can be scored. Reports the Brier score (mean
    squared error of those forecasts - lower is better, 0.25 is what you get by
    saying 50% to everything), the accuracy curve per confidence band, and the
    "sure but wrong" rate per subject: answers stated at or above
    SURE_THRESHOLD confidence that were still wrong.

    Shared by the /calibration route and the Study agent."""
    days = max(7, min(365, days))
    db = _common.SessionLocal()
    try:
        now = _common._utcnow_naive()
        since = now - timedelta(days=days)
        aq = db.query(StudyAttempt).filter(StudyAttempt.attempted_at >= since)
        if user is not None:
            aq = aq.filter(StudyAttempt.owner == user)
        attempts = aq.all()

        def _conf(a):
            """Stated confidence as a probability, or None when untagged."""
            c = confidence_value(a.confidence)
            if c is None:
                return None
            return min(1.0, max(0.0, c / 100.0))

        graded = [a for a in attempts
                  if _conf(a) is not None
                  and (a.correct is not None or a.score is not None)]

        def _brier(rows):
            if not rows:
                return None
            total = sum((_conf(a) - (1.0 if _attempt_ok(a) else 0.0)) ** 2
                        for a in rows)
            return round(total / len(rows), 3)

        def _rate(hit, n):
            return round(hit / n, 3) if n else None

        sure_rows = [a for a in graded if (_conf(a) or 0) * 100 >= SURE_THRESHOLD]
        sure_wrong_rows = [a for a in sure_rows if not _attempt_ok(a)]

        # Accuracy per 20-point confidence band, stated vs actual.
        bands = {}
        for a in graded:
            b = min(int((_conf(a) * 100) // 20), 4)
            d = bands.setdefault(b, {"bucket": b,
                                     "label": "%d-%d" % (b * 20, (b + 1) * 20 - 1),
                                     "stated": [], "total": 0, "correct": 0})
            d["total"] += 1
            d["stated"].append(_conf(a))
            if _attempt_ok(a):
                d["correct"] += 1
        buckets = []
        for b in sorted(bands):
            d = bands[b]
            stated = sum(d["stated"]) / len(d["stated"])
            accuracy = _rate(d["correct"], d["total"])
            buckets.append({
                "bucket": d["bucket"], "label": d["label"],
                "attempts": d["total"], "correct": d["correct"],
                "expected": round(stated, 3),
                "accuracy": accuracy,
                # Negative = overconfident (claimed more than delivered).
                "gap": round(accuracy - stated, 3) if accuracy is not None else None,
            })

        by_deck_rows = {}
        for a in graded:
            by_deck_rows.setdefault(a.deck_id or "", []).append(a)
        names = {}
        if by_deck_rows:
            dq = db.query(StudyDeck).filter(StudyDeck.id.in_(list(by_deck_rows)))
            names = {d.id: d.name for d in dq.all()}
        by_deck = []
        for did, rows in by_deck_rows.items():
            d_sure = [a for a in rows if (_conf(a) or 0) * 100 >= SURE_THRESHOLD]
            d_sure_wrong = [a for a in d_sure if not _attempt_ok(a)]
            by_deck.append({
                "deck_id": did or None,
                "name": names.get(did, "(subject deleted)"),
                "attempts": len(rows),
                "correct": sum(1 for a in rows if _attempt_ok(a)),
                "sure": len(d_sure),
                "sure_wrong": len(d_sure_wrong),
                "sure_wrong_rate": _rate(len(d_sure_wrong), len(d_sure)),
                "brier": _brier(rows),
            })
        # Worst calibration first - that is where the re-tests belong.
        by_deck.sort(key=lambda r: (-(r["sure_wrong_rate"] or 0), -r["attempts"]))

        return {
            "days": days,
            "overall": {
                "attempts": len(attempts),
                "graded": len(graded),
                "correct": sum(1 for a in graded if _attempt_ok(a)),
                "brier": _brier(graded),
                "sure": len(sure_rows),
                "sure_wrong": len(sure_wrong_rows),
                "sure_wrong_rate": _rate(len(sure_wrong_rows), len(sure_rows)),
            },
            "buckets": buckets,
            "by_deck": by_deck,
            # phase0's finer-grained persistent curve, kept alongside.
            "curve": _common.get_calibration_curve(db, user, since),
        }
    finally:
        db.close()


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ overview + stats

    @router.get("/overview")
    def overview(request: Request):
        """Dashboard payload: per-subject due/new counts, today's retrievals, streak,"""
        return overview_payload(_owner(request))

    @router.get("/stats")
    def stats(request: Request, days: int = 42):
        """Daily retrieval chart (card reviews + practice attempts) and totals."""
        return stats_payload(_owner(request), days=days)

    @router.get("/calibration")
    def calibration(request: Request, days: int = 90):
        """Confidence calibration: Brier score, stated vs actual accuracy per
        band, the sure-but-wrong rate per subject, and the persistent curve."""
        return calibration_payload(_owner(request), days=days)

    @router.get("/history")
    def history(request: Request, limit: int = 100):
        """Answered-question + card-review log, newest first. Shared by the /history"""
        return history_entries(_owner(request), limit=limit)

