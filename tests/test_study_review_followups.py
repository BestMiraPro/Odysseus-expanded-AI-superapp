"""Follow-ups from `docs/study-review-2026-09-02.md` section 2.

Covers the service layer added/changed for the ranked recommendations:
cross-subject interleaving (#4), stats folding in question attempts (#6),
the calibration payload (#3) and mock-mode practice sessions (#2).
"""
import os
from datetime import timedelta

import pytest

from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture
def db(monkeypatch):
    import core.database as cd
    from routes import study_routes as sr

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)
    yield SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _two_subjects(SessionLocal, owner="alice"):
    """Two decks, three due questions each. Deck A's are all more overdue than
    deck B's, so a pure due-date ordering yields AAABBB."""
    from core.database import StudyDeck, StudyQuestion
    from routes.study_routes import _utcnow_naive

    s = SessionLocal()
    s.add_all([
        StudyDeck(id="dA", owner=owner, name="Micro", new_per_day=15, retention="0.9"),
        StudyDeck(id="dB", owner=owner, name="Stats", new_per_day=15, retention="0.9"),
    ])
    now = _utcnow_naive()
    for i in range(3):
        s.add(StudyQuestion(
            id=f"a{i}", owner=owner, deck_id="dA", qtype="open",
            question=f"A{i}", reference="r", topic="elasticity",
            state="review", due=now - timedelta(days=10 - i)))
    for i in range(3):
        s.add(StudyQuestion(
            id=f"b{i}", owner=owner, deck_id="dB", qtype="open",
            question=f"B{i}", reference="r", topic="regression",
            state="review", due=now - timedelta(days=3 - i)))
    s.commit()
    s.close()


# ------------------------------------------------------------------ #4 interleave

def test_practice_everything_interleaves_due_across_subjects(db):
    """"Practice everything" must round-robin subjects, not drain one deck
    before touching the next."""
    from routes.study_routes import practice_queue_payload

    _two_subjects(db)
    out = practice_queue_payload("alice", limit=6)
    decks = [q["deck_id"] for q in out["queue"]]

    assert len(decks) == 6
    assert decks == ["dA", "dB", "dA", "dB", "dA", "dB"]


def test_practice_everything_keeps_most_overdue_questions(db):
    """Interleaving changes the order, not the selection: with a small limit the
    queue still holds the most-overdue questions of each subject."""
    from routes.study_routes import practice_queue_payload

    _two_subjects(db)
    out = practice_queue_payload("alice", limit=2)
    assert {q["id"] for q in out["queue"]} == {"a0", "b0"}


def test_single_subject_queue_stays_in_due_order(db):
    """Scoped to one subject there is nothing to interleave — most overdue first."""
    from routes.study_routes import practice_queue_payload

    _two_subjects(db)
    out = practice_queue_payload("alice", deck_id="dA", limit=3)
    assert [q["id"] for q in out["queue"]] == ["a0", "a1", "a2"]


# ---------------------------------------------------------------------- #6 stats

def test_stats_counts_question_attempts_not_only_card_reviews(db):
    """The daily chart and success rate must reflect practice attempts, which is
    where most retrieval now happens."""
    from core.database import StudyAttempt
    from routes.study_routes import _utcnow_naive, stats_payload

    now = _utcnow_naive()
    s = db()
    s.add_all([
        StudyAttempt(id="t1", owner="alice", question_id="a0", deck_id="dA",
                     qtype="open", score=80, attempted_at=now),
        StudyAttempt(id="t2", owner="alice", question_id="a1", deck_id="dA",
                     qtype="mcq", correct=False, attempted_at=now),
    ])
    s.commit()
    s.close()

    out = stats_payload("alice", days=7)
    today = now.date().isoformat()
    row = next(d for d in out["daily"] if d["date"] == today)

    assert row["attempts"] == 2
    assert row["attempts_ok"] == 1
    assert out["totals"]["attempts"] == 2
    # One right out of two graded retrievals.
    assert out["totals"]["success_rate"] == 0.5


def test_stats_success_rate_blends_cards_and_attempts(db):
    """Card reviews and question attempts feed one combined retrieval rate."""
    from core.database import StudyAttempt, StudyReview
    from routes.study_routes import _utcnow_naive, stats_payload

    now = _utcnow_naive()
    s = db()
    # Two card reviews, both recalled (rating > 1).
    s.add_all([
        StudyReview(id="r1", owner="alice", card_id="c1", deck_id="dA",
                    rating=3, reviewed_at=now),
        StudyReview(id="r2", owner="alice", card_id="c2", deck_id="dA",
                    rating=3, reviewed_at=now),
        StudyAttempt(id="t1", owner="alice", question_id="a0", deck_id="dA",
                     qtype="mcq", correct=False, attempted_at=now),
        StudyAttempt(id="t2", owner="alice", question_id="a1", deck_id="dA",
                     qtype="mcq", correct=False, attempted_at=now),
    ])
    s.commit()
    s.close()

    out = stats_payload("alice", days=7)
    assert out["totals"]["reviews"] == 2
    assert out["totals"]["attempts"] == 2
    assert out["totals"]["success_rate"] == 0.5


def test_stats_scoped_to_owner(db):
    from core.database import StudyAttempt
    from routes.study_routes import _utcnow_naive, stats_payload

    now = _utcnow_naive()
    s = db()
    s.add(StudyAttempt(id="t1", owner="bob", question_id="a0", deck_id="dA",
                       qtype="open", score=90, attempted_at=now))
    s.commit()
    s.close()

    assert stats_payload("alice", days=7)["totals"]["attempts"] == 0
    assert stats_payload("bob", days=7)["totals"]["attempts"] == 1


# ---------------------------------------------------------------- #3 calibration

def _tagged_attempts(SessionLocal, owner="alice"):
    """Four confidence-tagged attempts in subject dA:
    sure+right, sure+right, sure+WRONG, guess+right."""
    from core.database import StudyAttempt
    from routes.study_routes import _utcnow_naive

    now = _utcnow_naive()
    s = SessionLocal()
    s.add_all([
        StudyAttempt(id="c1", owner=owner, question_id="a0", deck_id="dA",
                     qtype="mcq", correct=True, confidence="sure", attempted_at=now),
        StudyAttempt(id="c2", owner=owner, question_id="a1", deck_id="dA",
                     qtype="mcq", correct=True, confidence="sure", attempted_at=now),
        StudyAttempt(id="c3", owner=owner, question_id="a2", deck_id="dA",
                     qtype="mcq", correct=False, confidence="sure", attempted_at=now),
        StudyAttempt(id="c4", owner=owner, question_id="b0", deck_id="dA",
                     qtype="open", score=90, confidence="guess", attempted_at=now),
    ])
    s.commit()
    s.close()


def test_calibration_brier_score(db):
    """Brier = mean squared gap between stated confidence and outcome.
    sure/right .01 + .01, sure/wrong .81, guess/right .49 -> 1.32/4."""
    from routes.study_routes import calibration_payload

    _two_subjects(db)
    _tagged_attempts(db)
    out = calibration_payload("alice", days=90)

    assert out["overall"]["graded"] == 4
    assert out["overall"]["brier"] == 0.33


def test_calibration_buckets_expose_overconfidence(db):
    """The "sure" bucket must show stated vs actual accuracy — that gap is the
    whole point of tagging confidence."""
    from routes.study_routes import calibration_payload

    _two_subjects(db)
    _tagged_attempts(db)
    buckets = {b["confidence"]: b for b in calibration_payload("alice", days=90)["buckets"]}

    assert buckets["sure"]["attempts"] == 3
    assert buckets["sure"]["correct"] == 2
    assert buckets["sure"]["accuracy"] == 0.667
    assert buckets["sure"]["expected"] == 0.9
    # Claimed 90%, delivered 67% -> overconfident.
    assert buckets["sure"]["gap"] == round(0.667 - 0.9, 3)
    assert buckets["guess"]["attempts"] == 1


def test_calibration_sure_but_wrong_rate_per_subject(db):
    """"Sure but wrong" is the headline number, reported per subject."""
    from routes.study_routes import calibration_payload

    _two_subjects(db)
    _tagged_attempts(db)
    out = calibration_payload("alice", days=90)

    rows = {r["deck_id"]: r for r in out["by_deck"]}
    assert rows["dA"]["name"] == "Micro"
    assert rows["dA"]["sure_wrong"] == 1
    assert rows["dA"]["sure_wrong_rate"] == 0.333
    assert out["overall"]["sure_wrong"] == 1


def test_calibration_ignores_untagged_attempts(db):
    """Attempts answered without a confidence tag cannot be scored."""
    from core.database import StudyAttempt
    from routes.study_routes import _utcnow_naive, calibration_payload

    _two_subjects(db)
    _tagged_attempts(db)
    s = db()
    s.add(StudyAttempt(id="c5", owner="alice", question_id="a0", deck_id="dA",
                       qtype="mcq", correct=False, confidence=None,
                       attempted_at=_utcnow_naive()))
    s.commit()
    s.close()

    out = calibration_payload("alice", days=90)
    assert out["overall"]["attempts"] == 5
    assert out["overall"]["graded"] == 4
    assert out["overall"]["brier"] == 0.33


def test_calibration_empty_is_reported_not_crashed(db):
    from routes.study_routes import calibration_payload

    _two_subjects(db)
    out = calibration_payload("alice", days=90)
    assert out["overall"]["graded"] == 0
    assert out["overall"]["brier"] is None
    assert out["by_deck"] == []


def test_calibration_scoped_to_owner(db):
    from routes.study_routes import calibration_payload

    _two_subjects(db)
    _tagged_attempts(db, owner="bob")
    assert calibration_payload("alice", days=90)["overall"]["graded"] == 0
    assert calibration_payload("bob", days=90)["overall"]["graded"] == 4
