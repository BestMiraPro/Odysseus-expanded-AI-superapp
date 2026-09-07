"""B02 — "Ask the tutor" must reach the tutor.

``openAgent`` calls ``setTab('agent')``, but neither ``TABS`` nor ``setTab``'s
renderer map contained ``agent``. The tab variable changed while the previous
content stayed on screen, so the button looked broken — and the already-imported
studyAgent.js renderer was never reached.

The scope guards were wrong in the same area: ``if (deckId)`` / ``if (prefill)``
meant opening the tutor without one silently kept the *previous* subject's scope
and draft.
"""

from __future__ import annotations

from tests._study_js_harness import (
    DOM_STUB,
    extract_const,
    extract_function,
    needs_node,
    run_js,
)

pytestmark = needs_node

TAB_IDS = ["today", "subjects", "review", "practice", "plan", "focus",
           "stats", "history"]


def _prelude():
    """Stub every collaborator setTab/openAgent touch, recording the calls."""
    return DOM_STUB + extract_const("TABS") + """
let _viewGen = 0;   // setTab bumps the view generation (B09)
const rendered = [];
const scope = [];
const prefill = [];
let _tab = 'today';

const tabButtons = TABS.map(([id]) => ({
  dataset: { tab: id },
  _selected: 'false',
  classList: { toggle() {} },
  setAttribute(name, value) { if (name === 'aria-selected') this._selected = value; },
}));
const _pane = { querySelectorAll: () => tabButtons };
function body() { return { onclick: null, querySelector: () => null }; }

const S = { decks: [], subject: { deck: { id: 'from-state' } } };
function esc(s) { return s; }
function toast() {}
function setAgentScope(id) { scope.push(id === undefined ? '<undefined>' : id); }
function setAgentPrefill(t) { prefill.push(t === undefined ? '<undefined>' : t); }
function renderAgentTab() { rendered.push('agent'); }

function renderToday() { rendered.push('today'); }
function renderSubjects() { rendered.push('subjects'); }
function renderReview() { rendered.push('review'); }
function renderPractice() { rendered.push('practice'); }
function renderPlan() { rendered.push('plan'); }
function renderFocus() { rendered.push('focus'); }
function renderStats() { rendered.push('stats'); }
function renderHistory() { rendered.push('history'); }
"""


def _run(epilogue):
    return run_js(_prelude(), "bumpViewGen", "setTab", "renderAgent", "openAgent",
                  epilogue=epilogue)


def _report(extra=""):
    return f"""
{extra}
const selected = tabButtons.filter(b => b._selected === 'true').map(b => b.dataset.tab);
console.log(JSON.stringify({{ rendered, scope, prefill, selected, tab: _tab }}));
"""


# --------------------------------------------------------------------------
# The tutor actually renders
# --------------------------------------------------------------------------

def test_selecting_the_agent_tab_renders_the_tutor():
    out = _run(_report("setTab('agent');"))
    assert "agent" in out["rendered"], (
        "setTab('agent') rendered nothing — the tutor is unreachable"
    )


def test_the_agent_tab_has_a_visible_selected_location():
    """Without a tab button there is no selected state and no way back."""
    out = _run(_report("setTab('agent');"))
    assert out["selected"] == ["agent"], (
        f"expected the agent tab to be the selected one, got {out['selected']}"
    )


def test_open_agent_from_a_subject_scopes_and_renders():
    out = _run(_report("openAgent('deck-1', 'why is ATP special?');"))
    assert out["scope"] == ["deck-1"]
    assert out["prefill"] == ["why is ATP special?"]
    assert "agent" in out["rendered"]


def test_returning_to_another_tab_still_works():
    """The tutor must not be a dead end."""
    out = _run(_report("openAgent('deck-1', ''); setTab('practice');"))
    assert out["rendered"][-1] == "practice"
    assert out["selected"] == ["practice"]


# --------------------------------------------------------------------------
# Scope must not leak between subjects
# --------------------------------------------------------------------------

def test_opening_without_a_subject_clears_the_previous_scope():
    out = _run(_report("openAgent('deck-1', 'first'); openAgent(null, '');"))
    assert out["scope"] == ["deck-1", None], (
        f"second open did not clear the previous subject scope: {out['scope']}"
    )


def test_opening_without_a_draft_clears_the_previous_draft():
    out = _run(_report("openAgent('deck-1', 'first draft'); openAgent('deck-2', '');"))
    assert out["prefill"] == ["first draft", ""], (
        f"the previous subject's draft leaked into the new one: {out['prefill']}"
    )


def test_switching_subjects_rescopes():
    out = _run(_report("openAgent('deck-1', ''); openAgent('deck-2', '');"))
    assert out["scope"] == ["deck-1", "deck-2"]


# --------------------------------------------------------------------------
# Invariant that would have caught this class of bug
# --------------------------------------------------------------------------

def test_every_tab_has_a_renderer():
    """A tab with no renderer is a dead control — exactly this bug."""
    out = _run(_report(
        "for (const [id] of TABS) { setTab(id); }"
    ))
    missing = [t for [t] in [[i] for i in _tab_ids()] if t not in out["rendered"]]
    assert missing == [], f"tabs with no renderer: {missing}"


def _tab_ids():
    import re
    src = extract_const("TABS")
    return re.findall(r"\['([a-z]+)'", src)


def test_the_original_tabs_are_all_still_present():
    """Adding the tutor must not drop an existing tab."""
    ids = _tab_ids()
    assert [t for t in TAB_IDS if t in ids] == TAB_IDS, f"a tab went missing: {ids}"


def test_render_agent_forwards_the_current_subject():
    """The tutor needs the subject it was opened for."""
    src = extract_function("renderAgent")
    assert "deckId" in src and "S.subject" in src
