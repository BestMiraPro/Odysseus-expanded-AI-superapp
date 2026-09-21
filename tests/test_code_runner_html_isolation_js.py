"""HTML runner preview isolation (security plan S1, alert #11).

Drives the REAL extracted runHTML from static/js/codeRunner.js against a DOM
stub that records the exact construction order. Contract pinned here:

- the popup holds only a trusted wrapper; the untrusted document goes
  exclusively into a full-size iframe via ``srcdoc``;
- ``sandbox`` is set BEFORE ``srcdoc`` and BEFORE insertion;
- ``allow-scripts`` only — no ``allow-same-origin``;
- ``document.write`` of user/model HTML is gone;
- the popup-blocked path and the close-button behavior are preserved.

Origin behavior (opaque origin, storage/API access failing) is verified in a
real browser by scripts/browser_check_html_isolation.py — DOM stubs cannot
prove origin separation.
"""

from __future__ import annotations

from tests._study_js_harness import STUDY_JS, needs_node, run_js

CODE_RUNNER_JS = STUDY_JS.parent / "codeRunner.js"

PRELUDE = r"""
// --- popup/document stubs that record construction order ---
let blocked = false;
let calls = [];
const frames = [];

function makeFrame() {
  const f = {
    _sandbox: null, _srcdoc: null, inserted: false, title: '', style: {},
    attrs: {},
    _log(ev) { calls.push(ev); },
    setAttribute(k, v) { this.attrs[k] = v; this._sandbox = v; this._log('set:' + k); },
    get sandbox() { return this._sandbox; },
    set srcdoc(v) { this._srcdoc = v; this._log('set:srcdoc'); },
    get srcdoc() { return this._srcdoc; },
  };
  frames.push(f);
  return f;
}

const win = {
  opener: 'ORIGINAL-OPENER',
  document: {
    createElement(tag) { return makeFrame(); },
    body: {
      style: {},
      replaceChildren(...kids) {
        kids.forEach((k) => { if (k) { k.inserted = true; k._log('inserted'); } });
      },
    },
    open() { calls.push('document.open'); },
    write(code) { calls.push('document.write:' + code); },
    close() { calls.push('document.close'); },
  },
};
const window = {
  open(url, target, features) {
    calls.push('window.open');
    return blocked ? null : win;
  },
};
let panelShown = [];
let closeCount = 0;
function showOutput(panel, text, isError) { panelShown.push({ text: String(text), isError: !!isError }); }
function addCloseBtn(panel) { closeCount += 1; }
"""


def _run(epilogue: str) -> dict:
    return run_js(PRELUDE, "runHTML", epilogue=epilogue,
                  source_path=CODE_RUNNER_JS)


_HAPPY = r"""
const payload = '<script>alert(1)</script><img src=x onerror=alert(2)>';
runHTML(payload, { innerHTML: '' });
const frame = frames[0];
console.log(JSON.stringify({
  sandbox: frame._sandbox,
  srcdocIsCode: frame._srcdoc === payload,
  inserted: frame.inserted === true,
  openerCleared: win.opener === null,
  documentWriteNever: !calls.some((c) => c.startsWith('document.write')),
  documentOpenNever: !calls.includes('document.open'),
  order: calls,
  showedOpened: panelShown.some((p) => p.text === 'Opened in new window' && !p.isError),
  closeCount,
}));
"""

_BLOCKED = r"""
blocked = true;
runHTML('<script>x</script>', { innerHTML: '' });
console.log(JSON.stringify({
  noFrame: frames.length === 0,
  blockedMessage: panelShown.some((p) => p.text.includes('Popup blocked')),
  closeCount,
  writeNever: !calls.some((c) => c.startsWith('document.write')),
}));
"""


@needs_node
def test_runhtml_wraps_code_in_a_sandboxed_iframe():
    result = _run(_HAPPY)
    assert result["sandbox"] == "allow-scripts", (
        f"sandbox={result['sandbox']!r} — expected exactly allow-scripts, "
        "never allow-same-origin"
    )
    assert result["srcdocIsCode"], "the document must go exclusively in srcdoc"
    assert result["inserted"], "the frame never reached the wrapper body"
    assert result["openerCleared"], "opener is still cleared"
    assert result["documentWriteNever"], (
        "user/model HTML still reaches the popup document via document.write"
    )
    assert result["documentOpenNever"], "the popup document is no longer opened raw"
    # sandbox is set before srcdoc and before insertion
    seq = result["order"]
    assert seq.index("set:sandbox") < seq.index("set:srcdoc") < seq.index("inserted"), (
        f"construction order wrong: {seq}"
    )
    assert result["showedOpened"] and result["closeCount"] == 1


@needs_node
def test_runhtml_blocked_popup_still_reports_and_stays_inert():
    result = _run(_BLOCKED)
    assert result["noFrame"], "blocked path must not construct any frame"
    assert result["blockedMessage"], "the popup-blocked message disappeared"
    assert result["writeNever"]
    assert result["closeCount"] == 1