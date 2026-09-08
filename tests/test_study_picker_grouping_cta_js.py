"""The practice picker's empty state must be actionable, not a dead end.

It read:

    Nothing grouped yet — run "Detect chapters" or "Group themes" from Tidy
    bank in the subject view.

That names a control on a *different* screen and offers no way to get there.
Tidy bank is a small button sitting among the question-bank filters, so the
instruction sent you hunting for it.

The empty state now carries the button itself: it returns to the subject view,
expands Tidy bank, and scrolls it into view, so the sentence and the control
are the same thing.
"""

from __future__ import annotations

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

PRELUDE = """
const calls = [];
let _tab = 'practice';
let _viewGen = 0;
const S = {
  practice: { picker: true },
  subject: { deck: { id: 'deck-1', name: 'Stats' }, materials: [] },
};
function esc(s) { return String(s); }
function renderPractice() { calls.push('renderPractice'); }
function renderSubjectDetail() { calls.push('renderSubjectDetail'); }
function renderTidyBank() { calls.push('renderTidyBank'); }
function setTab(tab) { calls.push('setTab:' + tab); _tab = tab; }
function bumpViewGen() { return ++_viewGen; }

// Minimal DOM: record scrollIntoView and class changes on the tidy panel.
const tidyPanel = {
  hidden: true,
  _classes: new Set(),
  classList: {
    add: (c) => tidyPanel._classes.add(c),
    remove: (c) => tidyPanel._classes.delete(c),
    contains: (c) => tidyPanel._classes.has(c),
  },
  scrollIntoView: (opts) => calls.push('scrollIntoView'),
};
function body() {
  return { querySelector: (sel) => (sel === '#study-tidy' ? tidyPanel : null) };
}
"""


def _run(epilogue):
    return run_js(PRELUDE, "openTidyBankForGrouping", epilogue=epilogue)


def test_it_returns_to_the_subject_view():
    out = _run("""
openTidyBankForGrouping();
await new Promise(r => setTimeout(r, 50));
console.log(JSON.stringify({ calls, practice: S.practice }));
""")
    assert out["practice"] is None, "the picker was left open on top of the subject"
    assert "renderSubjectDetail" in out["calls"]


def test_it_expands_the_tidy_bank_panel():
    out = _run("""
openTidyBankForGrouping();
await new Promise(r => setTimeout(r, 50));
console.log(JSON.stringify({ tidyOpen: S.subject.tidyOpen, calls }));
""")
    assert out["tidyOpen"] is True, "Tidy bank was not expanded"
    assert "renderTidyBank" in out["calls"]


def test_it_scrolls_the_panel_into_view():
    out = _run("""
openTidyBankForGrouping();
await new Promise(r => setTimeout(r, 50));
console.log(JSON.stringify({ calls }));
""")
    assert "scrollIntoView" in out["calls"], (
        "the panel was expanded but left off screen, which is the original complaint"
    )


def test_it_highlights_the_panel_so_the_eye_lands_on_it():
    out = _run("""
openTidyBankForGrouping();
await new Promise(r => setTimeout(r, 50));
console.log(JSON.stringify({ highlighted: tidyPanel._classes.has('study-tidy-flash') }));
""")
    assert out["highlighted"] is True


def test_it_does_nothing_without_a_subject():
    """Defensive: the picker can outlive its subject."""
    prelude = PRELUDE.replace(
        "subject: { deck: { id: 'deck-1', name: 'Stats' }, materials: [] },",
        "subject: null,")
    out = run_js(prelude, "openTidyBankForGrouping", epilogue="""
let error = null;
try { openTidyBankForGrouping(); } catch (e) { error = String(e); }
await new Promise(r => setTimeout(r, 30));
console.log(JSON.stringify({ error, calls }));
""")
    assert out["error"] is None, out["error"]
