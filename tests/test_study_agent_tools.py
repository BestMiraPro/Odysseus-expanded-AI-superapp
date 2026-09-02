"""Study agent: tool registry + dispatch against a temp SQLite DB, the fenced
tool-call fallback parser, and history trimming.

Async tool handlers are driven with asyncio.run inside sync tests so the file
runs identically with or without pytest-asyncio.
"""
import asyncio
import json
import os

import pytest

from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture
def db(monkeypatch):
    import core.database as cd
    from routes import study_routes as sr
    from src import study_agent as sa

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)
    monkeypatch.setattr(sa, "SessionLocal", SessionLocal)
    yield SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def run(name, owner, args=None, **kw):
    from src import study_agent as sa
    return asyncio.run(sa.dispatch_tool(name, owner, args or {}, **kw))


def _subject(owner="alice", name="Micro"):
    res = run("create_subject", owner, {"name": name})
    assert res["ok"], res
    return res["result"]["id"]


MATERIAL_TEXT = (
    "[Page 1 text]:\nChapter 3. Demand.\nThe law of demand says quantity falls as price rises.\n\n"
    "[Page 2 text]:\nPrice elasticity of demand measures the responsiveness of quantity demanded "
    "to a change in price. Elastic demand has |e| > 1.\n\n"
    "[Page 3 text]:\nCross elasticity relates two goods."
)


# ---------------------------------------------------------------- subjects

def test_create_list_update_subject(db):
    did = _subject()
    res = run("list_subjects", "alice")
    subs = res["result"]["subjects"]
    assert [s["id"] for s in subs] == [did]
    assert subs[0]["materials"] == 0 and subs[0]["questions"] == 0
    res = run("update_subject", "alice", {"deck_id": did, "name": "Microeconomics", "new_per_day": 5})
    assert res["ok"] and res["result"]["name"] == "Microeconomics"


def test_owner_isolation(db):
    did = _subject("alice")
    assert run("list_subjects", "bob")["result"]["subjects"] == []
    res = run("update_subject", "bob", {"deck_id": did, "name": "hijack"})
    assert not res["ok"] and "not found" in res["error"].lower()


def test_delete_subject_requires_confirm(db):
    did = _subject()
    res = run("delete_subject", "alice", {"deck_id": did})
    assert not res["ok"] and "confirm" in res["error"].lower()
    assert run("list_subjects", "alice")["result"]["subjects"]  # still there
    res = run("delete_subject", "alice", {"deck_id": did, "confirm": True})
    assert res["ok"]
    assert run("list_subjects", "alice")["result"]["subjects"] == []


# ---------------------------------------------------------------- materials

def test_material_add_search_get(db):
    did = _subject()
    res = run("add_material", "alice", {"deck_id": did, "name": "Ch3 notes", "text": MATERIAL_TEXT})
    assert res["ok"], res
    mid = res["result"]["id"]
    assert res["result"]["category"] == "theory"

    mats = run("list_materials", "alice", {"deck_id": did})["result"]["materials"]
    assert len(mats) == 1 and mats[0]["question_count"] == 0

    hits = run("search_materials", "alice", {"deck_id": did, "query": "elasticity"})["result"]["hits"]
    assert hits and hits[0]["page"] == 2 and "elasticity" in hits[0]["snippet"].lower()

    page = run("get_material", "alice", {"material_id": mid, "page": 3})["result"]
    assert page["page"] == 3 and "Cross elasticity" in page["text"]

    paged = run("get_material", "alice", {"material_id": mid, "offset": 0})["result"]
    assert paged["total_chars"] == len(MATERIAL_TEXT) and paged["next_offset"] is None

    res = run("set_material_category", "alice", {"material_id": mid, "category": "exam"})
    assert res["ok"] and res["result"]["category"] == "exam"
    assert not run("set_material_category", "alice", {"material_id": mid, "category": "junk"})["ok"]


def test_remove_material_with_questions(db):
    did = _subject()
    mid = run("add_material", "alice", {"deck_id": did, "name": "Exam", "text": MATERIAL_TEXT})["result"]["id"]
    run("add_questions", "alice", {"deck_id": did, "material_id": mid, "questions": [
        {"type": "open", "question": "Define elasticity.", "reference": "Responsiveness of Q to P."}]})
    assert run("list_materials", "alice", {"deck_id": did})["result"]["materials"][0]["question_count"] == 1
    assert not run("remove_material", "alice", {"material_id": mid, "with_questions": True})["ok"]
    res = run("remove_material", "alice", {"material_id": mid, "with_questions": True, "confirm": True})
    assert res["ok"] and res["result"]["questions_removed"] == 1
    assert run("list_questions", "alice", {"deck_id": did})["result"]["total"] == 0


# ---------------------------------------------------------------- questions

def test_add_questions_validates_and_dedupes(db):
    did = _subject()
    items = [
        {"type": "mcq", "question": "Which is elastic?", "options": ["|e|<1", "|e|>1"], "answer": "b",
         "topic": "Elasticity", "difficulty": "easy"},
        {"type": "mcq", "question": "No answer given", "options": ["a", "b"]},   # rejected
        {"type": "open", "question": "State the law of demand.", "reference": "Q falls as P rises."},
    ]
    res = run("add_questions", "alice", {"deck_id": did, "questions": items})
    assert res["ok"], res
    assert res["result"]["created"] == 2
    again = run("add_questions", "alice", {"deck_id": did, "questions": items})["result"]
    assert again["created"] == 0 and again["duplicates"] == 2

    rows = run("list_questions", "alice", {"deck_id": did})["result"]
    assert rows["total"] == 2 and all(set(r) >= {"id", "qtype", "question"} for r in rows["questions"])
    mcq = next(r for r in rows["questions"] if r["qtype"] == "mcq")
    full = run("get_question", "alice", {"question_id": mcq["id"]})["result"]
    assert full["correct_index"] == 1 and full["options"] == ["|e|<1", "|e|>1"]

    res = run("update_question", "alice", {"question_id": mcq["id"], "correct_index": 5})
    assert not res["ok"]
    res = run("update_question", "alice", {"question_id": mcq["id"], "topic": "Demand", "difficulty": "hard"})
    assert res["ok"] and res["result"]["topic"] == "Demand" and res["result"]["difficulty"] == "hard"

    assert run("set_question_suspended", "alice", {"question_id": mcq["id"], "suspended": True})["result"]["suspended"]
    assert run("delete_question", "alice", {"question_id": mcq["id"]})["ok"]
    assert run("list_questions", "alice", {"deck_id": did})["result"]["total"] == 1


def test_list_questions_filters_by_material_and_pages(db):
    did = _subject()
    m1 = run("add_material", "alice", {"deck_id": did, "name": "A", "text": MATERIAL_TEXT})["result"]["id"]
    m2 = run("add_material", "alice", {"deck_id": did, "name": "B", "text": MATERIAL_TEXT})["result"]["id"]
    run("add_questions", "alice", {"deck_id": did, "material_id": m1, "questions": [
        {"type": "open", "question": f"Q{i} from A", "reference": "r"} for i in range(3)]})
    run("add_questions", "alice", {"deck_id": did, "material_id": m2, "questions": [
        {"type": "open", "question": "Q from B", "reference": "r"}]})
    res = run("list_questions", "alice", {"deck_id": did, "material_id": m1, "limit": 2})["result"]
    assert res["total"] == 3 and len(res["questions"]) == 2 and res["next_offset"] == 2
    res = run("list_questions", "alice", {"deck_id": did, "material_id": m1, "limit": 2, "offset": 2})["result"]
    assert len(res["questions"]) == 1 and res["next_offset"] is None


# ---------------------------------------------------------------- cards / exams / stats

def test_cards_roundtrip(db):
    did = _subject()
    res = run("add_cards", "alice", {"deck_id": did, "cards": [
        {"front": "Law of demand?", "back": "Q falls as P rises"}, {"front": "", "back": "skip"}]})
    assert res["result"]["created"] == 1
    cards = run("list_cards", "alice", {"deck_id": did})["result"]["cards"]
    assert len(cards) == 1
    assert run("delete_card", "alice", {"card_id": cards[0]["id"]})["ok"]


def test_exam_with_linked_subject_and_plan(db):
    did = _subject()
    res = run("create_exam", "alice", {"title": "Midterm", "exam_date": "2099-01-15",
                                       "topics": [{"name": "Demand", "importance": 5, "mastery": 2}],
                                       "deck_id": did})
    assert res["ok"], res
    eid = res["result"]["id"]
    plan = run("generate_plan", "alice", {"exam_id": eid})
    assert plan["ok"] and plan["result"]["days"] > 0
    exams = run("list_exams", "alice")["result"]["exams"]
    assert exams[0]["deck_id"] == did and exams[0]["has_plan"]
    assert not run("create_exam", "alice", {"title": "x", "exam_date": "15/01/2099", "topics": []})["ok"]


def test_study_stats_on_empty_db(db):
    res = run("study_stats", "alice")
    assert res["ok"], res
    assert set(res["result"]["overview"]) >= {"due_total", "today", "exams"}
    assert res["result"]["recent"] == []


# ---------------------------------------------------------------- gating / schemas

def test_code_tools_hidden_unless_enabled_and_admin(db, monkeypatch):
    from src import study_agent as sa
    names = {s["function"]["name"] for s in sa.tool_schemas(False)}
    assert not names & set(sa.CODE_TOOL_NAMES) and "app_info" not in names
    assert set(sa.CODE_TOOL_NAMES) <= {s["function"]["name"] for s in sa.tool_schemas(True)}

    res = run("read_file", "alice", {"path": "README.md"})
    assert not res["ok"] and "disabled" in res["error"]
    import src.tool_security as ts
    monkeypatch.setattr(ts, "owner_is_admin_or_single_user", lambda owner: False)
    res = run("read_file", "alice", {"path": "README.md"}, allow_code=True)
    assert not res["ok"] and "admin" in res["error"]


def test_unknown_tool_and_missing_args(db):
    assert not run("no_such_tool", "alice")["ok"]
    res = run("get_material", "alice", {})
    assert not res["ok"] and "material_id" in res["error"]


def test_tool_schemas_are_well_formed():
    from src import study_agent as sa
    for schema in sa.tool_schemas(True):
        fn = schema["function"]
        assert schema["type"] == "function" and fn["name"] and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])


# ---------------------------------------------------------------- parsing / history

def test_parse_fallback_tool_calls():
    from src import study_agent as sa
    text = ('Let me look.\n```tool_call\n{"name": "list_subjects", "arguments": {}}\n```\n'
            '<tool_call>{"name": "get_material", "arguments": "{\\"material_id\\": \\"m1\\"}"}</tool_call>\n'
            '```tool_call\n{"name": "rm_rf", "arguments": {}}\n```')
    calls = sa.parse_fallback_tool_calls(text)
    assert [c["name"] for c in calls] == ["list_subjects", "get_material"]
    assert json.loads(calls[1]["arguments"]) == {"material_id": "m1"}
    assert sa.strip_fallback_blocks(text) == "Let me look."
    assert sa.parse_fallback_tool_calls("plain prose with ```json\n{}\n```") == []


def test_trim_history_keeps_recent_rounds():
    from src import study_agent as sa
    big = "x" * 2000
    msgs = []
    for r in range(3):
        msgs.append({"role": "user", "content": f"turn {r}"})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{r}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{r}", "content": big})
        msgs.append({"role": "assistant", "content": "done"})
    out = sa.trim_history(msgs, keep_rounds=2)
    tools = [m for m in out if m["role"] == "tool"]
    assert len(tools[0]["content"]) < 400 and "trimmed" in tools[0]["content"]
    assert tools[1]["content"] == big and tools[2]["content"] == big
    assert sa.trim_history([{"role": "user", "content": "hi"}]) == [{"role": "user", "content": "hi"}]


def test_thread_persistence_roundtrip(db):
    from src import study_agent as sa
    t = sa.create_thread("alice", deck_id="d1")
    sa.save_message("alice", t["id"], "user", "Explain elasticity please")
    sa.save_message("alice", t["id"], "assistant", None,
                    tool_calls=[{"id": "c1", "name": "list_subjects", "arguments": "{}"}])
    sa.save_message("alice", t["id"], "tool", '{"subjects":[]}', tool_call_id="c1", name="list_subjects")
    sa.save_message("alice", t["id"], "assistant", "You have no subjects yet.")
    threads = sa.list_threads("alice")
    assert threads[0]["id"] == t["id"] and threads[0]["title"].startswith("Explain elasticity")
    assert sa.list_threads("bob") == []
    ui = sa.thread_messages_for_ui("alice", t["id"])
    assert [m["role"] for m in ui] == ["user", "assistant", "tool", "assistant"]
    assert ui[1]["tool_calls"][0]["name"] == "list_subjects"
    llm = sa.rows_to_llm_messages(sa._load_rows("alice", t["id"]))
    assert llm[1]["tool_calls"][0]["function"]["name"] == "list_subjects"
    assert llm[2] == {"role": "tool", "tool_call_id": "c1", "content": '{"subjects":[]}'}
    with pytest.raises(Exception):
        sa.thread_messages_for_ui("bob", t["id"])
    sa.delete_thread("alice", t["id"])
    assert sa.list_threads("alice") == []


def test_system_prompt_mentions_focus_and_code_state(db):
    from src import study_agent as sa
    did = _subject()
    run("add_material", "alice", {"deck_id": did, "name": "Ch3", "text": MATERIAL_TEXT})
    p = sa.build_system_prompt("alice", did, False, "kimi")
    assert "Focused subject: Micro" in p and "Ch3" in p and "disabled" in p
    p2 = sa.build_system_prompt("alice", None, True, "kimi")
    assert "Code tools are ENABLED" in p2 and sa.code_root() in p2


def test_run_agent_stream_executes_tool_and_persists(db, monkeypatch):
    """Drive the loop with a fake model: round 1 calls list_subjects, round 2 answers."""
    from src import study_agent as sa
    from routes import study_routes as sr
    monkeypatch.setattr(sr, "_resolve_study_model", lambda owner, prefer_text=False: ("http://x", "fake", {}))
    seen = []

    async def fake_stream(url, model, messages, **kw):
        seen.append([m["role"] for m in messages])
        if len(seen) == 1:
            yield 'data: {"delta": "Checking"}\n\n'
            yield 'data: {"type": "tool_calls", "calls": [{"id": "c1", "name": "list_subjects", "arguments": "{}"}]}\n\n'
        else:
            yield 'data: {"delta": "No subjects yet."}\n\n'
        yield "data: [DONE]\n\n"

    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", fake_stream)

    async def collect():
        t = sa.create_thread("alice")
        return t["id"], [c async for c in sa.run_study_agent("alice", t["id"], "what do I have?")]

    tid, chunks = asyncio.run(collect())
    kinds = [json.loads(c[6:]).get("type") for c in chunks if c.startswith("data: {")]
    assert "tool_start" in kinds and "tool_output" in kinds and chunks[-1] == "data: [DONE]\n\n"
    assert seen[1][-1] == "tool"            # tool result fed back to the model
    ui = sa.thread_messages_for_ui("alice", tid)
    assert [m["role"] for m in ui] == ["user", "assistant", "tool", "assistant"]
    assert ui[-1]["content"] == "No subjects yet."
