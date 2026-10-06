"""Inline editors in the subject view: flashcards and bank questions.

The card Edit button looked its row up through an undeclared ``el`` and a
``.study-row`` class the card rows never had, so every click threw (swallowed
into a toast) and editing a card was impossible. The question bank offered
only suspend/delete although ``PUT /questions/{id}`` exists.

These run the real handlers from static/js/study.js under Node against a
stub DOM just rich enough for them: an element answers ``querySelector`` for
an attribute only if its current innerHTML carries that attribute, so a
handler that targets markup the row does not have finds nothing, as in a
browser.
"""

from __future__ import annotations

import json

from tests._study_js_harness import needs_node, run_js

FUNCS = (
    "esc", "renderCardList", "renderQuestionList", "_mountRowEditor",
    "_focusRowEditButton", "_openCardEditor", "_openQuestionEditor",
)

PRELUDE = r"""
const toasts = [];
const puts = [];
let focused = null;
function toast(msg, err) { toasts.push([String(msg), !!err]); }
function fmtDue() { return 'soon'; }
function _qhtml(q) { return esc(q.question); }
function _originalQuestionButton() { return ''; }
function _enrichRendered() {}
function _updateQuestionCounts() {}
function _disarmDel() {}
function armThen() {}
async function jdel() {}
async function reloadSubject() { throw new Error('editors must not reload the subject'); }
async function jput(path, body) {
  puts.push([path, body]);
  return { ...body, _from_server: true };
}

class El {
  constructor(tag = 'div') {
    this.tag = tag; this.attrs = {}; this.dataset = {}; this.listeners = {};
    this.kids = {}; this._html = ''; this.value = ''; this.disabled = false;
    this.replacedWith = null; this.onclick = null;
  }
  set innerHTML(h) { this._html = String(h); this.kids = {}; }
  get innerHTML() { return this._html; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  async fire(t, ev = {}) { for (const fn of this.listeners[t] || []) await fn(ev); }
  focus() { focused = this; }
  replaceWith(n) { this.replacedWith = n; }
  _has(sel) {
    let m = sel.match(/^#([\w-]+)$/);
    if (m) return this._html.includes(`id="${m[1]}"`);
    m = sel.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
    if (!m) return false;
    if (m[2] !== undefined) return this._html.includes(`${m[1]}="${m[2]}"`);
    return new RegExp(`\\s${m[1]}(?=[\\s=>])`).test(this._html);
  }
  querySelector(sel) {
    if (this.kids[sel]) return this.kids[sel];
    return this._has(sel) ? (this.kids[sel] = new El('el')) : null;
  }
  querySelectorAll(sel) {
    const m = sel.match(/^\[([\w-]+)\]$/);
    if (!m) return [];
    return [...this._html.matchAll(new RegExp(`${m[1]}="([^"]*)"`, 'g'))].map(x => {
      const e = new El('button'); e.attrs[m[1]] = x[1]; return e;
    });
  }
}
const document = { createElement: (t) => new El(t) };
const lists = { '#study-card-list': new El(), '#study-q-list': new El() };
const root = { querySelector: (sel) => lists[sel] || null };
function body() { return root; }

// A button inside one rendered row, as the browser would hand it to the
// delegated click handler: closest() walks up to the row it sits in.
function rowButton(attr, id, rowClass = 'study-cardrow') {
  const row = new El('div');
  const key = attr.replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase());
  const btn = {
    dataset: { [key]: id },
    closest(sel) {
      if (sel === `[${attr}]`) return btn;
      if (sel === `.${rowClass}`) return row;
      return null;
    },
  };
  return { btn, row };
}
function target(dataset = {}, extra = {}) {
  const t = { dataset, ...extra };
  t.closest = (sel) => {
    const m = sel.match(/^\[data-([\w-]+)\]$/);
    if (!m) return null;
    const key = m[1].replace(/-(\w)/g, (_, c) => c.toUpperCase());
    return key in dataset ? t : null;
  };
  return t;
}
"""


def _run(epilogue: str) -> dict:
    return run_js(PRELUDE, *FUNCS, epilogue=epilogue)


CARD_SETUP = """
const S = { subject: { cards: [
  { id: 'c1', front: 'Front one', back: 'Back one', state: 'new', lapses: 0 },
  { id: 'c2', front: 'Front two', back: 'Back two', state: 'new', lapses: 0 },
] } };
renderCardList();
const wrap = lists['#study-card-list'];
const { btn, row } = rowButton('data-edit', 'c1');
await wrap.onclick({ target: btn });
const form = row.replacedWith;
"""


@needs_node
class TestCardEditor:
    def test_edit_opens_an_inline_form_in_place_of_the_row(self):
        out = _run(CARD_SETUP + """
console.log(JSON.stringify({
  toasts, opened: !!form,
  label: form && form.getAttribute('aria-label'),
  front: form && form.querySelector('[data-ed-front]').value,
  back: form && form.querySelector('[data-ed-back]').value,
  focusedFront: focused === (form && form.querySelector('[data-ed-front]')),
}));
""")
        assert out["toasts"] == [], "the edit click threw instead of opening the form"
        assert out["opened"], "the row was never replaced by the editor"
        assert out["label"] == "Edit card"
        assert (out["front"], out["back"]) == ("Front one", "Back one")
        assert out["focusedFront"], "focus did not move into the editor"

    def test_save_writes_the_card_and_restores_the_row(self):
        out = _run(CARD_SETUP + """
form.querySelector('[data-ed-front]').value = '  New front\\nsecond line  ';
await form.querySelector('[data-ed-save]').fire('click');
console.log(JSON.stringify({
  toasts, puts, card: S.subject.cards[0],
  listShowsIt: wrap.innerHTML.includes('New front'),
  focusedEdit: focused && focused.getAttribute('data-edit'),
}));
""")
        assert out["toasts"] == []
        assert out["puts"] == [["/api/study/cards/c1",
                                {"front": "New front\nsecond line", "back": "Back one"}]]
        assert out["card"]["front"] == "New front\nsecond line", "a multi-line front was flattened"
        assert out["listShowsIt"], "the list was not re-rendered with the saved text"
        assert out["focusedEdit"] == "c1", "focus was not returned to the row's edit button"

    def test_escape_cancels_without_saving_and_keeps_study_open(self):
        out = _run(CARD_SETUP + """
let stopped = false, prevented = false;
await form.fire('keydown', { key: 'Escape', preventDefault() { prevented = true; },
                             stopPropagation() { stopped = true; } });
console.log(JSON.stringify({ puts, stopped, prevented,
  restored: wrap.innerHTML.includes('Front one'),
  focusedEdit: focused && focused.getAttribute('data-edit') }));
""")
        assert out["puts"] == []
        assert out["stopped"], "Escape would bubble up and close the Study pane"
        assert out["prevented"]
        assert out["restored"]
        assert out["focusedEdit"] == "c1"

    def test_an_empty_side_is_refused_client_side(self):
        out = _run(CARD_SETUP + """
form.querySelector('[data-ed-back]').value = '   ';
await form.querySelector('[data-ed-save]').fire('click');
console.log(JSON.stringify({ puts, toasts }));
""")
        assert out["puts"] == []
        assert out["toasts"] and out["toasts"][0][1] is True


def _question_setup(question: dict) -> str:
    return f"""
const S = {{ subject: {{ qFilter: '', qMaterial: '', qLimit: 40,
  cards: [], questions: [{json.dumps(question)}] }} }};
renderQuestionList();
const wrap = lists['#study-q-list'];
const {{ btn, row }} = rowButton('data-qedit', '{question["id"]}');
await wrap.onclick({{ target: btn }});
const form = row.replacedWith;
const save = () => form.querySelector('[data-ed-save]').fire('click');
"""


MCQ = {"id": "q1", "qtype": "mcq", "question": "Pick one", "options": ["a", "b", "c"],
       "correct_index": 2, "reference": "", "topic": "", "difficulty": "easy",
       "state": "new", "suspended": False, "lapses": 0}
OPEN = {"id": "q2", "qtype": "open", "question": "Define demand.",
        "options": None, "correct_index": None, "reference": "Willingness to pay.",
        "topic": "", "difficulty": "easy", "state": "new", "suspended": False, "lapses": 0}


@needs_node
class TestQuestionEditor:
    def test_rows_offer_an_edit_button(self):
        out = _run(_question_setup(MCQ) + """
console.log(JSON.stringify({ toasts, opened: !!form,
  label: form && form.getAttribute('aria-label'),
  question: form && form.querySelector('[data-qe-question]').value,
  hasOptions: !!(form && form.querySelector('[data-qe-opts]')),
  optionsHtml: form && form.querySelector('[data-qe-opts]').innerHTML }));
""")
        assert out["toasts"] == []
        assert out["opened"] and out["label"] == "Edit question"
        assert out["question"] == "Pick one"
        assert out["hasOptions"]
        # Every option is labelled and the stored answer is pre-selected.
        assert 'aria-label="Option 3 is correct"' in out["optionsHtml"]
        assert 'data-qe-correct="2"\n          checked' in out["optionsHtml"]

    def test_unchanged_save_sends_nothing(self):
        out = _run(_question_setup(MCQ) + """
await save();
console.log(JSON.stringify({ puts, toasts, closed: wrap.innerHTML.includes('data-qedit="q1"') }));
""")
        assert out["puts"] == [] and out["toasts"] == []
        assert out["closed"]

    def test_changing_only_the_answer_sends_only_correct_index(self):
        out = _run(_question_setup(MCQ) + """
await form.fire('change', { target: target({ qeCorrect: '0' }, { checked: true }) });
await save();
console.log(JSON.stringify({ puts }));
""")
        assert out["puts"] == [["/api/study/questions/q1", {"correct_index": 0}]]

    def test_removing_an_option_renumbers_the_correct_one(self):
        out = _run(_question_setup(MCQ) + """
await form.fire('click', { target: target({ qeOptdel: '0' }) });
await save();
console.log(JSON.stringify({ puts, card: S.subject.questions[0] }));
""")
        assert out["puts"] == [["/api/study/questions/q1",
                                {"options": ["b", "c"], "correct_index": 1}]]
        assert out["card"]["_from_server"], "local state was not updated from the reply"

    def test_a_blank_option_is_dropped_before_numbering(self):
        out = _run(_question_setup(MCQ) + """
await form.fire('input', { target: target({ qeOpt: '1' }, { value: '   ' }) });
await save();
console.log(JSON.stringify({ puts }));
""")
        assert out["puts"] == [["/api/study/questions/q1",
                                {"options": ["a", "c"], "correct_index": 1}]]

    def test_removing_the_correct_option_requires_a_new_choice(self):
        out = _run(_question_setup(MCQ) + """
await form.fire('click', { target: target({ qeOptdel: '2' }) });
await save();
console.log(JSON.stringify({ puts, toasts }));
""")
        assert out["puts"] == []
        assert out["toasts"] and "correct" in out["toasts"][0][0].lower()

    def test_open_question_edits_prompt_and_answer(self):
        out = _run(_question_setup(OPEN) + """
form.querySelector('[data-qe-question]').value = 'Define demand precisely.';
form.querySelector('[data-qe-reference]').value = ' Quantity wanted at each price. ';
await save();
console.log(JSON.stringify({ puts,
  hasOptions: !!form.querySelector('[data-qe-opts]') }));
""")
        assert out["hasOptions"] is False
        assert out["puts"] == [["/api/study/questions/q2", {
            "question": "Define demand precisely.",
            "reference": "Quantity wanted at each price."}]]

    def test_escape_cancels(self):
        out = _run(_question_setup(OPEN) + """
let stopped = false;
await form.fire('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() { stopped = true; } });
console.log(JSON.stringify({ puts, stopped,
  focusedEdit: focused && focused.getAttribute('data-qedit') }));
""")
        assert out["puts"] == [] and out["stopped"]
        assert out["focusedEdit"] == "q2"
