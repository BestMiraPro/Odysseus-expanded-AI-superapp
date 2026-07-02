"""Study route sub-module: insights handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ overview + stats

    @router.get("/overview")
    def overview(request: Request):
        user = _owner(request)
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

    @router.get("/stats")
    def stats(request: Request, days: int = 42):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            return _common._get_stats(db, user, days=days, now=_common._utcnow_naive())
        finally:
            db.close()

    @router.get("/calibration")
    def calibration(request: Request, days: int = 42):
        """Persistent, owner-scoped calibration curve (numeric confidence 0-100).

        Returns {curve: [...]} where each item has predicted, accuracy, total, low_n.
        """
        user = _owner(request)
        days = max(7, min(180, days))
        db = _common.SessionLocal()
        try:
            since = _common._utcnow_naive() - timedelta(days=days)
            return {"curve": _common.get_calibration_curve(db, user, since)}
        finally:
            db.close()

    @router.get("/history")
    def history(request: Request, limit: int = 100):
        """Answered-question + card-review log, newest first. Reads the existing
        append-only StudyAttempt / StudyReview tables joined to their text."""
        user = _owner(request)
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

