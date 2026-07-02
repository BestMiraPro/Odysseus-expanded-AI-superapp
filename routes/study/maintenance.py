"""Study route sub-module: maintenance handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    @router.post("/reformat")
    async def reformat_text(request: Request):
        """One-time: reformat existing questions and cards to LaTeX (math) +
        Markdown, preserving content. Idempotent — items already using $ are
        skipped. Touches only text fields; answers/correct_index are untouched."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            qq = db.query(StudyQuestion)
            cc = db.query(StudyCard)
            if user is not None:
                qq = qq.filter(StudyQuestion.owner == user)
                cc = cc.filter(StudyCard.owner == user)
            q_items, q_opts = [], {}
            for q in qq.all():
                opts = json.loads(q.options) if q.options else None
                if not _needs_reformat(q.question, q.reference or "",
                                       " ".join(opts or [])):
                    continue
                item = {"id": q.id, "question": q.question}
                if opts:
                    item["options"] = opts
                if q.reference:
                    item["reference"] = q.reference
                q_items.append(item)
                q_opts[q.id] = opts
            c_items = []
            for c in cc.all():
                if not _needs_reformat(c.front, c.back):
                    continue
                c_items.append({"id": c.id, "front": c.front, "back": c.back})
        finally:
            db.close()

        q_updates = await _reformat_items(user, q_items)
        c_updates = await _reformat_items(user, c_items)

        q_n = c_n = 0
        db = _common.SessionLocal()
        try:
            for q in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(list(q_updates)))
                      .all() if q_updates else []):
                u = q_updates.get(q.id) or {}
                changed = False
                if isinstance(u.get("question"), str) and u["question"].strip():
                    q.question = u["question"]; changed = True
                if "reference" in u and isinstance(u["reference"], str):
                    q.reference = u["reference"]; changed = True
                # Only replace options if the count matches (keeps correct_index valid).
                orig = q_opts.get(q.id)
                if isinstance(u.get("options"), list) and orig and len(u["options"]) == len(orig):
                    q.options = json.dumps([str(o) for o in u["options"]]); changed = True
                if changed:
                    q.explanation = None  # cached MCQ explanation may be stale
                    q_n += 1
            for c in (db.query(StudyCard).filter(StudyCard.id.in_(list(c_updates)))
                      .all() if c_updates else []):
                u = c_updates.get(c.id) or {}
                if isinstance(u.get("front"), str) and u["front"].strip():
                    c.front = u["front"]
                if isinstance(u.get("back"), str) and u["back"].strip():
                    c.back = u["back"]
                c_n += 1
            db.commit()
        finally:
            db.close()
        return {"questions_reformatted": q_n, "cards_reformatted": c_n,
                "questions_scanned": len(q_items), "cards_scanned": len(c_items)}

    @router.post("/decks/{deck_id}/link-parts")
    async def link_parts(request: Request, deck_id: str):
        """Group multi-part problems (per material) and store each part's
        prerequisites — the earlier parts of the same problem. Practice then
        shows those earlier parts + your answers as exam-style context."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            study_service.get_deck(db, deck_id, user)
        finally:
            db.close()
        return await _link_deck_parts(user, deck_id)

    @router.post("/dedup")
    def dedup_questions_route(request: Request):
        """Remove duplicate questions across the caller's decks — the same exam
        part extracted twice (e.g. once plain, once in LaTeX) that older
        cross-run dedupe missed. Keeps the best copy of each cluster. Returns the
        number deleted per deck."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            dq = db.query(StudyDeck)
            if user is not None:
                dq = dq.filter(StudyDeck.owner == user)
            deck_ids = [d.id for d in dq.all()]
            out = {}
            total = 0
            for did in deck_ids:
                n = _dedup_deck_questions(db, did, user)
                if n:
                    out[did] = n
                    total += n
        finally:
            db.close()
        return {"deleted": total, "by_deck": out}

    @router.post("/backfill-context")
    async def backfill_context(request: Request):
        """One-time: recover the shared problem setup for multi-part questions
        that were split at extraction (their text references an objective / data
        / figure defined once for the whole problem). Per material, the AI reads
        the stored material text and fills each dependent question's `context`.
        Idempotent — questions that already have context are skipped; answers and
        all other fields are never touched. Covers every deck the caller owns."""
        from collections import defaultdict
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            qq = db.query(StudyQuestion)
            if user is not None:
                qq = qq.filter(StudyQuestion.owner == user)
            rows = qq.order_by(StudyQuestion.created_at.asc()).all()
            by_mat = defaultdict(list)
            for r in rows:
                if (r.context or "").strip():       # idempotent
                    continue
                if not r.material_id:               # no source to recover from
                    continue
                by_mat[r.material_id].append(
                    {"id": r.id, "number": r.number, "question": r.question[:600]})
            mats: Dict[str, str] = {}
            if by_mat:
                for m in db.query(StudyMaterial).filter(
                        StudyMaterial.id.in_(list(by_mat))).all():
                    mats[m.id] = m.content or ""
            valid = {r.id for r in rows}
        finally:
            db.close()

        results: Dict[str, str] = {}
        scanned = 0
        for mat_id, items in by_mat.items():
            mat_text = mats.get(mat_id, "")
            if not mat_text.strip():
                continue
            scanned += len(items)
            updates = await _backfill_context_items(user, mat_text, items)
            for qid, ctx in updates.items():
                if qid in valid:
                    results[qid] = ctx

        filled = 0
        db = _common.SessionLocal()
        try:
            for r in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(list(results))).all()
                      if results else []):
                if not (r.context or "").strip():
                    r.context = results[r.id]
                    filled += 1
            db.commit()
        finally:
            db.close()
        return {"filled": filled, "scanned": scanned}

    @router.post("/audit-questions")
    async def audit_questions(request: Request):
        """Audit the bank for 'questions' that actually state their own answer
        (worked-solution steps / conclusions leaked at extraction) and SUSPEND
        them so they drop out of practice. Reversible — unsuspend (or delete) in
        the question bank. Combines an AI judgment with the deterministic
        conclusion detector; only open, not-already-suspended questions are
        checked, across every deck the caller owns."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            qq = db.query(StudyQuestion).filter(StudyQuestion.qtype == "open")
            if user is not None:
                qq = qq.filter(StudyQuestion.owner == user)
            rows = [r for r in qq.all() if not r.suspended]
            items = [{"id": r.id, "question": (r.question or "")[:600]} for r in rows]
            # Deterministic layer: obvious conclusion openers, caught for free.
            deterministic = {
                r.id for r in rows
                if question_is_conclusion({"qtype": "open", "question": r.question or ""})
            }
        finally:
            db.close()

        flagged = await _audit_solution_statements(user, items)
        flagged |= deterministic

        suspended = 0
        db = _common.SessionLocal()
        try:
            for r in (db.query(StudyQuestion).filter(StudyQuestion.id.in_(list(flagged))).all()
                      if flagged else []):
                if not r.suspended:
                    r.suspended = True
                    suspended += 1
            db.commit()
        finally:
            db.close()
        return {"flagged": len(flagged), "suspended": suspended, "scanned": len(items)}

    @router.get("/questions/{question_id}/prereqs")
    def question_prereqs(request: Request, question_id: str):
        """The previous part this question depends on, with its question,
        the user's latest answer to it, and the correct answer — for the
        'Earlier in this problem' context box during practice."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            row = study_service.get_question(db, question_id, user)
            ids = json.loads(row.prereq_ids) if row.prereq_ids else []
            out = []
            for pid in reversed(ids):
                pq = db.query(StudyQuestion).filter(StudyQuestion.id == pid).first()
                if not pq or (user is not None and pq.owner != user):
                    continue
                if _same_study_question(row, pq):
                    continue
                if _same_or_later_study_part(row, pq):
                    continue
                if pq.qtype == "mcq" and pq.options is not None and pq.correct_index is not None:
                    opts = json.loads(pq.options)
                    correct = opts[pq.correct_index] if 0 <= pq.correct_index < len(opts) else ""
                else:
                    correct = pq.reference or ""
                att = db.query(StudyAttempt).filter(StudyAttempt.question_id == pid)
                if user is not None:
                    att = att.filter(StudyAttempt.owner == user)
                att = att.order_by(StudyAttempt.attempted_at.desc()).first()
                out.append({
                    "id": pq.id, "number": pq.number, "question": pq.question,
                    "your_answer": att.answer if att else None,
                    "correct": correct,
                })
                break
            return {"prereqs": out}
        finally:
            db.close()

    def _run_optimize(user: str, snapshots: list, reviews: list):
        """Background worker for FSRS w fitting (Phase 4.3: async DB)."""
        try:
            fitted = fsrs_optimizer.fit_w(snapshots, reviews, seed=42)
            if fitted is None:
                _optimize_status[user] = {
                    "status": "insufficient_data",
                    "reviews": len(reviews),
                    "min_required": fsrs_optimizer.MIN_REVIEWS,
                }
                return
            # Persist in a fresh session (the background thread can't reuse the
            # request's DB session).
            db2 = _common.SessionLocal()
            try:
                row = db2.query(StudyUserParams).filter(StudyUserParams.owner == user).first()
                now = _common._utcnow_naive()
                if row is None:
                    db2.add(StudyUserParams(
                        id=str(uuid.uuid4()), owner=user,
                        w_json=json.dumps(fitted),
                        review_count=len(reviews),
                        fitted_at=now,
                    ))
                else:
                    row.w_json = json.dumps(fitted)
                    row.review_count = len(reviews)
                    row.fitted_at = now
                db2.commit()
                _optimize_status[user] = {
                    "status": "ok", "reviews": len(reviews), "w": fitted,
                }
            finally:
                db2.close()
        except Exception as e:
            _optimize_status[user] = {"status": "error", "message": str(e)}

    @router.post("/optimize")
    def optimize_weights(request: Request):
        """Trigger per-user FSRS weight fitting (A6).

        Phase 4.3: runs asynchronously in a background thread so the request
        returns immediately with a task_id. Poll /optimize/status for results.
        """
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            # ----- load this user's review history -----
            cards = db.query(StudyCard).filter(StudyCard.owner == user).all() if user else []
            snapshots = []
            for c in cards:
                snapshots.append({
                    "id": c.id,
                    "state": c.state,
                    "stability": c.stability,
                    "difficulty": c.difficulty,
                    "last_review": _iso(c.last_review),
                    "reps": c.reps,
                    "lapses": c.lapses,
                })
            reviews_q = db.query(StudyReview).filter(StudyReview.owner == user)
            reviews = []
            for r in reviews_q.order_by(StudyReview.reviewed_at.asc()).all():
                reviews.append({
                    "id": r.id,
                    "card_id": r.card_id,
                    "rating": r.rating,
                    "interval_days": r.interval_days,
                    "state_before": r.state_before,
                    "reviewed_at": _iso(r.reviewed_at),
                })
            db.close()

            # Run in background thread (Phase 4.3)
            _optimize_status[user] = {"status": "running", "reviews": len(reviews)}
            t = threading.Thread(
                target=_run_optimize, args=(user, snapshots, reviews), daemon=True,
            )
            t.start()
            return {"status": "running", "reviews": len(reviews)}
        except Exception:
            db.close()
            raise

    @router.get("/optimize/status")
    def optimize_status(request: Request):
        """Poll the status of a background FSRS optimization (Phase 4.3)."""
        user = _owner(request)
        return _optimize_status.get(user, {"status": "never_run"})

