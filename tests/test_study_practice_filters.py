"""Practice-queue scoping (material / topics), vision-by-default decision,
discovery batching, live material counts — the study_routes service layer
against a temp SQLite DB."""
import asyncio
import json
import os
import uuid

import pytest
from fastapi import HTTPException

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


def _seed(SessionLocal, owner="alice"):
    from core.database import StudyDeck, StudyMaterial, StudyQuestion
    from routes.study_routes import _utcnow_naive
    s = SessionLocal()
    deck = StudyDeck(id="d1", owner=owner, name="Micro", new_per_day=15, retention="0.9")
    m1 = StudyMaterial(id="m1", owner=owner, deck_id="d1", name="Exam 2024", kind="text",
                       content="q", char_count=1, category="exam")
    m2 = StudyMaterial(id="m2", owner=owner, deck_id="d1", name="Ch3", kind="text",
                       content="t", char_count=1, category="theory")
    s.add_all([deck, m1, m2])
    now = _utcnow_naive()
    specs = [("m1", "Price elasticity"), ("m1", "Price elasticity"), ("m1", "Supply"),
             ("m2", "Demand curves"), ("m2", "Supply")]
    for i, (mid, topic) in enumerate(specs):
        s.add(StudyQuestion(id=f"q{i}", owner=owner, deck_id="d1", material_id=mid, qtype="open",
                            question=f"Question {i} about {topic}", reference="r", topic=topic,
                            difficulty="medium", state="new", due=now))
    s.commit()
    s.close()


# ---------------------------------------------------------------- pure helpers

def test_should_use_vision_decision():
    from src.study_ai import should_use_vision
    assert should_use_vision(has_pdf=True, vision_available=True) is True
    assert should_use_vision(has_pdf=True, vision_available=False) is False
    assert should_use_vision(has_pdf=False, vision_available=True) is False
    assert should_use_vision(has_pdf=True, vision_available=False, explicit=True) is True
    assert should_use_vision(has_pdf=True, vision_available=True, explicit=False) is False


def test_offset_manifest_shifts_pages():
    from src.study_ai import offset_manifest
    man = [{"number": "1", "kind": "open", "page": 1}, {"number": "2", "kind": "mcq", "page": 3}]
    assert offset_manifest(man, 0) == man
    assert [m["page"] for m in offset_manifest(man, 12)] == [13, 15]


def test_split_topics():
    from routes.study_routes import _split_topics
    assert _split_topics("Demand, Supply ,, ") == ["demand", "supply"]
    assert _split_topics(["A", " b "]) == ["a", "b"]
    assert _split_topics(None) == []


# ---------------------------------------------------------------- practice queue

def test_queue_scoped_to_material(db):
    from routes.study_routes import practice_queue_payload
    _seed(db)
    res = practice_queue_payload("alice", material_id="m1", limit=20)
    assert {q["material_id"] for q in res["queue"]} == {"m1"} and res["total"] == 3
    res = practice_queue_payload("alice", deck_id="d1", limit=20)
    assert res["total"] == 5 and res["topic_fallback"] is False


def test_queue_topic_filter_and_fallback(db):
    from routes.study_routes import practice_queue_payload
    _seed(db)
    res = practice_queue_payload("alice", deck_id="d1", topics="elasticity", limit=20)
    assert res["total"] == 2 and all("elasticity" in q["topic"].lower() for q in res["queue"])
    res = practice_queue_payload("alice", deck_id="d1", topics="Supply, Demand", limit=20)
    assert res["total"] == 3
    res = practice_queue_payload("alice", deck_id="d1", topics="quantum chromodynamics", limit=20)
    assert res["topic_fallback"] is True and res["total"] == 5


def test_queue_interleaves_new_questions_across_topics(db):
    from routes.study_routes import practice_queue_payload
    _seed(db)
    res = practice_queue_payload("alice", deck_id="d1", limit=20)
    topics = [q["topic"] for q in res["queue"][:3]]
    assert len(set(topics)) == 3        # round-robin, not blocked by topic


def test_queue_owner_isolation(db):
    from routes.study_routes import practice_queue_payload
    _seed(db)
    with pytest.raises(HTTPException):
        practice_queue_payload("bob", material_id="m1")
    with pytest.raises(HTTPException):
        practice_queue_payload("bob", deck_id="d1")


# ---------------------------------------------------------------- live counts

def test_material_counts_are_live(db):
    from core.database import StudyQuestion
    from routes.study_routes import material_rows_with_counts
    _seed(db)
    s = db()
    rows = {r["id"]: r["question_count"] for r in material_rows_with_counts(s, "d1", "alice")}
    assert rows == {"m1": 3, "m2": 2}
    s.query(StudyQuestion).filter(StudyQuestion.id == "q0").delete()
    s.commit()
    rows = {r["id"]: r["question_count"] for r in material_rows_with_counts(s, "d1", "alice")}
    assert rows["m1"] == 2
    s.close()


def test_create_material_record_text_and_category(db):
    from routes.study_routes import create_material_record
    _seed(db)
    row = create_material_record("alice", "d1", name="Mock exam", text="x" * 40)
    assert row["category"] == "exam" and row["kind"] == "text" and row["question_count"] == 0
    row = create_material_record("alice", "d1", name="Mock exam", text="x" * 40, category="theory")
    assert row["category"] == "theory"
    with pytest.raises(HTTPException):
        create_material_record("alice", "d1", name="short", text="tiny")
    with pytest.raises(HTTPException):
        create_material_record("bob", "d1", name="x", text="x" * 40)


# ---------------------------------------------------------------- discovery batching

def test_discovery_batches_pages_with_offsets(monkeypatch):
    from routes import study_routes as sr
    calls = []

    async def fake_vision(owner, system, instruction, urls, **kw):
        calls.append(len(urls))
        base = len(calls) * 100
        # each batch reports two questions on its 1st and 3rd page + key page 2
        return {"questions": [{"number": str(base + 1), "kind": "open", "page": 1},
                              {"number": str(base + 2), "kind": "mcq", "page": 3}],
                "answer_key_pages": [2]}

    monkeypatch.setattr(sr, "_llm_json_vision", fake_vision)
    pages = [f"data:{i}" for i in range(25)]
    manifest, keys = asyncio.run(sr._discover_questions_vision("alice", pages))
    assert calls == [12, 12, 1]
    assert [m["page"] for m in manifest] == [1, 3, 13, 15, 25, 27]
    assert keys == [2, 14, 26]


def test_discovery_survives_a_failed_batch(monkeypatch):
    from routes import study_routes as sr
    n = {"i": 0}

    async def flaky(owner, system, instruction, urls, **kw):
        n["i"] += 1
        if n["i"] == 1:
            raise HTTPException(502, "boom")
        return {"questions": [{"number": "7", "kind": "open", "page": 2}], "answer_key_pages": []}

    monkeypatch.setattr(sr, "_llm_json_vision", flaky)
    manifest, keys = asyncio.run(sr._discover_questions_vision("alice", [f"p{i}" for i in range(13)]))
    assert [(m["number"], m["page"]) for m in manifest] == [("7", 14)]


# ---------------------------------------------------------------- run_extraction path choice

def _pdf_material(SessionLocal, owner="alice", content=""):
    from core.database import StudyDeck, StudyMaterial
    s = SessionLocal()
    s.add(StudyDeck(id="d1", owner=owner, name="Micro", new_per_day=15, retention="0.9"))
    s.add(StudyMaterial(id="m1", owner=owner, deck_id="d1", name="exam.pdf", kind="pdf",
                        file_id="abc.pdf", content=content, char_count=len(content), category="exam"))
    s.commit()
    s.close()


def _fake_question(text="What is elasticity?"):
    return {"qtype": "open", "question": text, "options": None, "correct_index": None,
            "reference": "responsiveness", "topic": "Elasticity", "difficulty": "medium",
            "number": "1", "context": None}


def test_run_extraction_uses_vision_by_default_when_available(db, monkeypatch):
    from routes import study_routes as sr
    _pdf_material(db, content="lots of text " * 500)
    monkeypatch.setattr(sr, "_resolve_uploaded_file", lambda fid: "/tmp/abc.pdf")
    monkeypatch.setattr(sr, "_vision_candidates", lambda owner: [("u", "vlm", {})])
    used = {}

    async def fake_vision(owner, mode, types, pdf_path):
        used["vision"] = True
        return [_fake_question()], 1, 1, 0, None, {"pages": 1, "total_pages": 1, "truncated": False}

    async def fake_text(*a, **k):
        used["text"] = True
        return []

    async def no_link(*a, **k):
        return {"linked": 0, "analyzed": 0}

    monkeypatch.setattr(sr, "_extract_questions_vision", fake_vision)
    monkeypatch.setattr(sr, "_llm_json", fake_text)
    monkeypatch.setattr(sr, "_link_deck_parts", no_link)
    res = asyncio.run(sr.run_extraction("alice", "m1"))
    assert res["vision"] is True and res["created"] == 1 and res["pages"]["pages"] == 1
    assert "text" not in used


def test_run_extraction_falls_back_to_text_without_vision_model(db, monkeypatch):
    from routes import study_routes as sr
    _pdf_material(db, content="Question 1. What is elasticity? " * 50)
    monkeypatch.setattr(sr, "_resolve_uploaded_file", lambda fid: "/tmp/abc.pdf")
    monkeypatch.setattr(sr, "_vision_candidates", lambda owner: [])
    monkeypatch.setattr(sr, "pdf_page_count", lambda p: 1, raising=False)
    import src.study_vision as sv
    monkeypatch.setattr(sv, "pdf_page_count", lambda p: 1)
    used = {}

    async def fake_vision(*a, **k):
        used["vision"] = True
        return [], 0, 0, 0, None, None

    async def fake_llm_json(owner, system, user, **kw):
        used["text"] = True
        return [{"type": "open", "question": "What is elasticity?", "reference": "r", "number": "1"}]

    async def fake_discover(owner, content):
        return [{"number": "1", "kind": "open", "page": 1}]

    async def no_link(*a, **k):
        return {"linked": 0, "analyzed": 0}

    monkeypatch.setattr(sr, "_extract_questions_vision", fake_vision)
    monkeypatch.setattr(sr, "_llm_json", fake_llm_json)
    monkeypatch.setattr(sr, "_discover_questions_text", fake_discover)
    monkeypatch.setattr(sr, "_link_deck_parts", no_link)
    res = asyncio.run(sr.run_extraction("alice", "m1"))
    assert res["vision"] is False and res["created"] == 1 and used == {"text": True}
    assert res["coverage"] == {"expected": 1, "matched": 1, "missing": []}


def test_run_extraction_explicit_vision_false_forces_text(db, monkeypatch):
    from routes import study_routes as sr
    _pdf_material(db, content="Question 1. What is elasticity? " * 50)
    monkeypatch.setattr(sr, "_resolve_uploaded_file", lambda fid: "/tmp/abc.pdf")
    monkeypatch.setattr(sr, "_vision_candidates", lambda owner: [("u", "vlm", {})])
    import src.study_vision as sv
    monkeypatch.setattr(sv, "pdf_page_count", lambda p: 1)

    async def fake_vision(*a, **k):
        raise AssertionError("vision must not run")

    async def fake_llm_json(owner, system, user, **kw):
        return [{"type": "open", "question": "What is elasticity?", "reference": "r", "number": "1"}]

    async def fake_discover(owner, content):
        return []

    async def no_link(*a, **k):
        return {}

    monkeypatch.setattr(sr, "_extract_questions_vision", fake_vision)
    monkeypatch.setattr(sr, "_llm_json", fake_llm_json)
    monkeypatch.setattr(sr, "_discover_questions_text", fake_discover)
    monkeypatch.setattr(sr, "_link_deck_parts", no_link)
    res = asyncio.run(sr.run_extraction("alice", "m1", vision=False))
    assert res["vision"] is False and res["created"] == 1


def test_dead_quiz_grade_routes_removed():
    from routes.study_routes import setup_study_routes
    paths = {r.path for r in setup_study_routes().routes}
    assert "/api/study/ai/quiz" not in paths and "/api/study/ai/grade" not in paths
    assert "/api/study/materials/{material_id}/transcribe" in paths
    assert "/api/study/practice/queue" in paths
