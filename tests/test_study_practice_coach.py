"""Protected practice generation and spoiler review (plan Task 4).

Deterministic scripted-provider tests: a secret sentinel rides in the stored
reference, tool outputs, and the candidate; if any of it reaches rejected SSE,
visible history, or the persisted UI rows, a test fails. Review outcomes are
forced, so these pin enforcement and containment — the model's ability to
*notice* a semantic spoiler is evaluated separately in the live run
(scripts/evaluate_study_practice_coach.py).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src import study_agent as sa
from src.study_ai import normalize_answer_provenance
from src.study_practice_coach import (
    FIXED_FALLBACK,
    PRACTICE_ROLE,
    PRACTICE_TOOL_NAMES,
    _private_basis_block,
    build_practice_context,
    grading_basis,
    practice_system_prompt,
)

OWNER = "alice"
SENTINEL = "SEKRIT-ANSWER-B"


@pytest.fixture
def db(monkeypatch):
    import os

    import core.database as cdb
    from routes import study_routes as sr
    from tests.helpers.sqlite_db import make_temp_sqlite

    SessionLocal, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)  # forwards to _common
    monkeypatch.setattr(sa, "SessionLocal", SessionLocal)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)
    monkeypatch.setattr(
        sr, "_resolve_study_model",
        lambda owner, prefer_text=False: ("http://x", "scripted-model", {}))
    yield SessionLocal
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _mcq_question(maker):
    from core.database import StudyDeck, StudyQuestion
    session = maker()
    try:
        session.add(StudyDeck(id="d-1", owner=OWNER, name="History"))
        session.add(StudyQuestion(
            id="q-1", owner=OWNER, deck_id="d-1", qtype="mcq",
            question="Which option does the stored key select?",
            options=json.dumps(["Opt A", SENTINEL]), correct_index=1,
            reference=f"The correct option is {SENTINEL} per the source.",
            answer_provenance=json.dumps({
                "reference": {"origin": "document_transcribed",
                              "material_id": "m-1", "page": 3},
                "correct_index": {"origin": "document_transcribed",
                                  "material_id": "m-1", "page": 3},
            }),
            origin="extracted", state="new", stability="0",
            fsrs_difficulty="0", difficulty="medium"))
        session.commit()
    finally:
        session.close()


def _context(_session_maker, **body_kwargs):
    from routes.study._common import AskIn
    body = AskIn(message="help me", **body_kwargs)
    return build_practice_context(OWNER, "q-1", body)


def _chunk(delta=None, calls=None, done=True):
    out = []
    if delta is not None:
        out.append(f"data: {json.dumps({'delta': delta})}\n\n")
    if calls is not None:
        out.append(f"data: {json.dumps({'type': 'tool_calls', 'calls': calls})}\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return out


def _scripted_generation(script, capture=None):
    """script: list of per-round lists of SSE chunk strings. `capture` (dict)
    records every (messages, tools) the provider saw, per round."""
    calls = {"n": 0}

    async def fake_stream(url, model, messages, **kw):
        if capture is not None:
            capture.setdefault("rounds", []).append(
                {"messages": [dict(m) for m in messages],
                 "tools": kw.get("tools")})
        i = calls["n"]
        calls["n"] += 1
        if i >= len(script):
            raise AssertionError("scripted provider ran out of rounds")
        for c in script[i]:
            yield c

    return fake_stream


def _scripted_review(monkeypatch, outcomes):
    """outcomes: ordered list of {"safe": bool} dicts (or a non-dict to be
    malformed, or an exception to raise)."""
    from routes import study_routes as sr
    calls = {"n": 0}
    prompts = []

    async def fake_llm_json(owner, system, user, **kw):
        prompts.append(user)
        i = calls["n"]
        calls["n"] += 1
        outcome = outcomes[i] if i < len(outcomes) else outcomes[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sr, "_llm_json", fake_llm_json)
    return prompts


def _scripted_rewrite(monkeypatch, replies):
    """Returns the captured rewrite prompts alongside installing the fake."""
    from routes import study_routes as sr
    calls = {"n": 0}
    prompts = []

    async def fake_llm_text(owner, system, user, **kw):
        prompts.append(user)
        i = calls["n"]
        calls["n"] += 1
        if isinstance(replies[i], BaseException):
            raise replies[i]
        return replies[i]

    monkeypatch.setattr(sr, "_llm_text", fake_llm_text)
    return prompts


def _collect(chunks):
    events = []
    for c in chunks:
        if not c.startswith("data: "):
            continue
        payload = c[len("data: "):].strip()
        if payload in ("[DONE]",):
            continue
        try:
            events.append(json.loads(payload))
        except ValueError as e:
            raise AssertionError(f"unparseable SSE payload: {payload!r}") from e
    return events


async def _run(thread_id, text, context):
    return [c async for c in sa.run_study_agent(
        OWNER, thread_id, text, practice_context=context)]


# ---------------------------------------------------------------------------
# tool restriction
# ---------------------------------------------------------------------------

def test_practice_schemas_are_exactly_the_learning_allowlist():
    names = {s["function"]["name"] for s in sa.tool_schemas(practice=True)}
    assert names == PRACTICE_TOOL_NAMES
    assert "transcribe_material" not in names
    assert not any(
        "code" in s["function"]["name"] for s in sa.tool_schemas(practice=True))
    search = next(s for s in sa.tool_schemas(practice=True)
                  if s["function"]["name"] == "search_materials")
    assert "protectionism" in search["function"]["description"]


def test_excluded_tool_never_executes_even_with_allow_code(db, monkeypatch):
    called = []

    async def spy(owner, args):
        called.append(args)

    monkeypatch.setattr(sa.TOOLS["add_questions"], "handler", spy)
    res = asyncio.run(sa.dispatch_tool(
        "add_questions", OWNER, {"deck_id": "d-1", "questions": []},
        allow_code=True, practice=True))
    assert res["ok"] is False and called == []
    assert "not available" in res["error"]

    res = asyncio.run(sa.dispatch_tool(
        "bash", OWNER, {"command": "whoami"}, allow_code=True, practice=True))
    assert res["ok"] is False


def test_practice_tool_guidance_rescues_dead_end_retrieval():
    zero_hits = sa._practice_tool_guidance(
        "search_materials", {"ok": True, "result": {"hits": []}}, "[]")
    assert "Try a simpler query" in zero_hits or "simplify" in zero_hits.lower()

    bad_id = sa._practice_tool_guidance(
        "get_material", {"ok": False, "error": "material not found"}, "ERR")
    assert "list_materials" in bad_id

    no_text = sa._practice_tool_guidance(
        "get_material", {"ok": True, "result": {"text": ""}}, "{}")
    assert "no readable text" in no_text


# ---------------------------------------------------------------------------
# buffering, review, and containment
# ---------------------------------------------------------------------------

def test_reference_bearing_tool_output_stays_private_through_generation(
        db, monkeypatch):
    _mcq_question(db)
    context = _context(db)
    capture = {}

    script = [
        _chunk(calls=[{"id": "c1", "name": "get_question",
                       "arguments": json.dumps({"question_id": "q-1"})}]),
        _chunk(delta="Nothing about the answer; think about evidence."),
    ]
    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script, capture))
    _scripted_review(monkeypatch, [{"safe": True, "reason": ""}])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], "help", context))
    events = _collect(chunks)
    types = [e["type"] for e in events]
    assert "delta" not in types and "tool_start" not in types
    assert "tool_output" not in types
    assert types == ["model_info", "status", "status", "reply"]

    # the model privately received the reference-bearing tool output
    second_round = capture["rounds"][1]["messages"]
    assert any(m["role"] == "tool" and SENTINEL in m.get("content", "")
               for m in second_round)

    # nothing answer-bearing appears in visible events or UI history
    ui = sa.thread_messages_for_ui(OWNER, thread["id"])
    assert SENTINEL not in json.dumps(events)
    assert SENTINEL not in json.dumps(ui)
    assert [m["role"] for m in ui] == ["user", "assistant"]


def test_candidate_with_the_exact_answer_is_never_emitted(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)
    capture = {}

    script = [
        _chunk(delta=f"The correct option is {SENTINEL}. Just pick that one."),
    ]
    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script, capture))
    # reject the leaked candidate, accept the rewrite
    _scripted_review(monkeypatch,
                     [{"safe": False, "reason": "gives the exact answer"},
                      {"safe": True, "reason": ""}])
    _scripted_rewrite(monkeypatch,
                      ["Compare the two options with what the question asks."])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], "help", context))
    events = _collect(chunks)
    reply = next(e for e in events if e["type"] == "reply")
    assert reply["content"] == "Compare the two options with what the question asks."
    assert SENTINEL not in json.dumps(events)
    ui = sa.thread_messages_for_ui(OWNER, thread["id"])
    assert SENTINEL not in json.dumps(ui)


def test_rejected_then_still_unsafe_rewrite_falls_back(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)

    script = [_chunk(delta="Think about option A; it is wrong.")]
    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script))
    # first review rejects (decisive elimination), rewrite still leaks,
    # second review rejects again → fixed fallback
    _scripted_review(monkeypatch,
                     [{"safe": False, "reason": "eliminates options"},
                      {"safe": False, "reason": "still leaks"}])
    _scripted_rewrite(monkeypatch, [f"Eliminate A and B: the answer is {SENTINEL}."])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], "help", context))
    events = _collect(chunks)
    reply = next(e for e in events if e["type"] == "reply")
    assert reply["content"] == FIXED_FALLBACK
    assert reply["retryable"] is True
    assert SENTINEL not in json.dumps(events)
    rows = sa._load_rows(OWNER, thread["id"])
    assert SENTINEL not in json.dumps([r.content for r in rows])
    # only the approved fallback is a visible assistant reply
    final = [r for r in rows if r.role == "assistant" and not r.tool_calls]
    assert [r.content for r in final] == [FIXED_FALLBACK]


def test_malformed_reviewer_fails_closed_to_fallback(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)

    script = [_chunk(delta="A harmless clarification.")]
    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script))
    _scripted_review(monkeypatch, ["NOT JSON AT ALL", {"what": "even is this"}])
    _scripted_rewrite(monkeypatch, ["A harmless clarification, reworded."])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], "help", context))
    events = _collect(chunks)
    assert next(e for e in events if e["type"] == "reply")["content"] == \
        FIXED_FALLBACK


def test_reviewer_provider_error_fails_closed(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)

    script = [_chunk(delta="A harmless clarification.")]
    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script))
    _scripted_review(monkeypatch, [RuntimeError("provider down")])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], "help", context))
    events = _collect(chunks)
    assert next(e for e in events if e["type"] == "reply")["content"] == \
        FIXED_FALLBACK


def test_review_sees_prior_hints_and_the_private_basis(db, monkeypatch):
    _mcq_question(db)
    context = _context(db, hints=["Earlier hint: look at the evidence."])
    prior = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    sa.save_message(OWNER, prior["id"], "user", "first ask")
    sa.save_message(OWNER, prior["id"], "assistant", "Focus on demand, not taste.")

    script = [_chunk(delta="Some teaching reply.")]
    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script))
    prompts = _scripted_review(monkeypatch, [{"safe": True, "reason": ""}])

    chunks = asyncio.run(_run(prior["id"], "second ask", context))
    assert chunks  # a full turn ran

    review_prompt = next(p for p in prompts if "<candidate>" in p)
    assert "Earlier hint: look at the evidence." in review_prompt
    assert "Focus on demand, not taste." in review_prompt
    assert SENTINEL in review_prompt  # the private basis is present for review
    assert "private grading basis" in review_prompt


def test_provider_error_is_generic_and_persists_nothing(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)
    seen = []

    async def exploding_stream(url, model, messages, **kw):
        seen.append([m["role"] for m in messages])
        raise RuntimeError("provider exploded with internals 0xDEADBEEF")
        yield  # pragma: no cover - async generator, for the caller

    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", exploding_stream)

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], "help", context))
    events = _collect(chunks)
    err = next(e for e in events if e["type"] == "error")
    assert err["retryable"] is True
    assert "0xDEADBEEF" not in err["message"]
    assert "internals" not in err["message"]
    rows = sa._load_rows(OWNER, thread["id"])
    assert [r.role for r in rows] == ["user"], "no unreviewed text persisted"


def test_cancel_mid_tool_group_repairs_history_for_next_turn(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)
    capture = {}

    async def fake_dispatch(name, owner, args, **kw):
        if name == "study_stats":
            raise asyncio.CancelledError()
        return {"ok": True, "result": {"subjects": []}}

    monkeypatch.setattr(sa, "dispatch_tool", fake_dispatch)
    import src.llm_core as lc

    script = [_chunk(calls=[
        {"id": "c1", "name": "list_subjects", "arguments": "{}"},
        {"id": "c2", "name": "study_stats", "arguments": "{}"},
    ])]
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script, capture))

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run(thread["id"], "help", context))
    assert sa.practice_turn_active(thread["id"]) is False
    # the tool-call row exists but its second result does not
    rows = sa._load_rows(OWNER, thread["id"])
    assert [r.name for r in rows if r.role == "tool"] == ["list_subjects"]

    # resume: a plain answer turn must send a well-formed history to the model
    script2 = [_chunk(delta="Let's continue.")]
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script2, capture))
    _scripted_review(monkeypatch, [{"safe": True, "reason": ""}])
    chunks = asyncio.run(_run(thread["id"], "again", context))
    assert any(e["type"] == "reply" for e in _collect(chunks))
    resumed = capture["rounds"][-1]["messages"]
    tool_contents = [m.get("content", "") for m in resumed
                     if m.get("role") == "tool"]
    assert any("interrupted" in c for c in tool_contents), (
        "the dangling tool call must gain an explicit failed result")


def test_concurrent_turn_on_one_thread_is_rejected(db, monkeypatch):
    _mcq_question(db)
    context = _context(db)
    gate = asyncio.Event()

    async def blocked_stream(url, model, messages, **kw):
        await gate.wait()
        for c in _chunk(delta="Done."):
            yield c

    import src.llm_core as lc
    monkeypatch.setattr(lc, "stream_llm", blocked_stream)
    _scripted_review(monkeypatch, [{"safe": True, "reason": ""}])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")

    async def scenario():
        async def first_turn():
            return [c async for c in sa.run_study_agent(
                OWNER, thread["id"], "one", practice_context=context)]

        task = asyncio.ensure_future(first_turn())
        for _ in range(200):
            if sa.practice_turn_active(thread["id"]):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("first turn never became active")

        second = await _run(thread["id"], "two", context)
        err = next(e for e in _collect(second) if e["type"] == "error")
        assert "already answering" in err["message"]

        gate.set()
        first = await task
        assert any(e["type"] == "reply" for e in _collect(first))
        assert sa.practice_turn_active(thread["id"]) is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# policy and ordinary-tutor non-regression
# ---------------------------------------------------------------------------

def test_practice_prompt_is_the_practice_role_not_tutor(db):
    _mcq_question(db)
    context = _context(db, answered=True)
    prompt = practice_system_prompt(context, model="scripted-model")
    assert "Ask AI coach" in prompt
    assert "withhold" in prompt.lower()
    assert "unrestricted tutor" not in prompt


def test_ordinary_tutor_path_still_streams_and_uses_full_schemas(
        db, monkeypatch):
    import src.llm_core as lc
    capture = {}
    script = [_chunk(calls=[{"id": "c1", "name": "list_subjects",
                             "arguments": "{}"}]),
              _chunk(delta="You have no subjects yet.")]
    monkeypatch.setattr(lc, "stream_llm", _scripted_generation(script, capture))

    thread = sa.create_thread(OWNER, deck_id="d-1")

    async def collect():
        return [c async for c in sa.run_study_agent(
            OWNER, thread["id"], "what do I have?")]

    chunks = asyncio.run(collect())
    events = _collect(chunks)
    types = [e.get("type") or "delta" for e in events]
    assert "delta" in types, "ordinary tutoring still streams deltas"
    assert "tool_start" in types and "tool_output" in types
    names = {s["function"]["name"] for s in capture["rounds"][0]["tools"]}
    assert "add_questions" in names and "list_subjects" in names


# ---------------------------------------------------------------------------
# review input + generator prompt completeness (review findings 1 and 6)
# ---------------------------------------------------------------------------

SETUP_SENTINEL = "SETUP-SENTINEL n=30 samples of x ~ N(0,1)"
DRAFT_SENTINEL = "DRAFT-SENTINEL my partial derivation: sigma_hat ="
ATTEMPT_SENTINEL = "ATTEMPT-SENTINEL stored answer text"
PROV_EXCERPT_SENTINEL = "PROV-EXCERPT-SENTINEL copied from page 3"
LATEST_MESSAGE = "Is my approach to this part right?"


def _open_multipart(db):
    """An open multipart question with a setup, NO reference, provenance
    evidence, a draft, and a server-verified attempt."""
    from core.database import StudyAttempt, StudyDeck, StudyQuestion
    s = db()
    try:
        s.add(StudyDeck(id="d-1", owner=OWNER, name="History"))
        s.add(StudyQuestion(
            id="q-1", owner=OWNER, deck_id="d-1", qtype="open",
            question="Calculate the value the setup defines.",
            context=SETUP_SENTINEL, reference=None, correct_index=None,
            answer_provenance=json.dumps({
                "reference": {"origin": "document_transcribed",
                              "material_id": "m-1", "page": 3,
                              "excerpt": PROV_EXCERPT_SENTINEL},
                "correct_index": {"origin": "unknown"},
            }),
            origin="extracted", state="new", stability="0",
            fsrs_difficulty="0", difficulty="medium"))
        s.add(StudyAttempt(
            id="att-match", owner=OWNER, question_id="q-1", deck_id="d-1",
            answer=ATTEMPT_SENTINEL, correct=False, score=55, rating=1,
            hints_used=1, confidence=55, idempotency_key="match-key"))
        s.commit()
    finally:
        s.close()


def _open_context(db, **body_kwargs):
    from routes.study._common import AskIn
    body = AskIn(message=LATEST_SENTINEL, **body_kwargs)
    return build_practice_context(OWNER, "q-1", body)


LATEST_SENTINEL = "Is my draft the right approach?"


def test_review_and_rewrite_requests_carry_the_full_problem(
        db, monkeypatch):
    _open_multipart(db)
    context = _open_context(db, draft=DRAFT_SENTINEL,
                            submission_id="match-key")
    import src.llm_core as lc
    capture = {}
    monkeypatch.setattr(
        lc, "stream_llm",
        _scripted_generation([_chunk(delta="A teaching reply.")], capture))
    review_prompts = _scripted_review(
        monkeypatch, [{"safe": False, "reason": "mentions the target result"},
                      {"safe": True, "reason": ""}])
    rewrite_prompts = _scripted_rewrite(monkeypatch, ["A safer teaching reply."])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], LATEST_SENTINEL, context))
    assert next(e for e in _collect(chunks) if e["type"] == "reply")

    review = review_prompts[0]
    assert SETUP_SENTINEL in review, "the reviewer never sees the problem setup"
    assert DRAFT_SENTINEL in review, (
        "draft edits are not chat messages — the reviewer input must carry them")
    assert ATTEMPT_SENTINEL in review, (
        "the verified stored attempt must be part of the review evidence")
    assert "submission verified by server: yes" in review
    assert LATEST_SENTINEL in review, "the latest request is missing"
    assert "grading instructions" in review and "Partial credit" in review
    assert PROV_EXCERPT_SENTINEL in review

    rewrite = rewrite_prompts[-1]
    assert SETUP_SENTINEL in rewrite and DRAFT_SENTINEL in rewrite
    assert ATTEMPT_SENTINEL in rewrite
    assert "Partial credit" in rewrite


def test_generator_prompt_carries_grading_instructions_and_evidence(
        db, monkeypatch):
    _open_multipart(db)
    context = _open_context(db, draft=DRAFT_SENTINEL,
                            submission_id="match-key")
    import src.llm_core as lc
    capture = {}
    monkeypatch.setattr(
        lc, "stream_llm",
        _scripted_generation([_chunk(delta="A harmless reply.")], capture))
    _scripted_review(monkeypatch, [{"safe": True, "reason": ""}])

    thread = sa.create_thread(OWNER, deck_id="d-1", question_id="q-1")
    chunks = asyncio.run(_run(thread["id"], LATEST_SENTINEL, context))
    events = _collect(chunks)

    system = capture["rounds"][0]["messages"][0]["content"]
    assert "grading instructions" in system and "Partial credit" in system, (
        "the coach never sees what the grader checks")
    assert "grading fallback" in system
    assert "derives an answer at submission time" in system, (
        "the no-reference fallback must be named as such")
    assert PROV_EXCERPT_SENTINEL in system, (
        "provenance evidence is collected but never shown to the model")
    assert "source material id: m-1" in system and "source page: 3" in system

    # all of this stays private: it may not appear in visible events
    visible = json.dumps(events)
    assert PROV_EXCERPT_SENTINEL not in visible
    assert "Partial credit" not in visible


# ---------------------------------------------------------------------------
# private grading-basis description matches the attempt route's grading inputs
# ---------------------------------------------------------------------------

def _prov():
    return {"reference": {"origin": "unknown"},
            "correct_index": {"origin": "unknown"}}


def test_private_basis_describes_actual_grading_path():
    # 1. open question with a reference: route grades via basis["open_reference"]
    open_with = grading_basis(qtype="open", reference="complete answer",
                              correct_index=None, options=[])
    assert open_with["open_reference"] == "complete answer"
    open_with_text = _private_basis_block(
        open_with, _prov(), intro="intro")
    assert "a stored reference exists as shown below" in open_with_text
    assert "derives an answer at submission time" not in open_with_text
    assert "reference text: complete answer" in open_with_text

    # 2. open question without a reference: route grades via the
    # derive-an-answer instruction in basis["open_reference"]
    open_without = grading_basis(qtype="open", reference="",
                                 correct_index=None, options=[])
    assert open_without["missing_reference"] is True
    assert "first work out the correct answer" in open_without["open_reference"]
    open_without_text = _private_basis_block(
        open_without, _prov(), intro="intro")
    assert "derives an answer at submission time" in open_without_text

    # 3. MCQ with a blank written reference but a valid stored key: the
    # attempt route grades an ordinary MCQ by comparing the submitted
    # option index with basis["index_basis"]["correct_index"], and typed
    # recall against basis["typed_recall_reference"] (the keyed option
    # text) — never via the open-question derive-an-answer instruction
    # (routes/study/practice.py:433-447).
    mcq_keyed = grading_basis(qtype="mcq", reference=None,
                              correct_index=1, options=["A", "B"])
    assert mcq_keyed["missing_reference"] is True
    assert mcq_keyed["index_basis"] == {"correct_index": 1}
    assert mcq_keyed["typed_recall_reference"] == "B"
    mcq_text = _private_basis_block(mcq_keyed, _prov(), intro="intro")
    assert "derives an answer at submission time" not in mcq_text
    assert "stored correct_index" in mcq_text
    assert "keyed option text" in mcq_text
    assert "correct option index: 1" in mcq_text
    assert "reference text: B" in mcq_text