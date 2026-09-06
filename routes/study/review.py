"""Study route sub-module: review handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ queue + review

    @router.get("/queue")
    def review_queue(request: Request, deck_id: Optional[str] = None, limit: int = 60):
        """Due learning/relearning first, then due reviews, then capped new cards."""
        user = _owner(request)
        limit = max(1, min(200, limit))
        db = _common.SessionLocal()
        try:
            now = _common._utcnow_naive()
            decks = ([study_service.get_deck(db, deck_id, user)] if deck_id else
                     db.query(StudyDeck).filter(
                         StudyDeck.archived == False,  # noqa: E712
                         *( [StudyDeck.owner == user] if user is not None else [] )
                     ).all())
            queue: List[Dict] = []
            for deck in decks:
                base = db.query(StudyCard).filter(
                    StudyCard.deck_id == deck.id,
                    StudyCard.suspended == False)  # noqa: E712
                if user is not None:
                    base = base.filter(StudyCard.owner == user)
                learning = base.filter(
                    StudyCard.state.in_(("learning", "relearning")),
                    StudyCard.due <= now).order_by(StudyCard.due.asc()).all()
                review = base.filter(
                    StudyCard.state == "review",
                    StudyCard.due <= now).order_by(StudyCard.due.asc()).all()
                cap = max(0, (deck.new_per_day or 0) - _new_introduced_today(db, user, deck.id))
                new = base.filter(StudyCard.state == "new") \
                    .order_by(StudyCard.created_at.asc()).limit(cap).all() if cap else []
                for c in learning + review + new:
                    queue.append(_card_to_dict(c, with_preview=True))
            # Default: already-seen cards (learning/review) sink behind new ones.
            # The per-user `study_order` pref = "review" restores the classic
            # spaced-repetition order (due reviews first, then new).
            if _common._read_pref(user, "study_order") == "review":
                phase_rank = {"learning": 0, "relearning": 0, "review": 1, "new": 2}
            else:
                phase_rank = {"new": 0, "learning": 1, "relearning": 1, "review": 2}
            queue.sort(key=lambda c: (phase_rank.get(c["state"], 3), c["due"] or ""))
            return {"queue": queue[:limit], "total": len(queue)}
        finally:
            db.close()

    @router.post("/cards/{card_id}/review")
    def review_card(request: Request, card_id: str, body: ReviewIn):
        """Apply an FSRS rating to a card.

        Idempotency policy (``body.idempotency_key``):

        * Keys are scoped to the owner. Two users may pick the same key; the
          UNIQUE index is ``(COALESCE(owner,''), idempotency_key)``, built by
          ``core.database.apply_study_idempotency_indexes``.
        * Replaying a key against a *different* card is a 409. The key
          identifies one intended write, so a mismatch is a client bug rather
          than a retry.
        * Replaying a key against the *same* card returns the first result and
          ignores the retry's payload — first write wins. A client that
          resends after a timeout cannot tell rejection from a lost response,
          so the retry must not re-advance the schedule.
        * Omitting the key keeps the legacy behaviour: every request is a new
          review.
        """
        user = _owner(request)
        if body.rating not in (1, 2, 3, 4):
            raise HTTPException(400, "rating must be 1-4")
        db = _common.SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            deck = study_service.get_deck(db, card.deck_id, user)
            if body.idempotency_key:
                prior = db.query(StudyReview).filter(
                    StudyReview.owner == user,
                    StudyReview.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    if prior.card_id != card_id:
                        raise HTTPException(409, "Idempotency key already used for another card")
                    return {
                        "card": _card_to_dict(card),
                        "interval_days": prior.interval_days,
                    }
            state_before = card.state or "new"
            user_w = _cached_w(db, user)
            result = _common.fsrs.schedule(
                _card_fsrs_dict(card), body.rating,
                desired_retention=_flt(deck.retention, 0.9),
                w=(user_w if user_w is not None else _common.fsrs.DEFAULT_W),
            )
            card.state = result["state"]
            card.stability = str(result["stability"])
            card.difficulty = str(result["difficulty"])
            card.due = _to_naive_utc(result["due"])
            card.last_review = _to_naive_utc(result["last_review"])
            card.reps = result["reps"]
            card.lapses = result["lapses"]
            try:
                db.add(StudyReview(
                    id=str(uuid.uuid4()), owner=user, card_id=card.id,
                    deck_id=card.deck_id, rating=body.rating,
                    state_before=state_before,
                    interval_days=result["interval_days"],
                    duration_ms=body.duration_ms,
                    reviewed_at=_common._utcnow_naive(),
                    idempotency_key=body.idempotency_key,
                ))
                db.commit()
            except IntegrityError:
                db.rollback()
                card = study_service.get_card(db, card_id, user)
                prior = db.query(StudyReview).filter(
                    StudyReview.owner == user,
                    StudyReview.idempotency_key == body.idempotency_key,
                ).first()
                if prior is not None:
                    if prior.card_id != card_id:
                        raise HTTPException(409, "Idempotency key already used for another card")
                    return {
                        "card": _card_to_dict(card),
                        "interval_days": prior.interval_days,
                    }
                raise
            return {"card": _card_to_dict(card), "interval_days": result["interval_days"]}
        finally:
            db.close()

