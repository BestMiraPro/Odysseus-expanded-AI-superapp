"""U04 — a search result must be a way to get to the item.

Results were rendered as plain `<div>` rows holding truncated text and a type
label: no id, no subject, no control. A user could find a fragment and then had
no way to reach it. The API already returns `id` and `deck_id` for both
questions and cards, so the identity was being discarded by the renderer.

Opening a result must not start a graded attempt — finding something is not
answering it.
"""

from __future__ import annotations

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

FNS = ("bumpViewGen", "captureView", "viewStillCurrent",
       "subjectNameFor", "searchResultRow", "runGlobalSearch")

PRELUDE = """
let _viewGen = 0;
let _tab = 'subjects';
let _searchGen = 0;
let _lastSearchQuery = '';
let _searchReturn = null;
const S = { subject: null, decks: [{ id: 'd1', name: 'Biology' }, { id: 'd2', name: 'Chemistry' }] };
const writes = [];
function esc(s) { return String(s).replace(/[&<>"]/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
const resEl = { set innerHTML(v) { writes.push(v); }, get innerHTML() { return ''; } };
const el = { querySelector: () => resEl };

let PLAN = {};
function setPlan(p) { PLAN = p; }
async function jget(path) {
  const q = decodeURIComponent(path.split('q=')[1].split('&')[0]);
  const spec = PLAN[q] || { body: { questions: [], cards: [] } };
  if (spec.delay) await new Promise(r => setTimeout(r, spec.delay));
  if (spec.error) throw new Error(spec.error);
  return spec.body;
}

const RESULTS = {
  questions: [{ id: 'q7', deck_id: 'd1', question: 'What is ATP?', qtype: 'mcq' }],
  cards: [{ id: 'c9', deck_id: 'd2', front: 'Avogadro number', back: '6.022e23' }],
};
"""


def _run(epilogue):
    return run_js(PRELUDE, *FNS, epilogue=epilogue)


def _search_html(extra=""):
    return _run(f"""
setPlan({{ atp: {{ body: RESULTS }} }});
{extra}
await runGlobalSearch(el, 'atp');
console.log(JSON.stringify({{ html: writes[writes.length - 1] }}));
""")["html"]


# --------------------------------------------------------------------------
# Results carry identity and are operable
# --------------------------------------------------------------------------

def test_each_result_is_an_operable_control():
    html = _search_html()
    assert "<button" in html, "results are still inert rows with no control"


def test_each_result_carries_its_item_identity():
    html = _search_html()
    assert 'data-result-id="q7"' in html
    assert 'data-result-id="c9"' in html


def test_each_result_carries_its_kind():
    html = _search_html()
    assert 'data-result-kind="question"' in html
    assert 'data-result-kind="card"' in html


def test_each_result_carries_the_subject_it_belongs_to():
    html = _search_html()
    assert 'data-deck-id="d1"' in html
    assert 'data-deck-id="d2"' in html


def test_each_result_names_its_subject_for_the_reader():
    """"Biology" is the context that makes a fragment meaningful."""
    html = _search_html()
    assert "Biology" in html
    assert "Chemistry" in html


def test_each_result_has_an_accessible_name():
    html = _search_html()
    assert "aria-label=" in html, "the buttons have no accessible name"


def test_the_excerpt_is_still_shown():
    html = _search_html()
    assert "What is ATP?" in html
    assert "Avogadro number" in html


# --------------------------------------------------------------------------
# Safety and existing behaviour
# --------------------------------------------------------------------------

def test_result_content_is_escaped():
    out = _run("""
setPlan({ x: { body: { questions: [
  { id: 'q1', deck_id: 'd1', question: '<img src=x onerror=alert(1)>', qtype: 'mcq' },
], cards: [] } } });
await runGlobalSearch(el, 'x');
console.log(JSON.stringify({ html: writes[writes.length - 1] }));
""")
    assert "<img" not in out["html"], "search results are not escaped"
    assert "&lt;img" in out["html"]


def test_an_unknown_subject_still_renders():
    """A result whose deck is not in the local list must not break the row."""
    out = _run("""
setPlan({ x: { body: { questions: [
  { id: 'q1', deck_id: 'unknown', question: 'orphan', qtype: 'mcq' },
], cards: [] } } });
await runGlobalSearch(el, 'x');
console.log(JSON.stringify({ html: writes[writes.length - 1] }));
""")
    assert "orphan" in out["html"]
    assert 'data-result-id="q1"' in out["html"]


def test_no_results_still_says_so():
    out = _run("""
setPlan({ none: { body: { questions: [], cards: [] } } });
await runGlobalSearch(el, 'none');
console.log(JSON.stringify({ html: writes[writes.length - 1] }));
""")
    assert "No results" in out["html"]


def test_the_generation_guard_still_holds():
    """U04 must not undo B09."""
    out = _run("""
setPlan({
  slow: { delay: 40, body: RESULTS },
  fast: { delay: 0, body: { questions: [], cards: [] } },
});
const a = runGlobalSearch(el, 'slow');
const b = runGlobalSearch(el, 'fast');
await Promise.all([a, b]);
console.log(JSON.stringify({ html: writes[writes.length - 1] }));
""")
    assert "No results" in out["html"], "an older query overwrote the newer one again"
