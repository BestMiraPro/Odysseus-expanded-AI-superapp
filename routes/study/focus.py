"""Study route sub-module: focus handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ focus

    @router.post("/focus/start")
    def focus_start(request: Request, body: FocusStart):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            s = StudyFocusSession(
                id=str(uuid.uuid4()), owner=user, label=body.label,
                planned_min=max(1, min(240, body.planned_min)),
                started_at=_common._utcnow_naive(),
                deck_id=getattr(body, "deck_id", None),
                exam_id=getattr(body, "exam_id", None),
                block_key=getattr(body, "block_key", None),
            )
            db.add(s)
            db.commit()
            return _focus_to_dict(s)
        finally:
            db.close()

    @router.post("/focus/{session_id}/finish")
    def focus_finish(request: Request, session_id: str, body: FocusFinish):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            s = db.query(StudyFocusSession).filter(StudyFocusSession.id == session_id).first()
            if not s or (user is not None and s.owner != user):
                raise HTTPException(404, "Focus session not found")
            s.actual_min = max(0, min(24 * 60, body.actual_min))
            s.completed = body.completed
            s.ended_at = _common._utcnow_naive()
            db.commit()
            return _focus_to_dict(s)
        finally:
            db.close()

    @router.get("/focus/recent")
    def focus_recent(request: Request, days: int = 14):
        user = _owner(request)
        days = max(1, min(90, days))
        db = _common.SessionLocal()
        try:
            since = _common._utcnow_naive() - timedelta(days=days)
            q = db.query(StudyFocusSession).filter(StudyFocusSession.started_at >= since)
            if user is not None:
                q = q.filter(StudyFocusSession.owner == user)
            sessions = q.order_by(StudyFocusSession.started_at.desc()).all()
            return {"sessions": [_focus_to_dict(s) for s in sessions]}
        finally:
            db.close()

    @router.get("/focus/today-blocks")
    def focus_today_blocks(request: Request):
        """Phase 3.2: return today's plan blocks that can be linked to a
        focus session, plus the deck list for subject attribution."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            now = _common._utcnow_naive()
            today_iso = now.date().isoformat()

            deck_q = db.query(StudyDeck).filter(StudyDeck.archived == False)  # noqa: E712
            if user is not None:
                deck_q = deck_q.filter(StudyDeck.owner == user)
            decks = [{"id": d.id, "name": d.name} for d in deck_q.all()]

            exam_q = db.query(StudyExam).filter(StudyExam.archived == False)  # noqa: E712
            if user is not None:
                exam_q = exam_q.filter(StudyExam.owner == user)
            blocks = []
            for e in exam_q.order_by(StudyExam.exam_date.asc()).all():
                plan = json.loads(e.plan) if e.plan else None
                if not plan:
                    continue
                for d in plan.get("days", []):
                    if d.get("date") == today_iso:
                        for i, b in enumerate(d.get("blocks", [])):
                            blocks.append({
                                "exam_id": e.id,
                                "exam_title": e.title,
                                "block_key": f"{today_iso}:{i}",
                                "type": b.get("type"),
                                "topics": b.get("topics", []),
                                "minutes": b.get("minutes", 25),
                            })
            return {"decks": decks, "blocks": blocks}
        finally:
            db.close()

