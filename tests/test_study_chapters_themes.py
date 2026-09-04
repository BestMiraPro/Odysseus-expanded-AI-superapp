"""Chapter and theme grouping for the Study question bank.

Two independent ways to slice a subject: chapters (one document, from its own
headings, ordered) and themes (subject-wide, clustered from the topic labels).
See website/superpowers/specs/2026-09-04-study-chapters-themes-design.md.
"""
import os

import pytest

from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture
def db(monkeypatch):
    import core.database as cd
    from routes.study import _common as common

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(common, "SessionLocal", SessionLocal)
    yield SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def test_grouping_columns_exist(db):
    """Chapters and themes need somewhere to live; all four are nullable so
    existing rows keep their meaning."""
    from core.database import StudyDeck, StudyMaterial, StudyQuestion

    for col in ("chapter", "chapter_index", "theme"):
        assert hasattr(StudyQuestion, col), f"StudyQuestion.{col} missing"
    assert hasattr(StudyMaterial, "chapter_count")

    s = db()
    s.add(StudyDeck(id="d1", owner="alice", name="Stats", new_per_day=15, retention="0.9"))
    s.add(StudyQuestion(id="q1", deck_id="d1", question="x", chapter="1 — Intro",
                        chapter_index=1, theme="Integration"))
    s.commit()
    got = s.query(StudyQuestion).filter_by(id="q1").first()
    assert (got.chapter, got.chapter_index, got.theme) == ("1 — Intro", 1, "Integration")
    s.close()


def _seed_chaptered(SessionLocal, owner="alice"):
    """One subject with a three-chapter workbook and a single-chapter exam.

    The exam shares a theme with chapter 2, so theme scoping has to reach across
    both documents to be correct."""
    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study._common import _utcnow_naive

    s = SessionLocal()
    s.add(StudyDeck(id="d1", owner=owner, name="Stats", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="m1", owner=owner, deck_id="d1", name="Workbook.pdf",
                        kind="pdf", content="x", char_count=1, chapter_count=3))
    s.add(StudyMaterial(id="m2", owner=owner, deck_id="d1", name="Exam.pdf",
                        kind="pdf", content="x", char_count=1, chapter_count=1))
    now = _utcnow_naive()
    for i in range(3):
        s.add(StudyQuestion(id=f"c1{i}", owner=owner, deck_id="d1", material_id="m1",
                            qtype="open", question=f"ch1 q{i}", reference="r",
                            topic="Bernoulli", chapter="1 — Probability", chapter_index=1,
                            theme="Probability", state="new", due=now))
    for i in range(2):
        s.add(StudyQuestion(id=f"c2{i}", owner=owner, deck_id="d1", material_id="m1",
                            qtype="open", question=f"ch2 q{i}", reference="r",
                            topic="Covariance", chapter="2 — Random variables",
                            chapter_index=2, theme="Distributions", state="new", due=now))
    s.add(StudyQuestion(id="e0", owner=owner, deck_id="d1", material_id="m2",
                        qtype="open", question="exam q", reference="r",
                        topic="Covariance", theme="Distributions", state="new", due=now))
    s.commit()
    s.close()


def test_queue_scoped_to_one_chapter(db):
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    out = practice_queue_payload("alice", deck_id="d1", chapter="1 — Probability", limit=20)

    assert {q["id"] for q in out["queue"]} == {"c10", "c11", "c12"}
    assert out["chapter"] == "1 — Probability"


def test_theme_spans_documents(db):
    """A theme is subject-wide: it must pull from every document that has it."""
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    out = practice_queue_payload("alice", deck_id="d1", theme="Distributions", limit=20)

    assert {q["id"] for q in out["queue"]} == {"c20", "c21", "e0"}
    assert len({q["material_id"] for q in out["queue"]}) == 2


def test_chapter_scope_leaves_ordering_to_the_scheduler(db, monkeypatch):
    """A chapter is a scope filter and nothing more: within the slice the user's
    study_order preference still decides the order, exactly as it does for an
    unscoped session. The default sinks already-seen questions behind unseen
    ones; "review" puts due retrievals first."""
    from datetime import timedelta

    from core.database import StudyQuestion
    from routes.study import practice as prac
    from routes.study._common import _utcnow_naive

    _seed_chaptered(db)
    s = db()
    s.add(StudyQuestion(id="due1", owner="alice", deck_id="d1", material_id="m1",
                        qtype="open", question="overdue", reference="r",
                        chapter="1 — Probability", chapter_index=1,
                        state="review", due=_utcnow_naive() - timedelta(days=5)))
    s.commit()
    s.close()

    monkeypatch.setattr(prac._common, "_read_pref", lambda u, k: "review")
    first = prac.practice_queue_payload("alice", deck_id="d1",
                                        chapter="1 — Probability", limit=20)
    assert first["queue"][0]["id"] == "due1"

    monkeypatch.setattr(prac._common, "_read_pref", lambda u, k: "")
    default = prac.practice_queue_payload("alice", deck_id="d1",
                                          chapter="1 — Probability", limit=20)
    assert default["queue"][-1]["id"] == "due1"
    # either way the slice holds only that chapter
    assert {q["id"] for q in default["queue"]} == {"c10", "c11", "c12", "due1"}


def test_unknown_chapter_returns_empty_not_everything(db):
    """A chapter filter is exact — unlike the fuzzy topic filter it must never
    fall back to the whole subject."""
    from routes.study.practice import practice_queue_payload

    _seed_chaptered(db)
    out = practice_queue_payload("alice", deck_id="d1", chapter="99 — Nope", limit=20)
    assert out["queue"] == []
