"""U01 — Study navigation must be usable from the keyboard.

The header renders a tablist, but every tab was a Tab stop with no arrow-key
handling, no aria-controls, and no labelled tabpanel. Opening the pane
established no focus and closing restored none, so a keyboard user could open
Study and have focus left behind on the page underneath.

These drive the real handlers under Node. The pattern is the standard tabs
one: a single Tab stop into the list (roving tabindex), Left/Right to move,
Home/End to jump.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import extract_const, needs_node, run_js

pytestmark = needs_node

FNS = ("bumpViewGen", "setTab", "studyTablistKeydown", "rovingTabIndex")

PRELUDE_TAIL = """
let _viewGen = 0;
let _tab = 'today';
const rendered = [];
const focused = [];

const tabButtons = TABS.map(([id]) => ({
  dataset: { tab: id },
  tabIndex: 0,
  _selected: 'false',
  classList: { toggle() {} },
  setAttribute(name, value) { if (name === 'aria-selected') this._selected = value; },
  focus() { focused.push(this.dataset.tab); },
}));
const _pane = { querySelectorAll: () => tabButtons };
function body() { return { onclick: null, querySelector: () => null }; }
const S = { decks: [], subject: null };
function esc(s) { return s; }
function toast() {}
function setAgentScope() {}
function setAgentPrefill() {}
function renderAgentTab() { rendered.push('agent'); }
function renderToday() { rendered.push('today'); }
function renderSubjects() { rendered.push('subjects'); }
function renderReview() { rendered.push('review'); }
function renderPractice() { rendered.push('practice'); }
function renderPlan() { rendered.push('plan'); }
function renderFocus() { rendered.push('focus'); }
function renderStats() { rendered.push('stats'); }
function renderHistory() { rendered.push('history'); }
function renderAgent() { rendered.push('agent'); }

function makeKey(key, tab) {
  let prevented = false;
  return {
    key,
    target: tabButtons.find(b => b.dataset.tab === tab) || tabButtons[0],
    preventDefault() { prevented = true; },
    get defaultPrevented() { return prevented; },
  };
}
"""


def _run(epilogue):
    return run_js(extract_const("TABS") + PRELUDE_TAIL, *FNS, epilogue=epilogue)


def _report(extra):
    return f"""
{extra}
console.log(JSON.stringify({{
  rendered, focused, tab: _tab,
  tabStops: tabButtons.filter(b => b.tabIndex === 0).map(b => b.dataset.tab),
  selected: tabButtons.filter(b => b._selected === 'true').map(b => b.dataset.tab),
}}));
"""


def _ids():
    import re
    return re.findall(r"\['([a-z]+)'", extract_const("TABS"))


# --------------------------------------------------------------------------
# Roving tabindex: one Tab stop into the list
# --------------------------------------------------------------------------

def test_only_the_selected_tab_is_a_tab_stop():
    out = _run(_report("setTab('plan'); rovingTabIndex();"))
    assert out["tabStops"] == ["plan"], (
        f"expected one Tab stop on the selected tab, got {out['tabStops']}"
    )


def test_the_selected_tab_is_marked_selected():
    out = _run(_report("setTab('stats'); rovingTabIndex();"))
    assert out["selected"] == ["stats"]


# --------------------------------------------------------------------------
# Arrow-key navigation
# --------------------------------------------------------------------------

def test_right_arrow_moves_to_the_next_tab():
    ids = _ids()
    out = _run(_report(f"setTab('{ids[0]}'); studyTablistKeydown(makeKey('ArrowRight', '{ids[0]}'));"))
    assert out["tab"] == ids[1]
    assert out["focused"][-1] == ids[1], "the newly selected tab did not take focus"


def test_left_arrow_moves_to_the_previous_tab():
    ids = _ids()
    out = _run(_report(f"setTab('{ids[2]}'); studyTablistKeydown(makeKey('ArrowLeft', '{ids[2]}'));"))
    assert out["tab"] == ids[1]


def test_arrow_navigation_wraps_at_both_ends():
    ids = _ids()
    out = _run(_report(f"setTab('{ids[0]}'); studyTablistKeydown(makeKey('ArrowLeft', '{ids[0]}'));"))
    assert out["tab"] == ids[-1], "Left from the first tab should wrap to the last"

    out = _run(_report(f"setTab('{ids[-1]}'); studyTablistKeydown(makeKey('ArrowRight', '{ids[-1]}'));"))
    assert out["tab"] == ids[0], "Right from the last tab should wrap to the first"


def test_home_and_end_jump_to_the_ends():
    ids = _ids()
    out = _run(_report(f"setTab('{ids[3]}'); studyTablistKeydown(makeKey('Home', '{ids[3]}'));"))
    assert out["tab"] == ids[0]

    out = _run(_report(f"setTab('{ids[3]}'); studyTablistKeydown(makeKey('End', '{ids[3]}'));"))
    assert out["tab"] == ids[-1]


@pytest.mark.parametrize("key", ["ArrowRight", "ArrowLeft", "Home", "End"])
def test_navigation_keys_are_consumed(key):
    ids = _ids()
    out = _run(f"""
const e = makeKey({key!r}, '{ids[1]}');
setTab('{ids[1]}');
studyTablistKeydown(e);
console.log(JSON.stringify({{ prevented: e.defaultPrevented }}));
""")
    assert out["prevented"] is True, f"{key} was not consumed by the tablist"


def test_an_unrelated_key_is_left_alone():
    ids = _ids()
    out = _run(f"""
const e = makeKey('a', '{ids[1]}');
setTab('{ids[1]}');
studyTablistKeydown(e);
console.log(JSON.stringify({{ prevented: e.defaultPrevented, tab: _tab }}));
""")
    assert out["prevented"] is False
    assert out["tab"] == ids[1]


def test_moving_renders_the_newly_selected_tab():
    """Arrow navigation must actually switch the panel, not just the highlight."""
    ids = _ids()
    out = _run(_report(f"setTab('{ids[0]}'); studyTablistKeydown(makeKey('ArrowRight', '{ids[0]}'));"))
    assert out["rendered"][-1] == ids[1]


# --------------------------------------------------------------------------
# Focus lifecycle
# --------------------------------------------------------------------------

FOCUS_PRELUDE = """
const focused = [];
let _focusReturn = null;
let _pane = {
  querySelector(sel) {
    if (sel.includes('aria-selected')) return { focus: () => focused.push('selected-tab') };
    return { focus: () => focused.push('first-tab') };
  },
};
function body() { return { focus: () => focused.push('body') }; }
"""


def _run_focus(epilogue):
    return run_js(FOCUS_PRELUDE, "focusStudySurface", "restoreFocusAfterStudy",
                  epilogue=epilogue)


def test_opening_focuses_the_selected_tab():
    out = _run_focus("""
focusStudySurface();
console.log(JSON.stringify({ focused }));
""")
    assert out["focused"] == ["selected-tab"]


def test_dismissing_restores_the_invoker():
    out = _run_focus("""
const invoker = { isConnected: true, focus: () => focused.push('invoker') };
_focusReturn = invoker;
restoreFocusAfterStudy();
console.log(JSON.stringify({ focused, cleared: _focusReturn === null }));
""")
    assert out["focused"] == ["invoker"], "focus was not returned to the opener"
    assert out["cleared"] is True, "the stored invoker was not released"


def test_a_detached_invoker_is_not_focused():
    out = _run_focus("""
_focusReturn = { isConnected: false, focus: () => focused.push('gone') };
restoreFocusAfterStudy();
console.log(JSON.stringify({ focused }));
""")
    assert out["focused"] == [], "focus was moved to an element no longer in the page"


def test_restoring_without_an_invoker_is_harmless():
    out = _run_focus("""
let error = null;
_focusReturn = null;
try { restoreFocusAfterStudy(); } catch (e) { error = String(e); }
console.log(JSON.stringify({ focused, error }));
""")
    assert out["error"] is None
    assert out["focused"] == []


def test_a_throwing_invoker_does_not_break_dismissal():
    out = _run_focus("""
let error = null;
_focusReturn = { isConnected: true, focus() { throw new Error('detached'); } };
try { restoreFocusAfterStudy(); } catch (e) { error = String(e); }
console.log(JSON.stringify({ error }));
""")
    assert out["error"] is None, "a failing focus() call escaped and broke close"
