"""B01 — Enter must submit a selected multiple-choice answer.

``practiceKeydown`` assigned ``el = body()`` without declaring ``el``. study.js
is an ES module, so that assignment is a ReferenceError and the Enter shortcut
threw before it could click Check answer.

These run the real handler under Node. Acceptance from the handoff: selected
MCQ + Enter submits once; no selection submits nothing; text editing submits
nothing; a hidden/detached Study submits nothing; no exception is raised. A
shortcut must also not bypass the disabled-button guard the click path has.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import DOM_STUB, needs_node, run_js

pytestmark = needs_node

SUBMIT = "#study-prac-submit"


def _prelude(
    *,
    choice="0",
    result="null",
    qtype="'mcq'",
    body_is_null=False,
    disabled=(),
    loading="false",
):
    disabled_js = "[" + ", ".join(repr(d).replace("'", '"') for d in disabled) + "]"
    body_js = "null" if body_is_null else f"makeEl({{}}, {{ disabled: {disabled_js} }})"
    return DOM_STUB + f"""
const S = {{
  practice: {{
    loading: {loading},
    idx: 0,
    queue: [{{ qtype: {qtype}, options: ['a', 'b', 'c'] }}],
    result: {result},
    choice: {choice},
    hints: [],
    hintBusy: false,
  }},
}};
function body() {{ return {body_js}; }}
function renderPractice() {{}}
"""


def _run(prelude, key="Enter", in_input=False):
    epilogue = f"""
let error = null;
try {{
  practiceKeydown(makeEvent({key!r}, {{ inInput: {str(in_input).lower()} }}));
}} catch (e) {{
  error = String(e && e.name ? e.name + ': ' + e.message : e);
}}
console.log(JSON.stringify({{ clicks, error }}));
"""
    # clickControl is the shared null-safe/disabled-aware click helper.
    return run_js(prelude, "clickControl", "practiceKeydown", epilogue=epilogue)


def test_enter_submits_a_selected_mcq_answer():
    result = _run(_prelude(choice="0"))
    assert result["error"] is None, result["error"]
    assert result["clicks"] == [SUBMIT]


def test_enter_submits_exactly_once():
    result = _run(_prelude(choice="2"))
    assert result["clicks"].count(SUBMIT) == 1


def test_enter_with_no_selection_submits_nothing():
    result = _run(_prelude(choice="null"))
    assert result["error"] is None
    assert result["clicks"] == []


def test_enter_while_typing_submits_nothing():
    result = _run(_prelude(choice="0"), in_input=True)
    assert result["error"] is None
    assert result["clicks"] == []


def test_enter_after_a_result_submits_nothing():
    """Once graded, Enter must not resubmit the same question."""
    result = _run(_prelude(choice="0", result="{ correct: true }"))
    assert result["error"] is None
    assert result["clicks"] == []


def test_enter_on_a_hidden_study_pane_does_not_throw_or_submit():
    """body() returns null when the pane is closed or detached."""
    result = _run(_prelude(choice="0", body_is_null=True))
    assert result["error"] is None, result["error"]
    assert result["clicks"] == []


def test_enter_does_not_bypass_a_disabled_submit_button():
    """The in-flight guard on Check answer must hold for the shortcut too."""
    result = _run(_prelude(choice="0", disabled=(SUBMIT,)))
    assert result["error"] is None
    assert SUBMIT not in result["clicks"], "shortcut clicked a disabled control"


def test_enter_while_loading_submits_nothing():
    result = _run(_prelude(choice="0", loading="true"))
    assert result["error"] is None
    assert result["clicks"] == []


def test_enter_on_a_non_mcq_question_submits_nothing():
    """Open questions submit through the textarea path, not this shortcut."""
    result = _run(_prelude(choice="0", qtype="'open'"))
    assert result["error"] is None
    assert result["clicks"] == []


@pytest.mark.parametrize("key, selector", [
    ("h", "#study-prac-hint"),
    ("c", "#study-prac-consult"),
])
def test_other_shortcuts_still_work(key, selector):
    """The sibling shortcuts must keep working after the fix."""
    result = _run(_prelude(choice="null"), key=key)
    assert result["error"] is None, result["error"]
    assert selector in result["clicks"]


def test_other_shortcuts_survive_a_detached_pane():
    """They dereference body() too; a closed pane must not throw."""
    result = _run(_prelude(choice="null", body_is_null=True), key="c")
    assert result["error"] is None, result["error"]
    assert result["clicks"] == []
