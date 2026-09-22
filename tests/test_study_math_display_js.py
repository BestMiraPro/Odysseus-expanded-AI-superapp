"""Question text must reach the reader typeset, on every Study surface.

Study renders question text through ``_md``/``_mdInline`` (markdown + KaTeX) on
the practice card, but several list views printed it through ``esc()`` instead,
so a formula arrived as its source: ``\\(q= 3\\sqrt{l}\\)`` rather than q = 3√l.
The question list had a quieter variant: its rendered-HTML cache kept rows made
before KaTeX loaded, and a later re-render put those untypeset placeholders
back with nothing left to typeset them.

The stub ``_mdInline`` wraps its output in ``<md>``, so each test can tell text
that went through the renderer from text that was only escaped.
"""

from __future__ import annotations

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

RENDER_STUBS = r"""
function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function _mdInline(s) { return '<md>' + esc(s) + '</md>'; }
"""

FORMULA = r"\(q= 3\sqrt{l}\)"


def test_question_list_typesets_on_every_render_including_cache_hits():
    out = run_js(RENDER_STUBS + r"""
const S = { subject: { cards: [], qFilter: '', qMaterial: '', qLimit: 40, questions: [
  { id: 'q1', qtype: 'open', question: String.raw`\(q= 3\sqrt{l}\)`, topic: '',
    difficulty: 'easy', state: 'new', suspended: false, material_id: 'm1' },
] } };
const wrap = { innerHTML: '', onclick: null };
function body() { return { querySelector: (sel) => (sel === '#study-q-list' ? wrap : null) }; }
const typeset = [];
function _enrichRendered(el) { typeset.push(el === wrap ? 'list' : 'elsewhere'); }
function fmtDue() { return ''; }
function _originalQuestionButton() { return ''; }
const _qhtmlCache = new Map();
""", "_qhtml", "renderQuestionList", epilogue="""
renderQuestionList();   // first render fills the cache
renderQuestionList();   // re-render (search, suspend, show more) is a cache hit
console.log(JSON.stringify({ typeset, html: wrap.innerHTML }));
""")

    assert out["typeset"] == ["list", "list"]
    assert f"<md>{FORMULA}</md>" in out["html"]


def test_sure_but_wrong_list_renders_question_text():
    out = run_js(RENDER_STUBS + r"""
const S = { practice: { mock: null, deckId: 'd1', scope: null, startTs: Date.now(), log: [
  { q: { question: String.raw`\(q= 3\sqrt{l}\)`, topic: 'production' },
    result: { correct: false, score: 10 }, confidence: 85, hints: 0 },
] } };
let html = '';
const el = { set innerHTML(v) { html = v; }, get innerHTML() { return html; },
             querySelector: () => ({ addEventListener() {} }) };
function body() { return el; }
function stopMockTimer() {}
function startPractice() {}
function setTab() {}
""", "renderPracticeSummary", epilogue="""
renderPracticeSummary();
console.log(JSON.stringify({ html }));
""")

    assert "Calibration alarms" in out["html"]
    assert f"<md>{FORMULA}</md>" in out["html"]


def test_history_entries_render_question_answer_and_feedback():
    out = run_js(RENDER_STUBS, "renderHistoryEntry", epilogue=r"""
const question = renderHistoryEntry({
  kind: 'question', qtype: 'open', score: 40, when: '2026-09-22T10:00:00',
  title: String.raw`\(q= 3\sqrt{l}\)`,
  answer: String.raw`\(l = q^2/9\)`,
  feedback: String.raw`Square both sides: \(l = \frac{q^2}{9}\).`,
});
const card = renderHistoryEntry({
  kind: 'card', rating: 3, when: '2026-09-22T10:00:00', title: String.raw`\(\sqrt{2}\)`,
});
console.log(JSON.stringify({ question, card }));
""")

    assert f"<md>{FORMULA}</md>" in out["question"]
    assert r"<md>\(l = q^2/9\)</md>" in out["question"]
    assert r"<md>Square both sides: \(l = \frac{q^2}{9}\).</md>" in out["question"]
    assert r"<md>\(\sqrt{2}\)</md>" in out["card"]
