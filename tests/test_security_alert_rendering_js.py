r"""S2 — contextual escaping and selector construction under Node.

Drives the real functions extracted from the real files (not copies), so if a
function changes the test runs the change:

- ``_escHtml`` (document.js) must delegate to the canonical ``uiModule.esc``,
  complete with quote escaping, because it renders inside quoted attributes.
- ``_clockFace`` (calendar.js) must validate a complete HH:MM before
  interpolating into the hero-flock markup; anything else renders the empty
  em-dash placeholder, so the minute/colon slots can never carry markup.
- ``_taskCardById`` (tasks.js) must select by dataset comparison over a
  constant ``.task-card`` selector — never by interpolating the id into a
  selector string, which is how ids containing ``"``, ``\\``, ``]`` or a
  newline broke out of the attribute-value context.

The compaction-exception sink in chatRenderer.js and the attachment-card
lookups in chat.js are not pure functions, so those are pinned at the source
level with the same assertions the existing notes helpers use.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._study_js_harness import run_js

_REPO = Path(__file__).resolve().parents[1]
_DOCUMENT_JS = _REPO / "static" / "js" / "document.js"
_UI_JS = _REPO / "static" / "js" / "ui.js"
_CALENDAR_JS = _REPO / "static" / "js" / "calendar.js"
_TASKS_JS = _REPO / "static" / "js" / "tasks.js"
_CHAT_JS = _REPO / "static" / "js" / "chat.js"
_CHAT_RENDERER_JS = _REPO / "static" / "js" / "chatRenderer.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node binary not on PATH"
)

_PLACEHOLDER = (
    '<span class="cal-hero-clock-hh" data-seg="hh">\u2014</span>'
    '<span class="cal-hero-sep"> : </span>'
    '<span class="cal-hero-clock-mm" data-seg="mm">\u2014</span>'
)
_HHMM = re.compile(
    r'<span class="cal-hero-clock-hh" data-seg="hh">(\d{2})</span>'
    r'<span class="cal-hero-sep"> : </span>'
    r'<span class="cal-hero-clock-mm" data-seg="mm">(\d{2})</span>'
)


def _extract_function(name: str, path: Path) -> str:
    """Brace-matched source of ``function name(...) {...}``, nested or not."""
    src = path.read_text(encoding="utf-8")
    match = re.search(r"\bfunction\s+" + re.escape(name) + r"\s*\([^{]*\)\s*\{", src)
    assert match, f"{name} not found in {path.name}"
    depth, i = 0, src.index("{", match.start())
    j = i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[match.start() : j + 1]
        j += 1
    raise AssertionError(f"{name} body did not close in {path.name}")


def _ui_esc_prelude() -> str:
    """The real _ESC_MAP + esc out of ui.js, slotted in as ``uiModule.esc``."""
    src = _UI_JS.read_text(encoding="utf-8")
    mapping = re.search(r"const _ESC_MAP = \{[^}]+\};", src)
    fn = re.search(r"export function esc\(s\) \{\n[^\n]+\n\}", src)
    assert mapping and fn, "esc/_ESC_MAP not found in ui.js"
    esc_fn = fn.group(0).replace("export function", "function", 1)
    return f"{mapping.group(0)}\n{esc_fn}\nconst uiModule = {{ esc }};\n"


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(_REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "no output from node"
    return proc.stdout.strip()


@needs_node
def test_eschtml_escapes_quotes_for_attribute_context():
    body = _extract_function("_escHtml", _DOCUMENT_JS)
    assert "uiModule.esc(" in body and "replace(/&/g" not in body

    vectors = [
        'x" onerror="window.__xss=1',
        "<img src=x onerror=alert(1)>",
        "&quot;",
        "a'b",
    ]
    script = (
        _ui_esc_prelude()
        + body
        + "\n\nconsole.log(JSON.stringify(["
        + ", ".join(json.dumps(v) for v in vectors)
        + "].map(v => _escHtml(v))));\n"
    )
    out = json.loads(_run_node(script))

    assert out[0] == "x&quot; onerror=&quot;window.__xss=1"
    assert out[1] == "&lt;img src=x onerror=alert(1)&gt;"
    assert out[2] == "&amp;quot;"  # single encoding: pass-through stays readable
    assert out[3] == "a&#39;b"
    for rendered in out:
        assert not re.search(r'[<>"\']', rendered)


@needs_node
def test_clockface_renders_placeholder_for_non_hhmm_values():
    payloads = [
        "13:47",
        "00:00",
        "23:59",
        "12:05",
        'x" onerror="window.__xss=1',
        "<img src=x onerror=alert(1)>",
        "&quot;",
        "13:4",     # minute not zero-padded
        "13:470",   # trailing junk
        "3:47",     # hour not zero-padded
        "99:47",    # hour out of range
        "",
    ]
    prelude = "const payloads = " + json.dumps(payloads) + ";\n"
    epilogue = "console.log(JSON.stringify(payloads.map(p => _clockFace(p))));\n"
    out = run_js(prelude, "_clockFace", epilogue=epilogue, source_path=_CALENDAR_JS)

    def hhmm(markup):
        m = _HHMM.match(markup)
        assert m, markup
        return m.group(1), m.group(2)

    assert hhmm(out[0])[0] in {"13", "01"} and hhmm(out[0])[1] == "47"
    assert hhmm(out[1])[0] in {"00", "12"} and hhmm(out[1])[1] == "00"
    assert hhmm(out[2])[0] in {"23", "11"} and hhmm(out[2])[1] == "59"
    assert hhmm(out[3]) == ("12", "05")
    for i in range(4, len(payloads)):
        # exact placeholder — nothing of the payload reached the markup
        assert out[i] == _PLACEHOLDER
        assert "'" not in out[i] and '<img' not in out[i] and 'onerror' not in out[i]


@needs_node
def test_task_card_by_id_matches_exact_dataset_value_only():
    prelude = textwrap.dedent(
        r"""
        const WEIRD = ['weird] "', '\\', '\n', ' name'].join('');
        const cards = [
          { dataset: { id: 'task-1' } },
          { dataset: { id: 'task-1x' } },
          { dataset: { id: WEIRD } },
          { dataset: { id: 'task-1 name] "\\\n something' } },
        ];
        const selectors = [];
        const document = {
          querySelectorAll(sel) { selectors.push(sel); return cards; },
          querySelector() { throw new Error('id interpolated into querySelector'); },
        };
        const window = { CSS: undefined };
        """
    )
    epilogue = textwrap.dedent(
        """
        console.log(JSON.stringify({
          exact: _taskCardById('task-1') === cards[0],
          prefixMiss: _taskCardById('task-1x') === cards[1],
          weird: _taskCardById(WEIRD) === cards[2],
          miss: _taskCardById('nope') === undefined,
          selectors,
        }));
        """
    )
    out = run_js(prelude, "_taskCardById", epilogue=epilogue, source_path=_TASKS_JS)

    assert out["exact"] is True
    assert out["prefixMiss"] is True
    assert out["weird"] is True
    assert out["miss"] is True
    # every lookup used the constant selector — a value can never reach the
    # selector syntax (the stubbed querySelector throws if it does)
    assert out["selectors"] == [".task-card"] * 4


def test_compaction_exception_renders_as_text():
    src = _CHAT_RENDERER_JS.read_text(encoding="utf-8")
    assert "compactBody.textContent = 'Compaction failed: ' + String(err.message || err);" in src
    assert "compactBody.style.color = 'var(--red)';" in src
    assert "innerHTML = '<span style=\"color:var(--red);\">Compaction failed" not in src


def test_chat_attachment_lookups_compare_datasets_not_selectors():
    src = _CHAT_JS.read_text(encoding="utf-8")
    assert '[data-file-id="' not in src
    assert '[data-name="' not in src
    assert ".replace(/\"g" not in src
    assert "const _findAttachCardByName = (name) => Array.from(_aw.querySelectorAll('.attach-card'))" in src
    assert ".find(el => el.dataset.name === String(name || ''));" in src
    assert "Array.from(_aw.querySelectorAll('[data-file-id]'))" in src
    assert ".find(el => el.dataset.fileId === String(_att.id));" in src


def test_task_card_never_interpolates_id_into_selector():
    body = _extract_function("_taskCardById", _TASKS_JS)
    assert "querySelectorAll('.task-card')" in body
    assert "el.dataset.id === String(id)" in body
    assert "querySelector(`" not in body
    assert "CSS.escape" not in body


# ---------------------------------------------------------------------------
# S6 - detached-div HTML-to-text conversions (#16/#17) replaced by an inert
# template parse; and the emailInbox reply body (#31/#51) reusing it.
# ---------------------------------------------------------------------------

# A fragment DOM for the email text helper: parses tags, rotates out
# script/style/iframe/object/embed nodes on querySelectorAll+remove, and
# models innerText's <br> as line breaks and its one-pass entity decoding.
_EMAIL_TEXT_DOM = """
const BLOCK = /^(address|article|blockquote|div|footer|header|h[1-6]|li|main|nav|ol|p|pre|section|table|tbody|tr|ul)$/i;
function entityDecode(s) {
  return s.replace(/&(#39|#x27|quot|amp|lt|gt|nbsp);/g, (e) => ({
    '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&#x27;': "'", '&nbsp;': ' ',
  })[e]);
}
function fragmentText(nodes) {
  let out = '';
  for (const n of nodes) {
    if (n.removed) continue;
    if (n.text !== undefined) { out += entityDecode(n.text); continue; }
    out += fragmentText(n.children);
  }
  return out;
}
function parse(html) {
  const root = { tag: null, children: [] };
  const stack = [root];
  const re = /<(\\/?)([A-Za-z][A-Za-z0-9:-]*)([^<>]*?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[4] !== undefined) { stack[stack.length - 1].children.push({ text: m[4] }); continue; }
    const tag = m[2];
    if (m[1] === '/') {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tag && stack[i].tag.toLowerCase() === tag.toLowerCase()) { stack.length = i; break; }
      }
      continue;
    }
    if (/\\/\\s*$/.test(m[3])) continue;
    const el = {
      tag, children: [], removed: false, parent: stack[stack.length - 1],
      remove() { this.removed = true; },
      replaceWith(text) { this.text = text; this.children = []; },
      append(text) { this.children.push({text}); },
    };
    stack[stack.length - 1].children.push(el);
    if (!/^(br|img|hr|input|meta|link|col|embed|source|track|wbr)$/i.test(tag)) stack.push(el);
  }
  return root;
}
const document = {
  createElement(tag) {
    if (tag === 'template') {
      const tpl = {
        set innerHTML(v) { this._root = parse(v); },
        content: {
          _tpl: null,
          get textContent() { return fragmentText(this._tpl._root.children); },
          querySelectorAll(sel) {
            const names = sel.split(',').map((s) => s.trim().toLowerCase());
            const out = [];
            const walk = (nodes) => {
              for (const n of nodes) {
                if (n.tag && !n.removed) {
                  if (names.includes(n.tag.toLowerCase())) out.push(n);
                  walk(n.children);
                }
              }
            };
            walk(this._tpl._root.children);
            return out;
          },
        },
        _root: null,
      };
      tpl.content._tpl = tpl;
      return tpl;
    }
    if (tag === 'div') {
      const div = {
        _nodes: [],
        appendChild(content) { this._nodes = [...content._tpl._root.children]; },
        get innerText() { return fragmentText(this._nodes); },
        get textContent() { return fragmentText(this._nodes); },
      };
      return div;
    }
    throw new Error('unsupported element: ' + tag);
  },
};
"""


@needs_node
def test_email_html_to_plain_text_drops_payload_nodes_before_extraction():
    body = _extract_function("_emailHtmlToPlainText", _DOCUMENT_JS)
    assert "createElement('template')" in body
    assert "querySelectorAll('script,style,iframe,object,embed')" in body
    assert "createElement('div')" not in body

    script = (
        _EMAIL_TEXT_DOM
        + body
        + r"""
console.log(JSON.stringify(_emailHtmlToPlainText(
  '<p>Hello <b>world</b></p><script>alert(1)</script><style>*{}</style>' +
  '<iframe src="x"></iframe>visible <object></object><embed src="y"><span>tail</span>'
)));
"""
    )
    out = json.loads(_run_node(script))
    assert 'Hello world' in out and 'visible' in out and 'tail' in out
    assert 'alert(1)' not in out
    assert '{}' not in out


@needs_node
def test_email_html_to_plain_text_preserves_breaks_and_decodes_once():
    body = _extract_function("_emailHtmlToPlainText", _DOCUMENT_JS)
    script = (
        _EMAIL_TEXT_DOM
        + body
        + r"""
console.log(JSON.stringify(_emailHtmlToPlainText(
  'Hello<br>world<br><br>again &amp; &lt; &gt; &quot; &#39; &amp;lt;'
)));
"""
    )
    out = json.loads(_run_node(script))
    assert "Hello\nworld\n\nagain" in out
    tail = out.split("again", 1)[1]
    # one DOM decode: the raw entities surface once...
    assert " & < > \" '" in tail
    # ...and a double-encoded entity decodes exactly one level, never to markup
    assert "&lt;" in tail
    assert "&amp;" not in tail


@needs_node
def test_sanitize_outgoing_email_body_goes_through_the_inert_extraction():
    sanitize = _extract_function("_sanitizeOutgoingEmailBody", _DOCUMENT_JS)
    to_text = _extract_function("_emailHtmlToPlainText", _DOCUMENT_JS)
    assert "probe.innerHTML = text" not in sanitize
    assert "_emailHtmlToPlainText(text).trim()" in sanitize
    script = (
        _EMAIL_TEXT_DOM
        + "const _decodeBase64EmailWrapper = () => null;\n"
        + "const _looksLikeWrappedEmailContent = () => false;\n"
        + to_text
        + "\n"
        + sanitize
        + r"""
console.log(JSON.stringify(_sanitizeOutgoingEmailBody(
  '<div>A</div><br><br><br><div>B</div><script>steal()</script>'
)));
"""
    )
    out = json.loads(_run_node(script))
    # the converted plain text is kept (newline runs collapsed by the
    # recursion), and the script payload never survives the extraction
    assert "A\n\nB" in out
    assert 'steal()' not in out
    assert '<div' not in out


def test_email_inbox_reply_uses_the_shared_template_extraction():
    src = (_REPO / "static" / "js" / "emailInbox.js").read_text(encoding="utf-8")
    assert "_docModule.emailHtmlToPlainText(String(data.body_html))" in src
    assert "typeof _docModule.emailHtmlToPlainText === 'function'" in src
    # the flagged double-decode chain must be gone: no entity re-decode pass
    # remains after the DOM extraction
    assert '.replace(/&amp;/g, \'&\')' not in src
    assert ".replace(/&lt;/g, '<')" not in src
