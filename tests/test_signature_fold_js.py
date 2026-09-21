import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_extract_quote_meta_ignores_non_string_inputs():
    values = _node_eval(
        """
        globalThis.document = {
          createElement() {
            return {
              set textContent(value) { this._text = value; },
              get innerHTML() { return this._text || ''; }
            };
          }
        };
        const { _extractQuoteMeta } = await import('./static/js/emailLibrary/signatureFold.js');
        console.log(JSON.stringify({
          nullValue: _extractQuoteMeta(null),
          objectValue: _extractQuoteMeta({bad: true})
        }));
        """
    )

    assert values == {"nullValue": "", "objectValue": ""}


def test_extract_quote_meta_keeps_outlook_headers():
    values = _node_eval(
        """
        globalThis.document = {
          createElement() {
            return {
              set textContent(value) { this._text = value; },
              get innerHTML() { return this._text || ''; }
            };
          }
        };
        const { _extractQuoteMeta } = await import('./static/js/emailLibrary/signatureFold.js');
        const html = 'From: Alice <alice@example.com> Sent: Monday, May 4, 2026 To: Bob Subject: hi';
        console.log(JSON.stringify({ meta: _extractQuoteMeta(html) }));
        """
    )

    assert values["meta"] == "Alice · Monday, May 4, 2026"


_ESC_DOC = r"""
globalThis.document = {
  createElement() {
    const d = {};
    let t = '';
    const escS = (s) => String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    Object.defineProperty(d, 'textContent', { set(v) { t = escS(v); } });
    Object.defineProperty(d, 'innerHTML', { get() { return t; } });
    return d;
  },
};
"""


def test_fold_summary_escapes_attacker_quote_meta():
    # #29/#30/#50: _extractQuoteMeta decodes entities so a quoted block can
    # carry attacker angle brackets -- but its only HTML sink is _foldSummary,
    # which must escape both the name and the sub-meta parts.
    values = _node_eval(
        _ESC_DOC
        + """
        const { _foldSummary, _extractQuoteMeta } = await import('./static/js/emailLibrary/signatureFold.js');
        const meta = _extractQuoteMeta(
          'On Monday, <img src=x onerror="alert(1)"> wrote:'
        );
        const summary = _foldSummary('Earlier reply', '', meta);
        const direct = _foldSummary('Earlier reply', '', '<img src="x" onerror="alert(2)"> · tail');
        console.log(JSON.stringify({ meta, summary, direct }));
        """
    )

    for rendered in (values["summary"], values["direct"]):
        assert "<img" not in rendered
        assert "onerror=\"alert(1)\"" not in rendered or "&quot;" in rendered
    assert "&lt;img" in values["direct"] or "&lt;img" in values["summary"]
    # the decoded attack text must survive as DATA, escaped, never as markup
    combined = values["summary"] + values["direct"]
    assert "&lt;" in combined


def test_quote_meta_decodes_once_but_never_reaches_markup():
    # The entity-decoded text is consumed by length-only heuristics
    # (_isBloatedSig/_looksLikeSignature, both booleans) and by the escaping
    # _foldSummary sink -- it is never spliced into html anywhere else.
    values = _node_eval(
        _ESC_DOC
        + """
        const { _isBloatedSig, _looksLikeSignature, _foldSignature } = await import('./static/js/emailLibrary/signatureFold.js');
        const h = 'Hi there<br><br>-- <br>Best regards,<br>Bob Smith<br>Head of Engineering<br>'
          + 'Acme Corporation Pte. Ltd.<br>Registered in Singapore, UEN 202600123X<br>'
          + 'This message is confidential and intended solely for the named recipient.<br>'
          + '+65 6123 4567 www.acme.example';
        const wrapped = _foldSignature(h);
        console.log(JSON.stringify({
          bool1: _isBloatedSig('a'.repeat(600) + '<img src=x onerror=alert(1)>'),
          bool2: _looksLikeSignature('Confidential'.repeat(10) + '<img src=x>'.repeat(3)),
          wrapped,
        }));
        """
    )

    assert values["bool1"] is True
    assert isinstance(values["bool2"], bool)
    # quote/signature folding re-wraps the original html and never injects the
    # decoded text; the summary builds only from the escaping _foldSummary
    assert "<details class=\"email-sig-fold\">" in values["wrapped"]
    assert "<summary class=\"email-fold-summary\">" in values["wrapped"]
    assert "<img src=x" not in values["wrapped"]
