"""Study route sub-module: practice handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def _split_topics(topics) -> List[str]:
    """Normalize a topics filter (list or comma-separated string) to lowercase
    non-empty labels."""
    if not topics:
        return []
    items = topics if isinstance(topics, (list, tuple)) else str(topics).split(",")
    return [t.strip().lower() for t in items if t and t.strip()]


def _round_robin(rows: List, key) -> List:
    """Reorder rows so consecutive items come from different groups, preserving
    each group's internal order. Groups are visited in first-appearance order,
    so the highest-priority group still leads."""
    groups: Dict[str, List] = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out: List = []
    while groups:
        for k in list(groups.keys()):
            out.append(groups[k].pop(0))
            if not groups[k]:
                del groups[k]
    return out


def practice_queue_payload(user, *, deck_id=None, material_id=None, topics=None,
                           limit: int = 20, mock: bool = False,
                           mode=None, adaptive: bool = False) -> Dict:
    """Build the practice queue. Shared by the /practice/queue route and the
    Study agent.

    Scope: all subjects, one subject, one material, and/or a topic filter
    (case-insensitive substring, OR-ed). A topic filter matching nothing is
    dropped (``topic_fallback``) rather than returning an empty session — exam
    plan topics rarely spell the extractor's labels exactly.

    Unscoped sessions round-robin *subjects* as well as topics, so a large
    backlog in one subject cannot crowd the others out of the session.

    ``mock=True`` builds a timed full-format paper instead: a fixed number of
    questions drawn from the whole scope regardless of FSRS state. A mock
    measures where you stand today, so it must be able to ask questions that
    are not due yet.

    ``mode="pretest"`` lifts one unseen question per topic ahead of the rest
    (errorful generation); ``adaptive`` reweights toward weak areas."""
    limit = max(1, min(100, limit))
    wanted = _split_topics(topics)
    db = _common.SessionLocal()
    try:
        now = _common._utcnow_naive()
        base = db.query(StudyQuestion).filter(
            StudyQuestion.suspended == False)  # noqa: E712
        if material_id:
            m = study_service.get_material(db, material_id, user)
            base = base.filter(StudyQuestion.material_id == m.id)
            deck_id = deck_id or m.deck_id
        if deck_id:
            study_service.get_deck(db, deck_id, user)
            base = base.filter(StudyQuestion.deck_id == deck_id)
        if user is not None:
            base = base.filter(StudyQuestion.owner == user)

        scoped_to_one = bool(material_id or deck_id)

        def _pull(q):
            if mock:
                # A mock ignores the schedule: draw the whole scope, oldest
                # first, and let the round-robin below shape the paper.
                return [], q.order_by(StudyQuestion.created_at.asc()).limit(
                    max(limit * 4, 100)).all()
            due_base = q.filter(StudyQuestion.state != "new",
                                StudyQuestion.due <= now)
            if scoped_to_one:
                due_rows = due_base.order_by(
                    StudyQuestion.due.asc()).limit(limit).all()
            else:
                # Round-robin subjects so one subject's backlog cannot crowd the
                # others out. Take a generous window of the most-overdue rows
                # and deal them out per subject, rather than one query per deck.
                pooled = due_base.order_by(StudyQuestion.due.asc()).limit(
                    max(limit * 10, 100)).all()
                due_rows = _round_robin(pooled, lambda r: r.deck_id or "")[:limit]
            new_r = q.filter(StudyQuestion.state == "new").order_by(
                StudyQuestion.created_at.asc()).limit(limit * 3).all()
            return due_rows, new_r

        topic_fallback = False
        if wanted:
            from sqlalchemy import or_
            tq = base.filter(or_(*[StudyQuestion.topic.ilike("%" + t + "%")
                                   for t in wanted]))
            due, new_rows = _pull(tq)
            if not due and not new_rows:
                topic_fallback = True
                due, new_rows = _pull(base)
        else:
            due, new_rows = _pull(base)

        # Interleave across subject *and* topic (within one subject the deck
        # part of the key is constant, leaving the old topic round-robin).
        def _key(r):
            return (r.deck_id or "") + "|" + (r.topic or "general")

        interleaved = _round_robin(new_rows, _key)[:limit]

        # Ordering mode (per-user pref `study_order`): the default sinks
        # answered questions behind unseen ones; "review" puts due reviews
        # first. A mock is a fixed paper, so it keeps its drawn order.
        if mock or _common._read_pref(user, "study_order") == "review":
            queue = due + interleaved
        else:
            queue = interleaved + due

        weak_area_weights = None
        if adaptive:
            since = now - timedelta(days=42)
            candidate_ids = [r.id for r in queue]
            signals = get_weak_question_signals(db, user, since,
                                                question_ids=candidate_ids)
            stab_map = get_question_stability_signal(db, user,
                                                     question_ids=candidate_ids)
            queue = _adaptive_question_priority(
                queue, signals["by_question"], signals["by_topic"], stab_map)
            rows = queue[:limit]
            weak_area_weights = []
            for r in rows:
                topic = getattr(r, "topic", None) or "general"
                weak_area_weights.append({
                    "question_id": r.id,
                    "topic": topic,
                    "accuracy": signals["by_question"].get(r.id, {}).get("accuracy"),
                    "topic_accuracy": signals["by_topic"].get(topic, {}).get("accuracy"),
                    "stability": stab_map.get(r.id),
                })
        else:
            rows = queue[:limit]

        pretest_ids = set()
        if mode == "pretest":
            unseen = {}
            for r in new_rows:
                if (r.state == "new") and (r.reps == 0):
                    unseen.setdefault(r.topic or "general", []).append(r)
            pretest = []
            for _topic, items in unseen.items():
                items.sort(key=lambda x: x.created_at or now)
                pretest.append(items[0])
            pretest_ids = set(r.id for r in pretest)
            rows = [r for r in rows if r.id not in pretest_ids]
            rows = (pretest + rows)[:limit]

        original_links = _question_original_links(db, rows)
        out = []
        for r in rows:
            d = _question_to_dict(r, with_answer=False,
                                  original=original_links.get(r.id))
            if mode == "pretest" and r.id in pretest_ids:
                d["pretest"] = True
            out.append(d)

        resp = {
            "queue": out,
            "due": len(due),
            "total": len(queue),
            "mock": bool(mock),
            "topic_fallback": topic_fallback,
            "pretest": sum(1 for r in rows if r.id in pretest_ids),
        }
        if adaptive:
            resp["adaptive_weights"] = weak_area_weights
        return resp
    finally:
        db.close()


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ question bank (v2)

    @router.get("/decks/{deck_id}/questions")
    def list_questions(request: Request, deck_id: str, q: Optional[str] = None,
                       qtype: Optional[str] = None):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            query = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck.id)
            if user is not None:
                query = query.filter(StudyQuestion.owner == user)
            if qtype in ("mcq", "open"):
                query = query.filter(StudyQuestion.qtype == qtype)
            if q:
                query = query.filter(StudyQuestion.question.ilike(f"%{q}%"))
            rows = query.order_by(StudyQuestion.created_at.desc()).all()
            original_links = _question_original_links(db, rows)
            return {"questions": [_question_to_dict(r, original=original_links.get(r.id))
                                  for r in rows]}
        finally:
            db.close()

    @router.get("/search")
    def cross_subject_search(request: Request, q: str = "", limit: int = 20):
        """Phase 5: global cross-subject search across all questions and cards.

        Searches question text and card fronts/backs across all the user's
        decks — useful for finding related material across subjects.
        """
        user = _owner(request)
        q = q.strip()
        if not q:
            return {"questions": [], "cards": []}
        limit = max(1, min(50, limit))
        db = _common.SessionLocal()
        try:
            q_rows = []
            c_rows = []
            if user is not None:
                q_rows = db.query(StudyQuestion).filter(
                    StudyQuestion.owner == user,
                    StudyQuestion.question.ilike(f"%{q}%"),
                ).order_by(StudyQuestion.created_at.desc()).limit(limit).all()
                c_rows = db.query(StudyCard).filter(
                    StudyCard.owner == user,
                    (StudyCard.front.ilike(f"%{q}%") | StudyCard.back.ilike(f"%{q}%")),
                ).order_by(StudyCard.created_at.desc()).limit(limit).all()
            else:
                q_rows = db.query(StudyQuestion).filter(
                    StudyQuestion.question.ilike(f"%{q}%"),
                ).order_by(StudyQuestion.created_at.desc()).limit(limit).all()
                c_rows = db.query(StudyCard).filter(
                    StudyCard.front.ilike(f"%{q}%") | StudyCard.back.ilike(f"%{q}%"),
                ).order_by(StudyCard.created_at.desc()).limit(limit).all()
            return {
                "questions": [_question_to_dict(r) for r in q_rows],
                "cards": [_card_to_dict(r) for r in c_rows],
            }
        finally:
            db.close()

    @router.put("/questions/{question_id}")
    def update_question(request: Request, question_id: str, body: QuestionUpdate):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if body.question is not None:
                row.question = body.question.strip() or row.question
            if body.options is not None:
                opts = [o.strip() for o in body.options if o.strip()]
                if row.qtype == "mcq" and len(opts) < 2:
                    raise HTTPException(400, "MCQ needs at least 2 options")
                row.options = json.dumps(opts) if opts else None
            if body.correct_index is not None:
                opts = json.loads(row.options) if row.options else []
                if not (0 <= body.correct_index < len(opts)):
                    raise HTTPException(400, "correct_index out of range")
                row.correct_index = body.correct_index
            if body.reference is not None:
                row.reference = body.reference
            if body.topic is not None:
                row.topic = body.topic.strip() or None
            if body.difficulty in ("easy", "medium", "hard"):
                row.difficulty = body.difficulty
            if body.suspended is not None:
                row.suspended = body.suspended
            row.explanation = None if body.options is not None or body.reference is not None \
                else row.explanation
            db.commit()
            material = db.query(StudyMaterial).filter(StudyMaterial.id == row.material_id).first() \
                if row.material_id else None
            return _question_to_dict(row, original=_question_original_link(row, material))
        finally:
            db.close()

    @router.delete("/questions/{question_id}")
    def delete_question(request: Request, question_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            db.query(StudyAttempt).filter(StudyAttempt.question_id == row.id).delete()
            db.delete(row)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ------------------------------------------------------------------ practice (v2)

    @router.get("/practice/queue")
    def practice_queue(request: Request, deck_id: Optional[str] = None,
                       material_id: Optional[str] = None,
                       topics: Optional[str] = None,
                       limit: int = 20, mock: bool = False,
                       mode: Optional[str] = None, adaptive: bool = False):
        """Due questions first (spaced retrieval), then new ones interleaved
        across subject and topic. Optional scope: one subject, one material,
        and/or a comma-separated topic list. ``mock=true`` draws a fixed-size
        paper regardless of the schedule (timed mock exams); ``mode=pretest``
        lifts one unseen question per topic ahead of the rest."""
        return practice_queue_payload(
            _owner(request), deck_id=deck_id, material_id=material_id,
            topics=topics, limit=limit, mock=mock, mode=mode, adaptive=adaptive)

    @router.post("/questions/{question_id}/attempt")
    async def attempt_question(request: Request, question_id: str, body: AttemptIn):
        """Submit an answer. MCQ is checked locally; open answers are AI-graded.
        The outcome maps to an FSRS rating so practice is spaced automatically."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            qtype = row.qtype
            options = json.loads(row.options) if row.options else []
            question_text = row.question
            reference = row.reference or ""
            correct_index = row.correct_index
            if body.idempotency_key:
                prior = db.query(StudyAttempt).filter(
                    StudyAttempt.owner == user,
                    StudyAttempt.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    return {
                        "qtype": prior.qtype,
                        "correct": prior.correct,
                        "score": prior.score,
                        "grading": json.loads(prior.grading) if prior.grading else None,
                        "correct_index": correct_index if prior.qtype == "mcq" else None,
                        "reference": reference,
                        "rating": prior.rating,
                        "interval_days": None,
                        "next_due": _iso(row.due),
                        "require_reengage": False,
                    }
        finally:
            db.close()
        async def _grade_open_answer(answer_text: str, ref_block: str) -> tuple:
            """Reuse the existing open-answer AI grading path (Phase 2.5)."""
            if not answer_text:
                return (0, {"score": 0, "verdict": "incorrect",
                            "feedback": "No answer given. Attempt the recall before "
                                        "checking — even a failed attempt strengthens "
                                        "the memory more than peeking.",
                            "followup": None})
            prompt = (f"QUESTION:\n{question_text}\n\n"
                      f"REFERENCE ANSWER:\n{ref_block}\n\n"
                      f"LEARNER'S ANSWER:\n{answer_text[:8000]}")
            value = await _common._llm_json(user, GRADE_OPEN_SYSTEM, prompt,
                                    temperature=0.2, max_tokens=8000, timeout=180)
            if not isinstance(value, dict):
                raise HTTPException(502, "Model grade was unparseable. Try again.")
            try:
                s = max(0, min(100, int(value.get("score"))))
            except (TypeError, ValueError):
                s = 0
            verdict = value.get("verdict")
            if verdict not in ("correct", "partial", "incorrect"):
                verdict = "correct" if s >= 85 else ("partial" if s >= 40 else "incorrect")
            grading = {"score": s, "verdict": verdict,
                       "feedback": str(value.get("feedback") or "").strip(),
                       "followup": (str(value.get("followup")).strip()
                                    if value.get("followup") else None)}
            return s, grading

        correct = None
        score = None
        grading = None
        answer_text = ""
        require_reengage = False

        if qtype == "mcq" and body.typed_recall:
            # Phase 2.5 typed-recall: free-typed answer for an MCQ, graded via open path
            answer_text = (body.answer or "").strip()
            ref_block = reference if reference.strip() else (
                options[correct_index] if correct_index is not None and options else "")
            score, grading = await _grade_open_answer(answer_text, ref_block)
            correct = (grading["verdict"] == "correct")
        elif qtype == "mcq":
            if body.choice_index is None or not (0 <= body.choice_index < len(options)):
                raise HTTPException(400, "choice_index required for MCQ")
            correct = (body.choice_index == correct_index)
            answer_text = options[body.choice_index]
        else:
            answer_text = (body.answer or "").strip()
            ref_block = reference if reference.strip() else (
                "(no reference available - first work out the correct answer "
                "yourself, then grade the learner's answer against it)")
            score, grading = await _grade_open_answer(answer_text, ref_block)
            correct = (grading["verdict"] == "correct")

        # Phase 2.5 wrong-MCQ gate: backend signal when flag is on and MCQ is wrong
        if body.wrong_mcq_gate and qtype == "mcq" and correct is False:
            require_reengage = True

        confidence = confidence_to_numeric(body.confidence)
        rating = rating_from_outcome(qtype, correct=correct, score=score,
                                     hints_used=body.hints_used or 0,
                                     confidence=confidence)

        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if body.idempotency_key:
                prior = db.query(StudyAttempt).filter(
                    StudyAttempt.owner == user,
                    StudyAttempt.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    return {
                        "qtype": prior.qtype,
                        "correct": prior.correct,
                        "score": prior.score,
                        "grading": json.loads(prior.grading) if prior.grading else None,
                        "correct_index": correct_index if prior.qtype == "mcq" else None,
                        "reference": reference,
                        "rating": prior.rating,
                        "interval_days": None,
                        "next_due": _iso(row.due),
                    }
            user_w = _cached_w(db, user)
            result = _common.fsrs.schedule({
                "state": row.state or "new",
                "stability": _flt(row.stability),
                "difficulty": _flt(row.fsrs_difficulty),
                "last_review": row.last_review,
                "reps": row.reps or 0,
                "lapses": row.lapses or 0,
            }, rating, w=(user_w if user_w is not None else _common.fsrs.DEFAULT_W))
            row.state = result["state"]
            row.stability = str(result["stability"])
            row.fsrs_difficulty = str(result["difficulty"])
            row.due = _to_naive_utc(result["due"])
            row.last_review = _to_naive_utc(result["last_review"])
            row.reps = result["reps"]
            row.lapses = result["lapses"]
            try:
                db.add(StudyAttempt(
                    id=str(uuid.uuid4()), owner=user, question_id=row.id,
                    deck_id=row.deck_id, qtype=qtype, answer=answer_text[:4000],
                    correct=correct, score=score, rating=rating,
                    confidence=confidence, hints_used=body.hints_used or 0,
                    grading=json.dumps(grading) if grading else None,
                    duration_ms=body.duration_ms, attempted_at=_common._utcnow_naive(),
                    idempotency_key=body.idempotency_key,
                ))
                db.commit()
            except IntegrityError:
                db.rollback()
                row = study_service.get_question(db, question_id, user)
                prior = db.query(StudyAttempt).filter(
                    StudyAttempt.owner == user,
                    StudyAttempt.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    prior_grading = json.loads(prior.grading) if prior.grading else None
                    return {
                        "qtype": prior.qtype,
                        "correct": prior.correct,
                        "score": prior.score,
                        "grading": prior_grading,
                        "correct_index": correct_index if prior.qtype == "mcq" else None,
                        "reference": reference,
                        "rating": prior.rating,
                        "interval_days": None,
                        "next_due": _iso(row.due),
                        "require_reengage": False,
                    }
                raise
            return {
                "qtype": qtype,
                "correct": correct,
                "score": score,
                "grading": grading,
                "correct_index": correct_index if qtype == "mcq" else None,
                "reference": reference,
                "rating": rating,
                "interval_days": result["interval_days"],
                "next_due": _iso(row.due),
                "require_reengage": require_reengage,
                "delay_feedback": (confidence is not None and confidence >= 85
                                    and correct is False),
            }
        finally:
            db.close()

    @router.post("/questions/{question_id}/hint")
    async def question_hint(request: Request, question_id: str, body: HintIn):
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        level = max(1, min(3, body.level))
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            question_text = row.question
            options = json.loads(row.options) if row.options else None
            reference = row.reference or ""
        finally:
            db.close()
        opts_txt = ("\nOPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options))) \
            if options else ""
        prompt = (f"HINT LEVEL: {level}\n\nQUESTION:\n{question_text}{opts_txt}\n\n"
                  f"REFERENCE SOLUTION (for your eyes only — do NOT reveal it):\n{reference}")
        hint = await _common._llm_text(user, HINT_SYSTEM, prompt,
                               temperature=0.3, max_tokens=4000, timeout=120)
        return {"level": level, "hint": hint}

    @router.post("/questions/{question_id}/ask")
    async def question_ask(request: Request, question_id: str, body: AskIn):
        """Conversational 'Ask AI' for a practice question. Before the student
        submits (answered=False) it runs in Socratic COACH mode — guidance/hints
        only, never the answer. After they submit it runs in TUTOR mode — full
        explanation. Grounded in the question + reference (for-eyes-only while
        coaching)."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        msg = (body.message or "").strip()
        if not msg:
            raise HTTPException(400, "Empty message")
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            question_text = row.question
            ctx = (row.context or "").strip()
            options = json.loads(row.options) if row.options else None
            reference = row.reference or ""
            correct_index = row.correct_index
        finally:
            db.close()

        opts_txt = ("\nOPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options))) if options else ""
        ctx_txt = f"\nPROBLEM SETUP:\n{ctx}" if ctx else ""
        draft = (body.draft or "").strip()
        if body.answered:
            if body.elaborate:
                system = ASK_ELABORATE_SYSTEM
                mode = "elaborate"
            else:
                system = ASK_TUTOR_SYSTEM
                mode = "tutor"
            ans = ""
            if options is not None and correct_index is not None:
                ans += f"\nCORRECT OPTION INDEX: {correct_index}"
            if reference:
                ans += f"\nREFERENCE SOLUTION:\n{reference}"
            if draft:
                ans += f"\n\nSTUDENT'S SUBMITTED ANSWER:\n{draft}"
        else:
            system = ASK_COACH_SYSTEM
            mode = "coach"
            ans = f"\nREFERENCE SOLUTION (FOR YOUR EYES ONLY — never reveal):\n{reference}" if reference else ""
            if draft:
                ans += f"\n\nStudent's current draft (NOT submitted):\n{draft}"

        convo = ""
        for t in (body.history or [])[-12:]:
            if not isinstance(t, dict):
                continue
            who = "Student" if t.get("role") == "student" else "AI"
            convo += f"{who}: {str(t.get('content', '')).strip()}\n"

        prompt = (f"QUESTION:\n{question_text}{opts_txt}{ctx_txt}{ans}\n\n"
                  f"CONVERSATION SO FAR:\n{convo}Student: {msg}\n\n"
                  f"Reply to the student's latest message.")
        reply = await _common._llm_text(user, system, prompt,
                                temperature=0.3, max_tokens=4000, timeout=120)
        return {"reply": reply, "mode": mode}

    @router.post("/questions/{question_id}/explain")
    async def question_explain(request: Request, question_id: str):
        """Post-attempt explanation for an MCQ (cached on the question)."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if row.explanation:
                return {"explanation": row.explanation, "cached": True}
            if row.qtype != "mcq":
                return {"explanation": row.reference or "", "cached": False}
            question_text = row.question
            options = json.loads(row.options) if row.options else []
            correct_index = row.correct_index
            reference = row.reference or ""
        finally:
            db.close()
        opts_txt = "\n".join(f"{i}. {o}" for i, o in enumerate(options))
        prompt = (f"QUESTION:\n{question_text}\n\nOPTIONS:\n{opts_txt}\n\n"
                  f"CORRECT OPTION INDEX: {correct_index}\n"
                  f"REFERENCE NOTE: {reference}")
        explanation = await _common._llm_text(user, EXPLAIN_SYSTEM, prompt,
                                      temperature=0.2, max_tokens=6000, timeout=120)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            row.explanation = explanation
            db.commit()
        finally:
            db.close()
        return {"explanation": explanation, "cached": False}

    @router.post("/questions/{question_id}/explain-further")
    async def question_explain_further(request: Request, question_id: str,
                                       refresh: bool = False):
        """Material-grounded theory for a question + where to review it.
        Cached on the question; ?refresh=1 regenerates."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            if row.deep_explanation and not refresh:
                return {"explanation": row.deep_explanation, "cached": True}
            q_text, options = row.question, json.loads(row.options) if row.options else None
            reference = row.reference or ""
            deck_id = row.deck_id
            own_material_id = row.material_id
            deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
            subject_name = deck.name if deck else "this subject"
            # Search the WHOLE subject — theory lives in the lecture files, not
            # the practice exam this question was extracted from.
            blocks, by_id = _deck_material_context(db, deck_id, user, theory_only=True)
        finally:
            db.close()

        prompt_parts = [f"QUESTION:\n{q_text}"]
        if options:
            prompt_parts.append("OPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options)))
        if reference:
            prompt_parts.append(f"ANSWER / REFERENCE:\n{reference}")
        if own_material_id and own_material_id in by_id:
            prompt_parts.append(f"(This question was extracted from MATERIAL "
                                f"{own_material_id} — likely a practice exam, not the theory source.)")
        notes_blocks = [f"### {by_id[mid]['name']}\n{by_id[mid]['summary'][:6000]}"
                        for mid in by_id if by_id[mid].get("summary")]
        if notes_blocks:
            prompt_parts.append("--- AI STUDY NOTES (by chapter) ---\n"
                                + "\n\n".join(notes_blocks)[:24000])
        prompt_parts.append("--- SUBJECT MATERIALS ---\n" + ("\n\n".join(blocks)
                            if blocks else "(no source materials available — explain from general theory)"))
        value = await _common._llm_json(user, EXPLAIN_FURTHER_SYSTEM, "\n\n".join(prompt_parts),
                                temperature=0.3, max_tokens=8000, timeout=240,
                                thinking_off=True)
        if not isinstance(value, dict):
            raise HTTPException(502, "The model reply was not usable. Try again.")
        md = _explain_further_markdown(value, by_id)
        if not _resolve_locations(value, by_id):
            md = await _append_web_theory(user, md, q_text, subject_name)
        if not md:
            raise HTTPException(502, "The model did not return an explanation. Try again.")
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            row.deep_explanation = md
            db.commit()
        finally:
            db.close()
        return {"explanation": md, "cached": False}

    @router.post("/questions/{question_id}/locate")
    async def question_locate(request: Request, question_id: str):
        """Consult: find which of the subject's files (and page) hold the content
        needed to answer this question. Returns {locations:[{file_id,name,page,
        label,url}]}; when nothing covers it, generates a hint instead so the
        learner isn't left empty-handed: {locations:[], hint:"..."}."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            q_text = row.question
            options = json.loads(row.options) if row.options else None
            reference = row.reference or ""
            deck_id = row.deck_id
            deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
            subject_name = deck.name if deck else "this subject"
            theory_blocks, by_id = _deck_material_context(db, deck_id, user, theory_only=True)
            all_blocks, _ = _deck_material_context(db, deck_id, user, theory_only=False)
        finally:
            db.close()

        opts_line = ("\nOPTIONS:\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options))) \
            if options else ""

        async def _locate(blocks):
            if not blocks:
                return []
            prompt = (f"QUESTION:\n{q_text}{opts_line}\n\n--- SUBJECT MATERIALS ---\n"
                      + "\n\n".join(blocks))
            try:
                value = await _common._llm_json(user, LOCATE_MATERIAL_SYSTEM, prompt,
                                        temperature=0.2, max_tokens=4000,
                                        timeout=180, thinking_off=True)
                return _resolve_locations(value, by_id) if isinstance(value, dict) else []
            except HTTPException as e:
                if e.status_code == 503:
                    raise
                logger.warning("study locate: %s", e.detail)
                return []

        # Escalate: theory files first, then all files (exercises may hold it).
        locations = await _locate(theory_blocks)
        if not locations and len(all_blocks) > len(theory_blocks):
            locations = await _locate(all_blocks)
        if locations:
            return {"locations": locations, "hint": None, "source": "material"}

        # No course material covers it → search the web for the theory.
        web_md, web_links = await _web_theory(user, f"{q_text}{opts_line}", subject_name)
        if web_links or web_md:
            return {"locations": web_links, "hint": web_md or None, "source": "web"}

        # Web unavailable too — fall back to a plain hint.
        hint_prompt = (f"HINT LEVEL: 1\n\nQUESTION:\n{q_text}{opts_line}\n\n"
                       f"REFERENCE SOLUTION (for your eyes only — do NOT reveal it):\n{reference}")
        try:
            hint = await _common._llm_text(user, HINT_SYSTEM, hint_prompt,
                                   temperature=0.3, max_tokens=4000, timeout=120)
        except HTTPException:
            hint = None
        return {"locations": [], "hint": hint, "source": "hint"}

    @router.post("/cards/{card_id}/explain-further")
    async def card_explain_further(request: Request, card_id: str,
                                   refresh: bool = False):
        """Material-grounded theory for a flashcard. Searches all of the deck's
        materials (theory lives in the lecture files). Cached; ?refresh=1 regenerates."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            if card.deep_explanation and not refresh:
                return {"explanation": card.deep_explanation, "cached": True}
            front, back, notes = card.front, card.back, card.notes or ""
            deck_id = card.deck_id
            deck = db.query(StudyDeck).filter(StudyDeck.id == deck_id).first()
            subject_name = deck.name if deck else "this subject"
            blocks, by_id = _deck_material_context(db, deck_id, user, theory_only=True)
        finally:
            db.close()

        prompt_parts = [f"FLASHCARD FRONT:\n{front}", f"FLASHCARD BACK (answer):\n{back}"]
        if notes:
            prompt_parts.append(f"CARD NOTES:\n{notes}")
        notes_blocks = [f"### {by_id[mid]['name']}\n{by_id[mid]['summary'][:6000]}"
                        for mid in by_id if by_id[mid].get("summary")]
        if notes_blocks:
            prompt_parts.append("--- AI STUDY NOTES (by chapter) ---\n"
                                + "\n\n".join(notes_blocks)[:24000])
        prompt_parts.append("--- SUBJECT MATERIALS ---\n" + ("\n\n".join(blocks)
                            if blocks else "(no source materials available — explain from general theory)"))
        value = await _common._llm_json(user, EXPLAIN_FURTHER_SYSTEM, "\n\n".join(prompt_parts),
                                temperature=0.3, max_tokens=8000, timeout=240,
                                thinking_off=True)
        if not isinstance(value, dict):
            raise HTTPException(502, "The model reply was not usable. Try again.")
        md = _explain_further_markdown(value, by_id)
        if not _resolve_locations(value, by_id):
            md = await _append_web_theory(user, md, f"{front}\n{back}", subject_name)
        if not md:
            raise HTTPException(502, "The model did not return an explanation. Try again.")
        db = _common.SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            card.deep_explanation = md
            db.commit()
        finally:
            db.close()
        return {"explanation": md, "cached": False}

