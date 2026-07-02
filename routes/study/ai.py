"""Study route sub-module: ai handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ AI

    @router.post("/ai/generate-cards")
    async def ai_generate_cards(request: Request, body: GenerateCardsIn):
        """Source text -> proposed cards. Returns proposals; nothing is saved."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        text = (body.text or "").strip()
        if body.material_id:
            db = _common.SessionLocal()
            try:
                m = db.query(StudyMaterial).filter(StudyMaterial.id == body.material_id).first()
                if not m or (user is not None and m.owner != user):
                    raise HTTPException(404, "Material not found")
                text = (m.content or "").strip()
            finally:
                db.close()
        if len(text) < 30:
            raise HTTPException(400, "Provide more source material (at least a paragraph).")
        count = max(1, min(40, body.count))
        focus = f"\nFocus especially on: {body.focus.strip()}" if body.focus else ""
        prompt = (f"Create about {count} flashcards from this material.{focus}\n\n"
                  f"--- MATERIAL ---\n{text[:24000]}")
        value = await _common._llm_json(user, CARD_AUTHOR_SYSTEM, prompt,
                                temperature=0.4, max_tokens=12000,
                                timeout=180, thinking_off=True)
        if isinstance(value, dict):
            value = value.get("cards") or [value]
        if not isinstance(value, list):
            raise HTTPException(502, "Model reply was not a card list. Try again.")
        cards = []
        for item in value:
            if isinstance(item, dict) and item.get("front") and item.get("back"):
                cards.append({"front": str(item["front"]).strip(),
                              "back": str(item["back"]).strip()})
        if not cards:
            raise HTTPException(502, "No usable cards in model reply. Try again.")
        return {"cards": cards}

    @router.post("/ai/quiz")
    async def ai_quiz(request: Request, body: QuizIn):
        """Free-recall quiz: from a deck (no LLM needed) or from pasted text (LLM)."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        count = max(1, min(20, body.count))
        if body.deck_id:
            db = _common.SessionLocal()
            try:
                deck = study_service.get_deck(db, body.deck_id, user)
                q = db.query(StudyCard).filter(
                    StudyCard.deck_id == deck.id,
                    StudyCard.suspended == False)  # noqa: E712
                if user is not None:
                    q = q.filter(StudyCard.owner == user)
                # Prioritize due/lapsed cards — quiz the weak spots first.
                now = _common._utcnow_naive()
                due = q.filter(StudyCard.state != "new", StudyCard.due <= now) \
                    .order_by(StudyCard.due.asc()).limit(count).all()
                rest_needed = count - len(due)
                rest = []
                if rest_needed > 0:
                    exclude = [c.id for c in due]
                    rq = q
                    if exclude:
                        rq = rq.filter(~StudyCard.id.in_(exclude))
                    rest = rq.order_by(StudyCard.lapses.desc(),
                                       StudyCard.created_at.desc()) \
                        .limit(rest_needed).all()
                cards = due + rest
                if not cards:
                    raise HTTPException(400, "Deck has no cards to quiz from.")
                return {"questions": [
                    {"question": c.front, "reference": c.back, "card_id": c.id}
                    for c in cards
                ], "source": "deck"}
            finally:
                db.close()
        text = (body.text or "").strip()
        if len(text) < 30:
            raise HTTPException(400, "Pick a deck or paste source material.")
        prompt = (f"Write {count} free-recall questions from this material.\n\n"
                  f"--- MATERIAL ---\n{text[:24000]}")
        value = await _common._llm_json(user, QUIZ_AUTHOR_SYSTEM, prompt,
                                temperature=0.5, max_tokens=12000,
                                timeout=180, thinking_off=True)
        if isinstance(value, dict):
            value = value.get("questions") or [value]
        questions = []
        for item in (value if isinstance(value, list) else []):
            if isinstance(item, dict) and item.get("question") and item.get("reference"):
                questions.append({"question": str(item["question"]).strip(),
                                  "reference": str(item["reference"]).strip(),
                                  "card_id": None})
        if not questions:
            raise HTTPException(502, "No usable questions in model reply. Try again.")
        return {"questions": questions, "source": "ai"}

    @router.post("/ai/grade")
    async def ai_grade(request: Request, body: GradeIn):
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        user = _owner(request)
        if not body.answer.strip():
            return {"score": 0, "verdict": "incorrect",
                    "feedback": "No answer given. Attempt a recall before checking — "
                                "even a wrong attempt strengthens the memory more than peeking.",
                    "followup": None}
        prompt = (f"QUESTION:\n{body.question.strip()}\n\n"
                  f"REFERENCE ANSWER:\n{body.reference.strip()}\n\n"
                  f"LEARNER'S ANSWER:\n{body.answer.strip()[:8000]}")
        # Thinking stays ON for grading — careful rubric comparison benefits
        # from reasoning; the budget covers the hidden tokens plus the JSON.
        value = await _common._llm_json(user, GRADER_SYSTEM, prompt,
                                temperature=0.2, max_tokens=8000, timeout=120)
        if not isinstance(value, dict):
            raise HTTPException(502, "Model reply was not a grade object. Try again.")
        score = value.get("score")
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            score = 0
        verdict = value.get("verdict")
        if verdict not in ("correct", "partial", "incorrect"):
            verdict = "correct" if score >= 85 else ("partial" if score >= 40 else "incorrect")
        return {"score": score, "verdict": verdict,
                "feedback": str(value.get("feedback") or "").strip(),
                "followup": (str(value.get("followup")).strip()
                             if value.get("followup") else None)}

