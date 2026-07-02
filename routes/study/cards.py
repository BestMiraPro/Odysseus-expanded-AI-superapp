"""Study route sub-module: cards handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ cards

    @router.get("/decks/{deck_id}/cards")
    def list_cards(request: Request, deck_id: str, q: Optional[str] = None):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            query = db.query(StudyCard).filter(StudyCard.deck_id == deck.id)
            if user is not None:
                query = query.filter(StudyCard.owner == user)
            if q:
                like = f"%{q}%"
                query = query.filter(
                    StudyCard.front.ilike(like) | StudyCard.back.ilike(like))
            cards = query.order_by(StudyCard.created_at.desc()).all()
            return {"cards": [_card_to_dict(c) for c in cards]}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/cards")
    def create_cards(request: Request, deck_id: str, body: CardsCreate):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            created = []
            now = _common._utcnow_naive()
            for c in body.cards:
                if not c.front.strip() or not c.back.strip():
                    continue
                card = StudyCard(
                    id=str(uuid.uuid4()), owner=user, deck_id=deck.id,
                    front=c.front.strip(), back=c.back.strip(),
                    notes=c.notes,
                    tags=json.dumps(c.tags) if c.tags else None,
                    source=body.source or "user",
                    state="new", due=now,
                )
                db.add(card)
                created.append(card.id)
            db.commit()
            return {"created": len(created), "ids": created}
        finally:
            db.close()

    @router.put("/cards/{card_id}")
    def update_card(request: Request, card_id: str, body: CardUpdate):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            if body.front is not None:
                card.front = body.front.strip() or card.front
            if body.back is not None:
                card.back = body.back.strip() or card.back
            if body.notes is not None:
                card.notes = body.notes
            if body.tags is not None:
                card.tags = json.dumps(body.tags)
            if body.suspended is not None:
                card.suspended = body.suspended
            if body.deck_id is not None:
                study_service.get_deck(db, body.deck_id, user)  # ownership check
                card.deck_id = body.deck_id
            db.commit()
            return _card_to_dict(card)
        finally:
            db.close()

    @router.delete("/cards/{card_id}")
    def delete_card(request: Request, card_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            card = study_service.get_card(db, card_id, user)
            db.delete(card)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

