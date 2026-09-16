"""Fixture invariants for the practice-coach live evaluation.

The evaluation runner builds its live-run contexts in
``scripts/evaluate_study_practice_coach._fixture_context``: the MCQ key comes
from the top-level integer ``correct_index`` only (``private_reference`` is a
display/human string), and the student draft comes from the ``draft`` field
only (never from chat history). These tests pin those invariants — and the
runner's own preflight — without any provider call or database access.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES_PATH = REPO / "tests" / "fixtures" / "study_practice_coach_cases.json"
SCRIPT_PATH = REPO / "scripts" / "evaluate_study_practice_coach.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "evaluate_study_practice_coach", str(SCRIPT_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_runner = _load_runner()


def _load_cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _mcq_cases(cases):
    return [c for c in cases
            if c.get("qtype") == "mcq" and (c.get("options") or [])]


def test_preflight_accepts_checked_in_fixtures():
    assert _runner._preflight_fixture_contexts(_load_cases()) == []


def test_every_keyed_mcq_context_has_in_range_index():
    for case in _mcq_cases(_load_cases()):
        raw = case.get("correct_index")
        assert type(raw) is int
        assert 0 <= raw < len(case["options"])
        ctx = _runner._fixture_context(case)
        assert ctx["basis"]["index_basis"]["correct_index"] == raw
        # The written reference must not smuggle the key as a bare string.
        assert (case.get("private_reference") or "").strip() not in {
            str(i) for i in range(len(case["options"]))}


def test_draft_spoiler_keeps_expression_out_of_visible_history():
    case = next(c for c in _load_cases() if c["id"] == "draft_spoiler")
    ctx = _runner._fixture_context(case)
    draft = ctx["student"]["draft"]
    assert draft.strip()
    visible = " ".join(m.get("content", "")
                       for m in ctx["history_rows"])
    visible += " " + (case.get("message") or "")
    assert _runner._compact(draft) not in _runner._compact(visible)
    # The latest message still refers to the draft it asks about.
    assert "draft" in (case.get("message") or "").lower()


def test_preflight_rejects_out_of_range_key():
    cases = _load_cases()
    bad = copy.deepcopy(next(c for c in cases if c["id"] == "chart_repeat"))
    bad["correct_index"] = len(bad["options"])
    errors = _runner._preflight_fixture_contexts([bad])
    assert len(errors) == 1 and "chart_repeat" in errors[0]


def test_preflight_rejects_string_key_and_draft_in_message():
    cases = _load_cases()
    string_key = copy.deepcopy(
        next(c for c in cases if c["id"] == "chart_repeat"))
    string_key["correct_index"] = "0"
    assert _runner._preflight_fixture_contexts([string_key])

    spoiler = copy.deepcopy(
        next(c for c in cases if c["id"] == "draft_spoiler"))
    spoiler["message"] = f"Check my draft: {spoiler['draft']} — is that right?"
    errors = _runner._preflight_fixture_contexts([spoiler])
    assert len(errors) == 1 and "draft_spoiler" in errors[0]
