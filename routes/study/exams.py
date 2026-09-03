"""Study route sub-module: exams handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ exams / plans

    @router.get("/exams")
    def list_exams(request: Request, archived: bool = False):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            q = db.query(StudyExam).filter(StudyExam.archived == archived)
            if user is not None:
                q = q.filter(StudyExam.owner == user)
            exams = q.order_by(StudyExam.exam_date.asc()).all()
            return {"exams": [_exam_to_dict(e) for e in exams]}
        finally:
            db.close()

    @router.post("/exams")
    def create_exam(request: Request, body: ExamCreate):
        user = _owner(request)
        if not body.title.strip():
            raise HTTPException(400, "Title is required")
        try:
            date.fromisoformat(body.exam_date)
        except ValueError:
            raise HTTPException(400, "exam_date must be an ISO date (YYYY-MM-DD)")
        db = _common.SessionLocal()
        try:
            exam = StudyExam(
                id=str(uuid.uuid4()), owner=user, title=body.title.strip(),
                exam_date=body.exam_date, exam_format=body.exam_format,
                hours_per_week=str(body.hours_per_week),
                rest_days=json.dumps(body.rest_days) if body.rest_days else None,
                topics=json.dumps(body.topics or []),
                deck_id=_vet_exam_deck(db, body.deck_id, user),
            )
            db.add(exam)
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.put("/exams/{exam_id}")
    def update_exam(request: Request, exam_id: str, body: ExamUpdate):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            if body.title is not None:
                exam.title = body.title.strip() or exam.title
            if body.exam_date is not None:
                try:
                    date.fromisoformat(body.exam_date)
                except ValueError:
                    raise HTTPException(400, "exam_date must be an ISO date")
                exam.exam_date = body.exam_date
            if body.exam_format is not None:
                exam.exam_format = body.exam_format
            if body.hours_per_week is not None:
                exam.hours_per_week = str(body.hours_per_week)
            if body.rest_days is not None:
                exam.rest_days = json.dumps(body.rest_days)
            if body.topics is not None:
                exam.topics = json.dumps(body.topics)
            if body.deck_id is not None:
                exam.deck_id = _vet_exam_deck(db, body.deck_id, user)
            if body.archived is not None:
                exam.archived = body.archived
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.delete("/exams/{exam_id}")
    def delete_exam(request: Request, exam_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            db.delete(exam)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/exams/{exam_id}/generate-plan")
    def generate_exam_plan(request: Request, exam_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            topics = json.loads(exam.topics) if exam.topics else []
            topic_names = [str(t.get("name") or "").strip() for t in topics]
            topic_names = [n for n in topic_names if n]

            # --- FSRS mastery injection ---
            mastery_scores: Dict[str, float] = {}
            if topic_names:
                # Gather cards for this user (no deck filter: topics may span decks).
                card_rows = db.query(StudyCard).filter(
                    StudyCard.owner == user,
                    StudyCard.suspended == False,
                ).all()
                card_dicts: List[Dict] = []
                for c in card_rows:
                    card_dicts.append(_card_to_dict(c))

                q_rows = db.query(StudyQuestion).filter(
                    StudyQuestion.owner == user,
                    StudyQuestion.suspended == False,
                ).all()
                q_dicts: List[Dict] = []
                for q in q_rows:
                    # use lightweight dict with only fields compute_mastery_scores needs
                    q_dicts.append({
                        "state": q.state or "new",
                        "stability": _flt(q.stability),
                        "topic": q.topic,
                    })
                mastery_scores = compute_mastery_scores(topic_names, card_dicts, q_dicts)

            try:
                # Phase 4.1: compute topic affinity from embeddings for
                # semantic interleaving. Gracefully degrades to round-robin
                # when embeddings are unavailable.
                topic_affinity = _compute_topic_affinity(topic_names)
                plan = generate_plan(
                    date.fromisoformat(exam.exam_date),
                    topics,
                    hours_per_week=_flt(exam.hours_per_week, 7.0),
                    rest_days=json.loads(exam.rest_days) if exam.rest_days else None,
                    mastery_scores=mastery_scores or None,
                    topic_affinity=topic_affinity,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            exam.plan = json.dumps(plan)

            # --- done_blocks preservation on regen ---
            old_done = json.loads(exam.done_blocks) if exam.done_blocks else []
            if isinstance(old_done, list):
                exam.done_blocks = json.dumps(migrate_done_blocks(old_done, plan))
            else:
                exam.done_blocks = json.dumps([])
            db.commit()
            return _exam_to_dict(exam)
        finally:
            db.close()

    @router.post("/exams/{exam_id}/toggle-block")
    def toggle_plan_block(request: Request, exam_id: str, body: ToggleBlockIn):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            exam = study_service.get_exam(db, exam_id, user)
            done = set(json.loads(exam.done_blocks) if exam.done_blocks else [])
            if body.key in done:
                done.discard(body.key)
            else:
                done.add(body.key)
            exam.done_blocks = json.dumps(sorted(done))
            db.commit()
            return {"done_blocks": sorted(done)}
        finally:
            db.close()

