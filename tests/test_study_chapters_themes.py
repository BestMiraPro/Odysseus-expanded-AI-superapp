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


def test_groupings_lists_chapters_and_themes(db):
    from routes.study.insights import groupings_payload

    _seed_chaptered(db)
    out = groupings_payload("alice", "d1")

    assert len(out["chapters"]) == 1                       # only the multi-chapter doc
    doc = out["chapters"][0]
    assert doc["material"] == "Workbook.pdf"
    assert [c["label"] for c in doc["chapters"]] == ["1 — Probability", "2 — Random variables"]
    assert [c["count"] for c in doc["chapters"]] == [3, 2]

    themes = {t["name"]: t for t in out["themes"]}
    assert themes["Distributions"]["count"] == 3
    assert themes["Distributions"]["materials"] == 2


def test_groupings_omits_single_chapter_documents(db):
    """Exam.pdf has chapter_count=1, so it must never appear as a chapter row."""
    from routes.study.insights import groupings_payload

    _seed_chaptered(db)
    names = [d["material"] for d in groupings_payload("alice", "d1")["chapters"]]
    assert "Exam.pdf" not in names


def test_groupings_empty_when_nothing_grouped(db):
    """A subject of plain exam papers must look exactly as it does today: both
    lists empty is the signal to hide both sections."""
    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study.insights import groupings_payload

    s = db()
    s.add(StudyDeck(id="d9", owner="alice", name="Plain", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="m9", owner="alice", deck_id="d9", name="Paper.pdf",
                        kind="pdf", content="x", char_count=1))
    s.add(StudyQuestion(id="p1", owner="alice", deck_id="d9", material_id="m9",
                        qtype="open", question="q", reference="r", state="new"))
    s.commit()
    s.close()

    assert groupings_payload("alice", "d9") == {"chapters": [], "themes": []}


# --------------------------------------------------------------- chapter detection

def _clear_grouping(SessionLocal):
    """Strip the seeded grouping so detection/clustering has real work to do."""
    from core.database import StudyMaterial, StudyQuestion

    s = SessionLocal()
    for q in s.query(StudyQuestion).all():
        q.chapter = q.chapter_index = q.theme = None
    for m in s.query(StudyMaterial).all():
        m.chapter_count = None
    s.commit()
    s.close()


def _seed_detectable(SessionLocal, owner="alice", n=10):
    """A document with enough questions to be worth splitting (>= the
    CHAPTER_MIN_QUESTIONS floor), and no grouping yet."""
    from core.database import StudyDeck, StudyMaterial, StudyQuestion

    s = SessionLocal()
    s.add(StudyDeck(id="dd", owner=owner, name="Book", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="md", owner=owner, deck_id="dd", name="Big.pdf",
                        kind="pdf", content="Chapter 1 ... Chapter 2 ...",
                        char_count=26))
    for i in range(n):
        s.add(StudyQuestion(id=f"b{i}", owner=owner, deck_id="dd", material_id="md",
                            qtype="open", question=f"q{i}", reference="r",
                            topic="T", state="new"))
    s.commit()
    s.close()


def test_detect_chapters_assigns_and_counts(db, monkeypatch):
    import asyncio

    from core.database import StudyMaterial, StudyQuestion
    from routes.study import maintenance as mnt

    _seed_detectable(db)

    async def fake_llm(owner, system, user, **kw):
        return {"chapters": [
            {"index": 1, "label": "1 — Probability", "questions": [f"b{i}" for i in range(6)]},
            {"index": 2, "label": "2 — Random variables", "questions": [f"b{i}" for i in range(6, 10)]},
        ]}

    monkeypatch.setattr(mnt, "_llm_json", fake_llm)
    out = asyncio.run(mnt.run_detect_chapters("alice", "md"))

    assert out["chapters"] == 2
    assert out["assigned"] == 10
    s = db()
    assert s.query(StudyMaterial).filter_by(id="md").first().chapter_count == 2
    assert s.query(StudyQuestion).filter_by(id="b0").first().chapter == "1 — Probability"
    assert s.query(StudyQuestion).filter_by(id="b9").first().chapter_index == 2
    s.close()


def test_detect_chapters_refuses_to_split_one_chapter(db, monkeypatch):
    """The rule the whole feature turns on: fewer than two headings means the
    document is never split, so it never shows up as a chapter picker."""
    import asyncio

    from core.database import StudyMaterial, StudyQuestion
    from routes.study import maintenance as mnt

    _seed_detectable(db)

    async def one_chapter(owner, system, user, **kw):
        return {"chapters": [{"index": 1, "label": "The whole paper",
                              "questions": [f"b{i}" for i in range(10)]}]}

    monkeypatch.setattr(mnt, "_llm_json", one_chapter)
    out = asyncio.run(mnt.run_detect_chapters("alice", "md"))

    assert out["chapters"] == 1
    assert out["assigned"] == 0
    s = db()
    assert s.query(StudyMaterial).filter_by(id="md").first().chapter_count == 1
    assert s.query(StudyQuestion).filter_by(id="b0").first().chapter is None
    s.close()


def test_detect_chapters_skips_tiny_documents_without_calling_the_model(db, monkeypatch):
    """Under the threshold a split leaves chapters too small to practise, and it
    must not cost an AI call."""
    import asyncio

    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study import maintenance as mnt

    s = db()
    s.add(StudyDeck(id="d2", owner="alice", name="Tiny", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="mt", owner="alice", deck_id="d2", name="Short.pdf",
                        kind="pdf", content="x", char_count=1))
    for i in range(3):
        s.add(StudyQuestion(id=f"t{i}", owner="alice", deck_id="d2", material_id="mt",
                            qtype="open", question="q", reference="r", state="new"))
    s.commit()
    s.close()

    called = {"n": 0}

    async def counter(owner, system, user, **kw):
        called["n"] += 1
        return {"chapters": []}

    monkeypatch.setattr(mnt, "_llm_json", counter)
    out = asyncio.run(mnt.run_detect_chapters("alice", "mt"))

    assert out["skipped"] == "too_few_questions"
    assert called["n"] == 0


# ------------------------------------------------------------------ theme clustering

def test_cluster_themes_labels_every_question_with_that_topic(db, monkeypatch):
    import asyncio

    from core.database import StudyQuestion
    from routes.study import maintenance as mnt

    _seed_chaptered(db)
    _clear_grouping(db)

    async def fake(owner, system, user, **kw):
        return {"themes": [
            {"name": "Probability", "topics": ["Bernoulli"]},
            {"name": "Distributions", "topics": ["Covariance"]},
        ]}

    monkeypatch.setattr(mnt, "_llm_json", fake)
    out = asyncio.run(mnt.run_cluster_themes("alice", "d1"))

    assert out["themes"] == 2
    assert out["labelled"] == 6
    s = db()
    assert s.query(StudyQuestion).filter_by(id="c10").first().theme == "Probability"
    # a theme must reach every document carrying that topic, not just one
    assert s.query(StudyQuestion).filter_by(id="e0").first().theme == "Distributions"
    s.close()


def test_cluster_themes_is_idempotent(db, monkeypatch):
    """Re-running re-clusters cleanly instead of accumulating."""
    import asyncio

    from core.database import StudyQuestion
    from routes.study import maintenance as mnt

    _seed_chaptered(db)

    async def fake(owner, system, user, **kw):
        return {"themes": [{"name": "Everything", "topics": ["Bernoulli", "Covariance"]}]}

    monkeypatch.setattr(mnt, "_llm_json", fake)
    asyncio.run(mnt.run_cluster_themes("alice", "d1"))
    second = asyncio.run(mnt.run_cluster_themes("alice", "d1"))

    assert second["themes"] == 1
    s = db()
    assert {q.theme for q in s.query(StudyQuestion).all()} == {"Everything"}
    s.close()
