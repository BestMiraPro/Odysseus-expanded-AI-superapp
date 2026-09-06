"""Study idempotency keys are unique per owner, not globally.

The replay lookup in ``routes/study`` is owner-scoped
(``StudyReview.owner == user``), so a globally unique index lets one owner's
key collide with another's: the insert raises ``IntegrityError``, the
owner-scoped replay lookup finds nothing, and the error escapes as a 500.

These tests pin the intended contract:

* the shipped index scopes uniqueness to ``(owner, idempotency_key)``;
* ``NULL`` owners (single-user deployments) are still constrained;
* a second owner reusing a key gets a normal 200, not a 500;
* the same owner reusing a key for a different target still gets 409.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database as cdb
from core.database import Base, StudyCard, StudyDeck, StudyReview
from routes import study_routes


ALICE = "alice"
BOB = "bob"


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'study.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=eng)
    # Exercise the shipped migration, not a hand-written copy of it.
    cdb.apply_study_idempotency_indexes(eng)
    return eng


@pytest.fixture
def study_app(engine, monkeypatch):
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    current = {"user": ALICE}

    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(study_routes, "get_current_user", lambda _r: current["user"])
    monkeypatch.setattr(study_routes, "_read_pref", lambda *_a, **_k: None)
    monkeypatch.setattr(study_routes.RateLimiter, "check", lambda *_a, **_k: True)
    monkeypatch.setattr(study_routes.fsrs, "schedule", _fake_schedule)
    return TestClient(app), TestSessionLocal, current


def _fake_schedule(card, rating, **_kwargs):
    return {
        "state": "review",
        "stability": (card.get("stability") or 0) + 1,
        "difficulty": 4.5,
        "due": datetime(2026, 1, 3),
        "last_review": datetime(2026, 1, 2),
        "reps": (card.get("reps") or 0) + 1,
        "lapses": card.get("lapses") or 0,
        "interval_days": 7,
    }


def _deck(owner, deck_id):
    return StudyDeck(
        id=deck_id, owner=owner, name="Biology", retention="0.9",
        new_per_day=15, archived=False, created_at=datetime(2026, 1, 1),
    )


def _card(owner, card_id, deck_id):
    return StudyCard(
        id=card_id, owner=owner, deck_id=deck_id, front="What is ATP?",
        back="Cellular energy currency.", tags="[]", suspended=False,
        state="review", stability="2", difficulty="5", due=datetime(2026, 1, 1),
        reps=4, lapses=0, created_at=datetime(2026, 1, 1),
    )


def _review(owner, review_id, card_id, key):
    return StudyReview(
        id=review_id, owner=owner, card_id=card_id, deck_id="deck-1", rating=3,
        state_before="review", interval_days=7, duration_ms=10,
        reviewed_at=datetime(2026, 1, 2), idempotency_key=key,
    )


def _seed(session_local, *rows):
    db = session_local()
    try:
        for row in rows:
            db.add(row)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Schema-level contract
# --------------------------------------------------------------------------

def test_same_key_allowed_for_different_owners(engine):
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(_review(ALICE, "rv-a", "card-a", "shared-key"))
        db.add(_review(BOB, "rv-b", "card-b", "shared-key"))
        db.commit()
        assert db.query(StudyReview).count() == 2
    finally:
        db.close()


def test_same_key_rejected_for_same_owner(engine):
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(_review(ALICE, "rv-1", "card-a", "dup-key"))
        db.commit()
        db.add(_review(ALICE, "rv-2", "card-b", "dup-key"))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_same_key_rejected_for_null_owner(engine):
    """Single-user deployments store owner=NULL; uniqueness must still hold.

    A plain UNIQUE(owner, idempotency_key) would not catch this: SQLite and
    PostgreSQL both treat NULLs as distinct in unique indexes.
    """
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(_review(None, "rv-1", "card-a", "anon-key"))
        db.commit()
        db.add(_review(None, "rv-2", "card-b", "anon-key"))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_null_keys_are_unconstrained(engine):
    """Reviews without an idempotency key are the common case; never collide."""
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(_review(ALICE, "rv-1", "card-a", None))
        db.add(_review(ALICE, "rv-2", "card-a", None))
        db.commit()
        assert db.query(StudyReview).count() == 2
    finally:
        db.close()


def test_legacy_global_index_is_replaced(engine):
    """A pre-existing global index would keep rejecting cross-owner keys."""
    with engine.connect() as conn:
        names = {
            row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' "
                     "AND tbl_name='study_reviews'")
            )
        }
    assert "ux_study_reviews_idempotency_key" not in names


def test_migration_replaces_a_previously_created_global_index(tmp_path):
    """Upgrading an existing install must drop the old global index."""
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", poolclass=NullPool)
    Base.metadata.create_all(bind=eng)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX ux_study_reviews_idempotency_key "
            "ON study_reviews(idempotency_key)"
        ))

    cdb.apply_study_idempotency_indexes(eng)

    Session = sessionmaker(bind=eng)
    db = Session()
    try:
        db.add(_review(ALICE, "rv-a", "card-a", "shared-key"))
        db.add(_review(BOB, "rv-b", "card-b", "shared-key"))
        db.commit()
        assert db.query(StudyReview).count() == 2
    finally:
        db.close()


# --------------------------------------------------------------------------
# Route-level contract
# --------------------------------------------------------------------------

def test_second_owner_reusing_a_key_is_not_a_server_error(study_app):
    client, session_local, current = study_app
    _seed(
        session_local,
        _deck(ALICE, "deck-1"), _card(ALICE, "card-1", "deck-1"),
        _deck(BOB, "deck-2"), _card(BOB, "card-2", "deck-2"),
    )
    body = {"rating": 3, "duration_ms": 1200, "idempotency_key": "rv:shared:1"}

    current["user"] = ALICE
    first = client.post("/api/study/cards/card-1/review", json=body)
    assert first.status_code == 200

    current["user"] = BOB
    second = client.post("/api/study/cards/card-2/review", json=body)
    assert second.status_code == 200, second.text

    db = session_local()
    try:
        assert db.query(StudyReview).count() == 2
        owners = {r.owner for r in db.query(StudyReview).all()}
        assert owners == {ALICE, BOB}
    finally:
        db.close()


def test_same_owner_reusing_a_key_for_another_card_still_conflicts(study_app):
    client, session_local, current = study_app
    _seed(
        session_local,
        _deck(ALICE, "deck-1"),
        _card(ALICE, "card-1", "deck-1"),
        _card(ALICE, "card-9", "deck-1"),
    )
    body = {"rating": 3, "idempotency_key": "rv:mine:1"}

    assert client.post("/api/study/cards/card-1/review", json=body).status_code == 200
    clash = client.post("/api/study/cards/card-9/review", json=body)
    assert clash.status_code == 409


# --------------------------------------------------------------------------
# Replay policy for a same-target retry carrying a different payload
# --------------------------------------------------------------------------

def test_same_target_different_payload_replays_the_first_result(study_app):
    """Documented policy: first write wins; the retry payload is ignored.

    A characterization test — it pins behaviour the code already had but never
    stated. The alternative (409 on payload mismatch) would break the honest
    retry case, where a client resends after a timeout it cannot distinguish
    from a rejection.
    """
    client, session_local, _current = study_app
    _seed(session_local, _deck(ALICE, "deck-1"), _card(ALICE, "card-1", "deck-1"))

    first = client.post(
        "/api/study/cards/card-1/review",
        json={"rating": 3, "duration_ms": 1200, "idempotency_key": "rv:policy:1"},
    )
    assert first.status_code == 200

    # Same key, same card, deliberately different rating and duration.
    second = client.post(
        "/api/study/cards/card-1/review",
        json={"rating": 1, "duration_ms": 9999, "idempotency_key": "rv:policy:1"},
    )
    assert second.status_code == 200
    assert second.json()["interval_days"] == first.json()["interval_days"]

    db = session_local()
    try:
        rows = db.query(StudyReview).all()
        assert len(rows) == 1
        assert rows[0].rating == 3          # the first payload, not the retry
        assert rows[0].duration_ms == 1200
    finally:
        db.close()
