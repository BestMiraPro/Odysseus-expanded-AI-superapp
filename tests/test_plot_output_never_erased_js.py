"""A figure the snippet drew must never be erased by what the snippet printed.

With the runtime finally loading, a matplotlib plot still showed nothing. In a
browser, the same plot run twice gave:

    cache cold   ->  0 images; panel: "Matplotlib is building the font cache..."
    cache warm   ->  1 image, 468x324

and the same plot with one line written to stderr gave 0 images every time.

runPython appended the figure, then handed stderr to showOutput -- and showOutput
begins with panel.innerHTML = '', destroying the figure it had just been given.
matplotlib writes that font-cache notice to stderr on its first import in every
session, so the first plot of every session was replaced by a notice. Then, as
the notice was treated as an error, showOutput hid the whole panel 7 s later,
leaving nothing where the plot should be. A print() erased a plot the same way.

These drive the real showLoading, showOutput and runPython over a small DOM in
which assigning innerHTML clears children, as it does in a browser. Timer delays
are capped so the 7 s auto-hide can be observed inside the test.
"""

from __future__ import annotations

from pathlib import Path

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

RUNNER = Path(__file__).resolve().parent.parent / "static" / "js" / "codeRunner.js"

PRELUDE = r"""
const PYODIDE_INDEX_URL = '/static/lib/pyodide/0.27.5/';
const PYODIDE_LOAD_TIMEOUT_MS = 60;

const timers = [];
const setTimeout = (fn, ms) => {
  const id = globalThis.setTimeout(fn, Math.min(ms, 30));
  timers.push({ id, ms });
  return id;
};
const clearTimeout = (id) => globalThis.clearTimeout(id);

// Assigning innerHTML clears children, exactly the property that erased figures.
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.className = ''; this.style = {}; this.dataset = {};
    this._text = ''; this._html = '';
  }
  appendChild(c) { this.children.push(c); return c; }
  set innerHTML(v) { this.children = []; this._html = String(v); }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  addEventListener() {}
}
const document = { createElement: (t) => new El(t) };
function addCloseBtn() {}

let RESULT = ['', '', []];       // [stdout, stderr, base64 figures]
let LOAD_FAIL = false;
const py = {
  async runPythonAsync(src) {
    if (src.includes('MPLBACKEND')) return undefined;
    const r = RESULT;
    return { toJs: () => r, destroy() {} };
  },
  loadPackage: () => Promise.resolve(),
};
async function loadPyodide() {
  if (LOAD_FAIL) throw new Error('offline');
  return py;
}

const FIG = 'iVBORw0KGgo=';
const PLOT = 'import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])';
const FONT = 'Matplotlib is building the font cache; this may take a moment.';

function walk(el, out = []) {
  for (const c of el.children) { out.push(c); walk(c, out); }
  return out;
}

async function go(code) {
  const panel = new El('div');
  const r = await runPython(code, panel);
  await new Promise(res => globalThis.setTimeout(res, 90));   // let any auto-hide fire
  const all = walk(panel);
  return {
    r,
    images: all.filter(e => e.tagName === 'IMG').length,
    pres: all.filter(e => e.tagName === 'PRE').map(e => ({ cls: e.className, text: e.textContent })),
    warnings: all.filter(e => (e.className || '').includes('code-runner-warning')).length,
    autoHide: timers.filter(t => t.ms === 7000).length,
    hidden: panel.style.display === 'none',
  };
}
"""

FUNCTIONS = ("showLoading", "showOutput", "codeUsesMatplotlib", "runPython")


def _go(setup: str, code: str = "PLOT") -> dict:
    return run_js(PRELUDE, *FUNCTIONS, source_path=RUNNER, epilogue=f"""
{setup}
console.log(JSON.stringify(await go({code})));
""")


def _is_error(pre: dict) -> bool:
    return "code-runner-error" in pre["cls"]


# --------------------------------------------------------------------------
# The reported case: the first plot of every session
# --------------------------------------------------------------------------

def test_the_font_cache_notice_does_not_erase_the_first_plot():
    out = _go("RESULT = ['', FONT, [FIG]];")
    assert out["images"] == 1, "matplotlib's first-import notice erased the figure"


def test_the_notice_is_shown_as_a_warning_not_an_error():
    out = _go("RESULT = ['', FONT, [FIG]];")
    assert out["warnings"] == 1
    assert [p["text"] for p in out["pres"]] == [
        "Matplotlib is building the font cache; this may take a moment."]
    assert not any(_is_error(p) for p in out["pres"])


def test_a_panel_with_a_figure_is_never_auto_hidden():
    """The second half of the disappearance: an 'error' hid the panel at 7 s."""
    out = _go("RESULT = ['', FONT, [FIG]];")
    assert out["autoHide"] == 0
    assert out["hidden"] is False


def test_a_print_does_not_erase_the_plot_either():
    out = _go("RESULT = ['Q at L=4: 4.0', '', [FIG]];")
    assert out["images"] == 1
    assert [p["text"] for p in out["pres"]] == ["Q at L=4: 4.0"]


def test_output_and_warning_both_appear_under_the_figure_in_order():
    out = _go("RESULT = ['Q at L=4: 4.0', FONT, [FIG]];")
    assert out["images"] == 1
    assert [p["text"] for p in out["pres"]] == [
        "Q at L=4: 4.0",
        "Matplotlib is building the font cache; this may take a moment.",
    ]


def test_every_figure_survives():
    out = _go("RESULT = ['', FONT, [FIG, FIG]];")
    assert out["images"] == 2


def test_a_figure_alone_adds_no_text():
    out = _go("RESULT = ['', '', [FIG]];")
    assert out["images"] == 1
    assert out["pres"] == []


# --------------------------------------------------------------------------
# Without a figure, the chat's Run button behaves exactly as before
# --------------------------------------------------------------------------

def test_a_real_error_with_no_figure_is_still_an_error():
    out = _go("RESULT = ['', \"NameError: name 'np' is not defined\", []];")
    assert out["images"] == 0
    assert len(out["pres"]) == 1 and _is_error(out["pres"][0])
    assert out["autoHide"] == 1, "the existing auto-hide for real errors changed"


def test_plain_output_is_unchanged():
    out = _go("RESULT = ['4', '', []];", code="'print(2 + 2)'")
    assert out["pres"] == [{"cls": "code-runner-pre", "text": "4"}]


def test_silence_still_says_so():
    out = _go("RESULT = ['', '', []];", code="'x = 1'")
    assert [p["text"] for p in out["pres"]] == ["(no output)"]


# --------------------------------------------------------------------------
# Callers can tell what happened
# --------------------------------------------------------------------------

def test_a_drawn_plot_reports_success_and_how_many_figures():
    out = _go("RESULT = ['', FONT, [FIG, FIG]];")
    assert out["r"] == {"ok": True, "images": 2, "error": None}


def test_a_failure_reports_its_error():
    out = _go("RESULT = ['', 'ZeroDivisionError: division by zero', []];")
    assert out["r"]["ok"] is False
    assert out["r"]["images"] == 0
    assert "ZeroDivisionError" in out["r"]["error"]


def test_a_runtime_that_cannot_load_resolves_rather_than_rejects():
    out = _go("LOAD_FAIL = true;")
    assert out["r"] == {"ok": False, "images": 0, "error": "offline"}
