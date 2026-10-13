"""Council steering: notes mid-run, "finish now", and stopped turns in later history."""

from __future__ import annotations

import asyncio
import json
import random

from src import council


def _member(name: str, provider: str = "openai") -> council.Member:
    return council.Member(endpoint_id=f"ep-{name}", model=name, endpoint_name=f"EP {name}",
                          kind="api", provider=provider, url=f"https://{name}.example/v1/chat/completions")


def _text(chunks):
    for text in chunks:
        yield f"data: {json.dumps({'delta': text})}\n\n"
    yield "data: [DONE]\n\n"


def _role(messages) -> str:
    system = messages[0]["content"]
    if system.startswith(council.REVIEW_SYSTEM):
        return "review"
    if system.startswith(council.CHAIR_SYSTEM):
        return "chair"
    return "opinion"


# ── history: stopped turns ──

SEATS = {"members": [{"model": "kimi", "endpoint_name": "W&B"}, {"model": "glm", "endpoint_name": "W&B"}]}


def test_a_finished_turn_replays_its_answer():
    entry = council.history_entry({"question": "Q?", "final": "A.", "status": "done"})
    assert entry == {"question": "Q?", "final": "A."}


def test_a_turn_stopped_before_synthesis_replays_the_members_answers():
    entry = council.history_entry({
        "question": "Plan my trip", "final": "", "status": "cancelled", "config": SEATS,
        "opinions": [{"member": 0, "text": "Go to Lisbon."},
                     {"member": 1, "text": "Porto, then", "error": "Stopped before this member finished.",
                      "stopped": True}],
    })
    assert "stopped before the chairman wrote" in entry["final"]
    assert "### kimi via W&B\nGo to Lisbon." in entry["final"]
    assert "### glm via W&B (cut off mid-answer)\nPorto, then" in entry["final"]


def test_a_turn_stopped_mid_synthesis_replays_the_partial_answer():
    entry = council.history_entry({"question": "Q", "final": "Half an answer", "status": "cancelled"})
    assert entry["final"].startswith("[NOTE: this round was stopped before the chairman finished")
    assert entry["final"].endswith("Half an answer")


def test_interrupted_and_empty_turns_still_appear():
    entry = council.history_entry({"question": "Q", "status": "running", "opinions": []})
    assert entry == {"question": "Q", "final": "[NOTE: this round was interrupted (Odysseus restarted) "
                                               "before any member answered.]"}


def test_history_entries_carry_the_turns_notes():
    entry = council.history_entry({"question": "Q", "final": "A", "status": "done",
                                   "notes": [{"text": "only EU options"}]})
    assert entry["question"].startswith("Q\n\nThe user added these notes")
    assert "1. only EU options" in entry["question"]


def test_stopped_turns_reach_the_next_turns_members():
    history = [council.history_entry({"question": "First?", "final": "", "status": "cancelled", "config": SEATS,
                                      "opinions": [{"member": 0, "text": "kimi's idea"}]})]
    msgs = council.opinion_messages("Go on", history)
    replayed = "\n".join(m["content"] for m in msgs)
    assert "First?" in replayed and "kimi's idea" in replayed


# ── steering: notes ──

def test_inbox_hands_over_only_notes_sent_after_the_prompt():
    steering = council.Steering()
    before = steering.add("in the prompt already")
    inbox = steering.inbox("opinions", 1)
    assert inbox() == []
    note = steering.add("use 2025 prices")
    texts = inbox()
    assert len(texts) == 1 and texts[0].endswith("use 2025 prices")
    assert "sent this while you were working" in texts[0]
    assert inbox() == []                               # each note once
    assert note["delivered"] == [{"stage": "opinions", "member": 1}]
    assert before["delivered"] == []
    assert [n["id"] for n in inbox.drain()] == [note["id"]]
    assert inbox.drain() == []


def test_notes_join_the_review_and_chair_prompts():
    labels = {"A": 0, "B": 1}
    answers = {0: "a", 1: "b"}
    notes = [{"text": "budget is 500 EUR"}]
    review = council.review_messages("Q", labels, answers, notes=notes)[-1]["content"]
    assert "budget is 500 EUR" in review and council.NOTES_REVIEW in review
    chair = council.chair_messages("Q", [], labels, answers, {0: "x", 1: "y"}, [], [], notes=notes)[-1]["content"]
    assert "budget is 500 EUR" in chair and council.NOTES_CHAIR in chair
    plain = council.chair_messages("Q", [], labels, answers, {0: "x", 1: "y"}, [], [])[-1]["content"]
    assert council.NOTES_CHAIR not in plain


def _steerable_stream(note_text: str, steering: council.Steering):
    """An agent-loop-like seat: polls its interjections between two rounds."""
    seen = {"prompts": [], "interjections": []}

    async def stream(url, model, messages, *, interjections=None, **kwargs):
        seen["prompts"].append((model, _role(messages), messages[-1]["content"]))
        if _role(messages) == "opinion" and model == "kimi":
            yield f"data: {json.dumps({'type': 'tool_start', 'tool': 'web_search', 'command': 'q'})}\n\n"
            steering.add(note_text)                      # the user types while the tool runs
            yield f"data: {json.dumps({'type': 'tool_output', 'tool': 'web_search', 'output': 'r'})}\n\n"
            got = interjections() if interjections else []
            seen["interjections"] += got
            for chunk in _text([f"kimi answer ({len(got)} note)"]):
                yield chunk
            return
        if _role(messages) == "review":
            for chunk in _text(["ok\nFINAL RANKING:\n1. Response A\n2. Response B"]):
                yield chunk
            return
        for chunk in _text([f"{model} {_role(messages)}"]):
            yield chunk

    stream.accepts_interjections = True
    return stream, seen


def test_a_note_reaches_a_running_member_and_every_later_stage():
    steering = council.Steering()
    stream, seen = _steerable_stream("only trains, no flights", steering)
    events = []

    async def emit(ev):
        events.append(ev)

    result = asyncio.run(council.run_council(
        question="Plan a trip", members=[_member("kimi"), _member("glm")], chairman=_member("chair"),
        mode=council.MODE_FULL, history=[], emit=emit, stream_fn=stream, rng=random.Random(1),
        steering=steering))

    (note,) = steering.notes
    assert seen["interjections"] and seen["interjections"][0].endswith("only trains, no flights")
    kimi = next(o for o in result["opinions"] if o["member"] == 0)
    assert kimi["text"] == "kimi answer (1 note)" and kimi["notes"] == [note["id"]]
    assert {"type": "note_delivered", "note": note["id"], "stage": "opinions", "member": 0} in events
    later = [(role, prompt) for _, role, prompt in seen["prompts"] if role in ("review", "chair")]
    assert later and all("only trains, no flights" in prompt for _, prompt in later)
    assert {"stage": "review"} in note["delivered"] and {"stage": "synthesis"} in note["delivered"]


# ── steering: finish now ──

def _slow_stream(release: asyncio.Event, partial: str = "half of glm's"):
    prompts = []

    async def stream(url, model, messages, **kwargs):
        prompts.append((model, _role(messages), messages[-1]["content"]))
        if _role(messages) == "opinion" and model == "glm":
            yield f"data: {json.dumps({'delta': partial})}\n\n"
            await release.wait()                          # never set: glm is the slow member
        for chunk in _text([f"{model} {_role(messages)}"]):
            yield chunk

    stream.prompts = prompts
    return stream


def test_finish_now_cuts_slow_members_skips_review_and_keeps_what_they_wrote():
    async def scenario():
        steering = council.Steering()
        stream = _slow_stream(asyncio.Event())
        events = []

        async def emit(ev):
            events.append(ev)
            if ev.get("type") == "member_done" and ev.get("member") == 0:
                steering.finish.set()

        result = await council.run_council(
            question="Q", members=[_member("kimi"), _member("glm")], chairman=_member("chair"),
            mode=council.MODE_FULL, history=[], emit=emit, stream_fn=stream, rng=random.Random(1),
            steering=steering)
        return result, events, stream.prompts

    result, events, prompts = asyncio.run(asyncio.wait_for(scenario(), 10))
    glm = result["opinions"][1]
    assert glm["cut_short"] is True and glm["error"] == council.CUT_SHORT and "pending" not in glm
    assert glm["text"] == "half of glm's"
    assert not result["reviews"] and result["final"] == "chair chair" and result["finished_early"]
    assert not any(ev.get("stage") == "review" for ev in events if ev.get("type") == "stage")
    assert {"type": "member_error", "stage": "opinions", "member": 1, "error": council.CUT_SHORT,
            "ms": None, "usage": None, "cost_usd": None, "cut_short": True} in events
    chair_prompt = next(p for _, role, p in prompts if role == "chair")
    assert "half of glm's" in chair_prompt and "[cut off" in chair_prompt


def test_finish_now_waits_for_a_first_answer():
    async def scenario():
        steering = council.Steering()
        steering.finish.set()                                 # pressed before anyone answered
        release = asyncio.Event()

        async def stream(url, model, messages, **kwargs):
            if _role(messages) == "opinion":
                await release.wait() if model == "glm" else await asyncio.sleep(0.05)
            for chunk in _text([f"{model} {_role(messages)}"]):
                yield chunk

        events = []

        async def emit(ev):
            events.append(ev)

        return await council.run_council(
            question="Q", members=[_member("kimi"), _member("glm")], chairman=_member("chair"),
            mode=council.MODE_FULL, history=[], emit=emit, stream_fn=stream, rng=random.Random(1),
            steering=steering)

    result = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert result["opinions"][0]["text"] == "kimi opinion"
    assert result["opinions"][1]["cut_short"] and result["final"] == "chair chair"


def test_a_cut_short_agent_seat_is_not_billed_twice():
    async def scenario():
        steering = council.Steering()

        async def stream(url, model, messages, **kwargs):
            if model == "glm":
                yield f"data: {json.dumps({'type': 'self_billed'})}\n\n"
                yield f"data: {json.dumps({'delta': 'partial'})}\n\n"
                await asyncio.Event().wait()
            for chunk in _text([f"{model} answer"]):
                yield chunk

        async def emit(ev):
            if ev.get("type") == "member_done" and ev.get("member") == 0:
                steering.finish.set()

        members = [_member("kimi"), _member("glm")]
        result = await council.run_council(
            question="Q", members=members, chairman=_member("chair"), mode=council.MODE_QUICK,
            history=[], emit=emit, stream_fn=stream, rng=random.Random(1), steering=steering)
        return result, members

    result, members = asyncio.run(asyncio.wait_for(scenario(), 10))
    billed = [m.model for m, *_ in council.billable_calls(result, members, _member("chair"))]
    assert "glm" not in billed and "kimi" in billed


def test_stop_while_waiting_still_cancels_every_member():
    async def scenario():
        started = asyncio.Event()

        async def stream(url, model, messages, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield ""

        async def emit(ev):
            pass

        state = {}
        task = asyncio.ensure_future(council.run_council(
            question="Q", members=[_member("a"), _member("b")], chairman=_member("c"), mode=council.MODE_FULL,
            history=[], emit=emit, stream_fn=stream, state=state, steering=council.Steering()))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return state

    state = asyncio.run(asyncio.wait_for(scenario(), 10))
    assert all(o.get("pending") for o in state["opinions"])   # the routes mark these stopped


# ── the agent loop takes notes between rounds ──

def test_agent_loop_adds_interjections_before_each_model_request(monkeypatch):
    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set(), raising=False)
    sent = []
    replies = iter(["```web_search\nlisbon trains\n```", "Final answer."])

    async def fake_stream(routes, messages, **kwargs):
        sent.append([dict(m) for m in messages])
        yield f"data: {json.dumps({'delta': next(replies, 'Done.')})}\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        notes.append("no flights")
        return block.tool_type, {"output": "results", "exit_code": 0}

    notes = []

    def interjections():
        out, notes[:] = list(notes), []
        return out

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute)

    async def collect():
        return [c async for c in agent_loop.stream_agent_loop(
            "http://local.test/v1", "m", [{"role": "user", "content": "plan it"}], max_rounds=3,
            relevant_tools={"web_search"}, gated_actions="allow", interjections=interjections)]

    asyncio.run(collect())
    assert len(sent) == 2
    assert not any(m.get("content") == "no flights" for m in sent[0])
    assert sent[1][-1] == {"role": "user", "content": "no flights"}
