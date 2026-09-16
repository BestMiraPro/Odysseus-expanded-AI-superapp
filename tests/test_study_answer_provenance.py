"""Answer provenance (plan Task 2).

The provenance column records where a question's reference answer and MCQ key
came from, per field, independent of where the *question* came from
(``origin``). These tests cover the pure normalization/update helpers, the
extraction claim validation (including the source-location-distinct rule), the
route/tool edit paths, the schema upgrade of a legacy table, and the shared
grading-basis helper that the attempt route and the coach must agree on.

No live database is touched: schema tests run against a temporary file and the
route tests use an in-memory SQLite app.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core import database as cdb
from core.database import Base, StudyDeck, StudyQuestion
from routes.study._common import _question_to_dict
from routes import study_routes
from src.study_ai import (
    normalize_answer_provenance,
    provenance_is_blank,
    question_answer_provenance,
    updated_answer_provenance,
)
from src.study_practice_coach import grading_basis
from src.study_ai import GRADE_OPEN_SYSTEM

OWNER = "alice"


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def _unknown() -> dict:
    return {"reference": {"origin": "unknown"},
            "correct_index": {"origin": "unknown"}}


def test_legacy_null_and_malformed_become_unknown():
    assert normalize_answer_provenance(None) == _unknown()
    assert normalize_answer_provenance("not json") == _unknown()
    assert normalize_answer_provenance("{broken") == _unknown()
    assert normalize_answer_provenance([]) == _unknown()


def test_json_encoded_string_round_trips():
    raw = json.dumps({"reference": {"origin": "document_transcribed",
                                    "material_id": "m-1", "page": 3,
                                    "excerpt": "short excerpt"}})
    prov = normalize_answer_provenance(raw)
    assert prov["reference"]["origin"] == "document_transcribed"
    assert prov["reference"]["material_id"] == "m-1"
    assert prov["reference"]["page"] == 3
    assert prov["correct_index"] == {"origin": "unknown"}


def test_invalid_origins_and_missing_subfields_fall_back():
    prov = normalize_answer_provenance({
        "reference": {},                       # missing subfield
        "correct_index": {"origin": "verified"}  # not an allowed origin
    })
    assert prov == _unknown()


def test_evidence_bounds():
    prov = normalize_answer_provenance({
        "reference": {"origin": "mixed", "excerpt": "x" * 1500,
                      "page": -4, "material_id": "m" * 300},
    })
    entry = prov["reference"]
    assert len(entry["excerpt"]) == 1000
    assert "page" not in entry
    assert len(entry["material_id"]) == 200


def test_user_edited_and_generated_entries_carry_no_evidence():
    prov = normalize_answer_provenance({
        "reference": {"origin": "user_edited", "material_id": "m-1",
                      "excerpt": "stale"},
        "correct_index": {"origin": "ai_generated", "page": 2},
    })
    assert prov["reference"] == {"origin": "user_edited"}
    assert prov["correct_index"] == {"origin": "ai_generated"}


def test_provenance_is_blank():
    assert provenance_is_blank(None)
    assert provenance_is_blank(_unknown())
    assert not provenance_is_blank(
        {"reference": {"origin": "ai_generated"},
         "correct_index": {"origin": "unknown"}})


# ---------------------------------------------------------------------------
# edit rules
# ---------------------------------------------------------------------------

_TRANSCRIBED = {
    "reference": {"origin": "document_transcribed", "material_id": "m-1",
                  "page": 3, "excerpt": "in the doc"},
    "correct_index": {"origin": "document_transcribed", "material_id": "m-1",
                      "page": 9},
}


def test_human_reference_edit_marks_only_that_entry_and_drops_evidence():
    prov = updated_answer_provenance(
        _TRANSCRIBED, changed_fields=["reference"], author="user")
    assert prov["reference"] == {"origin": "user_edited"}
    assert prov["correct_index"] == _TRANSCRIBED["correct_index"]


def test_ai_answer_edit_marks_entry_ai_generated():
    prov = updated_answer_provenance(
        _TRANSCRIBED, changed_fields=["correct_index"], author="ai")
    assert prov["correct_index"] == {"origin": "ai_generated"}
    assert prov["reference"] == _TRANSCRIBED["reference"]


@pytest.mark.parametrize("field", ["question", "context", "options"])
def test_prompt_level_changes_invalidate_both_entries(field):
    prov = updated_answer_provenance(
        _TRANSCRIBED, changed_fields=[field, "reference"], author="ai")
    assert prov == _unknown(), (
        f"a {field} change must invalidate both entries, even when the "
        "reference was edited in the same request"
    )


def test_formatting_only_preserves_origin_and_evidence():
    prov = updated_answer_provenance(
        _TRANSCRIBED, changed_fields=["question", "reference"],
        author="ai", formatting_only=True)
    assert prov == _TRANSCRIBED


def test_unrelated_edits_leave_provenance_untouched():
    prov = updated_answer_provenance(
        _TRANSCRIBED, changed_fields=["topic"], author="user")
    assert prov == _TRANSCRIBED


# ---------------------------------------------------------------------------
# extraction claims (validated against the actual source)
# ---------------------------------------------------------------------------

_SOURCE = (
    "Question 1 - State the lemma. Solution: the lemma says X follows from Y. "
    "Question 2 - Which policy? (a) tariffs (b) free trade. "
    "Answer key on page 9: (b) free trade."
)


def test_extraction_transcribed_claim_with_evidence():
    item = {"qtype": "open", "reference": "the lemma says X follows from Y",
            "answer_origin": "document_transcribed",
            "answer_source_page": 2, "answer_source_excerpt": "X follows from Y"}
    prov = question_answer_provenance(
        item, material_id="m-1", source_text=_SOURCE)
    assert prov["reference"]["origin"] == "document_transcribed"
    assert prov["reference"]["material_id"] == "m-1"
    assert prov["reference"]["page"] == 2


def test_answer_page_never_inferred_from_question_location():
    # The question starts on page 1 but the answer appears on page 9; only
    # the extractor's OWN page claim may appear.
    item = {"qtype": "mcq", "options": ["tariffs", "free trade"],
            "correct_index": 1, "source_page": 1, "reference": "",
            "answer_origin": "document_transcribed",
            "answer_source_page": 9, }
    prov = question_answer_provenance(
        item, material_id="m-1", source_text=_SOURCE)
    assert prov["correct_index"]["page"] == 9


def test_excerpt_not_in_source_is_dropped():
    prov = question_answer_provenance(
        {"qtype": "open", "reference": "derived answer",
         "answer_origin": "document_transcribed",
         "answer_source_excerpt": "invented words nobody wrote"},
        material_id="m-1", source_text=_SOURCE)
    assert prov["reference"] == {
        "origin": "document_transcribed", "material_id": "m-1"}


def test_generated_origin_keeps_no_evidence():
    prov = question_answer_provenance(
        {"qtype": "open", "reference": "derived",
         "answer_origin": "ai_generated"},
        material_id="m-1", source_text=_SOURCE)
    assert prov["reference"] == {"origin": "ai_generated"}
    assert prov["correct_index"] == {"origin": "unknown"}


def test_mcq_reference_filled_from_key_inherits_key_provenance():
    item = {"qtype": "mcq", "options": ["tariffs", "free trade"],
            "correct_index": 1, "reference": "free trade",
            "answer_origin": "document_transcribed",
            "answer_source_page": 9}
    prov = question_answer_provenance(
        item, material_id="m-1", source_text=_SOURCE)
    assert prov["reference"] == prov["correct_index"]
    assert "page" in prov["reference"]


def test_document_solution_augmented_with_reasoning_is_mixed():
    item = {"qtype": "open", "reference": "X, plus my own reasoning",
            "answer_origin": "mixed", "answer_source_excerpt": "X follows"}
    prov = question_answer_provenance(
        item, material_id="m-1", source_text=_SOURCE)
    assert prov["reference"]["origin"] == "mixed"


def test_authored_questions_are_ai_generated():
    prov = question_answer_provenance(
        {"qtype": "mcq", "options": ["a", "b"], "correct_index": 0,
         "reference": "because a"}, authored=True)
    assert prov["reference"] == {"origin": "ai_generated"}
    assert prov["correct_index"] == {"origin": "ai_generated"}
    # an open question without a reference has no answer to attribute
    prov = question_answer_provenance(
        {"qtype": "open", "reference": ""}, authored=True)
    assert prov["reference"] == {"origin": "unknown"}


# ---------------------------------------------------------------------------
# separate per-field provenance for the reference and the MCQ key
# ---------------------------------------------------------------------------

def test_per_field_extraction_keeps_distinct_origins_and_evidence():
    item = {
        "qtype": "mcq", "options": ["A", "B"], "correct_index": 0,
        "reference": "The model derived an explanation here.",
        "reference_excerpt": "excerpt-inherent",
        "reference_origin": "ai_generated",
        "key_origin": "document_transcribed",
        "key_page": 9,
        "key_excerpt": "Answer key: 1: A",
    }
    source = "Source text containing: Answer key: 1: A."
    prov = question_answer_provenance(item, material_id="m-1",
                                      source_text=source)
    assert prov["reference"] == {"origin": "ai_generated"}
    assert prov["correct_index"] == {
        "origin": "document_transcribed", "material_id": "m-1",
        "page": 9, "excerpt": "Answer key: 1: A"}


def test_mixed_reference_and_transcribed_key_stay_distinct():
    item = {
        "qtype": "mcq", "options": ["A", "B"], "correct_index": 1,
        "reference": "The passage plus reasoning",
        "reference_origin": "mixed",
        "reference_excerpt": "privacy-excerpt-in-source",
        "key_origin": "document_transcribed",
    }
    source = "Body: privacy-excerpt-in-source. And an answer key elsewhere."
    prov = question_answer_provenance(item, material_id="m-2",
                                      source_text=source)
    assert prov["reference"]["origin"] == "mixed"
    assert prov["reference"]["excerpt"] == "privacy-excerpt-in-source"
    assert prov["correct_index"] == {
        "origin": "document_transcribed", "material_id": "m-2"}


def test_legacy_single_origin_fallback_still_carries_to_both():
    """The old flat form is a compatibility fallback: it marks both entries
    and is never evidence that the origins were established independently."""
    item = {
        "qtype": "open", "reference": "an answer",
        "answer_origin": "document_transcribed",
        "answer_source_page": 4, "answer_source_excerpt": "in the doc",
    }
    prov = question_answer_provenance(item, material_id="m-1",
                                      source_text="doc: in the doc")
    assert prov["reference"]["origin"] == "document_transcribed"
    assert prov["reference"]["page"] == 4
    assert prov["correct_index"] == {"origin": "unknown"}  # open: no key


def test_malformed_per_field_origin_falls_back_to_unknown():
    item = {"qtype": "open", "reference": "r",
            "reference_origin": "verified-trust-me",
            "reference_excerpt": "not in source"}
    prov = question_answer_provenance(item, material_id="m-1", source_text="x")
    assert prov["reference"] == {"origin": "unknown"}


def test_distinct_per_field_round_trips_through_saving(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'perfield.db'}",
                           poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    session.add(StudyDeck(id="deck-1", owner=OWNER, name="D"))
    item = {
        "qtype": "mcq", "options": ["A", "B"], "correct_index": 0,
        "question": "Q?", "reference": "A generated explanation",
        "reference_origin": "ai_generated",
        "key_origin": "document_transcribed", "key_page": 9,
    }
    prov = question_answer_provenance(item, material_id="m-1")
    question = _question_row()
    question.answer_provenance = json.dumps(prov)
    session.add(question)
    session.commit()
    row = session.query(StudyQuestion).filter(StudyQuestion.id == "q-1").one()
    stored = normalize_answer_provenance(row.answer_provenance)
    assert stored["reference"] == {"origin": "ai_generated"}
    assert stored["correct_index"]["origin"] == "document_transcribed"
    assert stored["correct_index"]["page"] == 9
    session.close()
    engine.dispose()


def test_mcq_option_fill_still_inherits_the_key_provenance():
    item = {
        "qtype": "mcq", "options": ["tariffs", "free trade"],
        "correct_index": 1, "reference": "free trade",
        "key_origin": "document_transcribed", "key_page": 9,
    }
    prov = question_answer_provenance(item, material_id="m-1",
                                      source_text=_SOURCE)
    assert prov["reference"] == prov["correct_index"]
    assert prov["reference"]["page"] == 9


def test_backward_compatible_source_location_probe_unchanged():
    """The source-page probe (answer page != question page) works on the
    legacy flat form as well as per-field."""
    item = {"qtype": "mcq", "options": ["tariffs", "free trade"],
            "correct_index": 1, "source_page": 1, "reference": "",
            "answer_origin": "document_transcribed", "answer_source_page": 9}
    prov = question_answer_provenance(item, material_id="m-1",
                                      source_text=_SOURCE)
    assert prov["correct_index"]["page"] == 9


# ---------------------------------------------------------------------------
# grading basis: must remain behaviorally equivalent to the old inline picks
# ---------------------------------------------------------------------------

def test_grading_basis_mcq_typed_recall_reference_selection():
    opts = ["A", "B"]
    with_key = grading_basis(qtype="mcq", reference="long explanation",
                             correct_index=1, options=opts)
    assert with_key["typed_recall_reference"] == "long explanation"
    assert with_key["index_basis"] == {"correct_index": 1}
    assert with_key["missing_reference"] is False

    key_only = grading_basis(qtype="mcq", reference="", correct_index=1,
                             options=opts)
    assert key_only["typed_recall_reference"] == "B"
    assert key_only["missing_reference"] is True

    nothing = grading_basis(qtype="mcq", reference="", correct_index=None,
                            options=opts)
    assert nothing["typed_recall_reference"] == ""


def test_grading_basis_open_reference_fallback_matches_existing_instruction():
    with_ref = grading_basis(qtype="open", reference="complete answer",
                             correct_index=None, options=[])
    assert with_ref["open_reference"] == "complete answer"

    without = grading_basis(qtype="open", reference="", correct_index=None,
                            options=[])
    assert without["open_reference"] == (
        "(no reference available - first work out the correct answer "
        "yourself, then grade the learner's answer against it)")
    assert without["missing_reference"] is True


def test_grading_basis_uses_the_open_grading_prompt():
    basis = grading_basis(qtype="open", reference="r", correct_index=None,
                          options=[])
    assert basis["grading_prompt"] == GRADE_OPEN_SYSTEM


# ---------------------------------------------------------------------------
# serializers: full answers carry provenance, hidden queues do not
# ---------------------------------------------------------------------------

def _question_row(**overrides):
    data = {
        "id": "q-1", "owner": OWNER, "deck_id": "deck-1", "material_id": None,
        "qtype": "mcq", "question": "Which molecule stores energy?",
        "context": None, "options": '["ATP", "DNA"]', "correct_index": 0,
        "reference": "ATP stores readily usable cellular energy.",
        "explanation": None, "deep_explanation": None, "number": None,
        "source_page": None, "chapter": None, "chapter_index": None,
        "prereq_ids": None, "topic": "Cells", "difficulty": "medium",
        "origin": "extracted",
        "answer_provenance": None,
        "suspended": False, "state": "new", "stability": "0",
        "fsrs_difficulty": "0", "due": datetime(2026, 1, 1),
        "last_review": None, "reps": 0, "lapses": 0,
        "created_at": datetime(2026, 1, 1),
    }
    data.update(overrides)
    return StudyQuestion(**data)


def test_question_to_dict_carries_provenance_only_with_answer():
    row = _question_row(answer_provenance=json.dumps(_TRANSCRIBED))
    full = _question_to_dict(row)
    assert full["answer_provenance"]["reference"]["origin"] == \
        "document_transcribed"
    hidden = _question_to_dict(row, with_answer=False)
    assert "correct_index" not in hidden and "reference" not in hidden
    assert "answer_provenance" not in hidden


def test_question_to_dict_legacy_null_is_unknown():
    full = _question_to_dict(_question_row())
    assert full["answer_provenance"] == _unknown()


# ---------------------------------------------------------------------------
# schema: the additive column upgrades a legacy table idempotently
# ---------------------------------------------------------------------------

def _legacy_study_questions_db(tmp_path: Path) -> Path:
    """The study_questions table as it existed before answer_provenance."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE study_questions (
            id VARCHAR PRIMARY KEY,
            owner VARCHAR,
            deck_id VARCHAR NOT NULL,
            material_id VARCHAR,
            qtype VARCHAR,
            question TEXT NOT NULL,
            context TEXT,
            options TEXT,
            correct_index INTEGER,
            reference TEXT,
            explanation TEXT,
            deep_explanation TEXT,
            number VARCHAR,
            source_page INTEGER,
            chapter VARCHAR,
            chapter_index INTEGER,
            theme VARCHAR,
            prereq_ids TEXT,
            topic VARCHAR,
            difficulty VARCHAR,
            origin VARCHAR,
            suspended BOOLEAN,
            state VARCHAR,
            stability VARCHAR,
            fsrs_difficulty VARCHAR,
            due DATETIME,
            last_review DATETIME,
            reps INTEGER,
            lapses INTEGER,
            created_at DATETIME,
            updated_at DATETIME
        )
    """)
    conn.execute(
        "INSERT INTO study_questions (id, deck_id, question) "
        "VALUES ('legacy-q', 'd-1', 'Legacy row')")
    conn.commit()
    conn.close()
    return db


def test_legacy_table_gains_answer_provenance_and_stays_idempotent(tmp_path):
    db = _legacy_study_questions_db(tmp_path)
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)

    cdb.reconcile_schema_with_models(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("study_questions")}
    assert "answer_provenance" in columns

    with engine.begin() as conn:
        value, = conn.execute(text(
            "SELECT answer_provenance FROM study_questions")).fetchone()
    assert value is None, "legacy rows gain NULL provenance, never a claim"

    # a second upgrade must not change anything
    changed = cdb.reconcile_schema_with_models(engine)
    assert "answer_provenance" not in {c or "" for c in changed}, (
        "re-paying an already-added column is not idempotent")
    with engine.begin() as conn:
        value2, = conn.execute(text(
            "SELECT answer_provenance FROM study_questions")).fetchone()
    assert value2 is None
    engine.dispose()


def test_provenance_round_trips_through_real_orm_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orm.db'}", poolclass=NullPool)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()
    session.add(StudyDeck(id="deck-1", owner=OWNER, name="D"))
    session.add(_question_row(
        answer_provenance=json.dumps(_TRANSCRIBED)))
    session.commit()
    row = session.query(StudyQuestion).filter(StudyQuestion.id == "q-1").one()
    assert normalize_answer_provenance(row.answer_provenance) \
        == normalize_answer_provenance(_TRANSCRIBED)
    # the question origin stays untouched by answer provenance semantics
    assert row.origin == "extracted"
    session.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# routes: human edit path keeps provenance truthful
# ---------------------------------------------------------------------------

@pytest.fixture
def study_app(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'routes.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False,
                                    autoflush=False)
    session = TestSessionLocal()
    session.add(StudyDeck(id="deck-1", owner=OWNER, name="D"))
    session.add(_question_row())
    session.commit()
    session.close()

    app = FastAPI()
    app.include_router(study_routes.setup_study_routes())
    monkeypatch.setattr(study_routes, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(study_routes, "get_current_user",
                        lambda _request: OWNER)
    monkeypatch.setattr(study_routes, "_read_pref",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(study_routes.RateLimiter, "check",
                        lambda *_a, **_k: True)
    return TestClient(app), TestSessionLocal


def test_human_reference_edit_via_route_marks_user_edited(study_app):
    client, SessionLocal = study_app
    resp = client.put("/api/study/questions/q-1",
                      json={"reference": "new human wording"})
    assert resp.status_code == 200
    prov = resp.json()["answer_provenance"]
    assert prov["reference"] == {"origin": "user_edited"}
    # the untouched key keeps its provenance
    assert prov["correct_index"]["origin"] == "unknown"


def test_human_question_edit_via_route_invalidates_both(study_app):
    client, SessionLocal = study_app
    resp = client.put("/api/study/questions/q-1",
                      json={"question": "A different question"})
    assert resp.status_code == 200
    assert resp.json()["answer_provenance"] == _unknown()


def test_human_options_edit_via_route_invalidates_both(study_app):
    client, SessionLocal = study_app
    resp = client.put("/api/study/questions/q-1",
                      json={"options": ["ATP", "DNA", "RNA"]})
    assert resp.status_code == 200
    assert resp.json()["answer_provenance"] == _unknown()


# ---------------------------------------------------------------------------
# maintenance must follow the same invalidation contract (review finding 7)
# ---------------------------------------------------------------------------

def test_context_backfill_change_invalidates_provenance(study_app, monkeypatch):
    """run_backfill_context is shared by the HTTP route and the tutor's
    maintenance tool: a recovered setup is a semantic context change, so the
    answer can no longer be presented as sourced from the old passage."""
    from core.database import StudyMaterial
    from routes.study import maintenance as maint

    client, SessionLocal = study_app
    s = SessionLocal()
    s.add(StudyMaterial(id="m-1", owner=OWNER, deck_id="deck-1",
                        name="Source", kind="text", file_id=None,
                        content="A long enough source paragraph.", char_count=30,
                        question_count=0, summary=None, category="theory",
                        created_at=datetime(2026, 1, 1)))
    q = s.query(StudyQuestion).filter(StudyQuestion.id == "q-1").one()
    q.context = None
    q.material_id = "m-1"
    q.answer_provenance = json.dumps(_TRANSCRIBED)
    s.commit()
    s.close()

    async def fake_backfill_items(*_a, **_k):
        return {"q-1": "New setup: x=100"}

    monkeypatch.setattr(maint._common, "_backfill_context_items",
                        fake_backfill_items)
    out = asyncio.run(maint.run_backfill_context(OWNER, "deck-1"))
    assert out["filled"] == 1

    s = SessionLocal()
    row = s.query(StudyQuestion).filter(StudyQuestion.id == "q-1").one()
    assert row.context == "New setup: x=100"
    # both entries invalidated: the answer no longer claims the old passage
    assert normalize_answer_provenance(row.answer_provenance) == _unknown()


def test_context_backfill_preserves_provenance_when_unchanged(study_app):
    """The writer only touches rows whose context is empty, so an existing
    context — and its provenance — is never relabeled."""
    from core.database import StudyMaterial
    from routes.study import maintenance as maint

    client, SessionLocal = study_app
    s = SessionLocal()
    s.add(StudyMaterial(id="m-1", owner=OWNER, deck_id="deck-1",
                        name="Source", kind="text", file_id=None,
                        content="A long enough source paragraph.", char_count=30,
                        question_count=0, category="theory",
                        created_at=datetime(2026, 1, 1)))
    q = s.query(StudyQuestion).filter(StudyQuestion.id == "q-1").one()
    q.context = "Existing setup (unchanged by backfill)."
    q.material_id = "m-1"
    q.answer_provenance = json.dumps(_TRANSCRIBED)
    s.commit()
    s.close()

    out = asyncio.run(maint.run_backfill_context(OWNER, "deck-1"))
    assert out["filled"] == 0

    s = SessionLocal()
    row = s.query(StudyQuestion).filter(StudyQuestion.id == "q-1").one()
    assert row.context == "Existing setup (unchanged by backfill)."
    assert normalize_answer_provenance(row.answer_provenance) == \
        normalize_answer_provenance(_TRANSCRIBED)