"""AI Council engine: blind labels, ranking parse/aggregate, stage flow."""

from __future__ import annotations

import asyncio
import json
import random

from src import council


def _member(name: str) -> council.Member:
    return council.Member(endpoint_id=f"ep-{name}", model=name, endpoint_name=f"EP {name}",
                          kind="api", provider="openai", url=f"https://{name}.example/v1/chat/completions")


def _fake_stream(answers=None, fail=(), chair_fail=False, reviews=None):
    """stream_llm stand-in keyed on the system prompt of each stage."""
    answers = answers or {}
    calls = []

    async def stream(url, model, messages, **kwargs):
        system = messages[0]["content"]
        calls.append((system.split(".")[0], model, messages))
        if system == council.REVIEW_SYSTEM:
            if reviews and model in reviews:
                text = reviews[model]
            else:
                labels = sorted(set(council._RESPONSE_LABEL_RE.findall(messages[-1]["content"])))
                text = "Fine.\nFINAL RANKING:\n" + "\n".join(f"{i}. Response {l}" for i, l in enumerate(labels, 1))
        elif system == council.CHAIR_SYSTEM:
            if chair_fail:
                yield 'event: error\ndata: {"status": 503, "text": "chair down"}\n\n'
                return
            text = "FINAL ANSWER"
        else:
            if model in fail:
                yield 'event: error\ndata: {"status": 401, "text": "reconnect me"}\n\n'
                return
            text = answers.get(model, f"{model} answer")
        yield 'data: {"delta": "thinking...", "thinking": true}\n\n'
        for i in range(0, len(text), 4):
            yield f"data: {json.dumps({'delta': text[i:i + 4]})}\n\n"
        yield "data: [DONE]\n\n"

    stream.calls = calls
    return stream


def _run(**kwargs):
    events = []

    async def emit(ev):
        events.append(ev)

    async def go():
        return await council.run_council(emit=emit, history=kwargs.pop("history", []),
                                         rng=random.Random(7), **kwargs)

    return asyncio.run(go()), events


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

def test_parse_ranking_reads_the_final_section_only():
    text = ("Response B is wrong about X; Response A is solid.\n"
            "FINAL RANKING:\n1. Response A\n2. Response C\n3. Response B")
    assert council.parse_ranking(text, ["A", "B", "C"]) == ["A", "C", "B"]


def test_parse_ranking_ignores_unknown_and_repeated_labels_and_case():
    text = "final ranking\n1. Response D\n2. Response B\n3. Response B\n4. Response A"
    assert council.parse_ranking(text, ["A", "B"]) == ["B", "A"]


def test_review_without_final_ranking_counts_as_unranked():
    # Mention order is not a vote; it would present noise as a blind ranking.
    assert council.parse_ranking("I prefer Response C, then Response A.", ["A", "C"]) == []
    assert council.parse_ranking("", ["A"]) == []


def test_aggregate_excludes_each_reviewers_vote_on_itself():
    labels = {"A": 0, "B": 1, "C": 2}
    reviews = [
        # Every reviewer puts itself first; that must not count.
        {"member": 0, "ranking": ["A", "B", "C"]},
        {"member": 1, "ranking": ["B", "A", "C"]},
        {"member": 2, "ranking": ["C", "A", "B"]},
    ]
    rows = council.aggregate_rankings(reviews, labels)
    by_label = {r["label"]: r for r in rows}
    assert by_label["A"]["avg_rank"] == 1.0 and by_label["A"]["first_votes"] == 2
    assert by_label["B"]["avg_rank"] == 1.5
    assert by_label["C"]["avg_rank"] == 2.0
    assert [r["label"] for r in rows] == ["A", "B", "C"]


def test_aggregate_handles_missing_votes():
    rows = council.aggregate_rankings([], {"A": 0, "B": 1})
    assert all(r["avg_rank"] is None and r["votes"] == 0 for r in rows)


def test_assign_labels_covers_every_member_once():
    labels = council.assign_labels([0, 2, 5], random.Random(1))
    assert sorted(labels) == ["A", "B", "C"]
    assert sorted(labels.values()) == [0, 2, 5]


def test_review_prompt_is_anonymous_and_chair_prompt_names_models():
    labels = {"A": 1, "B": 0}
    opinions = {0: "zero says", 1: "one says"}
    review = council.review_messages("Q?", labels, opinions)[-1]["content"]
    assert "gpt" not in review and "claude" not in review
    assert "FINAL RANKING:" in review and "Response A" in review
    chair = council.chair_messages("Q?", [], labels, opinions, {0: "claude-opus via Claude", 1: "gpt-5.5 via ChatGPT"},
                                   [{"member": 0, "text": "B is better"}],
                                   [{"label": "B", "member": 0, "avg_rank": 1.0, "votes": 1, "first_votes": 1}])
    body = chair[-1]["content"]
    assert "Response B (claude-opus via Claude)" in body
    assert "Review by the author of Response B" in body
    assert "average rank 1.0" in body


def test_long_answers_are_clipped_in_later_stages():
    big = "x" * (council.MAX_ANSWER_CHARS + 500)
    body = council.review_messages("Q", {"A": 0}, {0: big})[-1]["content"]
    assert "[NOTE: this answer is clipped" in body and "[... CUT: " in body
    assert len(body) < council.MAX_ANSWER_CHARS + 2000


def test_history_keeps_the_first_and_the_recent_completed_turns():
    history = [{"question": f"q{i}", "final": f"a{i}"} for i in range(6)] + [{"question": "q", "final": ""}]
    msgs = council.history_messages(history)
    assert [m["content"] for m in msgs] == [
        "q0", "a0", "[NOTE: 2 earlier rounds are not replayed here.]\n\nq3", "a3", "q4", "a4", "q5", "a5"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] * 4


# ---------------------------------------------------------------------------
# stage flow
# ---------------------------------------------------------------------------

def test_full_council_runs_three_stages_and_drops_failed_members():
    stream = _fake_stream(fail={"bad"})
    members = [_member("a"), _member("bad"), _member("b"), _member("c")]
    result, events = _run(question="Q?", members=members, chairman=_member("a"), mode="full", stream_fn=stream)

    assert result["error"] is None
    assert result["final"] == "FINAL ANSWER"
    assert [o["error"] for o in result["opinions"]] == [None, "reconnect me", None, None]
    # The failed member gets no label and does not review.
    assert sorted(result["labels"].values()) == [0, 2, 3]
    assert sorted(r["member"] for r in result["reviews"]) == [0, 2, 3]
    assert all(len(r["ranking"]) == 3 for r in result["reviews"])
    assert len(result["ranking"]) == 3

    stages = [(e["stage"], e["status"]) for e in events if e["type"] == "stage"]
    assert stages == [("opinions", "start"), ("opinions", "done"), ("review", "start"),
                      ("review", "done"), ("synthesis", "start"), ("synthesis", "done")]
    # Thinking text is announced, never streamed as answer text.
    assert any(e["type"] == "thinking" for e in events)
    assert all("thinking" not in e.get("text", "") for e in events if e["type"] == "delta")
    opinion_text = "".join(e["text"] for e in events if e["type"] == "delta" and e["stage"] == "opinions" and e["member"] == 0)
    assert opinion_text == "a answer"


def test_quick_mode_skips_review():
    stream = _fake_stream()
    result, events = _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("b"),
                          mode="quick", stream_fn=stream)
    assert result["reviews"] == [] and result["ranking"] == []
    assert result["final"] == "FINAL ANSWER"
    assert not any(e.get("stage") == "review" for e in events)


def test_single_member_skips_review_even_in_full_mode():
    result, _ = _run(question="Q?", members=[_member("solo")], chairman=_member("solo"),
                     mode="full", stream_fn=_fake_stream())
    assert result["reviews"] == []
    assert result["final"] == "FINAL ANSWER"


def test_chairman_failure_falls_back_to_the_top_ranked_answer():
    # Labels are shuffled with a seeded RNG, so compute b's letter the same way
    # and have every reviewer rank it first.
    labels = council.assign_labels([0, 1, 2], random.Random(7))
    b_label = next(k for k, v in labels.items() if v == 1)
    order = [b_label] + [k for k in labels if k != b_label]
    ranking = "FINAL RANKING:\n" + "\n".join(f"{i}. Response {k}" for i, k in enumerate(order, 1))
    stream = _fake_stream(answers={"a": "answer from a", "b": "answer from b"}, chair_fail=True,
                          reviews={"a": ranking, "b": ranking, "c": ranking})

    result, _ = _run(question="Q?", members=[_member("a"), _member("b"), _member("c")],
                     chairman=_member("a"), mode="full", stream_fn=stream)

    assert result["labels"] == labels
    assert result["error"].startswith("Chairman failed")
    assert result["final"] == "answer from b"
    assert result["final_fallback_member"] == 1


def test_no_answers_is_an_error_without_later_stages():
    stream = _fake_stream(fail={"a", "b"})
    result, events = _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"),
                          mode="full", stream_fn=stream)
    assert result["error"] == "No council member produced an answer."
    assert not any(e.get("stage") in ("review", "synthesis") for e in events)
    assert all(call[0] != council.CHAIR_SYSTEM.split(".")[0] for call in stream.calls)


def test_checkpoint_sees_each_completed_stage():
    seen = []

    async def checkpoint(result):
        seen.append((len(result["opinions"]), len(result["reviews"])))

    _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full",
         stream_fn=_fake_stream(), checkpoint=checkpoint)
    assert seen == [(2, 0), (2, 2)]


def test_usage_and_cost_are_tracked_per_call():
    async def priced_stream(url, model, messages, **kwargs):
        yield 'data: {"delta": "answer FINAL RANKING: 1. Response A 2. Response B"}\n\n'
        yield 'data: {"type": "usage", "data": {"input_tokens": 1000, "output_tokens": 500}}\n\n'
        yield "data: [DONE]\n\n"

    paid = _member("paid")
    paid.input_per_mtok, paid.output_per_mtok = 2.0, 8.0
    sub = _member("sub")
    sub.billing = "subscription"
    result, events = _run(question="Q?", members=[paid, sub], chairman=paid, mode="full", stream_fn=priced_stream)
    by_member = {o["member"]: o for o in result["opinions"]}
    assert by_member[0]["usage"] == {"input_tokens": 1000, "output_tokens": 500}
    assert by_member[0]["cost_usd"] == 0.006            # 1000*2/1e6 + 500*8/1e6
    assert by_member[1]["cost_usd"] is None             # flat-rate subscription
    totals = council.usage_totals(result)
    assert totals["input_tokens"] == 5000 and totals["output_tokens"] == 2500   # 2 opinions + 2 reviews + chair
    assert totals["cost_usd"] == round(0.006 * 3, 6) and totals["unpriced_calls"] == 2
    done = [e for e in events if e["type"] == "member_done" and e["stage"] == "opinions" and e["member"] == 0]
    assert done[0]["cost_usd"] == 0.006


def _run_member(stream, member):
    async def emit(ev):
        pass

    return asyncio.run(council.run_member(stream, member, [{"role": "user", "content": "q"}],
                                          stage="opinions", index=0, emit=emit, timeout=30))


def test_self_billed_seat_shows_its_tokens_and_cost_but_is_not_billed_again():
    async def agent_loop_stream(url, model, messages, **kwargs):
        yield 'data: {"type": "self_billed"}\n\n'
        yield 'data: {"delta": "answer"}\n\n'
        yield 'data: {"type": "metrics", "data": {"input_tokens": 2000, "output_tokens": 300}}\n\n'
        yield "data: [DONE]\n\n"

    paid = _member("paid")
    paid.input_per_mtok, paid.output_per_mtok = 1.0, 4.0
    out = _run_member(agent_loop_stream, paid)
    assert out["usage"] == {"input_tokens": 2000, "output_tokens": 300}
    assert out["cost_usd"] == 0.0032 and out["self_billed"]
    assert council.usage_totals({"opinions": [out]})["cost_usd"] == 0.0032
    # The agent loop already put it in the spend ledger.
    assert council.billable_calls({"opinions": [{**out, "member": 0}]}, [paid], paid) == []


def test_a_refused_tool_call_still_ends_the_working_notes():
    """A call blocked before it starts sends only tool_output; the text before
    it is a working note, not part of the answer."""
    async def stream(url, model, messages, **kwargs):
        yield 'data: {"delta": "Now let me retry the blocked tool:"}\n\n'
        yield ('data: {"type": "tool_output", "tool": "web_fetch", "command": "example.com", '
               '"output": "Blocked by policy", "exit_code": 1}\n\n')
        yield 'data: {"delta": "Final answer."}\n\n'
        yield "data: [DONE]\n\n"

    out = _run_member(stream, _member("m"))
    assert out["text"] == "Final answer."
    assert out["tools"][0] == {"kind": "note", "text": "Now let me retry the blocked tool:"}
    assert out["tools"][1]["tool"] == "web_fetch" and out["tools"][1]["status"] == "error"


def test_cached_prompt_tokens_are_priced_at_cache_rates():
    m = _member("paid")
    m.input_per_mtok, m.output_per_mtok = 10.0, 50.0
    usage = {"input_tokens": 10000, "output_tokens": 100,
             "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 1000}
    # fresh 1000*10 + read 8000*1 + write 1000*12.5 + out 100*50 = 35500 -> $0.0355
    assert m.cost_usd(usage) == 0.0355
    assert m.cost_usd({"input_tokens": 10000, "output_tokens": 100}) == 0.105


def test_state_holds_partial_text_while_running():
    gate = asyncio.Event()
    state = {}

    async def slow(url, model, messages, **kwargs):
        yield 'data: {"delta": "half an "}\n\n'
        await gate.wait()
        yield 'data: {"delta": "answer"}\n\n'

    async def go():
        async def emit(ev):
            pass
        task = asyncio.create_task(council.run_council(question="Q", members=[_member("a")], chairman=_member("a"),
                                                       mode="quick", history=[], emit=emit, stream_fn=slow,
                                                       state=state))
        for _ in range(50):
            await asyncio.sleep(0)
        partial = state["opinions"][0]["text"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return partial

    assert asyncio.run(go()) == "half an "
    assert state["opinions"][0]["pending"] is True


def test_history_reaches_every_stage_model():
    stream = _fake_stream()
    _run(question="And now?", members=[_member("a")], chairman=_member("a"), mode="quick", stream_fn=stream,
         history=[{"question": "Earlier?", "final": "Earlier answer"}])
    for _stage, _model, messages in stream.calls:
        contents = [m["content"] for m in messages]
        assert "Earlier?" in contents and "Earlier answer" in contents


# ---------------------------------------------------------------------------
# history budget
# ---------------------------------------------------------------------------

def _long(tag: str, size: int) -> str:
    """``size`` characters of numbered lines, so any cut shows as missing lines."""
    body = "".join(f"{tag} line {i:05d}\n" for i in range(size // 10))
    return body[:size - 4] + " END"


BRIEF = _long("brief", 12_000)           # round 1: the user's brief
ROUND1 = _long("round1", 19_000)         # round 1: the council's answer
REPLY = "1 - all\n3 - mixed"             # round 2: the user answers its questions
PREVIOUS = _long("previous", 20_000)     # round 2: the council's answer
HISTORY = [{"question": BRIEF, "final": ROUND1}, {"question": REPLY, "final": PREVIOUS}]


def _sized(name: str, context_tokens: int) -> council.Member:
    member = _member(name)
    member.context_tokens = context_tokens
    return member


def _history_part(messages):
    """The replayed history: everything between the system prompt and the new question."""
    return [m["content"] for m in messages[1:-1]]


def _run_round3(context_tokens: int, answers=None):
    stream = _fake_stream(answers=answers)
    members = [_sized("a", context_tokens), _sized("b", context_tokens)]
    _run(question="Round 3?", members=members, chairman=_sized("a", context_tokens), mode="full",
         stream_fn=stream, history=HISTORY)
    stages = {call[0] for call in stream.calls}
    assert stages == {s.split(".")[0] for s in (council.OPINION_SYSTEM, council.REVIEW_SYSTEM, council.CHAIR_SYSTEM)}
    assert len(stream.calls) == 5               # 2 opinions, 2 reviews, 1 chairman
    return stream.calls


def test_large_windows_replay_the_brief_and_the_previous_answer_in_full():
    long_answer = _long("member", 20_000)
    for stage, _model, messages in _run_round3(1_000_000, answers={"a": long_answer}):
        assert _history_part(messages) == [BRIEF, ROUND1, REPLY, PREVIOUS], stage
        if stage != council.OPINION_SYSTEM.split(".")[0]:
            # A member's long answer also reaches reviewers and chairman uncut.
            assert long_answer in messages[-1]["content"] and "[NOTE:" not in messages[-1]["content"], stage


def test_small_windows_clip_the_middle_turns_first_and_say_so():
    for stage, _model, messages in _run_round3(40_000):
        brief, round1, reply, previous = _history_part(messages)
        assert (brief, reply, previous) == (BRIEF, REPLY, PREVIOUS), stage
        assert round1.startswith("[NOTE: this earlier council answer is clipped"), stage
        assert "[... CUT: " in round1 and "characters" in round1
        assert len(round1) < len(ROUND1) // 2
        assert "round1 line 00000" in round1 and round1.endswith(" END")   # start and end kept


def test_tiny_windows_clip_the_brief_and_the_previous_answer_with_a_note():
    for stage, _model, messages in _run_round3(8_000):
        brief, round1, reply, previous = _history_part(messages)
        assert reply == REPLY, stage
        assert round1.startswith("[NOTE: this earlier council answer is left out"), stage
        assert brief.startswith("[NOTE: this earlier question is clipped"), stage
        assert previous.startswith("[NOTE: this earlier council answer is clipped"), stage
        assert "brief line 00000" in brief and "previous line 00000" in previous
        assert sum(map(len, (brief, round1, reply, previous))) <= council.prompt_room(
            [_sized("a", 8_000)]) // 4 + 100


def test_clip_note_names_the_cut_characters():
    text = _long("x", 10_000)
    clipped = council._clip_middle(text, 3_000, "earlier question")
    head, cut = clipped.split("\n\n")[1:3]
    start = len(head) + 1
    assert f"characters {start:,}-" in clipped.splitlines()[0]
    assert cut.startswith("[... CUT: ") and f"of {len(text):,}" in cut
    assert len(clipped) <= 3_000


def test_unknown_windows_fall_back_to_a_conservative_budget():
    known = council.plan_limits([_sized("a", 1_000_000)], _sized("a", 1_000_000), "quick", "Q", HISTORY)
    unknown = council.plan_limits([_member("a")], _sized("a", 1_000_000), "quick", "Q", HISTORY)
    assert unknown.history < known.history
    assert unknown == council.plan_limits([_sized("a", council.FALLBACK_CONTEXT_TOKENS)],
                                          _sized("a", 1_000_000), "quick", "Q", HISTORY)


def test_estimate_counts_the_replayed_history():
    small = council.estimate_turn([_sized("a", 8_000)], _sized("a", 8_000), "quick", "Q", HISTORY)
    large = council.estimate_turn([_sized("a", 1_000_000)], _sized("a", 1_000_000), "quick", "Q", HISTORY)
    assert large["calls"][0]["input_tokens"] > small["calls"][0]["input_tokens"] + 10_000


# ---------------------------------------------------------------------------
# context about the user
# ---------------------------------------------------------------------------

def test_context_reaches_every_stage_system_prompt():
    stream = _fake_stream()
    calls = []

    async def recording(url, model, messages, **kwargs):
        calls.append(messages)
        # The fake keys on the bare stage prompt, so hand it the prompt without the context.
        bare = [{**messages[0], "content": messages[0]["content"].split("\n\nABOUT THE USER")[0]}, *messages[1:]]
        async for chunk in stream(url, model, bare, **kwargs):
            yield chunk

    result, _ = _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full",
                     stream_fn=recording, context="ABOUT THE USER: prefers short answers.")
    assert result["final"] == "FINAL ANSWER"
    assert len(calls) == 5
    for messages in calls:
        assert messages[0]["role"] == "system" and messages[0]["content"].endswith("prefers short answers.")
        assert sum("ABOUT THE USER" in m["content"] for m in messages) == 1


def test_context_counts_against_the_history_budget():
    members, chair = [_sized("a", 40_000)], _sized("a", 40_000)
    plain = council.plan_limits(members, chair, "quick", "Q", HISTORY)
    with_memory = council.plan_limits(members, chair, "quick", "Q", HISTORY, context="m" * 20_000)
    assert with_memory.history == plain.history - 20_000
    est = council.estimate_turn(members, chair, "quick", "Q", [], context="m" * 20_000)
    assert est["calls"][0]["input_tokens"] > council.estimate_turn(members, chair, "quick", "Q", [])["calls"][0][
        "input_tokens"] + 5_000


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------

VERDICT = (
    "## Verdict\n"
    "- Decision: run the replay ablation before any retrain\n"
    "- Confidence: medium - one seed only\n"
    "- Dissent: gpt-5.5 would retrain first, because replays miss tool errors\n"
    "- Would change it: a replay gain under 1 task on val18\n"
    "- Next test: replay v32 on 27 dumps; adopt if at least 2 more pass"
)


def test_chair_prompt_asks_for_the_verdict_block():
    system = council.chair_messages("Q?", [], {"A": 0}, {0: "a"}, {0: "m via EP"}, [], [])[0]["content"]
    for field in ("## Verdict", "- Decision:", "- Confidence:", "- Dissent:", "- Would change it:", "- Next test:"):
        assert field in system


def test_parse_verdict_reads_the_fields_of_the_last_verdict_block():
    text = f"Earlier text mentions ## Verdict\n- Decision: stale\n\nThe answer.\n\n{VERDICT}\n"
    assert council.parse_verdict(text) == {
        "decision": "run the replay ablation before any retrain",
        "confidence": "medium - one seed only",
        "dissent": "gpt-5.5 would retrain first, because replays miss tool errors",
        "would_change": "a replay gain under 1 task on val18",
        "next_test": "replay v32 on 27 dumps; adopt if at least 2 more pass",
    }


def test_parse_verdict_tolerates_bold_labels_and_missing_fields():
    assert council.parse_verdict("Answer.\n\n## Verdict\n- **Decision:** ship it\n* Confidence: high") == {
        "decision": "ship it", "confidence": "high"}


def test_an_answer_without_a_verdict_block_has_no_verdict():
    assert council.parse_verdict("- Decision: looks like one but has no heading") == {}
    assert council.parse_verdict("") == {}


def test_a_clipped_earlier_answer_keeps_its_verdict_whole():
    final = _long("body", 30000) + "\n\n" + VERDICT
    history = [{"question": "brief", "final": final}]
    # At this budget the plain clip leaves the whole answer out, verdict included.
    (_, answer) = council.history_messages(history, budget=1000)
    assert answer["content"].startswith("[NOTE: this earlier council answer is left out.")
    assert answer["content"].endswith(VERDICT)
    assert len(answer["content"]) <= 1000


def test_a_verdict_larger_than_the_budget_is_clipped_like_any_text():
    history = [{"question": "brief", "final": "short body\n\n" + VERDICT + "x" * 5000}]
    (_, answer) = council.history_messages(history, budget=1500)
    assert len(answer["content"]) <= 1500


# ---------------------------------------------------------------------------
# markdown export
# ---------------------------------------------------------------------------

def _turn(**over):
    turn = {
        "question": "Which experiment next?",
        "config": {"members": [{"model": "claude-opus", "endpoint_name": "Claude"},
                               {"model": "gpt-5.5", "endpoint_name": "ChatGPT"}],
                   "chairman": {"model": "claude-opus", "endpoint_name": "Claude"},
                   "mode": "full", "tools": "research", "workspace": "/repo/gemma4-astra"},
        "status": "done",
        "labels": {"A": 1, "B": 0},
        "opinions": [{"member": 0, "text": "retrain"},
                     {"member": 1, "text": "replay first",
                      "tools": [{"kind": "note", "text": "let me look"},
                                {"kind": "tool", "tool": "read_file", "command": "QUEUE.md", "status": "done"},
                                {"kind": "tool", "tool": "web_search", "command": "gemma\n4", "status": "error"}]}],
        "reviews": [{"member": 0, "text": "A is right"}, {"member": 1, "error": "timed out"}],
        "ranking": [{"label": "A", "member": 1, "avg_rank": 1.0, "votes": 1, "first_votes": 1},
                    {"label": "B", "member": 0, "avg_rank": None, "votes": 0, "first_votes": 0}],
        "final": "Replay first.\n\n" + VERDICT,
        "usage": {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.0123, "unpriced_calls": 2},
        "chair": {"tools": [{"kind": "tool", "tool": "read_file", "command": "INDEX.md", "status": "done"}]},
        "created_at": "2026-11-14T10:00:00",
    }
    turn.update(over)
    return turn


def test_turn_markdown_has_the_answer_the_run_settings_and_the_cost():
    text = council.turn_markdown(_turn())
    assert text.startswith("# Which experiment next?\n")
    assert "- Mode: full" in text
    assert "- Chairman: claude-opus (Claude)" in text
    assert "- Tools: research" in text
    assert "- Workspace: /repo/gemma4-astra" in text
    assert "- Asked: 2026-11-14T10:00:00" in text
    assert "- Usage: 1200 input + 300 output tokens, metered cost $0.0123 (2 calls not priced)" in text
    assert "## Council answer\n\nReplay first.\n\n## Verdict\n- Decision: run the replay" in text


def test_turn_markdown_lists_opinions_by_label_with_their_tools():
    text = council.turn_markdown(_turn())
    a, b = text.index("### Response A — gpt-5.5 (ChatGPT)"), text.index("### Response B — claude-opus (Claude)")
    assert a < b
    assert "Tools used:\n- ✓ `read_file` QUEUE.md\n- ✗ `web_search` gemma 4\n\nreplay first" in text
    assert "Chairman tools used:\n- ✓ `read_file` INDEX.md" in text


def test_turn_markdown_has_the_ranking_table_and_the_reviews():
    text = council.turn_markdown(_turn())
    assert "| 1 | Response A | gpt-5.5 (ChatGPT) | 1.0 | 1 |" in text
    assert "Response B |" not in text.split("## Reviews")[0].split("## Peer ranking")[1]
    assert "### Review by claude-opus (Claude)\n\nA is right" in text
    assert "### Review by gpt-5.5 (ChatGPT)\n\n_timed out_" in text


def test_turn_markdown_of_a_failed_turn_says_so():
    text = council.turn_markdown(_turn(final="", status="error", error="No council member produced an answer.",
                                       ranking=[], reviews=[], usage={}, chair=None))
    assert "- Status: error (No council member produced an answer.)" in text
    assert "_No answer_" in text
    assert "## Reviews" not in text and "## Peer ranking" not in text and "- Usage:" not in text


WORK = {"run_id": "abc123def456", "round": 2, "rounds": 5, "goal": "Make the parser 2x faster",
        "source": "/repo/parser", "branch": "odysseus/make-the-parser-2x-faster-abc123",
        "main": "/app/data/council-runs/abc123def456/main", "base_sha": "a" * 40, "head_sha": "b" * 40,
        "messages": [{"name": "20261115T100000.000000Z__seat-1__all__progress.md", "from": "seat-1",
                      "to": "all", "text": "seat-1 committed the tokenizer"},
                     {"name": "20261115T100001.000000Z__odysseus__all__notice.md", "from": "odysseus",
                      "to": "all", "text": "messages/x.md was deleted after delivery; restored."}],
        "notices": [], "run_status": "finished", "stop_reason": "done", "while_waiting": "stop"}
DIFF = "Work on branch seat-1 since a1.\n\nPatch:\ndiff --git a/x b/x\n+```python\n+x = 1\n"


def test_turn_markdown_of_a_work_round_has_the_round_branch_stop_reason_and_messages():
    text = council.turn_markdown(_turn(work=WORK, opinions=[{"member": 0, "text": "Changed: x", "attachment": DIFF},
                                                            {"member": 1, "text": "replay first"}]))
    assert "- Work run: round 2 of 5" in text
    assert "- Branch: odysseus/make-the-parser-2x-faster-abc123" in text
    assert "- Stop reason: done" in text
    messages = text.split("## Messages", 1)[1]
    assert "### seat-1 → all" in messages and "seat-1 committed the tokenizer" in messages
    assert "### odysseus → all" in messages and "was deleted after delivery; restored." in messages


def test_turn_markdown_shows_the_diff_apart_from_the_answer_text():
    text = council.turn_markdown(_turn(work=WORK, opinions=[{"member": 0, "text": "Changed: x", "attachment": DIFF}]))
    section = text.split("### Response B — claude-opus (Claude)", 1)[1]
    assert section.index("Changed: x") < section.index("Diff attached by Odysseus")
    # The fence is longer than any run of backticks inside the diff, so the diff cannot close it.
    assert "\n````\n" + DIFF.rstrip("\n") + "\n````\n" in section


def test_turn_markdown_of_a_running_work_round_has_no_stop_reason_and_a_plain_turn_no_work_lines():
    running = council.turn_markdown(_turn(work={**WORK, "run_status": "running", "stop_reason": None}))
    assert "- Work run: round 2 of 5" in running and "Stop reason" not in running
    plain = council.turn_markdown(_turn())
    assert "Work run" not in plain and "## Messages" not in plain and "Diff attached" not in plain


# ---------------------------------------------------------------------------
# hooks for work runs (all optional; without them the council is unchanged)
# ---------------------------------------------------------------------------

def test_parse_verdict_reads_status_continue_or_done_only():
    base = "Answer.\n\n## Verdict\n- Decision: merge seat-1\n"
    assert council.parse_verdict(base + "- Status: done")["status"] == "done"
    assert council.parse_verdict(base + "- **Status:** Continue.")["status"] == "continue"
    assert "status" not in council.parse_verdict(base + "- Status: maybe later")
    assert "status" not in council.parse_verdict(base)


def test_ordinary_chair_prompt_does_not_ask_for_a_status():
    assert "Status" not in council.CHAIR_SYSTEM


def _recording_stream(text_for):
    """Stream that answers ``text_for(stage, messages)`` and records every call."""
    calls = []

    async def stream(url, model, messages, **kwargs):
        stage = kwargs.get("stage")
        calls.append({"stage": stage, "model": model, "messages": messages, "kwargs": kwargs})
        yield f"data: {json.dumps({'delta': text_for(stage, messages)})}\n\n"
        yield "data: [DONE]\n\n"

    stream.accepts_stage = True
    stream.calls = calls
    return stream


def _ranked(stage, messages):
    if stage == "review":
        labels = sorted(set(council._RESPONSE_LABEL_RE.findall(messages[-1]["content"])))
        return "ok\nFINAL RANKING:\n" + "\n".join(f"{i}. Response {l}" for i, l in enumerate(labels, 1))
    return f"{stage} text"


def test_stage_prompts_replace_the_system_prompts():
    stream = _recording_stream(_ranked)
    prompts = {"opinions": "WORK PROMPT", "review": "REVIEW PROMPT", "synthesis": "INTEGRATE PROMPT"}
    _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full",
         stream_fn=stream, prompts=prompts)
    systems = {c["stage"]: c["messages"][0]["content"] for c in stream.calls}
    assert systems == prompts


def test_personalize_gives_each_seat_its_own_prompt():
    stream = _recording_stream(_ranked)
    seen = []

    def personalize(stage, index, messages):
        seen.append((stage, index))
        return [{**messages[0], "content": messages[0]["content"] + f"\nYOU ARE {index}"}, *messages[1:]]

    _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full",
         stream_fn=stream, personalize=personalize)
    assert sorted(seen, key=str) == sorted([("opinions", 0), ("opinions", 1), ("review", 0), ("review", 1),
                                            ("synthesis", "chair")], key=str)
    for call in stream.calls:
        idx = "chair" if call["stage"] == "synthesis" else int(call["model"] == "b")
        assert call["messages"][0]["content"].endswith(f"YOU ARE {idx}")


def test_a_seat_aware_stream_is_told_its_seat():
    stream = _recording_stream(_ranked)
    stream.accepts_seat = True
    _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full", stream_fn=stream)
    seats = sorted(((c["stage"], c["kwargs"]["seat"]) for c in stream.calls), key=str)
    assert seats == sorted([("opinions", 0), ("opinions", 1), ("review", 0), ("review", 1),
                            ("synthesis", "chair")], key=str)
    # A stream without the flag is called exactly as before.
    plain = _recording_stream(_ranked)
    _run(question="Q?", members=[_member("a")], chairman=_member("a"), mode="quick", stream_fn=plain)
    assert all("seat" not in c["kwargs"] for c in plain.calls)


def test_between_stages_runs_after_each_stage_and_can_attach_to_opinions():
    stream = _recording_stream(_ranked)
    order = []

    async def between(stage, result):
        order.append((stage, [o.get("text") for o in result["opinions"]], len(result["reviews"]),
                      result.get("final")))
        if stage == "opinions":
            for op in result["opinions"]:
                op["attachment"] = f"DIFF OF SEAT {op['member']}"

    result, _ = _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full",
                     stream_fn=stream, between_stages=between)
    assert [o[0] for o in order] == ["opinions", "review", "synthesis"]
    assert order[0][1] == ["opinions text", "opinions text"] and order[0][2] == 0
    assert order[1][2] == 2
    assert order[2][3] == "synthesis text"

    by_stage = {}
    for call in stream.calls:
        by_stage.setdefault(call["stage"], []).append(call["messages"][-1]["content"])
    for body in by_stage["review"] + by_stage["synthesis"]:
        assert "DIFF OF SEAT 0" in body and "DIFF OF SEAT 1" in body
    assert all("DIFF OF SEAT" not in body for body in by_stage["opinions"])
    # Shown alongside the answer, never saved as the answer text.
    assert all("DIFF" not in o["text"] for o in result["opinions"])
    assert result["opinions"][0]["attachment"] == "DIFF OF SEAT 0"
    assert "DIFF" not in result["final"]


def _attaching(size):
    async def between(stage, result):
        if stage == "opinions":
            for op in result["opinions"]:
                op["attachment"] = _long(f"diff{op['member']}", size)

    return between


def _prompt_chars(messages):
    return sum(len(m["content"]) for m in messages)


def test_attachments_get_room_so_the_chairman_prompt_still_fits_the_smallest_window():
    window = 100_000
    members, chair = [_sized("a", window), _sized("b", window)], _sized("a", window)
    stream = _fake_stream(answers={"a": _long("a", 90_000), "b": _long("b", 90_000)},
                          reviews={"a": _long("ra", 60_000), "b": _long("rb", 60_000)})
    result, _ = _run(question="Q?", members=members, chairman=chair, mode="full", stream_fn=stream,
                     between_stages=_attaching(12_000))
    assert result["final"] == "FINAL ANSWER"
    room = council.prompt_room([*members, chair])
    by_stage = {}
    for stage, _model, messages in stream.calls:
        by_stage.setdefault(stage, []).append(messages)
    for stage in (council.REVIEW_SYSTEM.split(".")[0], council.CHAIR_SYSTEM.split(".")[0]):
        for messages in by_stage[stage]:
            assert _prompt_chars(messages) <= room, stage
    # The window has room for both diffs whole: they are not what gives way.
    (chair_prompt,) = by_stage[council.CHAIR_SYSTEM.split(".")[0]]
    body = chair_prompt[-1]["content"]
    assert _long("diff0", 12_000) in body and _long("diff1", 12_000) in body


def test_plan_limits_reserves_room_for_attachments():
    members, chair = [_sized("a", 100_000), _sized("b", 100_000)], _sized("a", 100_000)
    plain = council.plan_limits(members, chair, "full", "Q", [])
    with_diffs = council.plan_limits(members, chair, "full", "Q", [], attachment_chars=12_000)
    assert with_diffs.attachment == 12_000
    assert 2 * (with_diffs.answer + with_diffs.review + with_diffs.attachment) <= 2 * (plain.answer + plain.review)
    # In a window too small for whole diffs, the diffs give way instead of overflowing it.
    small = [_sized("a", 30_000), _sized("b", 30_000)]
    tight = council.plan_limits(small, small[0], "full", "Q", [], attachment_chars=12_000)
    assert tight.attachment < 12_000
    assert plain.attachment is None


def test_between_stages_still_runs_when_no_seat_answered():
    stages = []

    async def between(stage, result):
        stages.append(stage)

    result, _ = _run(question="Q?", members=[_member("a")], chairman=_member("a"), mode="full",
                     stream_fn=_fake_stream(fail={"a"}), between_stages=between)
    assert result["error"] == "No council member produced an answer."
    assert stages == ["opinions"]


def _slow_stream():
    async def slow(url, model, messages, **kwargs):
        yield 'data: {"delta": "half a "}\n\n'
        yield 'data: {"delta": "report"}\n\n'
        await asyncio.sleep(30)
        yield 'data: {"delta": " never sent"}\n\n'

    return slow


def test_hard_timeout_keeps_the_text_written_so_far():
    async def emit(ev):
        pass

    out = asyncio.run(council.run_member(_slow_stream(), _member("m"), [{"role": "user", "content": "q"}],
                                         stage="opinions", index=0, emit=emit, timeout=600, hard_timeout=0.2))
    assert out["text"] == "half a report"
    assert out["error"] is None and out["timed_out"] is True


def test_hard_timeout_before_any_text_is_an_error():
    async def silent(url, model, messages, **kwargs):
        await asyncio.sleep(30)
        yield 'data: {"delta": "late"}\n\n'

    async def emit(ev):
        pass

    out = asyncio.run(council.run_member(silent, _member("m"), [{"role": "user", "content": "q"}],
                                         stage="opinions", index=0, emit=emit, timeout=600, hard_timeout=0.1))
    assert out["text"] == "" and out["timed_out"] is True
    assert "time limit" in out["error"]


def test_run_council_passes_the_hard_timeout_to_every_call():
    stream = _recording_stream(_ranked)
    slow = _slow_stream()

    async def mixed(url, model, messages, **kwargs):
        source = slow if (kwargs.get("stage") == "opinions" and model == "b") else stream
        async for chunk in source(url, model, messages, **kwargs):
            yield chunk

    mixed.accepts_stage = True
    result, _ = _run(question="Q?", members=[_member("a"), _member("b")], chairman=_member("a"), mode="full",
                     stream_fn=mixed, hard_timeout=0.3)
    assert result["opinions"][1]["text"] == "half a report" and result["opinions"][1]["timed_out"]
    assert len(result["labels"]) == 2          # the timed-out seat is still reviewed
