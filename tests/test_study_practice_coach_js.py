"""Ask AI mini panel: scroll regression, lifecycle, keyboard (Tasks 1 + 5).

The reported glitch: sending a message re-rendered the whole practice view, so
the ask thread's DOM node was recreated and its scroll position reset. The
panel tests drive the REAL createPracticeCoach
(static/js/studyPracticeCoach.js) against a scripted fetch/SSE stream; the
practice-view tests drive the REAL renderPractice/advancePractice
(static/js/study.js) with a minimal coach stub. Contracts pinned:

- sending appends to the SAME log node and never resets the reader mid-history,
- the practice view REATTACHES the preserved panel element instead of
  rebuilding it on a same-question re-render,
- advancing to another question retires the old instance (dispose), and the
  composer owns Enter on its own terms (IME, Shift+Enter).
"""

from __future__ import annotations

import json

from tests._study_js_harness import STUDY_JS, needs_node, run_js

COACH_JS = STUDY_JS.parent / "studyPracticeCoach.js"


def _tick(n: int) -> str:
    return "".join("await Promise.resolve();\n" for _ in range(n))


_coach_prelude = r"""
globalThis.window = { location: { origin: 'https://app.test' } };
globalThis.API = 'https://app.test';
globalThis.FOLLOW_PX = 48;

function makeNode(tag) {
  const n = {
    tagName: tag, children: [], _text: '', innerHTML: '', hidden: false,
    disabled: false, value: '', scrollTop: 0, clientHeight: 300, dataset: {},
    className: '', _classSet: new Set(), _h: 0,
    get textContent() { return n._text || n._html || ''; },
    set textContent(v) { n._text = v; },
    classList: {
      add(c) { n._classSet.add(c); },
      toggle(c, on) { if (on) n._classSet.add(c); else n._classSet.delete(c); },
    },
    listeners: {},
    addEventListener(ev, fn) { (n.listeners[ev] = n.listeners[ev] || []).push(fn); },
    dispatch(ev, extra) {
      for (const fn of (n.listeners[ev] || [])) {
        fn(Object.assign({
          target: n, currentTarget: n, preventDefault() {}, stopPropagation() {},
        }, extra || {}));
      }
    },
    appendChild(child) {
      n.children.push(child);
      child.parent = n;
      n._h = 500 + n.children.length * 70;
      if (n.onsize) n.onsize();
    },
    removeChild(child) { n.children = n.children.filter((c) => c !== child); },
    get firstChild() { return n.children[0] || null; },
    get scrollHeight() { return n._h; },
    set scrollHeight(v) { n._h = v; },
    focus() {},
  };
  return n;
}

const document = {
  createElement(tag) {
    const n = makeNode(tag);
    Object.defineProperty(n, 'innerHTML', {
      get() { return n._html || ''; },
      set(v) {
        n._html = v;
        n._bound = {};
        for (const m of v.matchAll(/study-coach-(log|status|jump|retry|history|input|send)/g)) {
          if (!n._bound[m[1]]) n._bound[m[1]] = makeNode('div');
        }
      },
    });
    n.querySelector = (sel) => {
      const part = sel.replace(/[#.]study-coach-/, '');
      return (n._bound || {})[part] || null;
    };
    return n;
  },
};

// Shared helpers used free inside createPracticeCoach.
const mdToHtml = (src) => String(src);
const enrichStudyMessage = () => {};
let posted = [];
let threadList = [];
let historyMessages = [];
// armable per-thread history responses: threads in armedThreads park their
// /messages fetch until resolveMessages(id) releases it (and then serve the
// CURRENT historyByThread[id] — the same contract out-of-order loads race on)
const messageWaits = {};
const armedThreads = new Set();
const failThreads = new Set();
let historyByThread = {};
function resolveMessages(id) {
  const q = messageWaits[id];
  if (!q || !q.length) return false;
  const resolve = q.shift();
  const msgs = historyByThread[id] !== undefined ? historyByThread[id] : historyMessages;
  resolve({ ok: true, json: async () => ({ messages: msgs }) });
  return true;
}
let streams = [];
let gateResolve = null;
const waitGate = () => new Promise((r) => { gateResolve = r; });
let blocked = false;           // when true, /ask never resolves until aborted
let abortCount = 0;
class Ctl extends AbortController {
  abort() { abortCount += 1; return super.abort(); }
}
AbortController = Ctl;

async function consumeStudyEvents(res, onEvent) {
  const seq = (res && res.body && res.body._events) || [];
  for (const ev of seq) {
    if (ev === '__gate__') await waitGate();
    else onEvent(ev);
  }
}

async function fetch(url, opts = {}) {
  const u = String(url);
  if (u.includes('/api/study/questions/') && u.includes('/ask')) {
    posted.push(opts.body);
    if (blocked) {
      return new Promise((_resolve, reject) => {
        opts.signal.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        });
      });
    }
    return { ok: true, body: { _events: streams.shift() || [] } };
  }
  if (u.includes('/messages')) {
    const id = decodeURIComponent((u.split('/threads/')[1] || 'x').split('/')[0]);
    const msgs = historyByThread[id] !== undefined ? historyByThread[id] : historyMessages;
    if (failThreads.has(id)) {
      return { ok: false, status: 500, json: async () => ({ detail: 'history boom' }) };
    }
    if (armedThreads.has(id)) {
      return new Promise((resolve) => {
        (messageWaits[id] = messageWaits[id] || []).push(resolve);
      });
    }
    return { ok: true, json: async () => ({ messages: msgs }) };
  }
  if (u.includes('/threads?')) {
    return { ok: true, json: async () => ({ threads: threadList }) };
  }
  return { ok: false, status: 500, json: async () => ({ detail: 'nope' }) };
}

const esc = (s) => String(s ?? '');
const toast = () => {};

// Real ResizeObserver semantics for the harness: callbacks can be FIRED
// manually for a target, neighbours may get unobserved, disposal disconnects.
const roInstances = [];
class FakeResizeObserver {
  constructor(cb) { this.cb = cb; this._targets = new Set(); roInstances.push(this); }
  observe(t) { this._targets.add(t); }
  unobserve(t) { this._targets.delete(t); }
  disconnect() { this._targets.clear(); this._dead = true; }
  fire(target) { if (this._targets.has(target)) this.cb([], this); }
}
globalThis.ResizeObserver = FakeResizeObserver;
"""


def _coach_run(epilogue: str) -> dict:
    return run_js(_coach_prelude, "createPracticeCoach", epilogue=epilogue,
                  source_path=COACH_JS)


_OPEN_HISTORY = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
await Promise.resolve();

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const select = coach.element.querySelector('.study-coach-history');

historyMessages = [];
for (let i = 0; i < 40; i++) {
  historyMessages.push({ role: 'user', content: 'old turn ' + i });
  historyMessages.push({ role: 'assistant', content: 'old reply ' + i });
}
threadList = [{ id: 't-history', title: 'History', question_id: 'q-1' }];
select.value = 't-history';
select.dispatch('change');
AWAIT

const logNodeBefore = log;
const childrenBefore = log.children.length;

// Send while the reader sits partway up the transcript.
log.scrollTop = 120;
log.dispatch('scroll');
streams.push([
  { type: 'thread', thread_id: 't-history' },
  { type: 'status', message: 'Reading materials' },
  { type: 'reply', content: 'the approved reply' },
]);
input.value = 'follow-up question';
send.dispatch('click');
AWAIT

console.log(JSON.stringify({
  sameNode: log === logNodeBefore,
  appended: log.children.length === childrenBefore + 2,   // user + assistant
  scrolledToBottom: log.scrollTop === log.scrollHeight,
  scrollNotZero: log.scrollTop > 0,
  postedSawThread: posted.length && posted[posted.length - 1].includes('t-history'),
}));
"""

_DELAYED_REPLY_WHILE_READING = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
await Promise.resolve();

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const jump = coach.element.querySelector('.study-coach-jump');
const status = coach.element.querySelector('.study-coach-status');

// Enough backlog for a real scroll.
for (let i = 0; i < 10; i++) {
  // direct children seeding is unavailable inside the closure; use history
  historyMessages = historyMessages.concat(
    [{ role: 'user', content: 'u' + i }, { role: 'assistant', content: 'a' + i }]);
}
threadList = [{ id: 't-1', title: 'One', question_id: 'q-1' }];
const select = coach.element.querySelector('.study-coach-history');
select.value = 't-1';
select.dispatch('change');
AWAIT

streams.push([
  { type: 'status', message: 'Reading materials' },
  '__gate__',
  { type: 'reply', content: 'the approved reply' },
]);
input.value = 'new question';
send.dispatch('click');
AWAIT
const busyWhilePending = send.textContent === 'Stop';

// The reader scrolls up while the reply is still being checked.
log.scrollTop = 120;
log.dispatch('scroll');
const posWhileReading = log.scrollTop;

gateResolve();
AWAIT
const posAfterReply = log.scrollTop;
const jumpVisibleAfter = jump.hidden === false;

jump.dispatch('click');
console.log(JSON.stringify({
  busyWhilePending,
  posWhileReading,
  posNotYanked: posAfterReply === posWhileReading,
  jumpShown: jumpVisibleAfter,
  jumpScrollsDown: log.scrollTop === log.scrollHeight,
  jumpHidesAgain: jump.hidden === true,
}));
"""

_KEYBOARD = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
await Promise.resolve();

const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
streams.push([{ type: 'reply', content: 'one' }]);

// shift+enter: a newline, no send
input.value = 'paragraph';
input.dispatch('keydown', { key: 'Enter', shiftKey: true, isComposing: false });
AWAIT
const afterShift = posted.length;
// mid-IME composition: not a command
input.value = 'composing';
input.dispatch('keydown', { key: 'Enter', shiftKey: false, isComposing: true, keyCode: 229 });
AWAIT
const afterIme = posted.length;
// a plain Enter sends
input.value = 'hello';
input.dispatch('keydown', { key: 'Enter', shiftKey: false, isComposing: false });
AWAIT
console.log(JSON.stringify({
  shiftEnterNoSend: afterShift === 0,
  imeNoSend: afterIme === 0,
  plainEnterSends: posted.length === 1,
  composerClearedAfterSend: input.value === '',
}));
"""

_STOP_PRESERVES_AND_BLOCKS = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
await Promise.resolve();

const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');

blocked = true;
input.value = 'slow question';
send.dispatch('click');
AWAIT
const busyLabel = send.textContent;
// stop aborts the request
send.dispatch('click');
AWAIT
console.log(JSON.stringify({
  busyLabel,
  stopped: abortCount >= 1,
  backToSend: send.textContent === 'Send',
}));
"""


@needs_node
def test_send_keeps_the_same_log_node_and_submits_the_bound_thread():
    result = _coach_run(
        _OPEN_HISTORY.replace("AWAIT", _tick(6)))
    assert result["sameNode"], "the send replaced the visible log node"
    assert result["appended"], "user and approved reply were not appended"
    assert result["scrolledToBottom"], "sending did not bring its message into view"
    assert result["scrollNotZero"], "scroll position reset to zero (the original bug)"
    assert result["postedSawThread"], "the bound thread id never reached the request"


@needs_node
def test_delayed_reply_does_not_yank_a_reader_and_jump_works():
    result = _coach_run(
        _DELAYED_REPLY_WHILE_READING.replace("AWAIT", _tick(6)))
    assert result["busyWhilePending"], "no busy state while the reply was pending"
    assert result["posNotYanked"], (
        "the arriving reply reset the reader's scroll position mid-history"
    )
    assert result["jumpShown"], "no Jump-to-latest while content sat below"
    assert result["jumpScrollsDown"], "Jump to latest did not scroll down"
    assert result["jumpHidesAgain"], "follow resumed but the jump button stayed"


@needs_node
def test_composer_keyboard_owns_enter_and_ime():
    result = _coach_run(_KEYBOARD.replace("AWAIT", _tick(6)))
    assert result["shiftEnterNoSend"], "Shift+Enter fired a send"
    assert result["imeNoSend"], "an IME composition Enter fired a send"
    assert result["plainEnterSends"], "plain Enter did not send"
    assert result["composerClearedAfterSend"], (
        "composer text was not cleared after the send captured it"
    )


@needs_node
def test_stop_aborts_the_active_request():
    result = _coach_run(_STOP_PRESERVES_AND_BLOCKS.replace("AWAIT", _tick(6)))
    assert result["busyLabel"] == "Stop", "no Stop affordance while generating"
    assert result["stopped"], "Stop did not abort the in-flight request"
    assert result["backToSend"], "the button did not return to Send after stopping"


# ---------------------------------------------------------------------------
# review finding 2: stale continuations die when the selection changes
# ---------------------------------------------------------------------------

_SWITCH_RACE = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const select = coach.element.querySelector('.study-coach-history');
const shownTexts = () => log.children.map((c) => c.textContent);

historyByThread = {
  A: [{ role: 'user', content: 'A old message' }],
  B: [{ role: 'user', content: 'B old message' }],
};
threadList = [
  { id: 'A', title: 'A', question_id: 'q-1' },
  { id: 'B', title: 'B', question_id: 'q-1' },
];
armedThreads.add('A'); armedThreads.add('B');

select.value = 'A';
select.dispatch('change');
AWAIT
select.value = 'B';
select.dispatch('change');
AWAIT
resolveMessages('B');   // B's history arrives first...
AWAIT
resolveMessages('A');   // ...the stale A response arrives later
AWAIT
const displayed = shownTexts();

// the next send must target the DISPLAYED conversation
streams.push([{ type: 'reply', content: 'reply in B' }]);
input.value = 'follow up on displayed conversation';
send.dispatch('click');
AWAIT
console.log(JSON.stringify({
  displayed: displayed,
  afterSend: shownTexts(),
  postedThread: JSON.parse(posted[posted.length - 1]).thread_id,
  replyShown: shownTexts().some((t) => t.includes('reply in B')),
}));
"""

_SWITCH_TO_NEW = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT

const log = coach.element.querySelector('.study-coach-log');
const select = coach.element.querySelector('.study-coach-history');

historyByThread = { A: [{ role: 'user', content: 'A old message' }] };
threadList = [{ id: 'A', title: 'A', question_id: 'q-1' }];
armedThreads.add('A');
select.value = 'A';
select.dispatch('change');
AWAIT
select.value = '';       // switch to "New conversation." while A pends
select.dispatch('change');
AWAIT
const cleared = log.children.length === 0;
resolveMessages('A');    // late A response
AWAIT
console.log(JSON.stringify({
  cleared,
  stayedEmpty: log.children.length === 0,
}));
"""

_SEND_CROSSES_SWITCH = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT

const log = coach.element.querySelector('.study-coach-log');
const select = coach.element.querySelector('.study-coach-history');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');

historyByThread = { B: [{ role: 'user', content: 'B old message' }] };
threadList = [{ id: 'B', title: 'B', question_id: 'q-1' }];
select.value = 'B';
select.dispatch('change');
AWAIT

// start a send on B, gate its reply, then change the conversation
streams.push([
  { type: 'status', message: 'Reading materials' },
  '__gate__',
  { type: 'reply', content: 'late reply from the abandoned send' },
]);
input.value = 'note typed on the old send';
send.dispatch('click');
AWAIT
select.value = '';   // switch away while the send is still pending
select.dispatch('change');
AWAIT
gateResolve();
AWAIT
console.log(JSON.stringify({
  clearedToNew: log.children.length === 0,
  noLateReply: !log.children.some((c) => c.textContent.includes('late reply')),
  backToSend: send.textContent === 'Send',
}));
"""


@needs_node
def test_stale_history_and_send_rejected_after_selection_change():
    result = _coach_run(_SWITCH_RACE.replace("AWAIT", _tick(6)))
    assert result.get("afterSend"), result
    assert result["afterSend"][0] == "B old message", (
        "displayed history must match the selected conversation")
    assert all(t != "A old message" for t in result["afterSend"]), (
        "a late stale response replaced the displayed conversation")
    assert "follow up on displayed conversation" in result["afterSend"], (
        "the new send must append to the preserved conversation view")
    assert result["replyShown"], "the approved reply never displayed"
    assert result["postedThread"] == "B", (
        "the next send must target the DISPLAYED conversation")


@needs_node
def test_switch_to_new_conversation_drops_pending_history():
    result = _coach_run(_SWITCH_TO_NEW.replace("AWAIT", _tick(6)))
    assert result["cleared"], "New conversation did not clear the log"
    assert result["stayedEmpty"], (
        "a late history response repopulated the new-conversation view")


# ---------------------------------------------------------------------------
# review finding 4: watch message content, not just the capped scroll box
# ---------------------------------------------------------------------------

_GROWTH_OBSERVED = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT
const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const jump = coach.element.querySelector('.study-coach-jump');
let ro = null;
AWAIT
streams.push([{ type: 'reply', content: 'the approved reply' }]);
input.value = 'a question that grows later';
send.dispatch('click');
AWAIT
ro = roInstances[roInstances.length - 1];
const studentNode = log.children[0];
const replyNode = log.children[1];
const logObserved = ro._targets.has(log);
const messageObserved = ro._targets.has(studentNode) && ro._targets.has(replyNode);

// A rendered message grows (late math/diagram/plot) while the reader is NOT
// following: position stays, Jump to latest appears.
log.scrollTop = 120;
log.dispatch('scroll');
const reading = log.scrollTop;
ro.fire(replyNode);
const kept = log.scrollTop === reading;
const jumpShown = jump.hidden === false;

// ...and while the reader IS following, growth scrolls them down again.
jump.dispatch('click');
log.scrollTop = log.scrollHeight;
ro.fire(replyNode);
const followed = log.scrollTop === log.scrollHeight;
const jumpGone = jump.hidden === true;

coach.dispose();
console.log(JSON.stringify({
  logObserved, messageObserved, kept, jumpShown, followed, jumpGone: jumpGone,
  disconnected: roInstances[roInstances.length - 1]._targets.size === 0,
}));
"""


@needs_node
def test_message_growth_is_observed_and_disposaldisconnects():
    result = _coach_run(_GROWTH_OBSERVED.replace("AWAIT", _tick(6)))
    assert result["logObserved"] and result["messageObserved"], (
        "message nodes must be observed for late math/diagram/plot growth")
    assert result["kept"], "growth yanked a reader who was not following"
    assert result["jumpShown"], "content growth below the fold did not show Jump"
    assert result["followed"], "growth not followed at the bottom"
    assert result["jumpGone"], "follow was resumed but Jump stayed visible"


@needs_node
def test_send_completing_across_a_selection_change_never_displays():
    result = _coach_run(_SEND_CROSSES_SWITCH.replace("AWAIT", _tick(6)))
    assert result["clearedToNew"]
    assert result["noLateReply"], (
        "a reply from the abandoned send reached the new conversation's view")
    assert result["backToSend"]


# ---------------------------------------------------------------------------
# review finding 2 follow-up: a pending history load must not wipe a newly
# sent exchange — sends block until the selected history resolves.
# ---------------------------------------------------------------------------

_SEND_BLOCKED_DURING_HISTORY = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const select = coach.element.querySelector('.study-coach-history');
const status = coach.element.querySelector('.study-coach-status');
const shownTexts = () => log.children.map((c) => c.textContent);

historyByThread = { H: [{ role: 'user', content: 'H old message' }] };
threadList = [{ id: 'H', title: 'H', question_id: 'q-1' }];
armedThreads.add('H');
select.value = 'H';
select.dispatch('change');
AWAIT
const loadingMarked = (status.textContent || '').includes('Loading');
const sendDisabledWhileLoading = send.disabled === true;
const clearedDuringLoad = log.children.length === 0;

// Attempt to send while the selected history is still pending: it must
// block without posting and without clearing the composer.
input.value = 'new question during load';
send.dispatch('click');
AWAIT
const postedDuringLoad = posted.length;
const draftPreserved = input.value === 'new question during load';
const logStillPending = log.children.length === 0;

resolveMessages('H');
AWAIT
const historyShown = shownTexts();
const unblocked = send.disabled === false;

// The next send targets the DISPLAYED conversation and both stay visible.
streams.push([{ type: 'reply', content: 'reply after history' }]);
input.value = 'follow up after load';
send.dispatch('click');
AWAIT
const afterSend = shownTexts();
console.log(JSON.stringify({
  loadingMarked, sendDisabledWhileLoading, clearedDuringLoad,
  postedDuringLoad, draftPreserved, logStillPending,
  historyShown, unblocked, afterSend,
  postedThread: posted.length ? JSON.parse(posted[posted.length - 1]).thread_id : null,
}));
"""

_FAILED_HISTORY_UNBLOCKS = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const select = coach.element.querySelector('.study-coach-history');
const shownTexts = () => log.children.map((c) => c.textContent);

threadList = [{ id: 'F', title: 'F', question_id: 'q-1' }];
failThreads.add('F');
select.value = 'F';
select.dispatch('change');
AWAIT
const unblockedAfterFail = send.disabled === false;
const errorShown = shownTexts().some((t) => t.includes('history boom'));

streams.push([{ type: 'reply', content: 'reply after failed history' }]);
input.value = 'question after failure';
send.dispatch('click');
AWAIT
console.log(JSON.stringify({
  unblockedAfterFail, errorShown,
  afterSend: shownTexts(),
  postedThread: posted.length ? JSON.parse(posted[posted.length - 1]).thread_id : null,
}));
"""

_SWITCH_TO_NEW_DURING_LOAD_UNBLOCKS = r"""
const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc, toast,
});
coach.update();
AWAIT

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const select = coach.element.querySelector('.study-coach-history');
const shownTexts = () => log.children.map((c) => c.textContent);

historyByThread = { H: [{ role: 'user', content: 'H old message' }] };
threadList = [{ id: 'H', title: 'H', question_id: 'q-1' }];
armedThreads.add('H');
select.value = 'H';
select.dispatch('change');
AWAIT
select.value = '';   // New conversation while H pends: clears + unblocks
select.dispatch('change');
AWAIT
const cleared = log.children.length === 0;
const unblocked = send.disabled === false;

streams.push([{ type: 'reply', content: 'reply in the new conversation' }]);
input.value = 'first question in the new conversation';
send.dispatch('click');
AWAIT
resolveMessages('H');   // late stale history must stay out
AWAIT
const afterSend = shownTexts();
const postedBody = posted.length ? JSON.parse(posted[posted.length - 1]) : {};
console.log(JSON.stringify({
  cleared, unblocked, afterSend,
  postedHasThread: ('thread_id' in postedBody),
}));
"""


@needs_node
def test_send_blocked_during_history_load_then_matches_post_thread():
    result = _coach_run(_SEND_BLOCKED_DURING_HISTORY.replace("AWAIT", _tick(6)))
    assert result["loadingMarked"], "pending history was not marked as loading"
    assert result["sendDisabledWhileLoading"], "send was not disabled while history loads"
    assert result["clearedDuringLoad"], "stale view lingered while history loads"
    assert result["postedDuringLoad"] == 0, "a send slipped out during history loading"
    assert result["draftPreserved"], "blocked send cleared the composer draft"
    assert result["logStillPending"], "blocked send mutated the pending view"
    assert result["historyShown"] == ["H old message"], result["historyShown"]
    assert result["unblocked"], "send stayed disabled after history resolved"
    assert "H old message" in result["afterSend"], "history missing after the next send"
    assert "follow up after load" in result["afterSend"], "new exchange wiped by history"
    assert "reply after history" in result["afterSend"], "approved reply missing"
    assert result["postedThread"] == "H", (
        "the next POST must target the DISPLAYED conversation")


@needs_node
def test_failed_history_load_unblocks_and_matches_post_thread():
    result = _coach_run(_FAILED_HISTORY_UNBLOCKS.replace("AWAIT", _tick(6)))
    assert result["errorShown"], "failed history load showed no error"
    assert result["unblockedAfterFail"], "send stayed disabled after a failed load"
    assert "question after failure" in result["afterSend"]
    assert "reply after failed history" in result["afterSend"]
    assert result["postedThread"] == "F", (
        "after a failed load the next POST must still target the selected thread")


@needs_node
def test_switch_to_new_during_history_load_unblocks():
    result = _coach_run(_SWITCH_TO_NEW_DURING_LOAD_UNBLOCKS.replace("AWAIT", _tick(6)))
    assert result["cleared"], "New conversation did not clear the pending view"
    assert result["unblocked"], "send stayed disabled after switching to New"
    assert "first question in the new conversation" in result["afterSend"]
    assert "reply in the new conversation" in result["afterSend"]
    assert all(t != "H old message" for t in result["afterSend"]), (
        "stale history repopulated the new conversation")
    assert result["postedHasThread"] is False, (
        "a New-conversation send must not carry the abandoned thread")


# ---------------------------------------------------------------------------
# practice view: reattachment + retirement (real renderPractice/advancePractice)
# ---------------------------------------------------------------------------

_practice_prelude = r"""
let reattached = [];
let disposed = [];
let created = 0;
let coachElements = [];
function createPracticeCoach() {
  created += 1;
  const focuser = {
    selectionStart: 4, selectionEnd: 9, selectionCalls: [],
    value: '',
    focus(opts) { this.focusCalls = this.focusCalls || []; this.focusCalls.push(opts); globalThis.document.activeElement = this; },
    setSelectionRange(a, b) { this.selectionCalls.push([a, b]); this.selectionStart = a; this.selectionEnd = b; },
  };
  const coachLog = { scrollTop: 0, scrollHeight: 2000, clientHeight: 300 };
  const element = {
    id: 'coach-node-' + created, coach: true,
    contains(n) { return n === focuser; },
    _focuser: focuser,
    _log: coachLog,
    querySelector(sel) { return sel === '.study-coach-log' ? coachLog : null; },
  };
  coachElements.push(element);
  return {
    element,
    update() {},
    pause() {},
    dispose() { disposed.push(element.id); },
  };
}
globalThis.document = { activeElement: null, body: { tagName: 'BODY' } };
// study.js calls the REAL helpers from studyPracticeCoach.js; mirror their
// contract here (the coach module's own exports are unit-covered elsewhere).
function captureCoachFocus(element) {
  const ae = document.activeElement;
  if (ae && typeof ae.focus === 'function'
      && element && element.contains(ae)) {
    return { el: ae, start: ae.selectionStart ?? null,
             end: ae.selectionEnd ?? null };
  }
  return null;
}
function restoreCoachFocus(state) {
  if (!state) return;
  state.el.focus({ preventScroll: true });
  if (state.start != null && typeof state.el.setSelectionRange === 'function') {
    state.el.setSelectionRange(state.start, state.end);
  }
}
let renderCount = 0;
const gens = new Map();
function makeNode(sel) {
  return {
    sel, value: '', disabled: false, textContent: '', scrollTop: 0,
    scrollHeight: 2000, clientHeight: 300, dataset: {},
    listeners: {},
    addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); },
    click() { for (const fn of (this.listeners.click || [])) fn({ currentTarget: this, target: this }); },
    replaceWith(node) { reattached.push(node.id); },
  };
}
function nodeFor(sel) {
  let m = gens.get(renderCount);
  if (!m) { m = new Map(); gens.set(renderCount, m); }
  if (!m.has(sel)) m.set(sel, makeNode(sel));
  return m.get(sel);
}
const el = {
  get innerHTML() { return ''; },
  set innerHTML(v) {
    renderCount += 1; gens.set(renderCount, new Map());
    // Replacing the practice DOM disconnects the focused composer in a real
    // browser, moving focus to the body and resetting the detached log's
    // scrollTop to 0. Mirror both so a capture running after replacement
    // sees the body instead of the detached composer.
    const ae = globalThis.document && globalThis.document.activeElement;
    if (ae) {
      let inside = false;
      for (const ce of coachElements) {
        try { if (ce.contains(ae)) { inside = true; break; } } catch {}
      }
      if (inside) globalThis.document.activeElement = globalThis.document.body;
    }
    for (const ce of coachElements) {
      try { if (ce._log) ce._log.scrollTop = 0; } catch {}
    }
  },
  querySelector(sel) { return nodeFor(sel); },
  querySelectorAll() { return []; },
};
function body() { return el; }
const esc = (s) => String(s);
const _md = (s) => String(s);
const _mdInline = (s) => String(s);
function _prereqsToShow() { return []; }
function _originalQuestionButton() { return ''; }
function _enrichRendered() {}
function toast() {}
function consultAction() {}
function openExplainFurther() {}
function openAgent() {}
function exitPractice() {}
function armThen(btn, fn) { fn(); }
function makeIdempotencyKey() { return 'key'; }
async function postDurably() { return {}; }
async function jpost() { return {}; }
function stopMockTimer() {}
function practiceCoachContext() { return {}; }
function renderMockPrediction() {}
function renderPracticeSummary() {}
const q1 = { id: 'q-1', qtype: 'text', question: 'A practice question', deck_id: 'd-1' };
const S = {
  practice: {
    queue: [q1], idx: 0, deckId: 'd-1', answerDraft: '', hints: [], result: null,
    consulted: false, consult: null, consultBusy: false,
    prereqs: null, prereqsFor: null, prereqsBusy: false,
    hintBusy: false, explainBusy: false, confidence: null, choice: null,
    submissionId: null, coach: null,
  },
};
"""


def _practice_run(epilogue: str) -> dict:
    return run_js(
        _practice_prelude, "renderPractice", "advancePractice",
        epilogue=epilogue, source_path=STUDY_JS)


_ATTACH = r"""
await renderPractice();
await renderPractice();   // same-question re-render (hint/reveal/re-engage)
console.log(JSON.stringify({
  created,
  reattached,
  coachQuestion: S.practice.coach && S.practice.coach.questionId,
}));
"""

_ADVANCE = r"""
await renderPractice();
S.practice.queue.push({ id: 'q-2', qtype: 'text', question: 'Another', deck_id: 'd-1' });
advancePractice();
console.log(JSON.stringify({
  disposed,
  created,
  coachQuestion: S.practice.coach && S.practice.coach.questionId,
}));
"""


@needs_node
def test_same_question_redraw_reattaches_the_same_panel():
    result = _practice_run(_ATTACH)
    assert result["created"] == 1, (
        f"a same-question re-render created another panel ({result['created']})"
    )
    assert result["coachQuestion"] == "q-1"
    assert result["reattached"] == ["coach-node-1", "coach-node-1"], (
        "the preserved panel element was not reattached on both renders"
    )


@needs_node
def test_advancing_question_retires_the_old_panel():
    result = _practice_run(_ADVANCE)
    assert result["disposed"] == ["coach-node-1"], (
        "advancing must dispose the old panel so its stale responses cannot "
        "reach the new question"
    )
    assert result["created"] == 2, "the new question needs its own panel"
    assert result["coachQuestion"] == "q-2"


# ---------------------------------------------------------------------------
# review finding 5: composer focus and selection survive a same-question
# rerender
# ---------------------------------------------------------------------------

_FOCUS_RESTORE = r"""
await renderPractice();                     // mounts coach-node-1
const inst = S.practice.coach.inst;
// The learner is typing inside the composer with a selected range.
inst.element._focuser.value = 'half typed sentence';
inst.element._focuser.selectionStart = 2;
inst.element._focuser.selectionEnd = 7;
document.activeElement = inst.element._focuser;
inst.element._log.scrollTop = 120;
el.scrollTop = 250;
await renderPractice();                     // same-question re-render
const focuser = inst.element._focuser;
console.log(JSON.stringify({
  focusCalls: focuser.focusCalls || [],
  selectionCalls: focuser.selectionCalls,
  activeIsComposer: document.activeElement === focuser,
  selectionKept: focuser.selectionStart === 2 && focuser.selectionEnd === 7,
  draftKept: focuser.value === 'half typed sentence',
  innerScrollKept: inst.element._log.scrollTop === 120,
  scrollKept: el.scrollTop === 250,
}));
"""

_FOCUS_NOT_STOLEN = r"""
await renderPractice();
const elsewhere = { focus() {}, selectionStart: null, selectionEnd: null };
document.activeElement = elsewhere;
await renderPractice();
console.log(JSON.stringify({
  untouched: document.activeElement === elsewhere,
  elsewhereGotNoFocus: (elsewhere.focusCalls || []).length === 0,
}));
"""


@needs_node
def test_practice_rerender_restores_composer_focus_and_selection():
    result = _practice_run(_FOCUS_RESTORE)
    assert result["activeIsComposer"], (
        "focus inside the coach was not restored after the reattachment"
    )
    assert result["focusCalls"] == [{"preventScroll": True}], (
        f"focus restore must not scroll the page: {result['focusCalls']}"
    )
    assert result["selectionCalls"] == [[2, 7]], (
        "the selected range was lost across the rerender"
    )
    assert result["selectionKept"], "selection endpoints were not preserved"
    assert result["draftKept"], "composer draft was not preserved"
    assert result["innerScrollKept"], "inner coach log scroll was not preserved"
    assert result["scrollKept"], "outer practice scroll was not preserved"


@needs_node
def test_practice_rerender_does_not_steal_focus_from_elsewhere():
    result = _practice_run(_FOCUS_NOT_STOLEN)
    assert result["untouched"], "the reattachment stole focus"