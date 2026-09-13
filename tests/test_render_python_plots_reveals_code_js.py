"""When a Study plot draws nothing, its code must come back into view.

renderPythonPlots hides a plotting snippet's source and shows the figure in its
place, with a toggle to read the code. If no figure arrives, hiding the code
leaves a blank space where both the graph and its explanation should be.

It meant to handle that with runPython(src, panel).catch(() => { pre.hidden =
false; }). But runPython catches every failure internally and never rejects, so
that handler could not fire. A structural test that only checked the handler's
text was present kept passing throughout.

runPython now resolves to { ok, images, error }, so the decision is made on what
actually happened. The code is revealed whenever nothing was drawn -- after a
failure, after a rejection, and after a run that "succeeded" without producing a
figure -- and stays hidden only when a figure is there to replace it.
"""

from __future__ import annotations

from pathlib import Path

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

RUNNER = Path(__file__).resolve().parent.parent / "static" / "js" / "codeRunner.js"

PRELUDE = r"""
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.className = ''; this.dataset = {}; this.style = {};
    this.hidden = false; this.textContent = ''; this.parentNode = null;
  }
  insertBefore(node, ref) { node.parentNode = this; this.children.push(node); return node; }
  appendChild(node) { node.parentNode = this; this.children.push(node); return node; }
  addEventListener() {}
  get nextSibling() { return null; }
}
const document = { createElement: (t) => new El(t) };

let OUTCOME = { ok: true, images: 1, error: null };
let REJECT = false;
let calls = 0;
async function runPython(src, panel) {
  calls += 1;
  if (REJECT) throw new Error('boom');
  return OUTCOME;
}

async function drive(source) {
  const container = new El('div');
  const pre = new El('pre');
  container.appendChild(pre);
  const code = { textContent: source, parentElement: pre };
  const root = { querySelectorAll: () => [code] };
  renderPythonPlots(root);
  const hiddenWhileRunning = pre.hidden;
  await new Promise(r => setTimeout(r, 25));
  return { hiddenWhileRunning, hiddenAfter: pre.hidden, calls };
}

const PLOT = 'import matplotlib.pyplot as plt\nplt.plot([1], [2])';
"""


def _drive(setup: str, source: str = "PLOT") -> dict:
    return run_js(PRELUDE, "codeUsesMatplotlib", "renderPythonPlots",
                  source_path=RUNNER, epilogue=f"""
{setup}
console.log(JSON.stringify(await drive({source})));
""")


def test_a_drawn_plot_keeps_its_code_tucked_away():
    out = _drive("OUTCOME = { ok: true, images: 1, error: null };")
    assert out["hiddenWhileRunning"] is True
    assert out["hiddenAfter"] is True


def test_a_failed_plot_reveals_its_code():
    out = _drive("OUTCOME = { ok: false, images: 0, error: 'NameError' };")
    assert out["hiddenAfter"] is False, (
        "a failed plot left its code hidden with nothing in its place"
    )


def test_a_run_that_draws_nothing_reveals_its_code():
    """ok with zero figures is still a blank space for a plot block."""
    out = _drive("OUTCOME = { ok: true, images: 0, error: null };")
    assert out["hiddenAfter"] is False


def test_a_rejected_run_reveals_its_code():
    out = _drive("REJECT = true;")
    assert out["hiddenAfter"] is False


def test_an_unexpected_result_shape_reveals_its_code():
    """Err towards showing the source: a blank space is the worst outcome."""
    out = _drive("OUTCOME = undefined;")
    assert out["hiddenAfter"] is False


def test_a_block_that_does_not_plot_is_left_alone():
    out = _drive("", source="'print(1 + 1)'")
    assert out["calls"] == 0
    assert out["hiddenWhileRunning"] is False
