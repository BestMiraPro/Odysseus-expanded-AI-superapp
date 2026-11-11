"""Socratic teaching sessions ("Teach me" in the Tutor tab).

The rules in src/study_socratic.py are the point of the feature: a
prompt-only tutor grades itself, this one cannot. So most of these tests pin
a rule a model would otherwise be free to bend (placement without a
diagnostic, levels for prompted recall, an early final exam, a pass with a
missed answer). The rest check the agent wiring: the session allowlist, the
thread id the model cannot choose, the hidden tool outputs, and a full turn.
"""
import asyncio
import json
import os

import pytest

from src import study_socratic as ss
from tests.helpers.sqlite_db import make_temp_sqlite

SKILLS = ["Definition", "Computing elasticity", "Determinants", "Total revenue link"]


def _session(target_full=False):
    st = ss.new_state("Price elasticity of demand", full_mastery=target_full)
    ss.set_skill_map(st, SKILLS)
    return st


def _diagnose(st, answers=3):
    for _ in range(answers):
        ss.begin_turn(st)
        ss.record_check(st, "Definition", "correct")
    return st


def _teaching(levels=None, focus=None):
    st = _diagnose(_session())
    ss.place(st, levels or {"Definition": 2, "Computing elasticity": 1}, focus=focus)
    return st


def _answer(st, skill, outcome, kind="unprompted"):
    ss.begin_turn(st)
    return ss.record_check(st, skill, outcome, kind)


def _skill(st, name):
    return ss.find_skill(st, name)


# ---------------------------------------------------------------- session start

def test_new_session_needs_a_topic_and_sets_the_target():
    with pytest.raises(ss.TutorError):
        ss.new_state("   ")
    assert ss.new_state("Bayes").get("target") == 4
    assert ss.new_state("Bayes", full_mastery=True)["target"] == 5
    assert ss.new_state("Bayes")["phase"] == "diagnostic"


def test_skill_map_size_and_duplicates():
    st = ss.new_state("Bayes")
    with pytest.raises(ss.TutorError):
        ss.set_skill_map(st, ["only one"])
    with pytest.raises(ss.TutorError):
        ss.set_skill_map(st, [f"s{i}" for i in range(11)])
    res = ss.set_skill_map(st, ["Priors", "priors", {"name": "Likelihood"}, ""], outline=["Conjugacy"])
    assert res["skills"] == ["Priors", "Likelihood"]
    assert st["outline"] == ["Conjugacy"]


def test_skill_map_is_fixed_once_teaching_starts():
    st = _teaching()
    with pytest.raises(ss.TutorError, match="diagnostic"):
        ss.set_skill_map(st, ["A", "B"])


def test_skills_resolve_by_key_name_or_unique_prefix():
    st = _session()
    assert _skill(st, "s2")["name"] == "Computing elasticity"
    assert _skill(st, "determinants")["name"] == "Determinants"
    assert _skill(st, "Total")["name"] == "Total revenue link"
    with pytest.raises(ss.TutorError, match="Unknown skill"):
        _skill(st, "Supply")


def test_placement_above_one_needs_a_diagnostic():
    st = _session()
    ss.begin_turn(st)
    ss.record_check(st, "Definition", "correct")
    with pytest.raises(ss.TutorError, match="diagnostic"):
        ss.place(st, {"Definition": 2})
    # Someone who knows nothing can be placed at once.
    ss.place(st, {"Definition": 1})
    assert st["phase"] == "teaching"


def test_one_answer_touching_two_skills_is_one_diagnostic_question():
    st = _session()
    for _ in range(2):
        ss.begin_turn(st)
        ss.record_check(st, "Definition", "correct")
        ss.record_check(st, "Determinants", "partial")
    with pytest.raises(ss.TutorError, match="2 answered"):
        ss.place(st, {"Definition": 3})


def test_placement_is_capped_and_focus_starts_lowest():
    st = _diagnose(_session())
    res = ss.place(st, {"Definition": 5, "Computing elasticity": 2, "Determinants": 1, "Total revenue link": 3})
    assert _skill(st, "Definition")["level"] == ss.MAX_PLACEMENT
    assert "capped" in res
    assert res["focus"] == "Determinants"
    assert all(s["placed"] for s in st["skills"])


def test_diagnostic_answers_do_not_move_levels():
    st = _session()
    for _ in range(4):
        ss.begin_turn(st)
        res = ss.record_check(st, "Definition", "correct", "unprompted")
        assert res["kind"] == "diagnostic" and res["level"] == 0


# ---------------------------------------------------------------- levels are earned

def test_two_prompted_answers_do_not_raise_a_level():
    st = _teaching()
    _answer(st, "Computing elasticity", "correct", "prompted")
    res = _answer(st, "Computing elasticity", "correct", "prompted")
    assert res["level"] == 1 and not res["changed"]
    assert "independent" in res["needs"]


def test_two_correct_with_one_independent_raises_by_exactly_one():
    st = _teaching()
    _answer(st, "Computing elasticity", "correct", "prompted")
    res = _answer(st, "Computing elasticity", "correct", "unprompted")
    assert res["changed"] and res["level"] == 2 and res["previous_level"] == 1
    assert "→ 2/5" in res["say"]
    # The evidence restarts: one more correct answer is not enough for 3.
    res = _answer(st, "Computing elasticity", "correct", "unprompted")
    assert res["level"] == 2


def test_a_wrong_answer_breaks_the_streak_and_partial_does_not_count():
    st = _teaching()
    _answer(st, "Computing elasticity", "correct", "unprompted")
    _answer(st, "Computing elasticity", "wrong", "unprompted")
    res = _answer(st, "Computing elasticity", "correct", "unprompted")
    assert res["level"] == 1                     # streak restarted after the miss
    res = _answer(st, "Computing elasticity", "partial", "unprompted")
    assert res["level"] == 1                     # partial is not correct...
    res = _answer(st, "Computing elasticity", "correct", "unprompted")
    assert res["level"] == 2                     # ...and did not reset either


def test_an_answer_right_after_an_explanation_counts_as_prompted():
    st = _teaching()
    ss.begin_turn(st)
    ss.mark_explained(st, "Computing elasticity")
    res = _answer(st, "Computing elasticity", "correct", "unprompted")   # next turn
    assert res["kind"] == "prompted" and "downgraded" in res
    res = _answer(st, "Computing elasticity", "correct", "unprompted")   # two turns later
    assert res["kind"] == "unprompted"
    assert res["level"] == 2


def test_failed_review_drops_a_level_but_ordinary_misses_do_not():
    st = _teaching()
    _answer(st, "Definition", "wrong", "unprompted")
    assert _skill(st, "Definition")["level"] == 2
    res = _answer(st, "Definition", "wrong", "review")
    assert res["level"] == 1 and res["changed"]


def test_bad_outcome_or_kind_is_refused():
    st = _teaching()
    with pytest.raises(ss.TutorError, match="outcome"):
        _answer(st, "Definition", "mostly right")
    with pytest.raises(ss.TutorError, match="kind"):
        _answer(st, "Definition", "correct", "vibes")


def test_level_five_needs_an_edge_case_and_a_teach_back():
    st = _teaching()
    s = _skill(st, "Definition")
    s["level"] = 4
    _answer(st, "Definition", "correct", "unprompted")
    res = _answer(st, "Definition", "correct", "review")
    assert res["level"] == 4 and "edge case" in res["needs"]
    _answer(st, "Definition", "correct", "edge_case")
    assert _skill(st, "Definition")["level"] == 4
    res = _answer(st, "Definition", "correct", "teach_back")
    assert res["level"] == 5


def test_a_drop_from_five_clears_the_edge_case_evidence():
    st = _teaching()
    s = _skill(st, "Definition")
    s.update(level=5, edge_ok=True, teach_ok=True)
    _answer(st, "Definition", "wrong", "review")
    assert s["level"] == 4 and not s["edge_ok"] and not s["teach_ok"]


def test_misconceptions_are_kept_once():
    st = _teaching()
    for _ in range(2):
        ss.begin_turn(st)
        ss.record_check(st, "Definition", "wrong", "unprompted",
                        misconception="Thinks elasticity is the slope of the demand curve")
    assert _skill(st, "Definition")["misconceptions"] == [
        "Thinks elasticity is the slope of the demand curve"]


# ---------------------------------------------------------------- spaced review

def test_review_comes_due_for_an_untested_skill_held_at_two_or_more():
    st = _teaching({"Definition": 2, "Computing elasticity": 1}, focus="Computing elasticity")
    assert ss.review_due(st) is None             # Definition was checked in the diagnostic just now
    for _ in range(ss.REVIEW_GAP_TURNS):
        _answer(st, "Computing elasticity", "partial")
    due = ss.review_due(st)
    assert due and due["skill"] == "Definition"
    assert "SPACED REVIEW DUE: Definition" in ss.prompt_state(st)
    _answer(st, "Definition", "correct", "review")
    assert ss.review_due(st) is None


def test_the_focus_skill_and_weak_skills_are_never_due():
    st = _teaching({"Definition": 1, "Computing elasticity": 3}, focus="Computing elasticity")
    for _ in range(ss.REVIEW_GAP_TURNS + 2):
        ss.begin_turn(st)
    assert ss.review_due(st) is None             # focus excluded; Definition is only 1/5


def test_the_learner_panel_never_sees_the_review_schedule():
    st = _teaching({"Definition": 2}, focus="Computing elasticity")
    for _ in range(ss.REVIEW_GAP_TURNS):
        ss.begin_turn(st)
    pub = ss.public_state(st)
    assert ss.review_due(st)
    assert "review" not in json.dumps(pub).lower()
    assert pub["focus"] == "Computing elasticity"
    assert [s["name"] for s in pub["skills"]] == SKILLS


# ---------------------------------------------------------------- final exam

def _all_at_target(st):
    for s in st["skills"]:
        s["level"] = st["target"]


def test_final_opens_only_at_the_target():
    st = _teaching()
    with pytest.raises(ss.TutorError, match="Below target"):
        ss.start_final(st)
    _all_at_target(st)
    assert ss.start_final(st)["phase"] == "final"


def test_finish_needs_an_answer_per_skill():
    st = _teaching()
    _all_at_target(st)
    ss.start_final(st)
    _answer(st, "Definition", "correct")
    with pytest.raises(ss.TutorError, match="Computing elasticity"):
        ss.finish(st, integrative_correct=True)


def test_finish_passes_only_when_every_final_answer_was_right():
    st = _teaching()
    _all_at_target(st)
    ss.start_final(st)
    for name in SKILLS:
        _answer(st, name, "correct")
    res = ss.finish(st, integrative_correct=True, summary="Solid.")
    assert res["passed"] and st["phase"] == "complete"
    with pytest.raises(ss.TutorError, match="complete"):
        _answer(st, "Definition", "correct")


def test_a_missed_final_answer_fails_and_reopens_the_loop():
    st = _teaching()
    _all_at_target(st)
    ss.start_final(st)
    for name in SKILLS:
        _answer(st, name, "wrong" if name == "Determinants" else "correct")
    assert _skill(st, "Determinants")["level"] == st["target"] - 1
    res = ss.finish(st, integrative_correct=True)
    assert not res["passed"] and res["missed"] == ["Determinants"]
    assert st["phase"] == "teaching" and st["focus"] == _skill(st, "Determinants")["key"]


def test_a_failed_integrative_problem_fails_the_session():
    st = _teaching()
    _all_at_target(st)
    ss.start_final(st)
    for name in SKILLS:
        _answer(st, name, "correct")
    assert not ss.finish(st, integrative_correct=False)["passed"]
    assert st["phase"] == "teaching"


def test_map_renders_bars_levels_and_focus():
    st = _teaching()
    out = ss.render_map(st)
    assert "Topic: Price elasticity of demand" in out
    assert "[████░░░░░░]  2/5" in out and "overall: ██░░░░░░░░ 15%" in out
    assert "<- current focus" in out
    assert "?/5" in ss.render_map(_session())


def test_difficulty_is_clamped():
    st = _session()
    for _ in range(5):
        res = ss.adjust_difficulty(st, "harder")
    assert res["difficulty"] == 2 and "note" in res
    with pytest.raises(ss.TutorError):
        ss.adjust_difficulty(st, "sideways")


# ---------------------------------------------------------------- agent wiring

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


def _teach_thread(owner="alice", topic="Elasticity", deck_id=None):
    from src import study_agent as sa
    return sa.create_thread(owner, deck_id=deck_id, teach_topic=topic)["id"]


def test_teach_thread_is_listed_as_a_session(db):
    from src import study_agent as sa
    tid = _teach_thread()
    sa.create_thread("alice")
    rows = {t["id"]: t for t in sa.list_threads("alice")}
    assert rows[tid]["mode"] == "socratic" and rows[tid]["title"] == "Elasticity"
    assert [t["mode"] for t in rows.values()].count(None) == 1
    assert sa.tutor_state("alice", tid)["phase"] == "diagnostic"
    assert sa.tutor_state("bob", tid) is None


def test_session_tools_are_scoped_to_the_session(db):
    from src import study_agent as sa
    tid = _teach_thread()
    names = {s["function"]["name"] for s in sa.tool_schemas(socratic=True)}
    assert names == set(sa.SOCRATIC_TOOL_NAMES)
    normal = {s["function"]["name"] for s in sa.tool_schemas(True)}
    assert sa.START_TEACHING_TOOL in normal
    assert not any(n.startswith("tutor_") for n in normal)
    # The allowlist holds at dispatch, whatever the model writes.
    for name in ("delete_subject", "bash", "remove_material", sa.START_TEACHING_TOOL):
        res = run(name, "alice", {"confirm": True}, socratic=True, thread_id=tid)
        assert not res["ok"] and "teaching session" in res["error"]
    res = run("tutor_progress", "alice", {}, thread_id=tid)
    assert not res["ok"] and "outside a teaching session" in res["error"]


def test_the_model_cannot_point_tutor_tools_at_another_thread(db):
    alice = _teach_thread("alice")
    bob = _teach_thread("bob")
    res = run("tutor_set_skill_map", "bob", {"skills": SKILLS, "_thread_id": alice},
              socratic=True, thread_id=bob)
    assert res["ok"]
    from src import study_agent as sa
    assert sa.tutor_state("alice", alice)["skills"] == []
    assert len(sa.tutor_state("bob", bob)["skills"]) == 4
    # And bob's dispatch cannot reach alice's thread even when it is the bound one.
    res = run("tutor_progress", "bob", {}, socratic=True, thread_id=alice)
    assert not res["ok"] and "not found" in res["error"].lower()


def test_a_refused_rule_saves_nothing(db):
    from src import study_agent as sa
    tid = _teach_thread()
    run("tutor_set_skill_map", "alice", {"skills": SKILLS}, socratic=True, thread_id=tid)
    res = run("tutor_place", "alice", {"levels": {"Definition": 3}}, socratic=True, thread_id=tid)
    assert not res["ok"] and "diagnostic" in res["error"]
    assert sa.tutor_state("alice", tid)["phase"] == "diagnostic"
    res = run("tutor_place", "alice", {"levels": '{"Definition": 1}'}, socratic=True, thread_id=tid)
    assert res["ok"] and sa.tutor_state("alice", tid)["phase"] == "teaching"


def test_start_teaching_session_converts_an_ordinary_chat_once(db):
    from src import study_agent as sa
    tid = sa.create_thread("alice")["id"]
    res = run(sa.START_TEACHING_TOOL, "alice", {"topic": "Bayes rule", "full_mastery": True}, thread_id=tid)
    assert res["ok"] and res["result"]["target"] == 5
    assert sa.list_threads("alice")[0]["mode"] == "socratic"
    res = run(sa.START_TEACHING_TOOL, "alice", {"topic": "Again"}, thread_id=tid)
    assert not res["ok"] and "already" in res["error"]


def test_a_practice_thread_cannot_become_a_session(db):
    from src import study_agent as sa
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        sa.create_thread("alice", question_id="q1", teach_topic="x")


def test_add_cards_keeps_tags(db):
    from src import study_agent as sa
    from core.database import StudyCard
    did = run("create_subject", "alice", {"name": "Micro"})["result"]["id"]
    res = run("add_cards", "alice", {"deck_id": did, "cards": [
        {"front": "Elastic demand?", "back": "|e| > 1", "tags": ["Elasticity", " ", "Definition"]}]},
        socratic=True, thread_id=_teach_thread())
    assert res["ok"] and res["result"]["created"] == 1
    s = db()
    try:
        assert json.loads(s.query(StudyCard).one().tags) == ["Elasticity", "Definition"]
    finally:
        s.close()


def test_session_history_shows_that_tools_ran_not_what_they_returned(db):
    from src import study_agent as sa
    tid = _teach_thread()
    sa.save_message("alice", tid, "user", "teach me")
    sa.save_message("alice", tid, "assistant", None, tool_calls=[{"id": "c1", "name": "search_materials",
                                                                  "arguments": "{}"}])
    sa.save_message("alice", tid, "tool", '{"hits": "the answer is |e| > 1"}', tool_call_id="c1",
                    name="search_materials")
    tool_rows = [m for m in sa.thread_messages_for_ui("alice", tid) if m["role"] == "tool"]
    assert tool_rows == [{**tool_rows[0], "output": ""}] and tool_rows[0]["name"] == "search_materials"


def test_socratic_prompt_carries_state_and_learner_history(db):
    from src import study_agent as sa
    from core.database import StudyAttempt, StudyQuestion
    did = run("create_subject", "alice", {"name": "Micro"})["result"]["id"]
    s = db()
    try:
        s.add(StudyQuestion(id="q1", owner="alice", deck_id=did, question="?", reference="a",
                            qtype="open", topic="Cross elasticity"))
        for i in range(3):
            s.add(StudyAttempt(id=f"a{i}", owner="alice", question_id="q1", correct=(i == 0)))
        s.commit()
    finally:
        s.close()
    st = sa.tutor_state  # keep the name used below short
    tid = _teach_thread(deck_id=did)
    p = sa.build_socratic_prompt("alice", did, st("alice", tid), "fake")
    assert "TEACHING SESSION — topic: Elasticity" in p
    assert "NOW: start the session" in p
    assert "Cross elasticity: 33% right over 3 answers" in p
    assert "Focused subject: Micro" in p


def _fake_model(monkeypatch, rounds):
    """Each round is a list of SSE lines; records the system prompt per call."""
    from routes import study_routes as sr
    import src.llm_core as lc
    monkeypatch.setattr(sr, "_resolve_study_model", lambda owner, prefer_text=False: ("http://x", "fake", {}))
    seen = []

    async def fake_stream(url, model, messages, **kw):
        seen.append({"system": messages[0]["content"],
                     "tools": {t["function"]["name"] for t in kw.get("tools") or []}})
        for line in rounds[min(len(seen), len(rounds)) - 1]:
            yield line
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(lc, "stream_llm", fake_stream)
    return seen


def _call(cid, name, args):
    return 'data: ' + json.dumps({"type": "tool_calls", "calls": [
        {"id": cid, "name": name, "arguments": json.dumps(args)}]}) + "\n\n"


def _events(chunks):
    return [json.loads(c[6:]) for c in chunks if c.startswith("data: {")]


def test_a_session_turn_streams_the_skill_map_and_hides_tool_output(db, monkeypatch):
    from src import study_agent as sa
    seen = _fake_model(monkeypatch, [
        [_call("c1", "tutor_set_skill_map", {"skills": SKILLS})],
        ['data: {"delta": "First question: what does elasticity measure?"}\n\n'],
    ])
    tid = _teach_thread()

    async def go():
        return [c async for c in sa.run_study_agent("alice", tid, "Teach me elasticity", allow_code=True)]

    evs = _events(asyncio.run(go()))
    assert "TEACHING SESSION" in seen[0]["system"] and "bash" not in seen[0]["tools"]
    assert seen[0]["tools"] == set(sa.SOCRATIC_TOOL_NAMES)
    states = [e["state"] for e in evs if e.get("type") == "tutor_state"]
    assert states[0]["skills"] == [] and [s["name"] for s in states[-1]["skills"]] == SKILLS
    outputs = [e for e in evs if e.get("type") == "tool_output"]
    assert outputs and all(e["output"] == "" for e in outputs)
    assert "Skills:\n- Definition: unplaced" in seen[1]["system"]   # prompt refreshed after the tool
    assert sa.tutor_state("alice", tid)["turn"] == 1


def test_an_ordinary_chat_switches_to_the_tutor_mid_turn(db, monkeypatch):
    from src import study_agent as sa
    seen = _fake_model(monkeypatch, [
        [_call("c1", sa.START_TEACHING_TOOL, {"topic": "Elasticity"})],
        [_call("c2", "tutor_set_skill_map", {"skills": SKILLS})],
        ['data: {"delta": "Let us start."}\n\n'],
    ])
    tid = sa.create_thread("alice")["id"]

    async def go():
        return [c async for c in sa.run_study_agent("alice", tid, "teach me elasticity properly")]

    evs = _events(asyncio.run(go()))
    assert "TEACHING SESSION" not in seen[0]["system"] and "tutor_record_check" not in seen[0]["tools"]
    assert "TEACHING SESSION" in seen[1]["system"] and "tutor_record_check" in seen[1]["tools"]
    assert any(e.get("type") == "mode" and e["mode"] == "socratic" for e in evs)
    assert len(sa.tutor_state("alice", tid)["skills"]) == 4


def test_a_skill_map_in_the_same_batch_as_the_start_call_runs_as_the_tutor(db, monkeypatch):
    from src import study_agent as sa
    both = 'data: ' + json.dumps({"type": "tool_calls", "calls": [
        {"id": "c1", "name": sa.START_TEACHING_TOOL, "arguments": json.dumps({"topic": "Elasticity"})},
        {"id": "c2", "name": "tutor_set_skill_map", "arguments": json.dumps({"skills": SKILLS})}]}) + "\n\n"
    _fake_model(monkeypatch, [[both], ['data: {"delta": "First question."}\n\n']])
    tid = sa.create_thread("alice")["id"]

    async def go():
        return [c async for c in sa.run_study_agent("alice", tid, "teach me elasticity")]

    evs = _events(asyncio.run(go()))
    assert all(e["ok"] for e in evs if e.get("type") == "tool_output")
    assert len(sa.tutor_state("alice", tid)["skills"]) == 4
