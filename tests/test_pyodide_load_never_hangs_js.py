"""Loading the Python runtime must always end -- in a runtime or an error.

When the page's Content-Security-Policy blocked Pyodide's fetches of its .wasm
and stdlib, window.loadPyodide() neither resolved nor rejected. codeRunner only
timed the *execution* of a snippet, never the load, so the panel sat on
"Loading Python runtime (first time ~10 MB)..." indefinitely with nothing to
say why. Worse, pyodideLoading stayed true, so every later attempt was queued
behind the same promise that would never settle: retrying could not help.

These drive the real loadPyodide under Node with a stub loader that can hang,
reject, succeed late, or fail to load at all. The timeout is shortened in the
prelude; the shipped value is 90 s.
"""

from __future__ import annotations

from pathlib import Path

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

RUNNER = Path(__file__).resolve().parent.parent / "static" / "js" / "codeRunner.js"

PRELUDE = """
let pyodideInstance = null;
let pyodideLoading = false;
const pyodideQueue = [];
const PYODIDE_INDEX_URL = 'https://cdn.example/pyodide/';
const PYODIDE_LOAD_TIMEOUT_MS = 60;   // shortened; the shipped value is 90000

// hang | ok | reject | scripterror | nodef | late
let MODE = 'hang';
let scriptsAppended = 0;
const window = {};
const RUNTIME = { runPython: () => '3.12' };

function installLoader() {
  if (MODE === 'nodef') { delete window.loadPyodide; return; }
  window.loadPyodide = () => {
    if (MODE === 'hang')   return new Promise(() => {});
    if (MODE === 'reject') return Promise.reject(new Error('blocked by CSP'));
    if (MODE === 'late')   return new Promise(r => setTimeout(() => r(RUNTIME), 200));
    return Promise.resolve(RUNTIME);
  };
}

const document = {
  createElement: () => ({}),
  head: {
    appendChild(s) {
      scriptsAppended += 1;
      setTimeout(() => {
        if (MODE === 'scripterror') s.onerror();
        else { installLoader(); s.onload(); }
      }, 0);
    },
  },
};

async function attempt() {
  try { await loadPyodide(); return { ok: true, err: null }; }
  catch (e) { return { ok: false, err: String((e && e.message) || e) }; }
}
"""


def _run(epilogue: str) -> dict:
    return run_js(PRELUDE, "loadPyodide", epilogue=epilogue, source_path=RUNNER)


# --------------------------------------------------------------------------
# The reported bug
# --------------------------------------------------------------------------

def test_a_loader_that_never_settles_becomes_an_error():
    out = _run("""
MODE = 'hang';
const r = await attempt();
console.log(JSON.stringify({ r, loading: pyodideLoading }));
""")
    assert out["r"]["ok"] is False, "a hung load still never settled"
    assert "did not finish loading" in out["r"]["err"]


def test_the_error_names_the_likely_cause():
    """'Failed to load Pyodide' told nobody anything."""
    out = _run("""
MODE = 'hang';
const r = await attempt();
console.log(JSON.stringify({ r }));
""")
    assert "Content-Security-Policy" in out["r"]["err"]


def test_a_failed_load_resets_so_a_retry_can_succeed():
    """The second half of the bug: retries queued behind the dead promise."""
    out = _run("""
MODE = 'hang';
const first = await attempt();
MODE = 'ok';
const second = await attempt();
console.log(JSON.stringify({ first, second, loading: pyodideLoading }));
""")
    assert out["first"]["ok"] is False
    assert out["second"]["ok"] is True, (
        "a retry after a failed load did not start fresh"
    )
    assert out["loading"] is False


def test_callers_queued_during_a_hang_are_all_released():
    out = _run("""
MODE = 'hang';
const results = await Promise.all([attempt(), attempt(), attempt()]);
console.log(JSON.stringify({ results, loading: pyodideLoading }));
""")
    assert [r["ok"] for r in out["results"]] == [False, False, False], (
        "a queued caller was left waiting forever"
    )
    assert out["loading"] is False


# --------------------------------------------------------------------------
# Every other outcome also settles exactly once
# --------------------------------------------------------------------------

def test_a_rejecting_loader_is_reported():
    out = _run("""
MODE = 'reject';
const r = await attempt();
console.log(JSON.stringify({ r, loading: pyodideLoading }));
""")
    assert out["r"]["ok"] is False
    assert "blocked by CSP" in out["r"]["err"]
    assert out["loading"] is False


def test_a_script_that_cannot_be_fetched_is_reported():
    out = _run("""
MODE = 'scripterror';
const r = await attempt();
console.log(JSON.stringify({ r, loading: pyodideLoading }));
""")
    assert out["r"]["ok"] is False
    assert "Could not fetch" in out["r"]["err"]
    assert out["loading"] is False


def test_a_script_that_defines_no_loader_is_reported():
    out = _run("""
MODE = 'nodef';
const r = await attempt();
console.log(JSON.stringify({ r }));
""")
    assert out["r"]["ok"] is False
    assert "did not define loadPyodide" in out["r"]["err"]


def test_a_success_arriving_after_the_timeout_does_not_revive_state():
    """Once reported failed, a straggling resolve must not flip the runtime
    into existence behind the error the user is already looking at."""
    out = _run("""
MODE = 'late';
const r = await attempt();
await new Promise(res => setTimeout(res, 300));   // let the straggler land
console.log(JSON.stringify({ r, cached: pyodideInstance !== null }));
""")
    assert out["r"]["ok"] is False
    assert out["cached"] is False


def test_a_successful_load_is_cached_and_not_fetched_twice():
    out = _run("""
MODE = 'ok';
const a = await attempt();
const b = await attempt();
console.log(JSON.stringify({ a, b, scriptsAppended }));
""")
    assert out["a"]["ok"] and out["b"]["ok"]
    assert out["scriptsAppended"] == 1
