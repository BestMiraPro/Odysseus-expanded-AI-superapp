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
