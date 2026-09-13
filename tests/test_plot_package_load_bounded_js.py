"""A plot must never leave the panel on "Loading plotting library" forever.

The reported hang was the runtime load: the page's CSP blocked Pyodide's own
downloads, loadPyodide() never settled, and the panel sat on "Loading Python
runtime" indefinitely. That load is now bounded and reports why. But the very next
await in runPython had the same shape and no bound at all:
py.loadPackage(['matplotlib', 'numpy']) fetches ~13 MB of wheels the page may be
unable to complete -- after a partial fetch_pyodide run, or a blocked request --
so the identical hang sat one step further along.

These drive the real runPython (and the real codeUsesMatplotlib) under Node
against a stub runtime whose loadPackage can hang, reject or succeed.

Two prelude details are load-bearing. The package bound is shortened through
PYODIDE_LOAD_TIMEOUT_MS, exactly as the runtime-load tests do. And every timer
delay is capped: a successful run leaves runPython's own 45 s execution guard
pending, which a browser shrugs off but which holds Node open past the harness's
30 s limit -- so without the cap the success case would fail by timing out, for
reasons unrelated to what it tests.
"""

from __future__ import annotations

from pathlib import Path

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

RUNNER = Path(__file__).resolve().parent.parent / "static" / "js" / "codeRunner.js"

PRELUDE = r"""
const PYODIDE_INDEX_URL = '/static/lib/pyodide/0.27.5/';
const PYODIDE_LOAD_TIMEOUT_MS = 60;   // shortened; the shipped value is 90000

// Record every timer, and cap delays so no guard can hold Node open for 45 s.
const timers = [];
const cleared = new Set();
const setTimeout = (fn, ms) => {
  const id = globalThis.setTimeout(fn, Math.min(ms, 200));
  timers.push({ id, ms });
  return id;
};
const clearTimeout = (id) => { cleared.add(id); globalThis.clearTimeout(id); };

const log = [];
function showLoading(panel, msg) { log.push(['loading', String(msg)]); }
function showOutput(panel, text, isError) { log.push([isError ? 'error' : 'output', String(text)]); }
function addCloseBtn() {}

let PKG = 'hang';            // hang | reject | ok
let packagesRequested = null;
const py = {
  async runPythonAsync(src) {
    if (src.includes('MPLBACKEND')) return undefined;
    return { toJs: () => ['', '', ['iVBORw0KGgo=']], destroy() {} };
  },
  loadPackage(names) {
    packagesRequested = names;
    if (PKG === 'hang')   return new Promise(() => {});
    if (PKG === 'reject') return Promise.reject(new Error('404 on a wheel'));
    return Promise.resolve();
  },
};
async function loadPyodide() { return py; }

const document = { createElement: () => ({}) };
const panel = { innerHTML: '', children: [], appendChild(c) { this.children.push(c); } };
const PLOT = 'import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])';
"""


def _run(epilogue: str) -> dict:
    return run_js(PRELUDE, "codeUsesMatplotlib", "runPython",
                  epilogue=epilogue, source_path=RUNNER)


def _errors(out: dict) -> list[str]:
    return [text for kind, text in out["log"] if kind == "error"]


# --------------------------------------------------------------------------
# The hang
# --------------------------------------------------------------------------

def test_a_package_load_that_never_settles_becomes_an_error():
    out = _run("""
PKG = 'hang';
await runPython(PLOT, panel);
console.log(JSON.stringify({ log, images: panel.children.length }));
""")
    errors = _errors(out)
    assert errors, "a hung loadPackage left runPython waiting with no error"
    assert "Could not load the plotting library" in errors[0]
    assert "did not finish loading" in errors[0]
    assert out["images"] == 0


def test_it_does_not_go_on_to_run_the_snippet():
    """With no matplotlib, executing the plot would only add a second error."""
    out = _run("""
PKG = 'hang';
await runPython(PLOT, panel);
console.log(JSON.stringify({ log }));
""")
    assert ["loading", "Running..."] not in out["log"]


def test_the_error_says_how_to_find_out_what_is_missing():
    out = _run("""
PKG = 'hang';
await runPython(PLOT, panel);
console.log(JSON.stringify({ log }));
""")
    assert "fetch_pyodide.py --check" in _errors(out)[0]


def test_a_rejecting_package_load_is_reported_with_its_cause():
    out = _run("""
PKG = 'reject';
await runPython(PLOT, panel);
console.log(JSON.stringify({ log }));
""")
    assert "404 on a wheel" in _errors(out)[0]


# --------------------------------------------------------------------------
# Success is unchanged, and leaves nothing behind
# --------------------------------------------------------------------------

def test_a_successful_load_runs_the_snippet_and_draws():
    out = _run("""
PKG = 'ok';
await runPython(PLOT, panel);
console.log(JSON.stringify({ log, images: panel.children.length }));
""")
    assert not _errors(out), _errors(out)
    assert ["loading", "Running..."] in out["log"]
    assert out["images"] == 1


def test_the_package_timer_is_cleared_once_the_load_succeeds():
    """Otherwise every plot leaves a 90 s timer running to reject a race it
    already lost."""
    out = _run("""
PKG = 'ok';
await runPython(PLOT, panel);
const pkg = timers.filter(t => t.ms === PYODIDE_LOAD_TIMEOUT_MS).map(t => cleared.has(t.id));
console.log(JSON.stringify({ pkg }));
""")
    assert out["pkg"] == [True]


def test_it_requests_exactly_the_plotting_stack():
    out = _run("""
PKG = 'ok';
await runPython(PLOT, panel);
console.log(JSON.stringify({ packagesRequested }));
""")
    assert out["packagesRequested"] == ["matplotlib", "numpy"]


def test_a_snippet_that_does_not_plot_loads_no_packages():
    out = _run("""
PKG = 'hang';        // would hang if it were ever called
await runPython('print(1 + 1)', panel);
console.log(JSON.stringify({ log, packagesRequested }));
""")
    assert out["packagesRequested"] is None
    assert not any("plotting library" in text for _, text in out["log"])
