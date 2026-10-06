"""The review buttons' interval labels must match what a rating schedules.

GET /queue previewed every card with retention 0.9 and the default weights,
while POST /cards/{id}/review schedules with the deck's retention and the
user's fitted weights, so a deck at 0.8 retention (or a fitted user) saw
labels that promised intervals the review never granted.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import fsrs
from tests.helpers.sqlite_db import make_temp_sqlite

OWNER = "alice"
# Visibly different from the defaults: slower recall growth, bigger Easy bonus.
FITTED_W = list(fsrs.DEFAULT_W)
FITTED_W[8] = 1.2
FITTED_W[16] = 4.0


@pytest.fixture
def env(monkeypatch):
    import core.database as cd
    from routes import study_routes as sr

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)
    monkeypatch.setattr(sr, "get_current_user", lambda _request: OWNER)
    monkeypatch.setattr(sr, "_read_pref", lambda *_a, **_k: None)
    app = FastAPI()
    app.include_router(sr.setup_study_routes())
    yield TestClient(app), SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _seed(SessionLocal, *, retention: str, fitted: bool):
    from core.database import StudyCard, StudyDeck, StudyUserParams
    s = SessionLocal()
    now = datetime.utcnow()
    s.add(StudyDeck(id="d1", owner=OWNER, name="Micro", new_per_day=15, retention=retention))
    s.add(StudyCard(id="c1", owner=OWNER, deck_id="d1", front="F", back="B",
                    state="review", stability="12", difficulty="5", reps=4, lapses=0,
                    last_review=now - timedelta(days=12), due=now - timedelta(hours=1)))
    if fitted:
        s.add(StudyUserParams(id=str(uuid.uuid4()), owner=OWNER, w_json=json.dumps(FITTED_W)))
    s.commit()
    s.close()


@pytest.mark.parametrize("retention,fitted", [("0.8", False), ("0.9", True), ("0.95", True)])
def test_preview_labels_match_the_scheduled_review(env, retention, fitted):
    client, db = env
    _seed(db, retention=retention, fitted=fitted)
    card = client.get("/api/study/queue?deck_id=d1").json()["queue"][0]
    preview = card["preview"]
    default_preview = fsrs.preview_intervals(
        {"state": "review", "stability": 12.0, "difficulty": 5.0,
         "last_review": datetime.utcnow() - timedelta(days=12), "reps": 4, "lapses": 0})
    assert preview != {str(k): v for k, v in default_preview.items()}, \
        "this case must differ from the 0.9/default-weights preview"

    res = client.post("/api/study/cards/c1/review", json={"rating": 4})
    assert res.status_code == 200, res.text
    days = res.json()["interval_days"]
    assert preview["4"] == (f"{days}d" if days < 30 else
                            f"{days / 30.44:.1f}mo" if days < 365 else f"{days / 365.25:.1f}y")
