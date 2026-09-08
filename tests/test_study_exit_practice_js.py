"""You must be able to leave a practice session from inside a question.

The question view offered Check answer, Hint, See original question, Consult
and Skip. None of them leave: Skip advances, and the only way out was to skip
to the end of the queue or close the whole Study panel — which drops the
session recap and reads like losing the work.

Nothing is actually at risk on the way out: each answer is written server side
as it is checked. The recap is the only client-only state, so exiting shows it
rather than discarding it, and exiting a session with nothing answered goes
straight back to the subject list.
"""

from __future__ import annotations

import re

from tests._study_js_harness import STUDY_JS, needs_node, run_js

_SRC = STUDY_JS.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Where the control lives
# --------------------------------------------------------------------------

def test_the_exit_control_is_in_the_question_view():
    assert 'id="study-prac-exit"' in _SRC, "there is still no way out of a question"


def test_it_sits_above_the_answer_branch_so_it_shows_in_both_states():
    """The view splits into answering and graded; a control in either branch
    is missing from the other. Exit belongs to the question, not to a phase."""
    exit_at = _SRC.index('id="study-prac-exit"')
    # The branch that swaps the controls for the grade panel.
    branch_at = _SRC.index("${!res ? `", exit_at - 6000)
    assert exit_at < branch_at, (
        "Exit is inside one phase of the question view, so it disappears in "
        "the other"
    )


def test_it_is_wired_to_a_handler():
    assert re.search(r"#study-prac-exit'\)\?\.addEventListener\('click'", _SRC), (
        "the Exit button is rendered but nothing listens to it"
    )


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------

PRELUDE = """
const calls = [];
let stopped = 0;
function stopMockTimer() { stopped += 1; }
function renderPractice() { calls.push('renderPractice'); }
function renderPracticeSummary() { calls.push('renderPracticeSummary'); }
const S = { practice: null };
"""

ANSWERED = "S.practice = { log: [{ result: { score: 80 } }, { skipped: true }] };"
NOTHING = "S.practice = { log: [{ skipped: true }] };"
MOCK = "S.practice = { mock: { phase: 'answer' }, log: [{ result: { score: 40 } }] };"


def _run(setup):
    """Drive exitPractice against a session shaped by `setup`."""
    return run_js(PRELUDE, "exitPractice", epilogue=f"""
{setup}
exitPractice();
console.log(JSON.stringify({{
  calls, stopped,
  practice: S.practice === null ? null : {{
    mockPhase: S.practice.mock ? S.practice.mock.phase : null,
  }},
}}));
""")


@needs_node
class TestExiting:
    """Where exiting lands you, per session state."""

    def test_a_session_with_answers_ends_on_its_summary(self):
        """The recap exists only in the client — leaving must not bin it."""
        assert _run(ANSWERED)["calls"] == ["renderPracticeSummary"]

    def test_a_session_with_nothing_answered_leaves_practice(self):
        out = _run(NOTHING)
        assert out["practice"] is None, "an untouched session was kept open"
        assert out["calls"] == ["renderPractice"]

    def test_an_empty_log_is_treated_as_nothing_answered(self):
        assert _run("S.practice = { log: [] };")["practice"] is None

    def test_a_missing_log_does_not_throw(self):
        """Defensive: exit is reachable in every state the view can render."""
        assert _run("S.practice = { };")["practice"] is None

    def test_exiting_a_mock_goes_to_the_prediction_step(self):
        """A mock is unmarked until you predict; ending early is a real path."""
        out = _run(MOCK)
        assert out["practice"]["mockPhase"] == "predict"
        assert out["calls"] == ["renderPractice"]

    def test_exiting_a_mock_stops_its_clock(self):
        assert _run(MOCK)["stopped"] == 1, "the mock timer kept running"

    def test_exiting_a_plain_session_also_stops_the_timer(self):
        """stopMockTimer is a no-op without a mock, but must not be skipped."""
        assert _run(ANSWERED)["stopped"] == 1

    def test_exiting_without_a_session_does_nothing(self):
        out = _run("S.practice = null;")
        assert out["calls"] == []
        assert out["stopped"] == 0
