"""Study route sub-module: decks handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ decks

    @router.get("/decks")
    def list_decks(request: Request, archived: bool = False):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            q = db.query(StudyDeck).filter(StudyDeck.archived == archived)
            if user is not None:
                q = q.filter(StudyDeck.owner == user)
            decks = q.order_by(StudyDeck.created_at.asc()).all()
            out = []
            for d in decks:
                row = {
                    "id": d.id, "name": d.name, "description": d.description,
                    "color": d.color, "archived": bool(d.archived),
                    "new_per_day": d.new_per_day, "retention": _flt(d.retention, 0.9),
                }
                row.update(_deck_counts(db, user, d))
                out.append(row)
            return {"decks": out}
        finally:
            db.close()

    @router.post("/decks")
    def create_deck(request: Request, body: DeckCreate):
        user = _owner(request)
        if not body.name.strip():
            raise HTTPException(400, "Deck name is required")
        db = _common.SessionLocal()
        try:
            deck = StudyDeck(
                id=str(uuid.uuid4()), owner=user, name=body.name.strip(),
                description=body.description, color=body.color,
                new_per_day=max(0, body.new_per_day),
                retention=str(min(0.99, max(0.7, body.retention))),
            )
            db.add(deck)
            db.commit()
            return {"id": deck.id, "name": deck.name}
        finally:
            db.close()

    @router.put("/decks/{deck_id}")
    def update_deck(request: Request, deck_id: str, body: DeckUpdate):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            if body.name is not None:
                deck.name = body.name.strip() or deck.name
            if body.description is not None:
                deck.description = body.description
            if body.color is not None:
                deck.color = body.color
            if body.new_per_day is not None:
                deck.new_per_day = max(0, body.new_per_day)
            if body.retention is not None:
                deck.retention = str(min(0.99, max(0.7, body.retention)))
            if body.archived is not None:
                deck.archived = body.archived
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.delete("/decks/{deck_id}")
    def delete_deck(request: Request, deck_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            db.query(StudyReview).filter(StudyReview.deck_id == deck.id).delete()
            db.query(StudyAttempt).filter(StudyAttempt.deck_id == deck.id).delete()
            db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck.id).delete()
            db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck.id).delete()
            db.delete(deck)  # cards cascade
            db.commit()
            return {"ok": True}
        finally:
            db.close()

