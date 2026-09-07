"""B09 — a late response must not overwrite a newer view.

Successful renderStats/renderHistory/renderSubjects already checked ``_tab``,
but their catch branches wrote the error into the shared body *before* any such
check. ``reloadSubject`` rendered the detail view without checking which subject
is now open, and the cross-subject search had no generation check at all, so a
slow query A could replace the results of a fast query B.

A guard on tab name alone is not enough: navigating A → B → A returns to the
same name, so a stale response for the first A would still pass. The token
carries a monotonic generation as well as the identity.
"""

from __future__ import annotations

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

VIEW_FNS = ("bumpViewGen", "captureView", "viewStillCurrent")

PRELUDE = """
let _viewGen = 0;
let _tab = 'stats';
const S = { subject: null };
"""


def _run(epilogue, *extra_fns):
    return run_js(PRELUDE, *VIEW_FNS, *extra_fns, epilogue=epilogue)


# --------------------------------------------------------------------------
# The token itself
# --------------------------------------------------------------------------

def test_a_token_is_current_when_nothing_changed():
    out = _run("""
const t = captureView();
console.log(JSON.stringify({ current: viewStillCurrent(t) }));
""")
    assert out["current"] is True


def test_a_token_is_stale_after_switching_tabs():
    out = _run("""
const t = captureView();
_tab = 'history'; bumpViewGen();
console.log(JSON.stringify({ current: viewStillCurrent(t) }));
""")
    assert out["current"] is False


def test_returning_to_the_same_tab_still_invalidates_the_old_token():
    """A → B → A: a guard on tab name alone would wrongly accept this."""
    out = _run("""
const t = captureView();              // on stats
_tab = 'history'; bumpViewGen();
_tab = 'stats';   bumpViewGen();      // back to the same tab name
console.log(JSON.stringify({ current: viewStillCurrent(t), tab: _tab }));
""")
    assert out["tab"] == "stats"
    assert out["current"] is False, (
        "a stale response for the first visit to this tab would have been rendered"
    )


def test_a_token_is_stale_after_opening_a_different_subject():
    out = _run("""
S.subject = { deck: { id: 'deck-1' } };
const t = captureView();
S.subject = { deck: { id: 'deck-2' } }; bumpViewGen();
console.log(JSON.stringify({ current: viewStillCurrent(t) }));
""")
    assert out["current"] is False


def test_a_token_is_stale_after_closing_the_subject():
    out = _run("""
S.subject = { deck: { id: 'deck-1' } };
const t = captureView();
S.subject = null; bumpViewGen();
console.log(JSON.stringify({ current: viewStillCurrent(t) }));
""")
    assert out["current"] is False


def test_generations_are_monotonic():
    out = _run("""
const a = bumpViewGen(); const b = bumpViewGen();
console.log(JSON.stringify({ increasing: b > a }));
""")
    assert out["increasing"] is True


# --------------------------------------------------------------------------
# Search: an older query must not replace a newer one
# --------------------------------------------------------------------------

SEARCH_PRELUDE = """
let _viewGen = 0;
let _tab = 'subjects';
let _searchGen = 0;
const S = { subject: null };
const writes = [];
function esc(s) { return String(s); }
const resEl = { set innerHTML(v) { writes.push(v); }, get innerHTML() { return ''; } };
const el = { querySelector: () => resEl };

let PLAN = {};
function setPlan(p) { PLAN = p; }
async function jget(path) {
  const q = decodeURIComponent(path.split('q=')[1].split('&')[0]);
  const spec = PLAN[q];
  if (spec.delay) await new Promise(r => setTimeout(r, spec.delay));
  if (spec.error) throw new Error(spec.error);
  return spec.body;
}
"""


def _run_search(epilogue):
    return run_js(SEARCH_PRELUDE, *VIEW_FNS, "runGlobalSearch", epilogue=epilogue)


def test_a_slow_earlier_query_cannot_replace_a_faster_later_one():
    out = _run_search("""
setPlan({
  slow: { delay: 40, body: { questions: [{ question: 'SLOW', qtype: 'mcq' }], cards: [] } },
  fast: { delay: 0,  body: { questions: [{ question: 'FAST', qtype: 'mcq' }], cards: [] } },
});
const a = runGlobalSearch(el, 'slow');
const b = runGlobalSearch(el, 'fast');
await Promise.all([a, b]);
console.log(JSON.stringify({ last: writes[writes.length - 1] }));
""")
    assert "FAST" in out["last"], f"the older query overwrote the newer results: {out['last']}"
    assert "SLOW" not in out["last"]


def test_a_failing_earlier_query_cannot_replace_a_newer_result():
    out = _run_search("""
setPlan({
  slow: { delay: 40, error: 'boom' },
  fast: { delay: 0, body: { questions: [{ question: 'FAST', qtype: 'mcq' }], cards: [] } },
});
const a = runGlobalSearch(el, 'slow');
const b = runGlobalSearch(el, 'fast');
await Promise.all([a, b]);
console.log(JSON.stringify({ last: writes[writes.length - 1] }));
""")
    assert "FAST" in out["last"], (
        f"an older query's error replaced the newer results: {out['last']}"
    )


def test_a_search_result_is_dropped_after_navigating_away():
    out = _run_search("""
setPlan({ q1: { delay: 20, body: { questions: [{ question: 'LATE', qtype: 'mcq' }], cards: [] } } });
const p = runGlobalSearch(el, 'q1');
_tab = 'plan'; bumpViewGen();          // user navigated while it was in flight
await p;
console.log(JSON.stringify({ writes }));
""")
    assert not any("LATE" in w for w in out["writes"]), (
        "a search result was rendered after the user left the view"
    )


def test_an_empty_query_clears_without_a_request():
    out = _run_search("""
setPlan({});
await runGlobalSearch(el, '   ');
console.log(JSON.stringify({ writes }));
""")
    assert out["writes"] == [""], f"an empty query did something unexpected: {out['writes']}"
