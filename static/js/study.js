/**
 * Study Module v2 — evidence-based studying built into Odysseus.
 *
 * Tabs:
 *   Today    — due counts, streak, today's plan blocks, focus minutes
 *   Subjects — materials (paste/upload) → AI question extraction + flashcards
 *   Cards    — FSRS spaced-repetition flashcard player (keyboard driven)
 *   Practice — question bank drilling: MCQ/open, confidence, hints, AI grading
 *   Plan     — exams with deterministic spaced/interleaved study plans
 *   Focus    — single-task focus timer with history
 *   Agent    — in-app AI agent: tutors from the materials, manages the bank,
 *              runs the AI pipelines, and (admins, opt-in) edits the app's code
 *
 * The science core: every question/card attempt is closed-book retrieval,
 * outcomes feed FSRS scheduling (spacing), new questions are served
 * interleaved across topics, and confidence-vs-outcome is tracked for
 * calibration. Scheduling lives server-side (src/fsrs.py, src/study_ai.py).
 */

import * as Modals from './modalManager.js';
import { mdToHtml } from './markdown.js';
import { renderAgentTab, setAgentPrefill, setAgentScope } from './studyAgent.js';

const API = window.location.origin;

let _open = false;
let _pane = null;
let _tab = 'today';
let _keyHandler = null;

const S = {
  overview: null,
  decks: [],
  subject: null,         // {deck, cards, materials, questions, proposals, qFilter}
  review: null,          // flashcard session
  practice: null,        // question session
  exams: [],
  examEditing: null,
  focus: null,           // active focus session {id, label, plannedMin, startTs, timerId}
  focusHistory: [],
};

// Every id here must have a renderer in setTab, or its button is a dead
// control: the tab variable changes, the old content stays, and the click
// looks ignored. tests/test_study_agent_tab_js.py pins that invariant.
const TABS = [
  ['today', 'Today'], ['subjects', 'Subjects'], ['review', 'Cards'],
  ['practice', 'Practice'], ['plan', 'Plan'], ['focus', 'Focus'],
  ['stats', 'Stats'], ['history', 'History'], ['agent', 'Tutor'],
];

const TIPS = [
  'Retrieval beats rereading: a failed recall attempt still strengthens memory more than another pass over the notes.',
  'If practice feels effortful, it is working — aim for ~70–85% success, not comfort.',
  'Interleaving topics feels less productive than blocking. It tests better. Trust the data, not the feeling.',
  'Consolidation happens during sleep. An hour of study traded against sleep usually nets negative.',
  'Recognition is not recall. "I know this" only counts after a closed-book attempt.',
  'Space it out: five 20-minute contacts across two weeks beat one 100-minute block, every time.',
  'Hints have a price: a hinted success schedules sooner than a clean one. Try 30 more seconds before asking.',
  'Calibration matters: being sure and wrong is the most valuable error you can find. Hunt those.',
];

// ---------------------------------------------------------------------------
// utils
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function jfetch(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `Request failed (${res.status})`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

const jget = (p) => jfetch(p);
const jpost = (p, body) => jfetch(p, { method: 'POST', body: JSON.stringify(body || {}) });
const jput = (p, body) => jfetch(p, { method: 'PUT', body: JSON.stringify(body || {}) });
const jdel = (p) => jfetch(p, { method: 'DELETE' });

const RETRY_QUEUE_KEY = 'study:durable-posts:v1';
let _retryTimer = null;
let _retryFlushing = false;

function loadRetryQueue() {
  try {
    const raw = localStorage.getItem(RETRY_QUEUE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveRetryQueue(queue) {
  try {
    if (queue.length) localStorage.setItem(RETRY_QUEUE_KEY, JSON.stringify(queue));
    else localStorage.removeItem(RETRY_QUEUE_KEY);
  } catch {
    /* best effort */
  }
}

function makeIdempotencyKey(prefix, entityId) {
  const nonce = crypto?.randomUUID ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  return `${prefix}:${String(entityId).slice(0, 64)}:${nonce}`.slice(0, 128);
}

function retryDelayMs(attempts) {
  return Math.min(60000, 1000 * (2 ** Math.min(attempts, 6)));
}

function enqueueDurablePost(kind, path, payload, keyPrefix, entityId) {
  const idempotencyKey = makeIdempotencyKey(keyPrefix, entityId);
  const item = {
    id: idempotencyKey,
    kind,
    path,
    payload: { ...(payload || {}), idempotency_key: idempotencyKey },
    attempts: 0,
    next_try: 0,
    created_at: Date.now(),
  };
  const queue = loadRetryQueue();
  queue.push(item);
  saveRetryQueue(queue);
  return item;
}

function removeRetryItem(id) {
  saveRetryQueue(loadRetryQueue().filter(item => item.id !== id));
}

function updateRetryItem(id, patch) {
  const queue = loadRetryQueue();
  const item = queue.find(x => x.id === id);
  if (!item) return;
  Object.assign(item, patch);
  saveRetryQueue(queue);
}

async function postDurably(kind, path, payload, keyPrefix, entityId) {
  const item = enqueueDurablePost(kind, path, payload, keyPrefix, entityId);
  try {
    const result = await jpost(item.path, item.payload);
    removeRetryItem(item.id);
    scheduleRetryFlush();
    return result;
  } catch (err) {
    const attempts = item.attempts + 1;
    updateRetryItem(item.id, {
      attempts,
      next_try: Date.now() + retryDelayMs(attempts),
      last_error: err.message,
    });
    scheduleRetryFlush();
    throw err;
  }
}

function scheduleRetryFlush(delay = 1500) {
  if (_retryTimer) return;
  _retryTimer = setTimeout(() => {
    _retryTimer = null;
    flushRetryQueue();
  }, delay);
}

async function flushRetryQueue() {
  if (_retryFlushing || navigator.onLine === false) return;
  _retryFlushing = true;
  try {
    const now = Date.now();
    const due = loadRetryQueue().filter(item => !item.next_try || item.next_try <= now);
    for (const item of due) {
      try {
        await jpost(item.path, item.payload);
        removeRetryItem(item.id);
      } catch (err) {
        const attempts = (item.attempts || 0) + 1;
        updateRetryItem(item.id, {
          attempts,
          next_try: Date.now() + retryDelayMs(attempts),
          last_error: err.message,
        });
      }
    }
  } finally {
    _retryFlushing = false;
  }
  if (loadRetryQueue().length) scheduleRetryFlush(5000);
}

window.addEventListener('online', () => scheduleRetryFlush(250));
scheduleRetryFlush(1000);

function toast(msg, isError = false) {
  const t = document.createElement('div');
  t.className = 'study-toast' + (isError ? ' study-toast-err' : '');
  t.textContent = msg;
  (_pane || document.body).appendChild(t);
  setTimeout(() => t.remove(), isError ? 5000 : 2600);
}

function fmtDue(iso) {
  if (!iso) return '—';
  const mins = Math.round((new Date(iso) - Date.now()) / 60000);
  if (mins <= 0) return 'now';
  if (mins < 60) return `${mins}m`;
  if (mins < 60 * 36) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// ---------------------------------------------------------------------------
// styles
// ---------------------------------------------------------------------------

function injectStyles() {
  if (document.getElementById('study-styles')) return;
  const st = document.createElement('style');
  st.id = 'study-styles';
  st.textContent = `
.study-pane { position: fixed; inset: 4vh 5vw; z-index: 160; display: flex; flex-direction: column;
  background: var(--panel, var(--bg)); color: var(--fg); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 18px 60px rgba(0,0,0,0.45); overflow: hidden; }
.study-backdrop { position: fixed; inset: 0; z-index: 159; background: rgba(0,0,0,0.35); }
.study-pane.hidden, .study-backdrop.hidden { display: none !important; }
.study-model-wrap { display: flex; gap: 4px; align-items: center; }
.study-model-status { font-size: 10.5px; opacity: 0.75; }
.study-model-status.error { color: var(--danger, #c0392b); opacity: 1; }
.study-model-status.ok { color: var(--ok, #2e7d32); }
.study-model-retry { margin-left: 4px; font-size: 10.5px; padding: 1px 6px; cursor: pointer;
  background: none; color: inherit; border: 1px solid currentColor; border-radius: 5px; }
.study-model-wrap select { max-width: 150px; font-size: 10.5px; padding: 3px 5px;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 6px; }
@media (max-width: 900px) { .study-model-wrap { display: none; } }
@media (max-width: 768px) { .study-pane { inset: 0; border-radius: 0; } }
.study-header { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); flex-shrink: 0; }
.study-title { font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 7px; }
.study-tabs { display: flex; gap: 2px; margin-left: 8px; flex-wrap: wrap; }
.study-tab { background: none; border: none; color: var(--fg); opacity: 0.62; cursor: pointer;
  font-size: 12px; padding: 6px 10px; border-radius: 6px; }
.study-tab:hover { opacity: 0.9; background: rgba(128,128,128,0.12); }
.study-tab.active { opacity: 1; background: rgba(128,128,128,0.18); font-weight: 600; }
.study-header-spacer { flex: 1; }
.study-x { background: none; border: none; color: var(--fg); opacity: 0.6; cursor: pointer;
  font-size: 15px; padding: 4px 8px; border-radius: 6px; }
.study-x:hover { opacity: 1; background: rgba(128,128,128,0.15); }
.study-body { flex: 1; overflow-y: auto; padding: 16px 18px 28px; }
.study-section-title { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
  opacity: 0.55; margin: 18px 0 8px; }
.study-chips { display: flex; gap: 10px; flex-wrap: wrap; }
.study-chip { border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px;
  min-width: 96px; }
.study-chip b { display: block; font-size: 20px; margin-bottom: 2px; }
.study-chip span { font-size: 11px; opacity: 0.6; }
.study-row { display: flex; align-items: center; gap: 10px; padding: 9px 10px;
  border: 1px solid var(--border); border-radius: 8px; margin-bottom: 6px; }
.study-row .grow { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.study-badge { font-size: 10.5px; padding: 2px 7px; border-radius: 99px;
  border: 1px solid var(--border); opacity: 0.85; white-space: nowrap; }
.study-badge.due { color: var(--red, #e05555); border-color: currentColor; }
.study-badge.new { color: var(--accent, #5b8abf); border-color: currentColor; }
.study-badge.q { color: var(--green, #4f9e60); border-color: currentColor; }
.study-btn { background: none; border: 1px solid var(--border); color: var(--fg);
  border-radius: 7px; padding: 6px 12px; cursor: pointer; font-size: 12px; }
a.study-btn { display: inline-flex; align-items: center; text-decoration: none; }
.study-btn:hover { background: rgba(128,128,128,0.12); }
.study-btn.primary { border-color: var(--accent, #5b8abf); color: var(--accent, #5b8abf); font-weight: 600; }
.study-btn.danger { color: var(--red, #e05555); }
.study-btn.armed { background: var(--red, #e05555); color: #fff; border-color: var(--red, #e05555); font-weight: 600; }
.study-block-row { display: flex; align-items: flex-start; gap: 8px; }
.study-block-row .study-block { flex: 1; }
.study-block-row .study-btn { margin-top: 3px; }
.study-btn:disabled { opacity: 0.4; cursor: default; }
.study-btn.small { padding: 3px 8px; font-size: 11px; }
.study-input, .study-select, .study-textarea { background: var(--bg); color: var(--fg);
  border: 1px solid var(--border); border-radius: 7px; padding: 7px 9px; font-size: 12.5px;
  font-family: inherit; }
.study-textarea { width: 100%; min-height: 70px; resize: vertical; box-sizing: border-box; }
.study-form-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.study-tip { font-size: 11.5px; opacity: 0.55; font-style: italic; margin-top: 22px;
  border-left: 2px solid var(--border); padding-left: 10px; }
.study-empty { opacity: 0.55; font-size: 12.5px; padding: 14px 4px; }
/* stats dashboard */
.study-dash-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 8px; }
@media (max-width: 720px) { .study-dash-grid { grid-template-columns: 1fr; } }
.study-card-box { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.study-card-box h4 { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
  opacity: 0.6; margin: 0 0 10px; font-weight: 600; }
.study-bar-row { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 11.5px; }
.study-bar-row .lbl { width: 70px; text-align: right; opacity: 0.7; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.study-bar-track { flex: 1; height: 14px; background: rgba(128,128,128,0.12); border-radius: 7px; overflow: hidden; }
.study-bar-fill { height: 100%; border-radius: 7px; background: var(--accent, #5b8abf); }
.study-bar-fill.good { background: var(--green, #4f9e60); }
.study-bar-fill.warn { background: var(--red, #e05555); }
.study-cal-row { display: flex; align-items: flex-end; gap: 4px; height: 80px; margin-top: 6px; }
.study-cal-col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; }
.study-cal-bar { width: 100%; border-radius: 3px 3px 0 0; background: var(--accent, #5b8abf); min-height: 2px; }
.study-cal-ideal { width: 100%; border-top: 1px dashed var(--border); position: relative; }
.study-cal-lbl { font-size: 9px; opacity: 0.5; }
.study-mini-row { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 0; }
.study-mini-row .grow { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.study-pct { font-size: 11px; font-weight: 600; min-width: 38px; text-align: right; }
.study-big { font-size: 22px; font-weight: 700; }
.study-subtle { font-size: 11px; opacity: 0.5; }
.study-toast { position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%);
  background: var(--panel, var(--bg)); color: var(--fg); border: 1px solid var(--border);
  border-radius: 8px; padding: 8px 16px; font-size: 12.5px; z-index: 20;
  box-shadow: 0 6px 24px rgba(0,0,0,0.35); }
.study-toast-err { border-color: var(--red, #e05555); color: var(--red, #e05555); }
/* card/question player */
.study-card-stage { max-width: 680px; margin: 3vh auto 0; text-align: center; }
.study-progress { height: 4px; background: rgba(128,128,128,0.18); border-radius: 99px;
  overflow: hidden; margin-bottom: 26px; }
.study-progress i { display: block; height: 100%; background: var(--accent, #5b8abf); }
.study-card-front { font-size: 18px; line-height: 1.5; white-space: pre-wrap; }
.study-card-back { font-size: 15.5px; line-height: 1.55; white-space: pre-wrap; margin-top: 18px;
  padding-top: 18px; border-top: 1px solid var(--border); }
.study-card-meta { font-size: 10.5px; opacity: 0.45; margin-top: 10px; }
.study-rate-row { display: flex; gap: 10px; justify-content: center; margin-top: 28px; flex-wrap: wrap; }
.study-rate { min-width: 86px; height: auto; padding: 10px 8px; border-radius: 9px; border: 1px solid var(--border);
  background: none; color: var(--fg); cursor: pointer; }
.study-rate:hover { background: rgba(128,128,128,0.12); }
.study-rate b { display: block; font-size: 12.5px; }
.study-rate span { font-size: 10.5px; opacity: 0.55; }
.study-rate[data-r="1"] b { color: var(--red, #e05555); }
.study-rate[data-r="4"] b { color: var(--green, #4f9e60); }
/* practice */
.study-q-wrap { max-width: 720px; margin: 0 auto; text-align: left; }
.study-opt { display: block; width: 100%; height: auto; box-sizing: border-box; text-align: left;
  margin-bottom: 8px; padding: 11px 14px;
  border: 1px solid var(--border); border-radius: 9px; background: none; color: var(--fg);
  cursor: pointer; font-size: 13.5px; line-height: 1.45; white-space: pre-wrap; }
.study-opt:hover { background: rgba(128,128,128,0.1); }
.study-opt.sel { border-color: var(--accent, #5b8abf); background: rgba(91,138,191,0.1); }
.study-opt.right { border-color: var(--green, #4f9e60); }
.study-opt.wrong { border-color: var(--red, #e05555); }
.study-conf { display: flex; flex-direction: column; gap: 8px; margin: 14px 0 4px; }
.study-conf .study-subtle { display: flex; gap: 6px; align-items: center; }
.study-conf-slider { width: 100%; height: 6px; cursor: pointer; }
.study-conf-quick { display: flex; gap: 6px; align-items: center; }
.study-conf-quick button { font-size: 11px; padding: 4px 10px; border-radius: 99px;
  border: 1px solid var(--border); background: none; color: var(--fg); cursor: pointer; opacity: 0.7; }
.study-conf-quick button.sel { opacity: 1; border-color: var(--accent, #5b8abf); color: var(--accent, #5b8abf); }
.study-hint { border-left: 2px solid var(--accent, #5b8abf); padding: 6px 10px; margin: 8px 0;
  font-size: 12.5px; opacity: 0.85; background: rgba(91,138,191,0.06); border-radius: 0 6px 6px 0; }
.study-ask { border: 1px solid var(--border); border-radius: 9px; padding: 8px 10px; margin-top: 14px;
  background: rgba(91,138,191,0.04); }
.study-ask-head { font-size: 11px; opacity: 0.7; margin-bottom: 6px; }
.study-ask-thread { display: flex; flex-direction: column; gap: 6px; max-height: 260px;
  overflow-y: auto; margin-bottom: 6px; }
.study-ask-msg { font-size: 12.5px; line-height: 1.5; padding: 5px 8px; border-radius: 7px; }
.study-ask-msg.student { background: rgba(128,128,128,0.10); align-self: flex-end; max-width: 85%; }
.study-ask-msg.ai { background: rgba(91,138,191,0.10); align-self: flex-start; max-width: 92%; }
.study-prereq { border: 1px dashed var(--border); border-radius: 9px; padding: 8px 12px;
  margin-bottom: 12px; background: rgba(128,128,128,0.05); }
.study-prereq-title { font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase;
  opacity: 0.55; margin-bottom: 6px; }
.study-prereq-item { font-size: 12.5px; padding: 6px 0; border-top: 1px solid var(--border); }
.study-prereq-item:first-of-type { border-top: none; }
.study-context { border-left: 3px solid var(--accent, #5b8abf); border-radius: 0 8px 8px 0;
  padding: 8px 12px; margin-bottom: 12px; background: rgba(91,138,191,0.07); font-size: 13.5px; }
.study-context-title { font-size: 10px; letter-spacing: 0.05em; text-transform: uppercase;
  opacity: 0.6; margin-bottom: 4px; }
.study-grade { border: 1px solid var(--border); border-radius: 9px; padding: 12px 14px; margin-top: 14px; }
.study-grade.correct { border-color: var(--green, #4f9e60); }
.study-grade.incorrect { border-color: var(--red, #e05555); }
.study-grade b.score { font-size: 18px; }
.study-qchip { font-size: 9.5px; letter-spacing: 0.04em; text-transform: uppercase;
  padding: 1px 6px; border-radius: 4px; border: 1px solid var(--border); opacity: 0.75;
  white-space: nowrap; }
.study-qchip.mcq { color: var(--accent, #5b8abf); }
.study-qchip.open { color: var(--green, #4f9e60); }
.study-qchip.hard { color: var(--red, #e05555); }
/* file viewer */
.study-viewer { position: fixed; inset: 4vh 5vw; z-index: 170; display: flex; flex-direction: column;
  background: var(--panel, var(--bg)); border: 1px solid var(--border); border-radius: 12px;
  overflow: hidden; box-shadow: 0 12px 48px rgba(0,0,0,0.5); }
.study-viewer-head { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); }
.study-viewer-head b { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.study-viewer iframe { flex: 1; width: 100%; border: 0; background: #fff; }
.study-viewer-empty { flex: 1; display: flex; align-items: center; justify-content: center;
  text-align: center; padding: 24px; opacity: 0.7; font-size: 13px; line-height: 1.6; }
.study-viewer-body { flex: 1; overflow: auto; padding: 18px 24px; font-size: 13.5px; line-height: 1.6; }
.study-viewer-body img { max-width: 100%; height: auto; border: 1px solid var(--border);
  border-radius: 8px; margin: 8px 0; background: #fff; }
.study-viewer-body h1 { font-size: 17px; margin: 4px 0 10px; }
.study-viewer-body h2 { font-size: 15px; margin: 18px 0 8px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
.study-viewer-body h3 { font-size: 13.5px; margin: 14px 0 6px; }
.study-viewer-body ul, .study-viewer-body ol { padding-left: 22px; }
.study-viewer-body em { opacity: 0.7; font-size: 12px; }
/* history */
.study-histrow { display: flex; gap: 10px; align-items: flex-start; padding: 8px 4px 8px 8px;
  border-bottom: 1px solid var(--border); font-size: 12.5px; }
.study-histrow .grow { flex: 1; min-width: 0; }
.study-histrow.ok { border-left: 2px solid var(--green, #4f9e60); }
.study-histrow.bad { border-left: 2px solid var(--red, #e05555); }
.study-histrow .study-state { white-space: nowrap; opacity: 0.7; font-size: 11px; }
.study-cat { font-size: 11px; height: auto; padding: 2px 5px;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 6px; vertical-align: middle; flex: 0 0 auto; }
/* material rows wrap so the category dropdown + actions always fit */
.study-mat-row { flex-wrap: wrap; }
.study-mat-name { flex: 1 1 200px; min-width: 120px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.study-row-actions { display: flex; align-items: center; gap: 6px;
  flex-wrap: wrap; margin-left: auto; }
/* rendered AI markdown (questions, options, references, notes…) */
.study-md > :first-child { margin-top: 0; }
.study-md > :last-child { margin-bottom: 0; }
.study-md p { margin: 0.35em 0; }
.study-md ul, .study-md ol { margin: 0.35em 0; padding-left: 1.4em; }
.study-md img { max-width: 100%; height: auto; }
.study-md .katex { font-size: 1.02em; }
.study-md .katex-display { margin: 0.4em 0; overflow-x: auto; overflow-y: hidden; }
/* plan */
.study-plan-day { border: 1px solid var(--border); border-radius: 9px; padding: 10px 12px; margin-bottom: 8px; }
.study-plan-day.today { border-color: var(--accent, #5b8abf); }
.study-plan-date { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
.study-block { display: flex; gap: 8px; align-items: flex-start; padding: 5px 0; font-size: 12.5px; }
.study-block input { margin-top: 2px; }
.study-block.done .study-block-text { opacity: 0.45; text-decoration: line-through; }
.study-block-type { font-size: 9.5px; letter-spacing: 0.05em; text-transform: uppercase;
  padding: 1px 6px; border-radius: 4px; border: 1px solid var(--border); opacity: 0.8;
  white-space: nowrap; margin-top: 1px; }
.study-block-type.mock { color: var(--red, #e05555); }
.study-block-type.first_contact { color: var(--accent, #5b8abf); }
.study-block-note { display: block; font-size: 11px; opacity: 0.5; margin-top: 2px; }
/* focus */
.study-focus-clock { font-size: 54px; font-variant-numeric: tabular-nums; text-align: center;
  margin: 26px 0 8px; letter-spacing: 0.03em; }
.study-bars { display: flex; gap: 4px; align-items: flex-end; height: 70px; margin-top: 10px; }
.study-bar { flex: 1; background: var(--accent, #5b8abf); opacity: 0.7; border-radius: 3px 3px 0 0;
  min-height: 2px; position: relative; }
.study-bar i { position: absolute; bottom: -16px; left: 50%; transform: translateX(-50%);
  font-size: 8.5px; opacity: 0.5; font-style: normal; }
/* subject detail lists */
.study-cardrow { display: flex; gap: 10px; padding: 8px 10px; border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 6px; font-size: 12.5px; align-items: flex-start; }
.study-cardrow .front { flex: 1.2; min-width: 0; }
.study-cardrow .back { flex: 1; min-width: 0; opacity: 0.7; }
.study-cardrow .front, .study-cardrow .back { overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; white-space: pre-wrap; }
.study-cardrow.suspended { opacity: 0.45; }
.study-state { font-size: 9.5px; text-transform: uppercase; opacity: 0.55; white-space: nowrap; }
.study-topic-row { display: grid; grid-template-columns: 1fr 110px 110px 30px; gap: 6px; margin-bottom: 6px; }
.study-subtle { font-size: 11.5px; opacity: 0.6; }
`;
  document.head.appendChild(st);
}

// ---------------------------------------------------------------------------
// pane lifecycle
// ---------------------------------------------------------------------------

const ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><path d="M9 8h6"/><path d="M9 12h4"/></svg>`;

export function openPanel() {
  if (_open) return;
  _open = true;
  injectStyles();

  const backdrop = document.createElement('div');
  backdrop.id = 'study-backdrop';
  backdrop.className = 'study-backdrop';
  backdrop.addEventListener('click', () => closePanel());
  document.body.appendChild(backdrop);

  _pane = document.createElement('div');
  _pane.id = 'study-pane';
  _pane.className = 'study-pane';
  _pane.setAttribute('role', 'dialog');
  _pane.setAttribute('aria-label', 'Study panel');
  _pane.innerHTML = `
    <div class="study-header">
      <span class="study-title">${ICON} Study</span>
      <div class="study-tabs" id="study-tabs" role="tablist">
        ${TABS.map(([k, label]) => `<button class="study-tab" data-tab="${k}" role="tab" aria-selected="${_tab === k}">${label}</button>`).join('')}
      </div>
      <span class="study-header-spacer"></span>
      <span class="study-model-wrap" id="study-model-wrap" title="Model used for extraction, grading and hints. 'Same as chat' falls back to the utility/default model.">
        <select id="study-ep-select" aria-label="Study model endpoint"><option value="">Same as chat</option></select>
        <select id="study-model-select" aria-label="Study model"><option value="">model…</option></select>
        <span class="study-model-status" id="study-model-status" role="status"></span>
      </span>
      <button class="study-x" id="study-min-btn" title="Minimize">–</button>
      <button class="study-x" id="study-close-btn" title="Close (Esc)">✕</button>
    </div>
    <div class="study-body" id="study-body"></div>
  `;
  document.body.appendChild(_pane);

  document.getElementById('tool-study-btn')?.classList.add('active');

  _pane.querySelector('#study-close-btn').addEventListener('click', () => closePanel());
  _pane.querySelector('#study-min-btn').addEventListener('click', () => {
    _ensureChipRegistered();
    document.getElementById('study-backdrop')?.classList.add('hidden');
    if (Modals.minimize) Modals.minimize('study-pane'); else closePanel();
  });
  initModelSelector();
  _pane.querySelector('#study-tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.study-tab');
    if (btn) setTab(btn.dataset.tab);
  });

  _keyHandler = (e) => {
    // Minimized, closed, unfocused or mid-edit: not ours to act on.
    if (!_studyAcceptsShortcut(e)) return;
    if (e.key === 'Escape') {
      // Close an open file viewer first, leaving the study pane open.
      const v = (_pane || document).querySelector('#study-viewer');
      if (v) { v.remove(); return; }
      closePanel();
      return;
    }
    if (_tab === 'review') reviewKeydown(e);
    if (_tab === 'practice') practiceKeydown(e);
  };
  document.addEventListener('keydown', _keyHandler);

  _ensureChipRegistered();
  setTab(_tab || 'today');
}

function _ensureChipRegistered() {
  try {
    if (Modals.isRegistered && Modals.isRegistered('study-pane')) return;
    Modals.register('study-pane', {
      sidebarBtnId: 'tool-study-btn',
      label: 'Study',
      icon: ICON,
      restoreFn: () => {
        document.getElementById('study-backdrop')?.classList.remove('hidden');
        if (!_open) openPanel();
      },
      closeFn: () => { _forceClose(); },
    });
  } catch { /* modal manager optional */ }
}

function _forceClose() {
  _open = false;
  if (_keyHandler) { document.removeEventListener('keydown', _keyHandler); _keyHandler = null; }
  document.getElementById('tool-study-btn')?.classList.remove('active');
  try { Modals.unregister('study-pane'); } catch { }
  document.getElementById('study-pane')?.remove();
  document.getElementById('study-backdrop')?.remove();
  _pane = null;
}

let _endpoints = [];

// ---------------------------------------------------------------------------
// MODEL SELECTOR PERSISTENCE
// ---------------------------------------------------------------------------
// The last selection the server confirmed, plus the single-flight queue state.
// Kept at module scope so a reload can be checked against what was actually
// persisted rather than what the control happens to display.
let _modelSavePending = null;
let _modelSaveRunning = false;
let _studyModelConfirmed = { endpointId: '', model: '' };

// Read JSON from an endpoint, treating a non-2xx as the failure it is.
// fetch() resolves normally for 403/422/500, so `.then(r => r.json())` parsed
// error bodies as settings.
async function fetchStudyJson(path, fetchImpl = fetch) {
  const res = await fetchImpl(`${API}${path}`, { credentials: 'same-origin' });
  if (!res.ok) {
    const err = new Error(`${path} failed (HTTP ${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// POST the study model choice. Returns a result rather than throwing, so the
// caller can render a pending/failed state instead of announcing success.
async function sendStudyModelSave(desired, fetchImpl = fetch) {
  try {
    const res = await fetchImpl(`${API}/api/auth/settings`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        study_endpoint_id: desired.endpointId || '',
        study_model: desired.model || '',
      }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = String(body.detail);
      } catch { /* error body was not JSON */ }
      return { ok: false, status: res.status, detail };
    }
    return { ok: true, status: res.status };
  } catch (e) {
    return { ok: false, status: 0, detail: (e && e.message) || String(e) };
  }
}

// Single-flight queue. Two POSTs racing could complete in either order and
// leave the server holding the *older* selection, so only one is ever in
// flight and a burst collapses to the latest desired value.
async function queueStudyModelSave(desired, fetchImpl = fetch) {
  _modelSavePending = desired;
  if (_modelSaveRunning) return { ok: true, coalesced: true };
  _modelSaveRunning = true;
  let last = { ok: true, coalesced: true };
  try {
    while (_modelSavePending) {
      const next = _modelSavePending;
      _modelSavePending = null;
      last = await sendStudyModelSave(next, fetchImpl);
      if (!last.ok) { _modelSavePending = null; return last; }
      _studyModelConfirmed = {
        endpointId: next.endpointId || '', model: next.model || '',
      };
    }
  } finally {
    _modelSaveRunning = false;
  }
  return last;
}

// Inline, persistent status beside the selector. A toast disappears; a failed
// save needs to stay visible and offer a retry.
function setModelStatus(kind, text, onRetry) {
  const el = _pane?.querySelector('#study-model-status');
  if (!el) return;
  el.className = `study-model-status ${kind}`;
  el.textContent = text || '';
  if (kind === 'error' && onRetry) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'study-model-retry';
    btn.textContent = 'Retry';
    btn.addEventListener('click', onRetry);
    el.appendChild(btn);
  }
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
}

async function initModelSelector() {
  const epSel = _pane?.querySelector('#study-ep-select');
  const mSel = _pane?.querySelector('#study-model-select');
  if (!epSel || !mSel) return;
  try {
    const [eps, settings] = await Promise.all([
      fetchStudyJson('/api/model-endpoints'),
      fetchStudyJson('/api/auth/settings'),
    ]);
    _endpoints = Array.isArray(eps) ? eps : [];
    _studyModelConfirmed = {
      endpointId: settings.study_endpoint_id || '',
      model: settings.study_model || '',
    };
    epSel.innerHTML = '<option value="">Same as chat</option>' + _endpoints.map(ep =>
      `<option value="${esc(ep.id)}">${esc(ep.name)}${ep.online === false ? ' (offline)' : ''}</option>`).join('');
    epSel.value = settings.study_endpoint_id || '';
    fillStudyModels(settings.study_model || '');
  } catch (e) {
    console.warn('study model selector init failed', e);
    return;
  }

  const save = async () => {
    const desired = { endpointId: epSel.value || '', model: mSel.value || '' };
    setModelStatus('pending', 'Saving…');
    const result = await queueStudyModelSave(desired);
    if (result.coalesced) return;      // a later change owns the outcome
    if (result.ok) {
      setModelStatus('ok', desired.endpointId ? 'Saved' : 'Same as chat');
      return;
    }
    // Announce nothing as saved: the server refused, and the last confirmed
    // selection is still what a reload will show.
    setModelStatus('error', `Not saved: ${result.detail}. `, save);
    toast(`Could not save the study model: ${result.detail}`, true);
  };
  epSel.addEventListener('change', () => { fillStudyModels(''); save(); });
  mSel.addEventListener('change', save);
}

function fillStudyModels(selected) {
  const epSel = _pane?.querySelector('#study-ep-select');
  const mSel = _pane?.querySelector('#study-model-select');
  if (!epSel || !mSel) return;
  const ep = _endpoints.find(e => e.id === epSel.value);
  const models = (ep && Array.isArray(ep.models)) ? ep.models : [];
  mSel.innerHTML = '<option value="">' + (epSel.value ? 'pick model…' : '—') + '</option>' +
    models.map(m => {
      const id = typeof m === 'string' ? m : (m.id || m.name || '');
      return `<option value="${esc(id)}" ${id === selected ? 'selected' : ''}>${esc(id)}</option>`;
    }).join('');
  mSel.disabled = !epSel.value;
}

export function closePanel() { _forceClose(); }
export function togglePanel() {
  try {
    if (Modals.isMinimized && Modals.isMinimized('study-pane')) {
      Modals.restore('study-pane');
      return;
    }
  } catch { /* modal manager optional */ }
  _open ? closePanel() : openPanel();
}
export function isPanelOpen() { return _open; }

function body() { return _pane?.querySelector('#study-body'); }

function setTab(tab) {
  _tab = tab;
  const b = body();
  if (b) b.onclick = null; // per-tab delegated handlers are reassigned below
  _pane.querySelectorAll('.study-tab').forEach(btn => {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const render = {
    today: renderToday, subjects: renderSubjects, review: renderReview,
    practice: renderPractice, plan: renderPlan, focus: renderFocus,
    stats: renderStats, history: renderHistory, agent: renderAgent,
  }[tab];
  if (render) render();
}

// ---------------------------------------------------------------------------
// AGENT (static/js/studyAgent.js)
// ---------------------------------------------------------------------------

function renderAgent() {
  const el = body();
  if (!el) return;
  renderAgentTab({ el, decks: S.decks, deckId: S.subject?.deck?.id || null, esc, toast });
}

// Open the Agent tab focused on a subject, optionally with a drafted message.
// Both are set unconditionally: guarding on a truthy value meant opening the
// tutor without a subject or draft silently kept the *previous* subject's
// scope and half-written question. setAgentScope/setAgentPrefill already
// normalise null/'' to "unscoped".
function openAgent(deckId, prefill) {
  setAgentScope(deckId || null);
  setAgentPrefill(prefill || '');
  setTab('agent');
}

function setTabSilent(tab) {
  _tab = tab;
  const b = body();
  if (b) b.onclick = null;
  _pane?.querySelectorAll('.study-tab').forEach(btn => {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

// ---------------------------------------------------------------------------
// STATS DASHBOARD (Phase 3.1)
// ---------------------------------------------------------------------------

function _pct(v) { return v == null ? '—' : `${Math.round(v * 100)}%`; }
function _num(v) { return v == null ? '—' : String(v); }

function _calibrationChart(curve) {
  if (!curve || !curve.length) {
    return '<div class="study-empty">Not enough data yet. Answer questions with a confidence rating to build your calibration curve.</div>';
  }
  const cols = curve.map(c => {
    const acc = c.accuracy == null ? 0 : c.accuracy;
    const h = Math.round(acc * 100);
    const low = c.low_n ? 'opacity:0.4;' : '';
    const fill = h < 40 ? 'warn' : (h >= 80 ? 'good' : '');
    return `<div class="study-cal-col">
      <div style="height:80px;display:flex;flex-direction:column;justify-content:flex-end;width:100%;">
        <div class="study-cal-bar ${fill}" style="height:${h}%;${low}"></div>
      </div>
      <div class="study-cal-lbl">${c.label}</div>
    </div>`;
  }).join('');
  return `<div class="study-cal-row">${cols}</div>
    <div class="study-subtle" style="margin-top:6px;">Bars = actual % correct in each confidence band. Dimmed bars have too few attempts to be reliable.</div>`;
}

function _dailyActivity(daily) {
  if (!daily || !daily.length) return '<div class="study-empty">No activity recorded yet.</div>';
  const maxR = Math.max(1, ...daily.map(d => d.reviews + d.attempts));
  return daily.map(d => {
    const total = d.reviews + d.attempts;
    const h = Math.round((total / maxR) * 100);
    const label = d.date.slice(5);
    return `<div class="study-bar-row">
      <span class="lbl">${label}</span>
      <div class="study-bar-track"><div class="study-bar-fill" style="width:${h}%"></div></div>
      <span class="study-pct">${total}</span>
    </div>`;
  }).join('');
}

function _topicList(topics) {
  if (!topics || !topics.length) return '<div class="study-empty">No topic-level data yet. Practice questions tagged with topics to see accuracy by area.</div>';
  return topics.map(t => {
    const h = Math.round((t.accuracy || 0) * 100);
    const fill = h < 40 ? 'warn' : (h >= 80 ? 'good' : '');
    return `<div class="study-bar-row">
      <span class="lbl" title="${esc(t.topic)}">${esc(t.topic)}</span>
      <div class="study-bar-track"><div class="study-bar-fill ${fill}" style="width:${h}%"></div></div>
      <span class="study-pct">${_pct(t.accuracy)}</span>
    </div>`;
  }).join('');
}

function _forecastChart(forecast) {
  if (!forecast || !forecast.length) return '<div class="study-empty">No scheduled reviews ahead.</div>';
  const maxD = Math.max(1, ...forecast.map(d => d.cards + d.questions));
  return forecast.map(d => {
    const total = d.cards + d.questions;
    const h = Math.round((total / maxD) * 100);
    const label = d.date.slice(5);
    return `<div class="study-bar-row">
      <span class="lbl">${label}</span>
      <div class="study-bar-track"><div class="study-bar-fill" style="width:${h}%"></div></div>
      <span class="study-pct">${total}</span>
    </div>`;
  }).join('');
}

async function renderStats() {
  const el = body();
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  let s, calib = null;
  try {
    [s, calib] = await Promise.all([
      jget('/api/study/stats?days=42'),
      // Brier + sure-but-wrong: the curve shows the shape, this scores it.
      jget('/api/study/calibration?days=90').catch(() => null),
    ]);
  }
  catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'stats') return;
  const t = s.totals || {};
  const ret = s.retention || {};
  const sr = t.success_rate == null ? '—' : `${Math.round(t.success_rate * 100)}%`;
  const acc = t.accuracy == null ? '—' : `${Math.round(t.accuracy * 100)}%`;
  el.innerHTML = `
    <div class="study-chips">
      <div class="study-chip"><b>${_num(t.reviews)}</b><span>card reviews (42d)</span></div>
      <div class="study-chip"><b>${sr}</b><span>recall success</span></div>
      <div class="study-chip"><b>${_num(t.cards)}</b><span>cards tracked</span></div>
      <div class="study-chip"><b>${acc}</b><span>question accuracy</span></div>
      <div class="study-chip"><b>${_num(t.focus_min)}</b><span>focus minutes</span></div>
      <div class="study-chip"><b>${ret.mean == null ? '—' : Math.round(ret.mean * 100) + '%'}</b><span>avg recall now</span></div>
    </div>

    <div class="study-dash-grid">
      <div class="study-card-box">
        <h4>Calibration — confidence vs accuracy</h4>
        ${_calibrationChart(s.calibration_curve)}
        ${_calibrationHtml(calib)}
      </div>
      <div class="study-card-box">
        <h4>Daily activity — last 6 weeks</h4>
        ${_dailyActivity(s.daily)}
      </div>
      <div class="study-card-box">
        <h4>Topic accuracy — weakest first</h4>
        ${_topicList(s.topic_accuracy)}
      </div>
      <div class="study-card-box">
        <h4>Due forecast — next 14 days</h4>
        ${_forecastChart(s.due_forecast)}
      </div>
    </div>
    <div class="study-subtle" style="margin-top:14px;">
      ${ret.n ? `${ret.n} review cards tracked · ${ret.mature_pct == null ? '—' : Math.round(ret.mature_pct * 100) + '%'} mature (≥21d stability)` : 'No cards in review state yet.'}
      ${t.avg_score != null ? ` · avg open-answer score ${t.avg_score}/100` : ''}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// HISTORY
// ---------------------------------------------------------------------------

function _histDayLabel(day) {
  const today = new Date().toISOString().slice(0, 10);
  const yest = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  if (day === today) return 'Today';
  if (day === yest) return 'Yesterday';
  return day;
}

function renderHistoryEntry(e) {
  const time = (e.when || '').slice(11, 16);
  if (e.kind === 'card') {
    const label = ['', 'Again', 'Hard', 'Good', 'Easy'][e.rating] || '';
    const ok = e.rating >= 3;
    return `<div class="study-histrow ${ok ? 'ok' : 'bad'}">
      <span class="study-qchip">card</span>
      <div class="grow">${esc(e.title)}</div>
      <span class="study-state">${label} · ${time}</span>
    </div>`;
  }
  const ok = e.correct === true || (e.score != null && e.score >= 60);
  const outcome = e.qtype === 'mcq'
    ? (e.correct ? '✓ correct' : '✗ wrong')
    : (e.score != null ? `${e.score}/100` : '');
  const conf = e.confidence != null ? ` · ${esc(String(e.confidence))}` : '';
  return `<div class="study-histrow ${ok ? 'ok' : 'bad'}">
    <span class="study-qchip ${e.qtype || ''}">${esc(e.qtype || 'q')}</span>
    <div class="grow">
      <div>${esc(e.title)}</div>
      ${e.answer ? `<div class="study-subtle" style="margin-top:2px;">Your answer: ${esc(e.answer)}</div>` : ''}
      ${e.feedback ? `<div class="study-subtle" style="margin-top:2px;">Feedback: ${esc(e.feedback)}</div>` : ''}
    </div>
    <span class="study-state">${outcome}${conf} · ${time}</span>
  </div>`;
}

async function renderHistory() {
  const el = body();
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  let entries;
  try { entries = (await jget('/api/study/history?limit=300')).entries; }
  catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'history') return;
  if (!entries.length) {
    el.innerHTML = '<div class="study-empty">No history yet — answer some practice questions or review cards and they’ll show up here.</div>';
    return;
  }
  const groups = {};
  for (const e of entries) {
    const day = (e.when || '').slice(0, 10) || 'unknown';
    (groups[day] = groups[day] || []).push(e);
  }
  el.innerHTML = `<div class="study-subtle" style="margin-bottom:8px;">Every answer you’ve given and the feedback on it. Newest first.</div>` +
    Object.keys(groups).sort().reverse().map(day =>
      `<div class="study-section-title">${esc(_histDayLabel(day))}</div>
       ${groups[day].map(renderHistoryEntry).join('')}`).join('');
}

// ---------------------------------------------------------------------------
// TODAY
// ---------------------------------------------------------------------------

async function renderToday() {
  const el = body();
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  try { S.overview = await jget('/api/study/overview'); }
  catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'today') return;
  const o = S.overview;
  const t = o.today;
  const succ = t.success_rate == null ? '' : ` · ${Math.round(t.success_rate * 100)}%`;
  const flexBadge = t.flex_used ? ` <span class="study-badge" style="opacity:0.7;font-size:10px;">${t.flex_used} flex day${t.flex_used > 1 ? 's' : ''}</span>` : '';
  el.innerHTML = `
    <div class="study-chips">
      <div class="study-chip"><b>${o.due_total}</b><span>cards due</span></div>
      <div class="study-chip"><b>${o.q_due_total ?? 0}</b><span>questions due</span></div>
      <div class="study-chip"><b>${t.streak_days}</b><span>day streak${flexBadge}</span></div>
      <div class="study-chip"><b>${t.reviews + (t.attempts || 0)}</b><span>retrievals today${succ}</span></div>
      <div class="study-chip"><b>${t.focus_min}</b><span>focus min today</span></div>
    </div>

    <div class="study-section-title">Subjects</div>
    <div id="study-today-decks"></div>

    <div class="study-section-title">Today's plan</div>
    <div id="study-today-plan"></div>

    <div class="study-tip">${esc(TIPS[Math.floor(Math.random() * TIPS.length)])}</div>
  `;

  const decksEl = el.querySelector('#study-today-decks');
  if (!o.decks.length) {
    decksEl.innerHTML = `<div class="study-empty">No subjects yet — create one in the Subjects tab and feed it your course material (past papers work best).</div>`;
  } else {
    decksEl.innerHTML = o.decks.map(d => `
      <div class="study-row">
        <span class="grow">${esc(d.name)}</span>
        <span class="study-badge due">${d.due_count} cards</span>
        <span class="study-badge q">${(d.q_due ?? 0)}+${(d.q_new ?? 0)} questions</span>
        <button class="study-btn small" data-review="${d.id}"
          ${d.due_count + d.new_available === 0 ? 'disabled' : ''}>Cards</button>
        <button class="study-btn small primary" data-practice="${d.id}"
          ${(d.q_due ?? 0) + (d.q_new ?? 0) === 0 ? 'disabled' : ''}>Practice</button>
      </div>`).join('');
    decksEl.addEventListener('click', (e) => {
      const rid = e.target.closest('[data-review]')?.dataset.review;
      const pid = e.target.closest('[data-practice]')?.dataset.practice;
      if (rid) startReview(rid);
      else if (pid) startPractice(pid);
    });
  }

  const planEl = el.querySelector('#study-today-plan');
  const withBlocks = o.exams.filter(x => x.today_blocks?.length);
  if (!o.exams.length) {
    planEl.innerHTML = `<div class="study-empty">No exams tracked. Add one in the Plan tab to get a spaced, interleaved schedule.</div>`;
  } else if (!withBlocks.length) {
    planEl.innerHTML = `<div class="study-empty">${o.exams.map(x =>
      `${esc(x.title)} — ${x.days_left}d left${x.has_plan ? '' : ' (no plan generated yet)'}`).join('<br>')}</div>`;
  } else {
    planEl.innerHTML = withBlocks.map(x => `
      <div style="margin-bottom:10px;">
        <div style="font-size:12px;font-weight:600;margin-bottom:4px;">${esc(x.title)}
          <span style="opacity:0.5;font-weight:400;">· ${x.days_left}d left</span></div>
        ${x.today_blocks.map(b => `
          <div class="study-block-row">
          <label class="study-block ${b.done ? 'done' : ''}">
            <input type="checkbox" data-exam="${x.id}" data-key="${esc(b.key)}" ${b.done ? 'checked' : ''}>
            <span class="study-block-type ${esc(b.type)}">${esc(b.type.replace('_', ' '))}</span>
            <span class="study-block-text">${b.topics.map(esc).join(', ')} · ${b.minutes}min</span>
          </label>
          ${x.deck_id && b.type !== 'mock' ? `<button class="study-btn small" data-plan-practice="${esc(x.deck_id)}" data-topics="${esc(b.topics.join(','))}" title="Practice this block's topics from the linked subject (whole subject if no question matches)">Practice</button>` : ''}
          </div>`).join('')}
      </div>`).join('');
    planEl.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-plan-practice]');
      if (btn) startPractice(btn.dataset.planPractice, 12, { topics: btn.dataset.topics, label: btn.dataset.topics });
    });
    planEl.addEventListener('change', async (e) => {
      const cb = e.target.closest('input[data-key]');
      if (!cb) return;
      try { await jpost(`/api/study/exams/${cb.dataset.exam}/toggle-block`, { key: cb.dataset.key }); }
      catch (err) { toast(err.message, true); }
      cb.closest('.study-block').classList.toggle('done', cb.checked);
    });
  }
}

// ---------------------------------------------------------------------------
// SUBJECTS
// ---------------------------------------------------------------------------

async function renderSubjects() {
  if (S.subject) return renderSubjectDetail();
  const el = body();
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  try { S.decks = (await jget('/api/study/decks')).decks; }
  catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'subjects' || S.subject) return;

  el.innerHTML = `
    <div class="study-form-row">
      <input class="study-input" id="study-new-deck-name" placeholder="New subject, e.g. Microeconomics" style="flex:1;max-width:320px;">
      <button class="study-btn primary" id="study-new-deck-btn">Create subject</button>
    </div>
    <div class="study-subtle" style="margin:2px 0 10px;">A subject holds your materials (past papers, notes), the question bank the AI extracts from them, and flashcards.</div>
    <div class="study-form-row" style="margin-bottom:10px;">
      <label class="study-subtle" for="study-school" style="white-space:nowrap;">Your school</label>
      <input class="study-input" id="study-school" placeholder="e.g. Nova SBE (optional)" style="flex:1;max-width:320px;"
        title="Used to localize web searches for theory (language & sources) when a subject has no theory material. Optional.">
    </div>
    <div class="study-form-row" style="margin-bottom:10px;">
      <label class="study-subtle" for="study-order" style="white-space:nowrap;">Practice order</label>
      <select class="study-input" id="study-order" style="flex:1;max-width:320px;"
        title="How practice questions and card reviews are ordered.">
        <option value="completed">New first — answered ones go last (default)</option>
        <option value="review">Due reviews first (immediate review)</option>
      </select>
    </div>
    <div id="study-deck-list"></div>
    <div class="study-form-row" style="margin-top:14px;">
      <input class="study-input" id="study-global-search" placeholder="Search across all subjects (questions + cards)…" style="flex:1;max-width:400px;" aria-label="Cross-subject search">
      <button class="study-btn" id="study-global-search-btn">Search</button>
    </div>
    <div id="study-search-results"></div>
  `;
  const list = el.querySelector('#study-deck-list');
  list.innerHTML = S.decks.length ? S.decks.map(d => `
    <div class="study-row" data-deck="${d.id}" style="cursor:pointer;">
      <span class="grow"><b>${esc(d.name)}</b>
        <span style="opacity:0.5;font-size:11px;"> · ${d.q_total ?? 0} questions · ${d.total} cards</span></span>
      <span class="study-badge q">${(d.q_due ?? 0)} q due</span>
      <span class="study-badge due">${d.due_count} cards due</span>
      <button class="study-btn small" data-open="${d.id}">Open</button>
      <button class="study-btn small danger" data-del="${d.id}" title="Delete subject">✕</button>
    </div>`).join('')
    : '<div class="study-empty">No subjects yet. One per course works well.</div>';

  el.querySelector('#study-new-deck-btn').addEventListener('click', async () => {
    const inp = el.querySelector('#study-new-deck-name');
    const name = inp.value.trim();
    if (!name) return;
    try { await jpost('/api/study/decks', { name }); inp.value = ''; renderSubjects(); }
    catch (e) { toast(e.message, true); }
  });

  // Per-user school (prefs store) — localizes web-theory searches.
  const schoolInput = el.querySelector('#study-school');
  if (schoolInput) {
    jget('/api/prefs/study_school').then(d => { schoolInput.value = (d && d.value) || ''; }).catch(() => {});
    schoolInput.addEventListener('change', () => {
      jput('/api/prefs/study_school', { value: schoolInput.value.trim() }).catch(() => {});
    });
  }

  // Per-user practice ordering (prefs store). "completed" sinks already-answered
  // questions and cards behind every new/unseen one; default reviews due first.
  const orderSel = el.querySelector('#study-order');
  if (orderSel) {
    jget('/api/prefs/study_order')
      .then(d => { orderSel.value = (d && d.value) === 'review' ? 'review' : 'completed'; })
      .catch(() => {});
    orderSel.addEventListener('change', () => {
      jput('/api/prefs/study_order', { value: orderSel.value }).catch(() => {});
    });
  }
  list.addEventListener('click', async (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      armThen(del, async () => {
        try { await jdel(`/api/study/decks/${del.dataset.del}`); renderSubjects(); }
        catch (err) { toast(err.message, true); }
      }, 'Delete all?');
      return;
    }
    const row = e.target.closest('[data-deck]');
    if (row) openSubject(row.dataset.deck);
  });

  // Phase 5: cross-subject search
  const doGlobalSearch = async () => {
    const query = el.querySelector('#study-global-search').value.trim();
    const resEl = el.querySelector('#study-search-results');
    if (!query) { resEl.innerHTML = ''; return; }
    resEl.innerHTML = '<div class="study-subtle">Searching…</div>';
    try {
      const r = await jget(`/api/study/search?q=${encodeURIComponent(query)}&limit=20`);
      const qs = r.questions || [];
      const cs = r.cards || [];
      if (!qs.length && !cs.length) {
        resEl.innerHTML = '<div class="study-empty">No results.</div>';
        return;
      }
      resEl.innerHTML = `
        ${qs.length ? `<div class="study-section-title">Questions (${qs.length})</div>` : ''}
        ${qs.map(q => `<div class="study-row"><span class="grow">${esc(q.question.slice(0, 100))}</span><span class="study-subtle">${esc(q.qtype)}</span></div>`).join('')}
        ${cs.length ? `<div class="study-section-title" style="margin-top:10px;">Cards (${cs.length})</div>` : ''}
        ${cs.map(c => `<div class="study-row"><span class="grow">${esc(c.front.slice(0, 80))}</span><span class="study-subtle">card</span></div>`).join('')}
      `;
    } catch (err) { resEl.innerHTML = `<div class="study-empty">${esc(err.message)}</div>`; }
  };
  el.querySelector('#study-global-search-btn')?.addEventListener('click', doGlobalSearch);
  el.querySelector('#study-global-search')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doGlobalSearch(); }
  });
}

async function openSubject(deckId) {
  const deck = S.decks.find(d => d.id === deckId) || { id: deckId, name: 'Subject' };
  S.subject = { deck, cards: [], materials: [], questions: [], proposals: null,
                qFilter: '', qMaterial: '', qLimit: 40, extracting: new Set() };
  await reloadSubject();
}

async function reloadSubject() {
  const s = S.subject;
  if (!s) return;
  try {
    const [cards, materials, questions] = await Promise.all([
      jget(`/api/study/decks/${s.deck.id}/cards`).then(r => r.cards),
      jget(`/api/study/decks/${s.deck.id}/materials`).then(r => r.materials),
      jget(`/api/study/decks/${s.deck.id}/questions`).then(r => r.questions),
    ]);
    s.cards = cards; s.materials = materials; s.questions = questions;
  } catch (e) { toast(e.message, true); }
  renderSubjectDetail();
}

function renderSubjectDetail() {
  const el = body();
  const s = S.subject;
  if (!s) return renderSubjects();
  const qDue = s.questions.filter(q => !q.suspended && q.state !== 'new' &&
    q.due && new Date(q.due) <= new Date()).length;
  el.innerHTML = `
    <div class="study-form-row">
      <button class="study-btn small" id="study-subj-back">← Subjects</button>
      <b style="font-size:14px;">${esc(s.deck.name)}</b>
      <span class="study-subtle" id="study-subj-counts">${s.questions.length} questions · ${s.cards.length} cards</span>
      <span style="flex:1;"></span>
      <button class="study-btn small" id="study-subj-ask" title="Chat with the Study agent about this subject: it tutors from your materials and can manage the bank for you">Ask the tutor</button>
      <button class="study-btn small" id="study-subj-overview" title="An AI overview of the subject that ties the chapters together">Overview</button>
      <button class="study-btn small" id="study-subj-review" ${!s.cards.length ? 'disabled' : ''}>Review cards</button>
      <button class="study-btn small primary" id="study-subj-practice" ${!s.questions.length ? 'disabled' : ''}>Practice questions</button>
    </div>

    <div class="study-section-title">Materials → question bank</div>
    <div class="study-subtle" style="margin-bottom:8px;">Mark each material as <b>Practice</b> (past paper, problem set — its real questions are extracted faithfully, by the vision model for PDFs) or <b>Theory</b> (notes, slides — new exam-style questions are generated from it). Everything becomes closed-book practice, spaced by the scheduler.</div>
    <div class="study-form-row" style="align-items:stretch;">
      <textarea class="study-textarea" id="study-mat-text" placeholder="Paste material here (lecture notes, a past paper, problem set + solutions)…" style="flex:1;min-height:64px;"></textarea>
    </div>
    <div class="study-form-row">
      <input class="study-input" id="study-mat-name" placeholder="Material name (optional)" style="width:220px;">
      <button class="study-btn" id="study-mat-add-text">Add pasted text</button>
      <label class="study-btn" style="cursor:pointer;">
        Upload files (PDF/docx/txt)
        <input type="file" id="study-mat-file" accept=".pdf,.txt,.md,.docx,.pptx,.csv" multiple style="display:none;">
      </label>
    </div>
    <div id="study-mat-list"></div>

    <div class="study-section-title">Question bank</div>
    <div class="study-form-row">
      <input class="study-input" id="study-q-search" placeholder="Search questions…" style="width:220px;" value="${esc(s.qFilter)}">
      <select class="study-select" id="study-q-mat" title="Only show questions from one material">
        <option value="">All materials</option>
        ${s.materials.map(m => `<option value="${esc(m.id)}" ${s.qMaterial === m.id ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}
      </select>
      <button class="study-btn small" id="study-tidy-toggle" title="Bank maintenance passes for this subject">Tidy bank</button>
    </div>
    <div id="study-tidy" ${s.tidyOpen ? '' : 'hidden'}></div>
    <div id="study-q-list"></div>

    <div class="study-section-title">Flashcards (atomic facts)</div>
    <div class="study-form-row" style="align-items:stretch;">
      <textarea class="study-textarea" id="study-card-front" placeholder="Front — one specific question or cue" style="flex:1;min-height:50px;"></textarea>
      <textarea class="study-textarea" id="study-card-back" placeholder="Back — shortest complete answer" style="flex:1;min-height:50px;"></textarea>
      <button class="study-btn primary" id="study-card-add" style="align-self:flex-end;">Add</button>
    </div>
    <div class="study-form-row">
      <select class="study-select" id="study-gen-cards-mat" title="Material to draft flashcards from">
        ${s.materials.map((m, i) => `<option value="${esc(m.id)}" ${i === 0 ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}
      </select>
      <button class="study-btn small" id="study-gen-cards-btn" title="Drafts atomic flashcards from the selected material">Generate cards</button>
      <label class="study-subtle">new cards/day
        <input class="study-input" id="study-deck-npd" type="number" min="0" max="200"
          value="${s.deck.new_per_day ?? 15}" style="width:60px;padding:4px 6px;"></label>
    </div>
    <div id="study-proposals"></div>
    <div id="study-card-list"></div>
  `;

  el.querySelector('#study-subj-back').addEventListener('click', () => { S.subject = null; renderSubjects(); });
  el.querySelector('#study-subj-review').addEventListener('click', () => startReview(s.deck.id));
  el.querySelector('#study-subj-practice').addEventListener('click', () => startPractice(s.deck.id));
  el.querySelector('#study-subj-overview').addEventListener('click', () => openSubjectOverview(s.deck.id, s.deck.name));
  el.querySelector('#study-subj-ask').addEventListener('click', () => openAgent(s.deck.id, ''));
  el.querySelector('#study-deck-npd').addEventListener('change', async (e) => {
    try { await jput(`/api/study/decks/${s.deck.id}`, { new_per_day: parseInt(e.target.value || '0', 10) }); }
    catch (err) { toast(err.message, true); }
  });

  // --- materials ---
  el.querySelector('#study-mat-add-text').addEventListener('click', async () => {
    const text = el.querySelector('#study-mat-text').value.trim();
    if (text.length < 30) { toast('Paste more material first (at least a paragraph)', true); return; }
    try {
      await jpost(`/api/study/decks/${s.deck.id}/materials`, {
        text, name: el.querySelector('#study-mat-name').value.trim() || null,
      });
      el.querySelector('#study-mat-text').value = '';
      el.querySelector('#study-mat-name').value = '';
      reloadSubject();
    } catch (e) { toast(e.message, true); }
  });
  el.querySelector('#study-mat-file').addEventListener('change', async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    const nameBox = el.querySelector('#study-mat-name');
    const single = files.length === 1;
    toast(single ? `Uploading ${files[0].name}…`
                 : `Uploading ${files.length} papers…`);
    try {
      // /api/upload takes the whole batch in one request and returns the
      // stored files in order.
      const fd = new FormData();
      files.forEach((f) => fd.append('files', f));
      const res = await fetch(`${API}/api/upload`, { method: 'POST', body: fd, credentials: 'same-origin' });
      const data = await res.json();
      if (!res.ok || !data.files?.length) throw new Error(data.detail || 'Upload failed');
      // One material per uploaded paper. A shared name box only makes sense
      // for a single file; otherwise each paper keeps its own filename.
      let added = 0;
      const failed = [];
      for (const f of data.files) {
        try {
          await jpost(`/api/study/decks/${s.deck.id}/materials`, {
            file_id: f.id,
            name: (single && nameBox?.value.trim()) ? nameBox.value.trim() : f.name,
          });
          added++;
        } catch (err) { failed.push(f.name || f.id); }
      }
      if (nameBox) nameBox.value = '';
      const msg = `${added} material${added === 1 ? '' : 's'} added`
        + (failed.length ? ` — ${failed.length} failed (${failed.join(', ')})` : '');
      toast(msg, failed.length > 0);
      reloadSubject();
    } catch (err) { toast(err.message, true); }
    e.target.value = '';
  });

  renderMaterialList();
  renderQuestionList();
  renderProposals();
  renderCardList();
  if (s.tidyOpen) renderTidyBank();

  // --- flashcards ---
  el.querySelector('#study-card-add').addEventListener('click', async () => {
    const front = el.querySelector('#study-card-front').value.trim();
    const back = el.querySelector('#study-card-back').value.trim();
    if (!front || !back) { toast('Front and back are both required', true); return; }
    try {
      await jpost(`/api/study/decks/${s.deck.id}/cards`, { cards: [{ front, back }] });
      el.querySelector('#study-card-front').value = '';
      el.querySelector('#study-card-back').value = '';
      reloadSubject();
    } catch (err) { toast(err.message, true); }
  });
  el.querySelector('#study-gen-cards-btn').addEventListener('click', async (e) => {
    const matId = el.querySelector('#study-gen-cards-mat')?.value;
    const mat = s.materials.find(m => m.id === matId) || s.materials[0];
    if (!mat) { toast('Add a material first', true); return; }
    const btn = e.target;
    btn.disabled = true; btn.textContent = 'Generating…';
    try {
      const res = await jpost('/api/study/ai/generate-cards', {
        material_id: mat.id, count: 12,
      });
      s.proposals = res.cards.map(c => ({ ...c, checked: true }));
      s.proposalSource = res.source || null;
      renderProposals();
      if (res.source === 'notes') toast('Cards written from this material\u2019s study notes');
    } catch (err) { toast(err.message, true); }
    btn.disabled = false; btn.textContent = 'Generate cards';
  });

  el.querySelector('#study-tidy-toggle').addEventListener('click', () => {
    s.tidyOpen = !s.tidyOpen;
    const box = el.querySelector('#study-tidy');
    box.hidden = !s.tidyOpen;
    if (s.tidyOpen) renderTidyBank();
  });

  el.querySelector('#study-q-mat').addEventListener('change', (e) => {
    s.qMaterial = e.target.value; s.qLimit = 40; renderQuestionList();
  });
  let searchT = null;
  el.querySelector('#study-q-search').addEventListener('input', (e) => {
    clearTimeout(searchT);
    searchT = setTimeout(() => { s.qFilter = e.target.value.trim(); s.qLimit = 40; renderQuestionList(); }, 250);
  });
}

// Shared overlay shell (one viewer open at a time). `actionsHtml` goes in the
// header before Close; `inner` is the body markup. Returns the overlay element.
function _viewerShell(title, actionsHtml, inner) {
  const root = _pane || document.body;
  root.querySelector('#study-viewer')?.remove();
  const v = document.createElement('div');
  v.id = 'study-viewer';
  v.className = 'study-viewer';
  v.innerHTML = `
    <div class="study-viewer-head">
      <b class="grow">${esc(title || '')}</b>
      ${actionsHtml || ''}
      <button class="study-btn small" id="study-viewer-close">Close</button>
    </div>
    ${inner || ''}`;
  root.appendChild(v);
  v.querySelector('#study-viewer-close').addEventListener('click', () => v.remove());
  return v;
}

function _renderMarkdownInto(el, md) {
  if (!el) return;
  try { el.innerHTML = mdToHtml(md || '', {}); }
  catch { el.textContent = md || ''; }
  // Source-material links (page citations in notes / explain-further) open in a
  // new browser tab at the file + #page anchor.
  el.querySelectorAll('a[href*="/api/upload/"]').forEach(a => {
    a.target = '_blank';
    a.rel = 'noopener';
  });
}

// Render AI-written study text as markdown + KaTeX, so transcribed formatting
// and formulas show faithfully. `_md` is block; `_mdInline` unwraps a single
// outer <p> for inline spots (MCQ options, list rows). Falls back to escaped
// text if the markdown renderer is unavailable.
function _md(src) {
  try { return mdToHtml(src || '', {}); }
  catch { return esc(src || ''); }
}
function _mdInline(src) {
  return _md(src).trim().replace(/^<p>([\s\S]*?)<\/p>\s*$/i, '$1');
}

// Open an uploaded file in a new browser tab (PDFs render inline there; the
// app does not embed them — iframe embedding is blocked by the browser).
function openFileTab(fileId) {
  if (!fileId) return;
  window.open(`${API}/api/upload/${encodeURIComponent(fileId)}?inline=1`, '_blank', 'noopener');
}

function _originalQuestionButton(q, small = false) {
  const url = q?.original?.url;
  if (!url) return '';
  const title = q.original.page
    ? `Open the original question on page ${q.original.page} in a new tab`
    : 'Open the original question file in a new tab';
  return `<a class="study-btn${small ? ' small' : ''}" href="${esc(url)}" target="_blank" rel="noopener" title="${esc(title)}">See original question</a>`;
}

// The material file route serves PDFs and images only (a framed HTML upload
// would run on our own origin), so everything else keeps opening in a tab.
function _previewable(m) {
  return !!m && !!m.file_id &&
    (m.kind === 'pdf' || /\.(pdf|png|jpe?g|gif|webp|bmp|svg)$/i.test(m.name || ''));
}

// In-pane material viewer. PDFs and images are framed from the material's own
// route, which is the one upload path allowed to be framed same-origin (see
// SecurityHeadersMiddleware); the browser's built-in PDF viewer honours #page=N.
// Anything it cannot render (or a blocked frame) falls back to a new tab.
function openMaterialViewer(materialId, page, title) {
  if (!materialId) return;
  const src = `${API}/api/study/materials/${encodeURIComponent(materialId)}/file`
    + (page ? `#page=${page}` : '');
  const actions = `<button class="study-btn small" id="study-view-tab">Open in a tab</button>`;
  const v = _viewerShell(title || 'Material', actions, `
    <iframe id="study-view-frame" src="${esc(src)}" title="${esc(title || 'Material')}"
      style="width:100%;height:min(78vh,900px);border:0;background:#fff;"></iframe>`);
  v.querySelector('#study-view-tab').addEventListener('click', () => {
    window.open(src, '_blank', 'noopener');
  });
}

// Generate-if-missing then show a Markdown doc (study notes / subject overview)
// in the viewer, with a Regenerate action. `kind` is 'material' or 'deck'.
async function _openMarkdownDoc({ title, getPath, postPath, field, confirmMsg, busyMsg }) {
  let r;
  try { r = await jget(getPath); }
  catch (e) { toast(e.message, true); return; }
  const show = () => {
    const v = _viewerShell(title,
      `<button class="study-btn small" id="study-doc-regen">Regenerate</button>`,
      `<div class="study-viewer-body" id="study-viewer-body"></div>`);
    _renderMarkdownInto(v.querySelector('#study-viewer-body'), r[field]);
    v.querySelector('#study-doc-regen').addEventListener('click', async (ev) => {
      ev.target.disabled = true; ev.target.textContent = 'Regenerating…';
      try {
        const rr = await jpost(postPath, {});
        _renderMarkdownInto(v.querySelector('#study-viewer-body'), rr[field]);
        reloadSubject();
      } catch (e) { toast(e.message, true); }
      ev.target.disabled = false; ev.target.textContent = 'Regenerate';
    });
  };
  if (r && r[field]) { show(); return; }
  const v0 = _viewerShell(title, '', `<div class="study-viewer-empty"><div>${esc(confirmMsg)}<br><br>
    <button class="study-btn primary" id="study-doc-gen">Generate now</button></div></div>`);
  v0.querySelector('#study-doc-gen').addEventListener('click', async (ev) => {
    ev.target.disabled = true; ev.target.textContent = busyMsg;
    try { r = await jpost(postPath, {}); }
    catch (e) { toast(e.message, true); ev.target.disabled = false; ev.target.textContent = 'Generate now'; return; }
    reloadSubject();
    show();
  });
}

function openMaterialNotes(materialId, name) {
  return _openMarkdownDoc({
    title: `${name} — study notes`,
    getPath: `/api/study/materials/${materialId}/notes`,
    postPath: `/api/study/materials/${materialId}/notes`,
    field: 'summary',
    confirmMsg: `No study notes for “${name}” yet. Generate them now? (uses the AI model — may take a minute)`,
    busyMsg: 'Writing study notes… (this can take a minute)',
  });
}

function openSubjectOverview(deckId, name) {
  return _openMarkdownDoc({
    title: `${name} — overview`,
    getPath: `/api/study/decks/${deckId}/overview`,
    postPath: `/api/study/decks/${deckId}/overview`,
    field: 'overview',
    confirmMsg: `No overview for “${name}” yet. Generate one now? (uses the AI model)`,
    busyMsg: 'Writing subject overview…',
  });
}

// "Explain further": material-grounded theory for a question or card, shown in
// the viewer with a "Where to review" footer (page link + notes section).
async function openExplainFurther(kind, id) {
  const path = kind === 'card'
    ? `/api/study/cards/${id}/explain-further`
    : `/api/study/questions/${id}/explain-further`;
  const v = _viewerShell('Explain further',
    '<button class="study-btn small" id="study-ef-regen">Regenerate</button>',
    '<div class="study-viewer-body" id="study-viewer-body"></div>');
  const body = v.querySelector('#study-viewer-body');
  const load = async (refresh) => {
    body.innerHTML = '<div class="study-empty">Pulling the theory from your material…</div>';
    try {
      const r = await jpost(path + (refresh ? '?refresh=1' : ''), {});
      _renderMarkdownInto(body, r.explanation);
    } catch (e) { body.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; }
  };
  v.querySelector('#study-ef-regen').addEventListener('click', () => load(true));
  load(false);
}

function _locLabel(l) {
  return (l.name || l.label || 'file') + (l.page ? `, p.${l.page}` : '');
}

// Web sources are absolute URLs; material files are app-relative.
function _openLoc(l) {
  if (!l || !l.url) return;
  // Material citations open in the pane so consulting never leaves the app;
  // web sources are external and still get a tab.
  if (l.material_id) return openMaterialViewer(l.material_id, l.page, _locLabel(l));
  const u = /^https?:\/\//i.test(l.url) ? l.url : `${API}${l.url}`;
  window.open(u, '_blank', 'noopener');
}

// When several files/sources are relevant, a chooser of buttons (each opens in
// a new tab).
function openFileChooser(locations) {
  const inner = `<div class="study-viewer-body">
    <p class="study-subtle" style="margin-top:0;">Open one:</p>
    <div class="study-form-row">
      ${locations.map((l, i) => `<button class="study-btn" data-loc="${i}">${esc(_locLabel(l))}</button>`).join('')}
    </div></div>`;
  const v = _viewerShell('Open', '', inner);
  v.querySelectorAll('[data-loc]').forEach(b => b.addEventListener('click', () => {
    _openLoc(locations[parseInt(b.dataset.loc, 10)]);
  }));
}

// Consult = locate the file+page with the content to answer this question.
// First click searches and cites; the button then becomes "Open file".
// Consulting before answering counts like a hint (preserves retrieval effort).
async function consultAction(q, penalize) {
  const p = S.practice;
  if (!p || p.consultBusy) return;
  // Already located -> open the file(s)/source(s).
  if (p.consult && p.consult.locations && p.consult.locations.length) {
    const locs = p.consult.locations;
    if (locs.length === 1) _openLoc(locs[0]);
    else openFileChooser(locs);
    return;
  }
  if (penalize) p.consulted = true;   // looking it up before answering = a hint
  p.consultBusy = true; renderPractice();
  try {
    const r = await jpost(`/api/study/questions/${q.id}/locate`, {});
    p.consult = r;
    if (r.source === 'web') {
      toast('No course material covers it — found theory on the web.', true);
    } else if (!r.locations || !r.locations.length) {
      toast('No material covers it directly — generated a hint instead.', true);
    }
  } catch (e) { toast(e.message, true); }
  p.consultBusy = false; renderPractice();
}

function renderMaterialList() {
  const wrap = body()?.querySelector('#study-mat-list');
  const s = S.subject;
  if (!wrap || !s) return;
  if (!s.materials.length) {
    wrap.innerHTML = '<div class="study-empty">No materials yet.</div>';
    return;
  }
  wrap.innerHTML = s.materials.map(m => {
    const practice = m.category === 'exam';
    const size = m.page_count ? `${m.page_count} pages` : `${(m.char_count / 1000).toFixed(1)}k chars`;
    return `
    <div class="study-row study-mat-row">
      <span class="study-mat-name" title="${esc(m.name)}"><b>${esc(m.name)}</b>
        <span class="study-subtle"> · ${m.kind} · ${size} · ${m.question_count} questions${m.thin_text ? ' · <span title="Almost no text layer (scan or formula images). Transcribe it so notes, consult and search can read it.">scan</span>' : ''}</span></span>
      <span class="study-row-actions">
        <select class="study-cat" data-cat="${m.id}" title="Theory (notes, slides, textbook): new questions are generated from it, and Consult / Explain-further search it for theory. Practice (exam, problem set): its real questions are extracted faithfully.">
          <option value="theory" ${!practice ? 'selected' : ''}>Theory</option>
          <option value="exam" ${practice ? 'selected' : ''}>Practice (exam / problem set)</option>
        </select>
        ${s.extracting.has(m.id)
          ? '<span class="study-subtle">Working… (vision can take a few minutes)</span>'
          : `${m.file_id ? `<button class="study-btn small" data-openfile="${m.id}" title="Open this file in the viewer">Open</button>` : ''}
             ${m.thin_text ? `<button class="study-btn small" data-transcribe="${m.id}" title="Vision OCR: transcribe the pages to text so notes, search and text extraction can read them">Transcribe</button>` : ''}
             <button class="study-btn small" data-notes="${m.id}" title="${m.has_summary ? 'View AI study notes for this material' : 'Generate AI study notes to consult while practising'}">${m.has_summary ? 'Notes' : 'Make notes'}</button>
             ${practice
               ? `<button class="study-btn small primary" data-extract="${m.id}" title="Pull the actual questions out of this paper, faithfully. PDFs are read page-by-page by the vision model when one is configured (formulas and scans included).">Extract questions</button>`
               : `<button class="study-btn small primary" data-author="${m.id}" title="Write new exam-style questions from this theory material">Generate questions</button>`}
             ${m.question_count ? `<button class="study-btn small" data-pracmat="${m.id}" title="Practice only the questions of this material">Practice</button>` : ''}
             <button class="study-btn small danger" data-delmat="${m.id}" title="Remove this material (its questions stay in the bank)">✕</button>`}
      </span>
    </div>`;
  }).join('');
  wrap.onchange = async (e) => {
    const sel = e.target.closest('[data-cat]');
    if (!sel) return;
    const id = sel.dataset.cat;
    try {
      await jput(`/api/study/materials/${id}/category`, { category: sel.value });
      const m = s.materials.find(x => x.id === id);
      if (m) m.category = sel.value;
      toast(sel.value === 'exam' ? 'Practice material — its questions get extracted' : 'Theory material — questions get generated from it');
      renderMaterialList();   // the action buttons depend on the category
    } catch (err) { toast(err.message, true); }
  };
  wrap.onclick = async (e) => {
    const ex = e.target.closest('[data-extract]')?.dataset.extract;
    const au = e.target.closest('[data-author]')?.dataset.author;
    const tr = e.target.closest('[data-transcribe]')?.dataset.transcribe;
    const delBtn = e.target.closest('[data-delmat]');
    const of = e.target.closest('[data-openfile]')?.dataset.openfile;
    const nt = e.target.closest('[data-notes]')?.dataset.notes;
    const pm = e.target.closest('[data-pracmat]')?.dataset.pracmat;
    if (of) {
      const m = s.materials.find(x => x.id === of);
      // Only PDFs and images can be framed; anything else still gets a tab.
      if (m) (_previewable(m) ? openMaterialViewer(m.id, null, m.name)
                              : openFileTab(m.file_id));
      return;
    }
    if (nt) {
      const m = s.materials.find(x => x.id === nt);
      if (m) openMaterialNotes(m.id, m.name);
      return;
    }
    if (pm) {
      const m = s.materials.find(x => x.id === pm);
      startPractice(s.deck.id, 12, { materialId: pm, label: m ? m.name : 'material' });
      return;
    }
    if (delBtn) {
      armThen(delBtn, async () => {
        try { await jdel(`/api/study/materials/${delBtn.dataset.delmat}`); reloadSubject(); }
        catch (err) { toast(err.message, true); }
      }, 'Remove?');
      return;
    }
    const id = ex || au || tr;
    if (!id) return;
    if (S.subject.extracting.has(id)) return;   // already running for this material
    // Set-based so several materials can run in parallel.
    S.subject.extracting.add(id);
    renderMaterialList();
    try {
      if (tr) {
        const r = await jpost(`/api/study/materials/${id}/transcribe`, {});
        toast(`Transcribed ${r.pages} page(s) → ${(r.char_count / 1000).toFixed(1)}k chars`
          + (r.pages_failed ? ` (${r.pages_failed} page(s) failed)` : ''), !!r.pages_failed);
      } else {
        // Practice materials are extracted (vision by default for PDFs, decided
        // server-side); theory materials get authored questions.
        const res = await jpost(`/api/study/materials/${id}/extract`,
          { mode: au ? 'author' : 'extract', count: 15 });
        const cov = res.coverage;
        const covNote = cov && cov.missing && cov.missing.length
          ? ` — could not extract question(s) ${cov.missing.join(', ')}; they may need manual entry`
          : (cov && cov.matched != null ? ` — all ${cov.expected} detected questions covered` : '');
        const pageNote = res.pages && res.pages.truncated
          ? ` — only the first ${res.pages.pages} of ${res.pages.total_pages} pages were read` : '';
        const dupNote = res.duplicates ? ` (${res.duplicates} already in bank, skipped)` : '';
        const head = res.created === 0 && res.duplicates
          ? 'No new questions — everything was already in the bank'
          : `${res.created} questions added`;
        toast(`${head}${res.vision ? ' (vision)' : ''}${dupNote}${res.chunk_errors ? ` (${res.chunk_errors} batch(es) failed)` : ''}${covNote}${pageNote}`,
          !!((cov && cov.missing && cov.missing.length) || pageNote));
      }
    } catch (err) { toast(err.message, true); }
    S.subject?.extracting.delete(id);
    if (S.subject) reloadSubject();   // leaves S.subject.extracting intact for still-running ones
  };
}

// Rendered-HTML cache for question rows. The markdown+KaTeX pipeline is heavy,
// so memoize each row's HTML by id+content: re-renders (suspend, delete, search,
// pagination) reuse the HTML instead of re-running KaTeX over the whole bank.
// Content is part of the key, so edited text renders fresh; bounded to cap memory.
const _qhtmlCache = new Map();
function _qhtml(q) {
  const key = q.id + '::' + (q.question || '');
  let html = _qhtmlCache.get(key);
  if (html === undefined) {
    html = _mdInline(q.question);
    if (_qhtmlCache.size > 800) _qhtmlCache.clear();
    _qhtmlCache.set(key, html);
  }
  return html;
}

function _updateQuestionCounts() {
  const s = S.subject;
  const c = body()?.querySelector('#study-subj-counts');
  if (c && s) c.textContent = `${s.questions.length} questions · ${s.cards.length} cards`;
}

function _disarmDel(btn) {
  if (!btn) return;
  btn.dataset.armed = '';
  btn.classList.remove('armed');
  btn.textContent = btn._label || '✕';
}

// Two-click confirm for destructive buttons. After a few native dialogs
// browsers offer "prevent additional dialogs", after which confirm() silently
// returns false and deletes appear to do nothing — so no window.confirm here.
// First click arms the button (label changes), second click within 3.5s runs fn.
function armThen(btn, fn, label = 'Sure?') {
  if (!btn) return;
  if (btn.dataset.armed === '1') { clearTimeout(btn._disarmT); _disarmDel(btn); return fn(); }
  btn._label = btn._label || btn.textContent;
  btn.dataset.armed = '1';
  btn.classList.add('armed');
  btn.textContent = label;
  btn._disarmT = setTimeout(() => _disarmDel(btn), 3500);
  return undefined;
}

function renderQuestionList() {
  const wrap = body()?.querySelector('#study-q-list');
  const s = S.subject;
  if (!wrap || !s) return;
  const f = s.qFilter.toLowerCase();
  const rows = s.questions.filter(q => (!s.qMaterial || q.material_id === s.qMaterial) && (!f ||
    q.question.toLowerCase().includes(f) || (q.topic || '').toLowerCase().includes(f)));
  if (!rows.length) {
    wrap.innerHTML = '<div class="study-empty">No questions yet — extract some from a material above.</div>';
    return;
  }
  // Only render a page at a time — rendering all (often 100+) rows through
  // markdown+KaTeX at once is what pegged CPU and bloated the DOM.
  const limit = s.qLimit || 40;
  const shown = rows.slice(0, limit);
  wrap.innerHTML = shown.map(q => `
    <div class="study-cardrow ${q.suspended ? 'suspended' : ''}">
      <span class="study-qchip ${q.qtype}">${q.qtype}</span>
      <span class="front study-md" style="flex:2;">${_qhtml(q)}</span>
      <span class="study-state">${esc(q.topic || '')}${q.topic ? ' · ' : ''}${esc(q.difficulty)}
        · ${esc(q.state)}${q.state !== 'new' ? ` · due ${fmtDue(q.due)}` : ''}${q.lapses ? ` · ${q.lapses}✗` : ''}</span>
      ${_originalQuestionButton(q, true)}
      <button class="study-btn small" data-qsusp="${q.id}" title="${q.suspended ? 'Unsuspend' : 'Suspend'}">${q.suspended ? '▶' : '⏸'}</button>
      <button class="study-btn small danger" data-qdel="${q.id}" title="Delete">✕</button>
    </div>`).join('')
    + (rows.length > shown.length
      ? `<div class="study-form-row" style="justify-content:center;margin-top:8px;">
           <button class="study-btn small" id="study-q-more">Show more (${shown.length} of ${rows.length})</button>
         </div>`
      : '');
  wrap.onclick = async (e) => {
    if (e.target.closest('#study-q-more')) { s.qLimit = (s.qLimit || 40) + 40; renderQuestionList(); return; }
    const su = e.target.closest('[data-qsusp]')?.dataset.qsusp;
    const delBtn = e.target.closest('[data-qdel]');
    try {
      if (su) {
        const q = s.questions.find(x => x.id === su);
        await jput(`/api/study/questions/${su}`, { suspended: !q.suspended });
        q.suspended = !q.suspended;   // local update — avoids a full subject reload + re-render
        renderQuestionList();
      } else if (delBtn) {
        const id = delBtn.dataset.qdel;
        // Two-click confirm instead of window.confirm(): after a few native
        // dialogs browsers offer "prevent additional dialogs", after which
        // confirm() silently returns false and deletes appear to do nothing.
        // First click arms this button, second click (within 3.5s) deletes.
        if (delBtn.dataset.armed !== '1') {
          wrap.querySelectorAll('[data-qdel].armed').forEach(_disarmDel);
          delBtn.dataset.armed = '1';
          delBtn.classList.add('armed');
          delBtn.textContent = 'Delete?';
          clearTimeout(delBtn._disarmT);
          delBtn._disarmT = setTimeout(() => _disarmDel(delBtn), 3500);
          return;
        }
        clearTimeout(delBtn._disarmT);
        await jdel(`/api/study/questions/${id}`);
        s.questions = s.questions.filter(x => x.id !== id);   // local removal — no heavy reload
        renderQuestionList();
        _updateQuestionCounts();
      }
    } catch (err) { toast(err.message, true); }
  };
}

// Bank maintenance for one subject. These passes used to be reachable only
// through the agent; each is scoped to this subject by deck_id. Dedup and audit
// change the bank in bulk, so both are armed two-click buttons.
const TIDY_ACTIONS = [
  { key: 'dedup', label: 'Remove duplicates', arm: true,
    hint: 'Deletes repeated questions, keeping the best copy of each.',
    path: 'dedup', report: r => `${r.deleted} duplicate(s) removed` },
  { key: 'link_parts', label: 'Link multi-part problems', arm: false,
    hint: 'Groups parts of one problem so practice shows the earlier parts first.',
    path: 'link-parts', report: r => `${r.linked} part(s) linked of ${r.analyzed} analysed` },
  { key: 'backfill', label: 'Recover problem setups', arm: false,
    hint: 'Rebuilds the shared stem for multi-part questions that lost it. Slow (AI).',
    path: 'backfill-context', report: r => `${r.filled} of ${r.scanned} filled` },
  { key: 'audit', label: 'Suspend leaked answers', arm: true,
    hint: 'Suspends "questions" that state their own answer. Reversible in the bank. Slow (AI).',
    path: 'audit-questions', report: r => `${r.suspended} suspended of ${r.scanned} scanned` },
  { key: 'detect_chapters', label: 'Detect chapters', arm: false, perMaterial: true,
    hint: 'Split each document that has several chapters, so you can practise one at a time. Documents with a single chapter are left alone. Slow (AI).',
    report: r => `${r.split} document(s) split, ${r.single} single-chapter, ${r.skipped} too small` },
  { key: 'group_themes', label: 'Group themes', arm: false,
    hint: 'Cluster this subject\u2019s topics into a handful of themes you can drill across every document. Slow (AI).',
    path: 'group-themes', report: r => `${r.themes} themes over ${r.labelled} questions` },
  { key: 'reformat', label: 'Reformat maths', arm: false,
    hint: 'Rewrites questions and cards to LaTeX + Markdown. Idempotent. Slow (AI).',
    path: 'reformat', report: r => `${r.questions_reformatted} question(s), ${r.cards_reformatted} card(s)` },
];

function renderTidyBank() {
  const box = body()?.querySelector('#study-tidy');
  const s = S.subject;
  if (!box || !s) return;
  box.innerHTML = `
    <div class="study-subtle" style="margin:4px 0 8px;">
      Maintenance passes for <b>${esc(s.deck.name)}</b> only. The AI passes take a while.
    </div>
    ${TIDY_ACTIONS.map(a => `
      <div class="study-row">
        <span class="grow">
          <b style="font-size:12.5px;">${a.label}</b>
          <div class="study-subtle">${a.hint}</div>
        </span>
        <span class="study-subtle" data-tidy-out="${a.key}">${esc(s.tidyOut?.[a.key] || '')}</span>
        <button class="study-btn small" data-tidy="${a.key}">Run</button>
      </div>`).join('')}`;

  box.querySelectorAll('[data-tidy]').forEach(btn => {
    const act = TIDY_ACTIONS.find(a => a.key === btn.dataset.tidy);
    const run = async () => {
      const out = box.querySelector(`[data-tidy-out="${act.key}"]`);
      btn.disabled = true; btn.textContent = 'Running…'; out.textContent = '';
      try {
        let res;
        if (act.perMaterial) {
          // Chapters belong to a document, so this pass walks the subject's
          // materials and rolls the outcomes up into one line.
          const roll = { split: 0, single: 0, skipped: 0 };
          for (const m of (s.materials || [])) {
            try {
              const r = await jpost(`/api/study/materials/${encodeURIComponent(m.id)}/detect-chapters`, {});
              if (r.skipped) roll.skipped += 1;
              else if ((r.chapters || 0) >= 2) roll.split += 1;
              else roll.single += 1;
            } catch { roll.skipped += 1; }
          }
          res = roll;
        } else {
          const url = act.key === 'link_parts'
            ? `/api/study/decks/${encodeURIComponent(s.deck.id)}/link-parts`
            : (act.key === 'group_themes'
              ? `/api/study/decks/${encodeURIComponent(s.deck.id)}/group-themes`
              : `/api/study/${act.path}?deck_id=${encodeURIComponent(s.deck.id)}`);
          res = await jpost(url, {});
        }
        const msg = act.report(res || {});
        (S.subject.tidyOut ||= {})[act.key] = msg;
        out.textContent = msg;
        await reloadSubject();
      } catch (e) { toast(e.message, true); }
      btn.disabled = false; btn.textContent = 'Run';
    };
    btn.addEventListener('click', act.arm ? () => armThen(btn, run) : run);
  });
}

function renderProposals() {
  const wrap = body()?.querySelector('#study-proposals');
  const s = S.subject;
  if (!wrap || !s) return;
  if (!s.proposals) { wrap.innerHTML = ''; return; }
  wrap.innerHTML = `
    <div style="margin-top:10px;">
      <div class="study-form-row">
        <b style="font-size:12.5px;">${s.proposals.filter(p => p.checked).length}/${s.proposals.length} selected</b>
        ${s.proposalSource ? `<span class="study-subtle">from ${s.proposalSource === 'notes' ? 'study notes' : 'the material text'}</span>` : ''}
        <button class="study-btn small" id="study-prop-all">Toggle all</button>
        <button class="study-btn small primary" id="study-prop-add">Add selected</button>
        <button class="study-btn small" id="study-prop-discard">Discard</button>
      </div>
      ${s.proposals.map((p, i) => `
        <label class="study-cardrow" style="cursor:pointer;">
          <input type="checkbox" data-prop="${i}" ${p.checked ? 'checked' : ''}>
          <span class="front">${esc(p.front)}</span>
          <span class="back">${esc(p.back)}</span>
        </label>`).join('')}
    </div>`;
  wrap.querySelector('#study-prop-all').addEventListener('click', () => {
    const any = s.proposals.some(p => !p.checked);
    s.proposals.forEach(p => { p.checked = any; });
    renderProposals();
  });
  wrap.querySelector('#study-prop-discard').addEventListener('click', () => {
    s.proposals = null; renderProposals();
  });
  wrap.querySelector('#study-prop-add').addEventListener('click', async () => {
    const chosen = s.proposals.filter(p => p.checked).map(p => ({ front: p.front, back: p.back }));
    if (!chosen.length) return;
    try {
      const res = await jpost(`/api/study/decks/${s.deck.id}/cards`, { cards: chosen, source: 'ai' });
      toast(`Added ${res.created} cards`);
      s.proposals = null;
      reloadSubject();
    } catch (err) { toast(err.message, true); }
  });
  wrap.onchange = (e) => {
    const cb = e.target.closest('[data-prop]');
    if (cb) s.proposals[+cb.dataset.prop].checked = cb.checked;
  };
}

function renderCardList() {
  const wrap = body()?.querySelector('#study-card-list');
  const s = S.subject;
  if (!wrap || !s) return;
  if (!s.cards.length) {
    wrap.innerHTML = '<div class="study-empty">No flashcards yet.</div>';
    return;
  }
  wrap.innerHTML = s.cards.slice(0, 200).map(c => `
    <div class="study-cardrow ${c.suspended ? 'suspended' : ''}">
      <span class="front">${esc(c.front)}</span>
      <span class="back">${esc(c.back)}</span>
      <span class="study-state">${esc(c.state)}${c.state !== 'new' ? ` · due ${fmtDue(c.due)}` : ''}${c.lapses ? ` · ${c.lapses}✗` : ''}</span>
      <button class="study-btn small" data-edit="${c.id}" title="Edit">✎</button>
      <button class="study-btn small" data-susp="${c.id}" title="${c.suspended ? 'Unsuspend' : 'Suspend'}">${c.suspended ? '▶' : '⏸'}</button>
      <button class="study-btn small danger" data-delc="${c.id}" title="Delete">✕</button>
    </div>`).join('');
  wrap.onclick = async (e) => {
    const editId = e.target.closest('[data-edit]')?.dataset.edit;
    const suspId = e.target.closest('[data-susp]')?.dataset.susp;
    const delId = e.target.closest('[data-delc]')?.dataset.delc;
    try {
      if (editId) {
        const c = s.cards.find(x => x.id === editId);
        // Inline edit form (Phase 4.2: replaces native prompt())
        const wrap2 = el.querySelector(`[data-edit="${editId}"]`)?.closest('.study-row');
        if (wrap2) {
          wrap2.outerHTML = `<div class="study-card-edit" role="dialog" aria-label="Edit card">
            <input class="study-input" id="study-edit-front" value="${esc(c.front)}" placeholder="Front" style="width:100%;margin-bottom:6px;" aria-label="Card front">
            <textarea class="study-textarea" id="study-edit-back" style="min-height:80px;margin-bottom:6px;" aria-label="Card back">${esc(c.back)}</textarea>
            <div class="study-form-row">
              <button class="study-btn primary" id="study-edit-save" data-id="${editId}">Save</button>
              <button class="study-btn" id="study-edit-cancel">Cancel</button>
            </div>
          </div>`;
          el.querySelector('#study-edit-save')?.addEventListener('click', async () => {
            const front = el.querySelector('#study-edit-front').value.trim();
            const back = el.querySelector('#study-edit-back').value.trim();
            if (!front) return toast('Front cannot be empty', true);
            await jput(`/api/study/cards/${editId}`, { front, back });
            reloadSubject();
          });
          el.querySelector('#study-edit-cancel')?.addEventListener('click', () => reloadSubject());
          el.querySelector('#study-edit-front')?.focus();
        }
      } else if (suspId) {
        const c = s.cards.find(x => x.id === suspId);
        await jput(`/api/study/cards/${suspId}`, { suspended: !c.suspended });
        reloadSubject();
      } else if (delId) {
        armThen(e.target.closest('[data-delc]'), async () => {
          try { await jdel(`/api/study/cards/${delId}`); reloadSubject(); }
          catch (err) { toast(err.message, true); }
        });
      }
    } catch (err) { toast(err.message, true); }
  };
}

// ---------------------------------------------------------------------------
// CARDS (flashcard review) — unchanged FSRS player
// ---------------------------------------------------------------------------

async function startReview(deckId = null) {
  setTabSilent('review');
  S.review = { queue: [], idx: 0, revealed: false, deckId, startTs: Date.now(),
               counts: { 1: 0, 2: 0, 3: 0, 4: 0 }, cardShownTs: Date.now(), loading: true };
  renderReview();
  try {
    const res = await jget(`/api/study/queue${deckId ? `?deck_id=${deckId}` : ''}`);
    S.review.queue = res.queue;
  } catch (e) { toast(e.message, true); }
  S.review.loading = false;
  renderReview();
}

async function renderReview() {
  const el = body();
  const r = S.review;
  if (!r) {
    el.innerHTML = '<div class="study-empty">Loading…</div>';
    let decks = [];
    try { decks = (await jget('/api/study/decks')).decks; } catch { }
    if (_tab !== 'review' || S.review) return;
    const total = decks.reduce((a, d) => a + d.due_count + d.new_available, 0);
    el.innerHTML = `
      <div class="study-card-stage">
        <div style="font-size:15px;margin-bottom:18px;">${total} cards waiting across ${decks.length} subject${decks.length === 1 ? '' : 's'}</div>
        <div class="study-rate-row" style="margin-top:0;">
          <button class="study-btn primary" id="study-review-all" ${total === 0 ? 'disabled' : ''}>Review everything due</button>
        </div>
        <div style="margin-top:22px;">${decks.map(d => `
          <div class="study-row" style="max-width:430px;margin:0 auto 6px;">
            <span class="grow" style="text-align:left;">${esc(d.name)}</span>
            <span class="study-badge due">${d.due_count}</span>
            <span class="study-badge new">${d.new_available}</span>
            <button class="study-btn small" data-rev="${d.id}" ${d.due_count + d.new_available === 0 ? 'disabled' : ''}>Start</button>
          </div>`).join('')}</div>
        <div class="study-tip" style="text-align:left;max-width:430px;margin:26px auto 0;">Keyboard: <b>Space</b> reveals, <b>1–4</b> rates (Again / Hard / Good / Easy). Rate honestly — the scheduler can only fix what you report.</div>
      </div>`;
    el.querySelector('#study-review-all')?.addEventListener('click', () => startReview(null));
    el.onclick = (e) => {
      const id = e.target.closest('[data-rev]')?.dataset.rev;
      if (id) startReview(id);
    };
    return;
  }

  if (r.loading) { el.innerHTML = '<div class="study-empty">Loading queue…</div>'; return; }

  const card = r.queue[r.idx];
  if (!card) {
    const mins = Math.max(1, Math.round((Date.now() - r.startTs) / 60000));
    const done = r.counts[1] + r.counts[2] + r.counts[3] + r.counts[4];
    const succ = done ? Math.round((1 - r.counts[1] / done) * 100) : 0;
    el.innerHTML = `
      <div class="study-card-stage">
        <div style="font-size:22px;margin-bottom:6px;">Session complete</div>
        <div style="opacity:0.65;font-size:13px;">${done} reviews · ${succ}% recalled · ~${mins} min</div>
        <div class="study-chips" style="justify-content:center;margin-top:18px;">
          <div class="study-chip"><b>${r.counts[1]}</b><span>again</span></div>
          <div class="study-chip"><b>${r.counts[2]}</b><span>hard</span></div>
          <div class="study-chip"><b>${r.counts[3]}</b><span>good</span></div>
          <div class="study-chip"><b>${r.counts[4]}</b><span>easy</span></div>
        </div>
        <div class="study-rate-row">
          <button class="study-btn" id="study-review-again">Check for more</button>
          <button class="study-btn primary" id="study-review-home">Back to Today</button>
        </div>
        ${succ > 0 && succ < 70 ? '<div class="study-tip" style="text-align:left;">Success under 70% — the gap is too wide. Shrink it: smaller cards, or re-read the source once and re-test today.</div>' : ''}
        ${succ > 92 && done >= 10 ? '<div class="study-tip" style="text-align:left;">Over 92% success — reviews may be too easy to be efficient. Consider raising new cards/day or lowering desired retention.</div>' : ''}
      </div>`;
    el.querySelector('#study-review-again').addEventListener('click', () => startReview(r.deckId));
    el.querySelector('#study-review-home').addEventListener('click', () => { S.review = null; setTab('today'); });
    return;
  }

  const progress = Math.round((r.idx / r.queue.length) * 100);
  const ratings = [[1, 'Again'], [2, 'Hard'], [3, 'Good'], [4, 'Easy']];
  el.innerHTML = `
    <div class="study-card-stage">
      <div class="study-progress"><i style="width:${progress}%"></i></div>
      <div class="study-card-front study-md">${_md(card.front)}</div>
      ${r.revealed ? `<div class="study-card-back study-md">${_md(card.back)}</div>
        ${card.notes ? `<div class="study-card-meta">${esc(card.notes)}</div>` : ''}
        <div style="margin-top:10px;"><button class="study-btn small" id="study-card-explain" title="Pull the underlying theory from your subject's material, with where to review it">Explain further</button></div>` : ''}
      <div class="study-card-meta">${r.idx + 1}/${r.queue.length} · ${esc(card.state)}${card.lapses ? ` · ${card.lapses} lapses` : ''}</div>
      <div class="study-rate-row">
        ${r.revealed
          ? ratings.map(([n, label]) => `
              <button class="study-rate" data-r="${n}">
                <b>${label}</b><span>${esc(card.preview?.[n] ?? '')}</span>
              </button>`).join('')
          : '<button class="study-btn primary" id="study-reveal" style="min-width:180px;padding:11px;">Show answer <span style="opacity:0.5;">(Space)</span></button>'}
      </div>
    </div>`;
  el.querySelector('#study-reveal')?.addEventListener('click', revealCard);
  el.querySelector('#study-card-explain')?.addEventListener('click', () => openExplainFurther('card', card.id));
  el.querySelectorAll('.study-rate').forEach(b =>
    b.addEventListener('click', () => rateCard(parseInt(b.dataset.r, 10))));
}

function revealCard() {
  if (!S.review || S.review.revealed) return;
  S.review.revealed = true;
  renderReview();
}

async function rateCard(rating) {
  const r = S.review;
  if (!r || !r.revealed) return;
  const card = r.queue[r.idx];
  r.counts[rating] += 1;
  r.revealed = false;
  const duration = Date.now() - r.cardShownTs;
  r.cardShownTs = Date.now();
  if (rating <= 2 && card.state !== 'review') {
    r.queue.push({ ...card, preview: null });
  } else if (rating === 1) {
    r.queue.push({ ...card, state: 'relearning', preview: null });
  }
  r.idx += 1;
  renderReview();
  try {
    await postDurably(
      'review',
      `/api/study/cards/${card.id}/review`,
      { rating, duration_ms: duration },
      'rv',
      card.id,
    );
  } catch (e) { toast(`Review not saved: ${e.message}`, true); }
}

function reviewKeydown(e) {
  if (!S.review || S.review.loading) return;
  if (e.target.closest('input, textarea, select')) return;
  if (e.code === 'Space') { e.preventDefault(); revealCard(); return; }
  if (['1', '2', '3', '4'].includes(e.key) && S.review.revealed) {
    e.preventDefault();
    rateCard(parseInt(e.key, 10));
  }
}

// Decide whether a document-level keydown belongs to Study.
//
// The keydown listener is installed on `document` in openPanel and only
// removed by _forceClose. Minimizing goes through the modal manager, which
// just adds `hidden`/`modal-minimized` to the pane — so without this guard a
// minimized Study kept rating cards and answering Escape from anywhere on the
// page. Visibility is checked through the modal manager's own flag first, and
// the classes it sets as a fallback for when that module is unavailable.
function _studyAcceptsShortcut(e) {
  if (!_open || !_pane) return false;

  // A minimized window is still in the DOM, just hidden.
  try { if (Modals.isMinimized && Modals.isMinimized('study-pane')) return false; }
  catch { /* modal manager optional */ }
  if (_pane.classList?.contains('hidden')
      || _pane.classList?.contains('modal-minimized')) return false;

  // Ctrl/Cmd/Alt chords belong to the browser or the app, never to a
  // single-letter study shortcut.
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  // Mid-IME keystrokes are text being composed, not commands.
  if (e.isComposing || e.keyCode === 229) return false;

  const t = e.target;
  // A neutral target (nothing focused) means no other surface claimed the
  // key, so a visible Study may take it. Anything else has to be ours.
  const neutral = !t || t === document.body || t === document.documentElement;
  if (neutral) return true;
  if (!_pane.contains(t)) return false;

  // Inside Study, but still editing: the old guard missed contenteditable,
  // which is what document editors use.
  if (t.isContentEditable) return false;
  if (typeof t.closest === 'function'
      && t.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]')) {
    return false;
  }
  return true;
}

// Click a control inside the Study body on behalf of a keyboard shortcut.
// The pane can be closed or detached (body() is null) while a document-level
// keydown listener is still installed, so the lookup has to be null-safe; and
// a shortcut must never activate a control the click path has disabled, which
// is what guards an in-flight submission.
function clickControl(selector) {
  const el = body()?.querySelector(selector);
  if (el && !el.disabled) el.click();
}

function practiceKeydown(e) {
  if (!S.practice || S.practice.loading) return;
  if (e.target.closest('input, textarea, select')) return;
  const p = S.practice;
  const q = p.queue[p.idx];
  if (!q) return;

  // MCQ option selection: 1-n selects, Enter submits
  if (q.qtype === 'mcq' && !p.result) {
    if (/^[1-9]$/.test(e.key)) {
      const idx = parseInt(e.key, 10) - 1;
      if (idx < (q.options || []).length) {
        e.preventDefault();
        p.choice = idx;
        renderPractice();
      }
    } else if (e.key === 'Enter' && p.choice != null) {
      e.preventDefault();
      // Route through the real control so the shortcut inherits the
      // disabled/in-flight guard the click path already has, instead of
      // starting a second submission.
      clickControl('#study-prac-submit');
    }
  }

  // H = hint, C = consult, N = next (when result shown)
  if (e.key === 'h' || e.key === 'H') {
    if (!p.result && p.hints.length < 3 && !p.hintBusy) {
      e.preventDefault();
      clickControl('#study-prac-hint');
    }
  }
  if (e.key === 'c' || e.key === 'C') {
    e.preventDefault();
    clickControl('#study-prac-consult');
  }
  if ((e.key === 'n' || e.key === 'N') && p.result) {
    e.preventDefault();
    clickControl('#study-prac-next');
  }
}

// ---------------------------------------------------------------------------
// PRACTICE (question bank)
// ---------------------------------------------------------------------------

// `scope`: optional {materialId, topics (array|csv), label} — practice only one
// material's questions, or an exam-plan block's topics.
// `mock`: optional {minutes, label} — a timed, closed-book full-format pass.
// No hints, no consult, no per-question feedback: you answer everything, predict
// your score, and only then see the marking. That prediction gap is the
// calibration data the exam plan's mock blocks are asking for.
async function startPractice(deckId = null, limit = 12, scope = null, mock = null) {
  setTabSilent('practice');
  stopMockTimer();
  S.practice = { queue: [], idx: 0, deckId, scope: scope || null, loading: true,
                 mock: mock ? { minutes: mock.minutes || 60, endsAt: null,
                                predicted: null, phase: 'answer' } : null,
                 phase: 'answer', confidence: null, choice: null, emptyArmed: false,
                 hints: [], hintBusy: false, answerDraft: '', result: null,
                 consulted: false, consult: null, consultBusy: false,
                 prereqs: null, prereqsFor: null, prereqsBusy: false,
                 explainText: null, explainBusy: false,
                 ask: [], askBusy: false,
                 log: [], startTs: Date.now(), qShownTs: Date.now(),
                 reengaged: false };
  renderPractice();
  try {
    const params = new URLSearchParams({ limit: String(limit) });
    if (mock) params.set('mock', 'true');
    if (deckId) params.set('deck_id', deckId);
    if (scope?.materialId) params.set('material_id', scope.materialId);
    if (scope?.topics) params.set('topics', Array.isArray(scope.topics) ? scope.topics.join(',') : scope.topics);
    if (scope?.chapter) params.set('chapter', scope.chapter);
    if (scope?.theme) params.set('theme', scope.theme);
    const res = await jget(`/api/study/practice/queue?${params}`);
    S.practice.queue = res.queue;
    if (res.topic_fallback) toast('No questions matched those topics — practising the whole subject instead');
  } catch (e) { toast(e.message, true); }
  S.practice.loading = false;
  if (S.practice.mock && S.practice.queue.length) startMockTimer();
  renderPractice();
}

// The mock clock runs off a wall-clock deadline, so it stays honest while the
// pane is closed or another tab is open; only the visible clock needs the DOM.
// Held module-level, not on S.practice, so the interval is still clearable
// after the session object is dropped (leaving practice, closing the pane).
let _mockTimer = null;

function startMockTimer() {
  const p = S.practice;
  if (!p?.mock) return;
  p.mock.endsAt = Date.now() + p.mock.minutes * 60000;
  stopMockTimer();
  _mockTimer = setInterval(() => {
    const cur = S.practice;
    if (!cur?.mock) return stopMockTimer();
    const left = mockRemainingSec();
    const clock = (_open && _tab === 'practice')
      ? body()?.querySelector('#study-mock-clock') : null;
    if (clock) clock.textContent = fmtClock(left);
    if (left <= 0) {
      stopMockTimer();
      cur.mock.phase = 'predict';
      cur.mock.timeUp = true;
      if (_open && _tab === 'practice') renderPractice();
    }
  }, 500);
}

function stopMockTimer() {
  if (_mockTimer) clearInterval(_mockTimer);
  _mockTimer = null;
}

function mockRemainingSec() {
  const m = S.practice?.mock;
  if (!m?.endsAt) return 0;
  return Math.max(0, Math.round((m.endsAt - Date.now()) / 1000));
}

function fmtClock(sec) {
  return `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
}

// What to practise, for one subject. The two axes answer different questions:
// a chapter is one document in its own order, a theme is one idea across every
// document. Each section is omitted when empty, so a subject of plain exam
// papers looks exactly as it did before any of this existed.
async function renderPracticePicker(deckId) {
  const el = body();
  el.innerHTML = '<div class="study-empty">Loading\u2026</div>';
  const deck = (S.decks || []).find(d => d.id === deckId);
  let g = { chapters: [], themes: [] };
  try { g = await jget(`/api/study/decks/${deckId}/groupings`); }
  catch { /* the picker still offers Everything */ }
  if (_tab !== 'practice' || S.practice) return;

  const chapterRows = (g.chapters || []).map(doc => `
    <div style="margin-bottom:12px;">
      <div class="study-subtle" style="margin-bottom:4px;">${esc(doc.material)}</div>
      ${doc.chapters.map(c => `
        <div class="study-row">
          <span class="grow">${esc(c.label)}</span>
          <span class="study-badge q">${c.count} q</span>
          <button class="study-btn small" data-chapter="${esc(c.label)}">Practice</button>
        </div>`).join('')}
    </div>`).join('');

  const themeRows = (g.themes || []).map(t => `
    <div class="study-row">
      <span class="grow">${esc(t.name)}</span>
      <span class="study-subtle">${t.count} q \u00b7 ${t.materials} document${t.materials === 1 ? '' : 's'}</span>
      <button class="study-btn small" data-study-theme="${esc(t.name)}">Practice</button>
    </div>`).join('');

  el.innerHTML = `
    <div style="max-width:620px;">
      <div class="study-form-row">
        <button class="study-btn small" id="study-pick-back">\u2190 Subjects</button>
        <b style="font-size:14px;">${esc(deck?.name || 'Practice')}</b>
      </div>
      <div class="study-rate-row" style="margin:14px 0 6px;">
        <button class="study-btn primary" id="study-pick-all">Everything \u00b7 ${deck?.q_due ?? 0} due</button>
      </div>
      ${chapterRows ? `<div class="study-section-title" style="margin-top:22px;">By chapter</div>
        <div class="study-subtle" style="margin-bottom:8px;">One document, in its own order.</div>${chapterRows}` : ''}
      ${themeRows ? `<div class="study-section-title" style="margin-top:22px;">By theme</div>
        <div class="study-subtle" style="margin-bottom:8px;">One idea, across every document in this subject.</div>${themeRows}` : ''}
      ${!chapterRows && !themeRows ? `<div class="study-subtle" style="margin-top:18px;">
        Nothing grouped yet \u2014 run \u201cDetect chapters\u201d or \u201cGroup themes\u201d from Tidy bank in the subject view.</div>` : ''}
    </div>`;

  el.querySelector('#study-pick-back').addEventListener('click', () => { S.practice = null; renderPractice(); });
  el.querySelector('#study-pick-all').addEventListener('click', () => startPractice(deckId));
  el.querySelectorAll('[data-chapter]').forEach(b => b.addEventListener('click', () =>
    startPractice(deckId, 12, { chapter: b.dataset.chapter, label: b.dataset.chapter })));
  el.querySelectorAll('[data-study-theme]').forEach(b => b.addEventListener('click', () =>
    startPractice(deckId, 12, { theme: b.dataset.studyTheme, label: b.dataset.studyTheme })));
}

async function renderPractice() {
  const el = body();
  const p = S.practice;

  if (!p) {
    el.innerHTML = '<div class="study-empty">Loading…</div>';
    let decks = [];
    try { decks = (await jget('/api/study/decks')).decks; S.decks = decks; } catch { }
    if (_tab !== 'practice' || S.practice) return;
    const totalQ = decks.reduce((a, d) => a + (d.q_due ?? 0) + (d.q_new ?? 0), 0);
    el.innerHTML = `
      <div class="study-card-stage">
        <div style="font-size:15px;margin-bottom:6px;">Question practice</div>
        <div class="study-subtle" style="max-width:480px;margin:0 auto 18px;">Exam-format retrieval from your extracted question banks. Due questions come first (spacing); new ones are mixed across topics (interleaving). Hints cost you — a hinted success reschedules sooner. Timed mocks start from a mock block in the Plan tab.</div>
        <div class="study-rate-row" style="margin-top:0;">
          <button class="study-btn primary" id="study-practice-all" ${totalQ === 0 ? 'disabled' : ''}>Practice everything</button>
        </div>
        <div style="margin-top:22px;">${decks.map(d => `
          <div class="study-row" style="max-width:460px;margin:0 auto 6px;">
            <span class="grow" style="text-align:left;">${esc(d.name)}</span>
            <span class="study-badge q">${d.q_due ?? 0} due</span>
            <span class="study-badge new">${d.q_new ?? 0} new</span>
            <button class="study-btn small" data-prac="${d.id}" ${(d.q_due ?? 0) + (d.q_new ?? 0) === 0 ? 'disabled' : ''}>Choose\u2026</button>
          </div>`).join('')}</div>
        ${totalQ === 0 ? '<div class="study-empty" style="margin-top:14px;">No questions yet — go to Subjects, add a material, and extract questions from it.</div>' : ''}
      </div>`;
    el.querySelector('#study-practice-all')?.addEventListener('click', () => startPractice(null));
    el.onclick = (e) => {
      const id = e.target.closest('[data-prac]')?.dataset.prac;
      if (id) renderPracticePicker(id);
    };
    return;
  }

  if (p.loading) { el.innerHTML = '<div class="study-empty">Loading questions…</div>'; return; }

  const q = p.queue[p.idx];
  // A mock is marked only after you commit to a prediction.
  // Nothing answered (an empty paper, or every question skipped) — there is
  // nothing to predict, so go straight to the summary.
  if (p.mock && (!q || p.mock.phase === 'predict')
      && p.mock.predicted === null && p.log.some(l => l.result)) {
    return renderMockPrediction();
  }
  if (!q || (p.mock && p.mock.phase === 'predict')) { return renderPracticeSummary(); }

  // Lazily fetch prerequisite parts (multi-part questions) for the context box.
  if (q.has_prereqs && p.prereqsFor !== q.id && !p.prereqsBusy) {
    p.prereqsBusy = true;
    jget(`/api/study/questions/${q.id}/prereqs`).then(r => {
      p.prereqs = r.prereqs || []; p.prereqsFor = q.id; p.prereqsBusy = false;
      const cur = S.practice && S.practice.queue[S.practice.idx];
      if (cur && cur.id === q.id) renderPractice();
    }).catch(() => { p.prereqsBusy = false; p.prereqsFor = q.id; });
  }
  const prereqs = (p.prereqsFor === q.id && p.prereqs) ? p.prereqs : [];

  const progress = Math.round((p.idx / p.queue.length) * 100);
  const isMcq = q.qtype === 'mcq';
  const res = p.result;
  const located = p.consult && p.consult.locations && p.consult.locations.length;
  const consultWeb = p.consult && p.consult.source === 'web';
  const consultLabel = p.consultBusy ? 'Locating…'
    : (located ? (consultWeb ? 'Open source' : 'Open file') : 'Consult');

  el.innerHTML = `
    <div class="study-q-wrap">
      <div class="study-progress"><i style="width:${progress}%"></i></div>
      <div class="study-form-row" style="margin-bottom:10px;">
        <span class="study-qchip ${q.qtype}">${q.qtype}</span>
        ${q.topic ? `<span class="study-qchip">${esc(q.topic)}</span>` : ''}
        <span class="study-qchip ${q.difficulty === 'hard' ? 'hard' : ''}">${esc(q.difficulty)}</span>
        ${p.scope?.label ? `<span class="study-qchip" title="Practice scope">${esc(p.scope.label)}</span>` : ''}
        <span style="flex:1;"></span>
        ${p.mock ? `<span class="study-qchip" id="study-mock-clock" title="Time left in this mock">${fmtClock(mockRemainingSec())}</span>` : ''}
        <span class="study-subtle">${p.idx + 1}/${p.queue.length}</span>
      </div>
      ${prereqs.length ? `<div class="study-prereq">
        <div class="study-prereq-title">Earlier in this problem</div>
        ${prereqs.map(pr => `<div class="study-prereq-item">
          <div class="study-md">${pr.number ? '<b>' + esc(pr.number) + '.</b> ' : ''}${_mdInline(pr.question)}</div>
          ${pr.your_answer ? `<div class="study-subtle study-md" style="margin-top:3px;">Your answer: ${_mdInline(pr.your_answer)}</div>` : ''}
          ${pr.correct ? `<div class="study-md" style="margin-top:3px;opacity:0.8;"><b>Answer:</b> ${_mdInline(pr.correct)}</div>` : ''}
        </div>`).join('')}
      </div>` : ''}
      ${q.context ? `<div class="study-context">
        <div class="study-context-title">Problem setup</div>
        <div class="study-md">${_md(q.context)}</div>
      </div>` : ''}
      <div class="study-card-front study-md" style="font-size:16px;">${_md(q.question)}</div>

      ${isMcq ? `<div style="margin-top:16px;" id="study-opts">
        ${(q.options || []).map((o, i) => {
          let cls = 'study-opt';
          if (!res && p.choice === i) cls += ' sel';
          if (res) {
            if (i === res.correct_index) cls += ' right';
            else if (i === p.choice && !res.correct) cls += ' wrong';
          }
          return `<button class="${cls} study-md" data-opt="${i}" ${res ? 'disabled' : ''}>${_mdInline(o)}</button>`;
        }).join('')}
      </div>` : `
        <textarea class="study-textarea" id="study-prac-answer" style="margin-top:14px;min-height:110px;"
          placeholder="Answer from memory — method and result. No peeking." ${res ? 'disabled' : ''}>${esc(p.answerDraft)}</textarea>`}

      ${p.hints.map((h, i) => `<div class="study-hint study-md"><b>Hint ${i + 1}:</b> ${_mdInline(h)}</div>`).join('')}
      ${located
        ? `<div class="study-hint">${consultWeb ? '🌐 From the web' : '📄 Relevant material'}: ${p.consult.locations.map(l => esc(_locLabel(l))).join(' · ')} — use “${consultWeb ? 'Open source' : 'Open file'}”.</div>`
        : ''}
      ${consultWeb && p.consult.hint
        ? `<div class="study-hint study-md">${_md(p.consult.hint)}</div>` : ''}
      ${p.consult && !located && !consultWeb && p.consult.hint
        ? `<div class="study-hint study-md"><b>Consult hint:</b> ${_mdInline(p.consult.hint)}</div>` : ''}

      ${!res ? `
        <div class="study-conf">
          <span class="study-subtle">Before you check — how confident? <b id="study-conf-val">${p.confidence != null ? p.confidence + '%' : '—'}</b></span>
          <input type="range" min="0" max="100" value="${p.confidence != null ? p.confidence : 50}" id="study-conf-slider"
                 class="study-conf-slider" list="study-conf-ticks">
          <datalist id="study-conf-ticks">
            <option value="25"></option>
            <option value="55"></option>
            <option value="85"></option>
          </datalist>
          <div class="study-conf-quick">
            <button data-conf="25" class="${p.confidence === 25 ? 'sel' : ''}">guess</button>
            <button data-conf="55" class="${p.confidence === 55 ? 'sel' : ''}">unsure</button>
            <button data-conf="85" class="${p.confidence === 85 ? 'sel' : ''}">sure</button>
          </div>
        </div>
        <div class="study-form-row" style="margin-top:10px;">
          <button class="study-btn primary" id="study-prac-submit">${p.mock ? 'Submit →' : 'Check answer'}</button>
          ${p.mock ? '' : `
          <button class="study-btn" id="study-prac-hint" ${p.hints.length >= 3 || p.hintBusy ? 'disabled' : ''}>
            ${p.hintBusy ? 'Thinking…' : `Hint (${p.hints.length}/3)`}</button>
          ${_originalQuestionButton(q)}
          <button class="study-btn" id="study-prac-consult" title="Find which of your files (and page) covers this, then open it. Consulting before you answer counts like a hint.">${consultLabel}</button>`}
          <button class="study-btn" id="study-prac-skip">Skip</button>
          ${p.mock ? `<span style="flex:1;"></span><button class="study-btn" id="study-prac-endmock" title="Stop answering and predict your score">Finish early</button>` : ''}
        </div>
        <div class="study-tip" style="text-align:left;font-size:11px;">Keyboard: <b>1–9</b> select option, <b>Enter</b> check, <b>H</b> hint, <b>C</b> consult, <b>N</b> next.</div>` : `
        <div class="study-grade ${res.correct === true || (res.score ?? 0) >= 85 ? 'correct' : (res.correct === false || (res.score ?? 0) < 60 ? 'incorrect' : '')}">
          ${isMcq
            ? `<b class="score">${res.correct ? 'Correct' : 'Incorrect'}</b>`
            : `<b class="score">${res.score}/100</b> <b style="margin-left:8px;text-transform:capitalize;">${esc(res.grading?.verdict || '')}</b>`}
          <span class="study-subtle" style="float:right;">next: ${res.interval_days > 0 ? res.interval_days + 'd' : 'soon (relearn)'}</span>
          ${res.grading?.feedback ? `<div class="study-md" style="font-size:12.5px;margin-top:8px;line-height:1.5;">${_mdInline(res.grading.feedback)}</div>` : ''}
          ${res.grading?.followup ? `<div class="study-md" style="font-size:12px;margin-top:8px;opacity:0.75;"><b>Probe:</b> ${_mdInline(res.grading.followup)}</div>` : ''}
          ${res.reference ? `<div class="study-md" style="font-size:12px;margin-top:10px;opacity:0.65;"><b>Reference:</b> ${_mdInline(res.reference)}</div>` : ''}
          ${p.explainText ? `<div class="study-md" style="font-size:12.5px;margin-top:10px;line-height:1.5;border-top:1px solid var(--border);padding-top:8px;">${_md(p.explainText)}</div>` : ''}
        </div>
        ${res.require_reengage && !p.reengaged ? `<div class="study-hint" style="border-color:var(--red,#e05555);"><b>Re-engage required:</b> Review the correct answer before continuing.</div>` : ''}
        <div class="study-form-row" style="margin-top:12px;">
          <button class="study-btn primary" id="study-prac-next">${res.require_reengage && !p.reengaged ? 'Acknowledge →' : 'Next →'}</button>
          ${isMcq && !p.explainText ? `<button class="study-btn" id="study-prac-explain" ${p.explainBusy ? 'disabled' : ''}>${p.explainBusy ? 'Explaining…' : 'Explain options'}</button>` : ''}
          <button class="study-btn" id="study-prac-explain-further" title="Pull the underlying theory from your material, with where to review it">Explain further</button>
          ${_originalQuestionButton(q)}
          <button class="study-btn" id="study-prac-consult" title="Find which of your files (and page) covers this, then open it (free now that you've answered)">${consultLabel}</button>
          <button class="study-btn" id="study-prac-ask" title="Discuss this question with the Study agent (tutor grounded in your materials)">Ask the tutor</button>
        </div>`}

        ${p.mock ? '' : `
      <div class="study-ask">
        <div class="study-ask-head">${res
          ? '💬 Ask AI — anything about this question'
          : '💬 Ask AI — stuck? I’ll nudge you toward the answer (I won’t give it away)'}</div>
        ${p.ask.length ? `<div class="study-ask-thread">${p.ask.map(m => `
          <div class="study-ask-msg ${m.role} study-md">${m.role === 'student' ? esc(m.content) : _md(m.content)}</div>`).join('')}</div>` : ''}
        <div class="study-form-row">
          <input class="study-input" id="study-ask-input" style="flex:1;" ${p.askBusy ? 'disabled' : ''}
            placeholder="${res ? 'Ask why, go deeper, clear a doubt…' : 'Ask for a hint or to clarify the question…'}">
          <button class="study-btn" id="study-ask-send" ${p.askBusy ? 'disabled' : ''}>${p.askBusy ? '…' : 'Ask'}</button>
        </div>
      </div>
        `}
    </div>`;

  // handlers
  el.querySelectorAll('[data-opt]').forEach(b => b.addEventListener('click', () => {
    if (p.result) return;
    p.choice = parseInt(b.dataset.opt, 10);
    renderPractice();
  }));
  el.querySelectorAll('[data-conf]').forEach(b => b.addEventListener('click', () => {
    const val = parseInt(b.dataset.conf, 10);
    p.confidence = p.confidence === val ? null : val;
    const slider = el.querySelector('#study-conf-slider');
    if (slider) slider.value = p.confidence != null ? p.confidence : 50;
    renderPractice();
  }));
  const confSlider = el.querySelector('#study-conf-slider');
  if (confSlider) {
    confSlider.addEventListener('input', () => {
      p.confidence = parseInt(confSlider.value, 10);
      const valLabel = el.querySelector('#study-conf-val');
      if (valLabel) valLabel.textContent = p.confidence + '%';
    });
  }
  const ta = el.querySelector('#study-prac-answer');
  if (ta) ta.addEventListener('input', () => { p.answerDraft = ta.value; });

  el.querySelector('#study-prac-hint')?.addEventListener('click', async () => {
    if (p.hintBusy || p.hints.length >= 3) return;
    p.hintBusy = true; renderPractice();
    try {
      const h = await jpost(`/api/study/questions/${q.id}/hint`, { level: p.hints.length + 1 });
      p.hints.push(h.hint);
    } catch (e) { toast(e.message, true); }
    p.hintBusy = false; renderPractice();
  });

  // Ask AI — Socratic coach before submit (never reveals the answer), full
  // tutor after submit. Conversation lives in p.ask, reset per question.
  const askInput = el.querySelector('#study-ask-input');
  const doAsk = async () => {
    const msg = (askInput?.value || '').trim();
    if (!msg || p.askBusy) return;
    const prior = p.ask.map(m => ({ role: m.role, content: m.content }));
    p.ask.push({ role: 'student', content: msg });
    p.askBusy = true; renderPractice();
    try {
      const r = await jpost(`/api/study/questions/${q.id}/ask`, {
        message: msg, history: prior, answered: !!p.result, draft: p.answerDraft || '',
      });
      p.ask.push({ role: 'ai', content: r.reply || '(no reply)' });
    } catch (e) { p.ask.push({ role: 'ai', content: '⚠️ ' + e.message }); }
    p.askBusy = false; renderPractice();
  };
  el.querySelector('#study-ask-send')?.addEventListener('click', doAsk);
  askInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doAsk(); }
  });

  el.querySelector('#study-prac-skip')?.addEventListener('click', () => {
    p.log.push({ q, skipped: true });
    advancePractice();
  });

  el.querySelector('#study-prac-endmock')?.addEventListener('click', () => {
    stopMockTimer();
    p.mock.phase = 'predict';
    renderPractice();
  });

  el.querySelector('#study-prac-consult')?.addEventListener('click', () => {
    consultAction(q, !p.result);  // penalize only before the answer is submitted
  });

  el.querySelector('#study-prac-explain-further')?.addEventListener('click', () => {
    openExplainFurther('question', q.id);
  });

  el.querySelector('#study-prac-ask')?.addEventListener('click', () => {
    const r = p.result || {};
    const mine = isMcq ? (q.options || [])[p.choice] : p.answerDraft;
    const outcome = isMcq ? (r.correct ? 'correct' : 'wrong') : `${r.score ?? '?'}/100`;
    openAgent(q.deck_id || p.deckId,
      `About this practice question (id ${q.id}):\n\n${q.question}\n\nMy answer: ${mine || '(empty)'} — graded ${outcome}.`
      + `\n\nHelp me understand it from my materials: where do I go wrong, and what is the method?`);
  });

  el.querySelector('#study-prac-submit')?.addEventListener('click', async () => {
    if (isMcq && p.choice == null) { toast('Pick an option first', true); return; }
    if (!isMcq && !p.answerDraft.trim() && !p.emptyArmed) {
      p.emptyArmed = true;
      toast('Empty answer counts as a failed recall — press Check again to submit it anyway', true);
      return;
    }
    const btn = el.querySelector('#study-prac-submit');
    btn.disabled = true; btn.textContent = isMcq ? 'Checking…' : 'Grading…';
    try {
      p.result = await postDurably(
        'attempt',
        `/api/study/questions/${q.id}/attempt`,
        {
          choice_index: isMcq ? p.choice : null,
          answer: isMcq ? null : p.answerDraft,
          confidence: p.confidence,
          // Consulting before answering counts like a hint (retrieval was assisted).
          hints_used: p.hints.length + (p.consulted ? 1 : 0),
          duration_ms: Date.now() - p.qShownTs,
        },
        'att',
        q.id,
      );
      p.log.push({ q, result: p.result, confidence: p.confidence, hints: p.hints.length });
      // Closed book: the marking is graded server-side but stays hidden until
      // the prediction is in.
      if (p.mock) { p.result = null; advancePractice(); return; }
      renderPractice();
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false; btn.textContent = 'Check answer';
    }
  });

  el.querySelector('#study-prac-explain')?.addEventListener('click', async () => {
    p.explainBusy = true; renderPractice();
    try {
      const r = await jpost(`/api/study/questions/${q.id}/explain`);
      p.explainText = r.explanation;
    } catch (e) { toast(e.message, true); }
    p.explainBusy = false; renderPractice();
  });

  el.querySelector('#study-prac-next')?.addEventListener('click', () => {
    // Phase 2.5 wrong-MCQ gate: require acknowledgment before advancing
    if (p.result && p.result.require_reengage && !p.reengaged) {
      p.reengaged = true;
      renderPractice();
      return;
    }
    // failed questions come back at the end of this session (re-drill)
    if (p.result && p.result.rating === 1) {
      p.queue.push({ ...q });
    }
    advancePractice();
  });
}

function advancePractice() {
  const p = S.practice;
  p.idx += 1;
  p.result = null; p.choice = null; p.confidence = null; p.emptyArmed = false;
  p.hints = []; p.answerDraft = ''; p.explainText = null;
  p.consulted = false; p.consult = null; p.consultBusy = false;
  p.prereqs = null; p.prereqsFor = null; p.prereqsBusy = false;
  p.ask = []; p.askBusy = false;
  p.qShownTs = Date.now();
  p.reengaged = false;
  renderPractice();
}

// Predict before marking: the gap between what you think you scored and what
// you scored is the calibration signal the plan's mock blocks are built around.
function renderMockPrediction() {
  const el = body();
  const p = S.practice;
  const answered = p.log.filter(l => l.result).length;
  const asked = p.log.length;
  el.innerHTML = `
    <div class="study-card-stage">
      <div style="font-size:22px;margin-bottom:6px;">Paper down</div>
      <div class="study-subtle" style="max-width:460px;margin:0 auto 4px;">
        ${p.mock.timeUp ? 'Time is up. ' : ''}${answered} answered${asked > answered ? ` · ${asked - answered} skipped` : ''}.
      </div>
      <div class="study-subtle" style="max-width:460px;margin:0 auto 18px;">
        Before you see the marking: how many of the ${answered || 1} you answered did you get right?
      </div>
      <div class="study-form-row" style="justify-content:center;">
        <input class="study-input" id="study-mock-predict" type="number" min="0"
          max="${answered}" step="1" style="width:90px;" placeholder="0">
        <span class="study-subtle">of ${answered}</span>
        <button class="study-btn primary" id="study-mock-reveal">Mark it</button>
      </div>
    </div>`;
  const input = el.querySelector('#study-mock-predict');
  const reveal = () => {
    const v = parseInt(input.value, 10);
    if (Number.isNaN(v) || v < 0 || v > answered) {
      toast(`Give a number between 0 and ${answered}`, true); return;
    }
    p.mock.predicted = v;
    renderPractice();
  };
  el.querySelector('#study-mock-reveal').addEventListener('click', reveal);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') reveal(); });
  input.focus();
}

function renderPracticeSummary() {
  const el = body();
  const p = S.practice;
  stopMockTimer();
  const answered = p.log.filter(l => l.result);
  const ok = answered.filter(l => l.result.correct === true || (l.result.score ?? 0) >= 60);
  // Pinned enum→number mapping (must match src/study_ai.py CONFIDENCE_ENUM_TO_NUMERIC)
  const CONF_SURE = 85, CONF_GUESS = 25;
  const sureWrong = answered.filter(l => l.confidence === CONF_SURE &&
    !(l.result.correct === true || (l.result.score ?? 0) >= 60));
  const guessRight = answered.filter(l => l.confidence === CONF_GUESS &&
    (l.result.correct === true || (l.result.score ?? 0) >= 60));
  const hintsTotal = answered.reduce((a, l) => a + (l.hints || 0), 0);
  const mins = Math.max(1, Math.round((Date.now() - p.startTs) / 60000));
  const weakTopics = [...new Set(answered
    .filter(l => !(l.result.correct === true || (l.result.score ?? 0) >= 60))
    .map(l => l.q.topic).filter(Boolean))];

  el.innerHTML = `
    <div class="study-card-stage">
      <div style="font-size:22px;margin-bottom:6px;">${p.mock ? 'Mock marked' : 'Practice complete'}</div>
      <div style="opacity:0.65;font-size:13px;">${answered.length} answered · ${answered.length ? Math.round(ok.length / answered.length * 100) : 0}% success · ${hintsTotal} hints · ~${mins} min</div>
      ${p.mock && p.mock.predicted !== null ? (() => {
        const pred = p.mock.predicted;
        const gap = pred - ok.length;
        const verdict = gap === 0 ? 'called it exactly'
          : gap > 0 ? `overestimated by ${gap}` : `underestimated by ${-gap}`;
        return `<div class="study-chips" style="justify-content:center;margin-top:18px;">
          <div class="study-chip"><b>${pred}</b><span>you predicted</span></div>
          <div class="study-chip"><b>${ok.length}</b><span>actually right</span></div>
          <div class="study-chip"><b>${answered.length ? Math.round(ok.length / answered.length * 100) : 0}%</b><span>score</span></div>
        </div>
        <div class="study-subtle" style="margin-top:8px;">You ${esc(verdict)}${Math.abs(gap) > 1 ? ' — the gap, not the score, is what to work on.' : '.'}</div>`;
      })() : ''}
      <div class="study-chips" style="justify-content:center;margin-top:18px;">
        <div class="study-chip"><b>${ok.length}</b><span>recalled</span></div>
        <div class="study-chip"><b>${answered.length - ok.length}</b><span>missed</span></div>
        <div class="study-chip"><b>${sureWrong.length}</b><span>sure but wrong</span></div>
        <div class="study-chip"><b>${guessRight.length}</b><span>lucky guesses</span></div>
      </div>
      ${sureWrong.length ? `
        <div style="text-align:left;max-width:560px;margin:18px auto 0;">
          <div class="study-section-title">Calibration alarms — sure but wrong</div>
          ${sureWrong.map(l => `<div class="study-row"><span class="grow" style="font-size:12px;">${esc(l.q.question)}</span></div>`).join('')}
          <div class="study-subtle" style="margin-top:6px;">These are the highest-value misses you have: confident, wrong, and now scheduled for early re-test.</div>
        </div>` : ''}
      ${weakTopics.length ? `<div class="study-subtle" style="margin-top:14px;">Weak topics this session: <b>${weakTopics.map(esc).join(', ')}</b></div>` : ''}
      <div class="study-rate-row">
        <button class="study-btn" id="study-prac-more">Practice more</button>
        ${p.mock ? '<button class="study-btn" id="study-prac-review" title="See every answer with its marking">Review answers</button>' : ''}
        <button class="study-btn primary" id="study-prac-home">Back to Today</button>
      </div>
    </div>`;
  el.querySelector('#study-prac-more').addEventListener('click', () => startPractice(p.deckId, 12, p.scope));
  el.querySelector('#study-prac-review')?.addEventListener('click', () => setTab('history'));
  el.querySelector('#study-prac-home').addEventListener('click', () => { stopMockTimer(); S.practice = null; setTab('today'); });
}

// ---------------------------------------------------------------------------
// PLAN
// ---------------------------------------------------------------------------

async function renderPlan() {
  const el = body();
  if (S.examEditing) return renderExamEditor();
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  try { S.exams = (await jget('/api/study/exams')).exams; }
  catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'plan' || S.examEditing) return;

  el.innerHTML = `
    <div class="study-form-row">
      <button class="study-btn primary" id="study-exam-new">+ New exam</button>
      <span class="study-subtle">Backwards-designed schedule: spaced contacts (1·3·7·14·30), interleaved blocks, mocks, and a sleep-protected taper.</span>
    </div>
    <div id="study-exam-list" style="margin-top:12px;"></div>
  `;
  const list = el.querySelector('#study-exam-list');
  list.innerHTML = S.exams.length ? '' : '<div class="study-empty">No exams yet.</div>';

  for (const x of S.exams) {
    const wrap = document.createElement('div');
    wrap.style.marginBottom = '20px';
    const daysLeft = Math.ceil((new Date(x.exam_date + 'T00:00:00') - Date.now()) / 86400000);
    wrap.innerHTML = `
      <div class="study-row">
        <span class="grow"><b>${esc(x.title)}</b>
          <span style="opacity:0.55;font-size:11.5px;"> · ${esc(x.exam_date)} (${daysLeft}d) · ${x.topics.length} topics</span></span>
        <button class="study-btn small" data-edit="${x.id}">Edit</button>
        <button class="study-btn small primary" data-gen="${x.id}">${x.plan ? 'Regenerate plan' : 'Generate plan'}</button>
        <button class="study-btn small danger" data-del="${x.id}">✕</button>
      </div>
      <div data-plan="${x.id}"></div>`;
    list.appendChild(wrap);
    if (x.plan) renderPlanDays(wrap.querySelector(`[data-plan="${x.id}"]`), x);
  }

  el.querySelector('#study-exam-new').addEventListener('click', () => {
    S.examEditing = { title: '', exam_date: '', exam_format: '', hours_per_week: 7,
                      rest_days: [], topics: [{ name: '', importance: 4, mastery: 2 }] };
    renderExamEditor();
  });
  list.addEventListener('click', async (e) => {
    const editId = e.target.closest('[data-edit]')?.dataset.edit;
    const genId = e.target.closest('[data-gen]')?.dataset.gen;
    const delId = e.target.closest('[data-del]')?.dataset.del;
    try {
      if (editId) {
        S.examEditing = JSON.parse(JSON.stringify(S.exams.find(x => x.id === editId)));
        renderExamEditor();
      } else if (genId) {
        const btn = e.target.closest('[data-gen]');
        btn.disabled = true; btn.textContent = 'Generating…';
        await jpost(`/api/study/exams/${genId}/generate-plan`);
        renderPlan();
      } else if (delId) {
        armThen(e.target.closest('[data-del]'), async () => {
          try { await jdel(`/api/study/exams/${delId}`); renderPlan(); }
          catch (err) { toast(err.message, true); }
        });
      }
    } catch (err) { toast(err.message, true); renderPlan(); }
  });
}

function renderPlanDays(container, exam) {
  const plan = exam.plan;
  const done = new Set(exam.done_blocks || []);
  const today = todayISO();
  const meta = plan.meta || {};
  container.innerHTML = `
    <div style="font-size:11px;opacity:0.55;margin:6px 2px 8px;">
      ${meta.mode === 'cram' ? `⚠ ${esc(meta.warning || 'Cram mode')}` :
        `~${meta.daily_minutes} min/day · reviews at +${(meta.offsets || []).join(', +')}d · mocks: ${(meta.mock_dates || []).join(', ') || '—'}`}
    </div>
    ${(meta.intention_cues || []).filter(c => c.date === today).map(c => `
      <div class="study-tip" style="margin:4px 2px 12px;">💡 <b>Implementation intention:</b> ${esc(c.cue)}</div>
    `).join('')}
    ${plan.days.map(d => `
      <div class="study-plan-day ${d.date === today ? 'today' : ''}" ${d.date < today ? 'style="opacity:0.5;"' : ''}>
        <div class="study-plan-date">${esc(d.date)}${d.date === today ? ' · today' : ''}</div>
        ${d.blocks.map((b, i) => {
          const key = `${d.date}:${i}`;
          return `
          <div class="study-block-row">
          <label class="study-block ${done.has(key) ? 'done' : ''}">
            <input type="checkbox" data-key="${esc(key)}" ${done.has(key) ? 'checked' : ''}>
            <span class="study-block-type ${esc(b.type)}">${esc(b.type.replace('_', ' '))}</span>
            <span class="study-block-text">${b.topics.map(esc).join(', ')} · ${b.minutes}min
              <span class="study-block-note">${esc(b.note || '')}</span></span>
          </label>
          ${exam.deck_id && d.date >= today ? (b.type === 'mock'
            ? `<button class="study-btn small primary" data-plan-mock="${esc(exam.deck_id)}" data-topics="${esc(b.topics.join(','))}" data-minutes="${b.minutes}" title="Timed, closed-book, full format — predict your score before it is marked">Start mock</button>`
            : `<button class="study-btn small" data-plan-practice="${esc(exam.deck_id)}" data-topics="${esc(b.topics.join(','))}" title="Practice this block's topics from the linked subject">Practice</button>`) : ''}
          </div>`;
        }).join('')}
      </div>`).join('')}
  `;
  container.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-plan-practice]');
    if (btn) startPractice(btn.dataset.planPractice, 12, { topics: btn.dataset.topics, label: btn.dataset.topics });
    const mock = e.target.closest('[data-plan-mock]');
    if (mock) {
      const minutes = parseInt(mock.dataset.minutes, 10) || 60;
      // Roughly one question per 4 minutes of the block, kept in sane bounds.
      const count = Math.max(5, Math.min(40, Math.round(minutes / 4)));
      startPractice(mock.dataset.planMock, count,
        { topics: mock.dataset.topics, label: `mock · ${minutes}min` },
        { minutes });
    }
  });
  container.addEventListener('change', async (e) => {
    const cb = e.target.closest('input[data-key]');
    if (!cb) return;
    try {
      const res = await jpost(`/api/study/exams/${exam.id}/toggle-block`, { key: cb.dataset.key });
      exam.done_blocks = res.done_blocks;
      cb.closest('.study-block').classList.toggle('done', cb.checked);
    } catch (err) { toast(err.message, true); }
  });
  setTimeout(() => container.querySelector('.study-plan-day.today')
    ?.scrollIntoView({ block: 'nearest' }), 50);
}

async function renderExamEditor() {
  const el = body();
  const x = S.examEditing;
  if (!S.decks.length) { try { S.decks = (await jget('/api/study/decks')).decks; } catch { /* optional */ } }
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  el.innerHTML = `
    <div style="max-width:620px;">
      <div class="study-form-row">
        <button class="study-btn small" id="study-exam-back">← Plans</button>
        <b style="font-size:14px;">${x.id ? 'Edit exam' : 'New exam'}</b>
      </div>
      <div class="study-form-row">
        <input class="study-input" id="study-ex-title" placeholder="Exam title" value="${esc(x.title)}" style="flex:1;">
        <input class="study-input" id="study-ex-date" type="date" value="${esc(x.exam_date)}">
      </div>
      <div class="study-form-row">
        <input class="study-input" id="study-ex-format" placeholder="Format, e.g. MCQ + problem set" value="${esc(x.exam_format || '')}" style="flex:1;">
        <label class="study-subtle">h/week
          <input class="study-input" id="study-ex-hours" type="number" min="1" max="60" step="0.5"
            value="${x.hours_per_week}" style="width:64px;"></label>
      </div>
      <div class="study-form-row">
        <label class="study-subtle">Subject
          <select class="study-select" id="study-ex-deck" title="Link the exam to a subject: its plan blocks get a Practice button that drills that subject's questions on the block's topics">
            <option value="">— none —</option>
            ${(S.decks || []).map(d => `<option value="${esc(d.id)}" ${x.deck_id === d.id ? 'selected' : ''}>${esc(d.name)}</option>`).join('')}
          </select></label>
      </div>
      <div class="study-form-row" style="font-size:11.5px;">
        <span style="opacity:0.6;">Rest days:</span>
        ${days.map((d, i) => `<label><input type="checkbox" data-rest="${i}"
          ${x.rest_days?.includes(i) ? 'checked' : ''}> ${d}</label>`).join('')}
      </div>
      <div class="study-section-title">Topics — importance × current mastery drives the time split</div>
      <div id="study-topic-rows"></div>
      <button class="study-btn small" id="study-topic-add">+ Topic</button>
      <div class="study-form-row" style="margin-top:18px;">
        <button class="study-btn primary" id="study-ex-save">Save${x.id ? '' : ' exam'}</button>
        ${x.id ? '<span class="study-subtle">Regenerate the plan after saving to apply changes.</span>' : ''}
      </div>
    </div>`;

  const rowsEl = el.querySelector('#study-topic-rows');
  const drawTopics = () => {
    rowsEl.innerHTML = x.topics.map((t, i) => `
      <div class="study-topic-row">
        <input class="study-input" data-tname="${i}" placeholder="Topic name" value="${esc(t.name)}">
        <select class="study-select" data-timp="${i}">
          ${[1, 2, 3, 4, 5].map(v => `<option value="${v}" ${t.importance == v ? 'selected' : ''}>imp ${v}</option>`).join('')}
        </select>
        <select class="study-select" data-tmas="${i}">
          ${[1, 2, 3, 4, 5].map(v => `<option value="${v}" ${t.mastery == v ? 'selected' : ''}>mastery ${v}</option>`).join('')}
        </select>
        <button class="study-btn small danger" data-tdel="${i}">✕</button>
      </div>`).join('');
  };
  drawTopics();

  rowsEl.addEventListener('input', (e) => {
    const n = e.target.dataset.tname, im = e.target.dataset.timp, ma = e.target.dataset.tmas;
    if (n !== undefined) x.topics[+n].name = e.target.value;
    if (im !== undefined) x.topics[+im].importance = +e.target.value;
    if (ma !== undefined) x.topics[+ma].mastery = +e.target.value;
  });
  rowsEl.addEventListener('click', (e) => {
    const i = e.target.closest('[data-tdel]')?.dataset.tdel;
    if (i !== undefined) { x.topics.splice(+i, 1); drawTopics(); }
  });
  el.querySelector('#study-topic-add').addEventListener('click', () => {
    x.topics.push({ name: '', importance: 3, mastery: 2 });
    drawTopics();
  });
  el.querySelector('#study-exam-back').addEventListener('click', () => { S.examEditing = null; renderPlan(); });
  el.querySelector('#study-ex-save').addEventListener('click', async () => {
    const payload = {
      title: el.querySelector('#study-ex-title').value.trim(),
      exam_date: el.querySelector('#study-ex-date').value,
      exam_format: el.querySelector('#study-ex-format').value.trim() || null,
      hours_per_week: parseFloat(el.querySelector('#study-ex-hours').value || '7'),
      rest_days: [...el.querySelectorAll('[data-rest]:checked')].map(c => +c.dataset.rest),
      topics: x.topics.filter(t => t.name.trim()),
      deck_id: el.querySelector('#study-ex-deck')?.value || '',
    };
    if (!payload.title || !payload.exam_date) { toast('Title and date are required', true); return; }
    if (!payload.topics.length) { toast('Add at least one topic', true); return; }
    try {
      if (x.id) await jput(`/api/study/exams/${x.id}`, payload);
      else await jpost('/api/study/exams', payload);
      S.examEditing = null;
      renderPlan();
    } catch (e) { toast(e.message, true); }
  });
}

// ---------------------------------------------------------------------------
// FOCUS
// ---------------------------------------------------------------------------

function focusRemainingSec() {
  const f = S.focus;
  if (!f) return 0;
  return Math.max(0, f.plannedMin * 60 - Math.floor((Date.now() - f.startTs) / 1000));
}

// Confidence calibration over weeks — the per-session version lives in the
// practice summary, but the habit only becomes visible over a long window.
// `gap` is actual minus claimed accuracy: negative means overconfident.
function _calibrationHtml(c) {
  if (!c || !c.overall || !c.overall.graded) {
    return '';   // the curve above already explains the empty state
  }
  const o = c.overall;
  const pct = (v) => v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`;
  const swr = o.sure_wrong_rate;
  const rows = (c.buckets || []).filter(b => b.attempts).map(b => `
    <div class="study-row">
      <span class="grow">Said <b>${esc(b.label)}%</b> <span class="study-subtle">(${b.attempts})</span></span>
      <span class="study-subtle">claimed ${pct(b.expected)} · actual ${pct(b.accuracy)}</span>
      <span style="min-width:54px;text-align:right;color:${b.gap < -0.1 ? 'var(--danger,#e5534b)' : 'inherit'};">
        ${b.gap === null ? '' : (b.gap > 0 ? '+' : '') + Math.round(b.gap * 100) + 'pt'}</span>
    </div>`).join('');
  const decks = (c.by_deck || []).filter(d => d.sure).slice(0, 6).map(d => `
    <div class="study-row">
      <span class="grow">${esc(d.name)}</span>
      <span class="study-subtle">${d.sure_wrong}/${d.sure} sure but wrong</span>
      <span style="min-width:54px;text-align:right;">${pct(d.sure_wrong_rate)}</span>
    </div>`).join('');
  return `
    <div class="study-chips" style="margin:14px 0 10px;">
      <div class="study-chip"><b>${o.brier === null ? '—' : o.brier}</b><span>Brier score</span></div>
      <div class="study-chip"><b>${pct(swr)}</b><span>sure but wrong</span></div>
      <div class="study-chip"><b>${o.graded}</b><span>tagged answers</span></div>
    </div>
    <div class="study-subtle" style="margin-bottom:8px;">
      Lower Brier is better — 0.25 is what saying "50%" to everything scores.
    </div>
    ${rows}
    ${decks ? `<div class="study-section-title" style="margin-top:18px;">Sure but wrong, by subject</div>${decks}` : ''}`;
}

async function renderFocus() {
  const el = body();
  const f = S.focus;

  if (f) {
    const tick = () => {
      if (!S.focus) return;
      const sec = focusRemainingSec();
      // Keep counting while the pane is closed or another tab is open, so the
      // session still auto-finishes; only the clock update needs the DOM.
      const clock = (_open && _tab === 'focus') ? body()?.querySelector('#study-focus-clock') : null;
      if (clock) clock.textContent =
        `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`;
      if (sec <= 0) { finishFocus(true); }
    };
    el.innerHTML = `
      <div class="study-card-stage">
        <div style="font-size:13px;opacity:0.6;">${esc(f.label || 'Focus session')} · ${f.plannedMin} min planned</div>
        <div class="study-focus-clock" id="study-focus-clock">--:--</div>
        <div class="study-subtle" style="margin-bottom:18px;">Single task. Phone in another room. Tabs closed.</div>
        <div class="study-rate-row" style="margin-top:8px;">
          <button class="study-btn primary" id="study-focus-done">Finish now</button>
          <button class="study-btn danger" id="study-focus-abandon">Abandon</button>
        </div>
      </div>`;
    el.querySelector('#study-focus-done').addEventListener('click', () => finishFocus(true));
    el.querySelector('#study-focus-abandon').addEventListener('click', () => finishFocus(false));
    clearInterval(f.timerId);
    f.timerId = setInterval(tick, 500);
    tick();
    return;
  }

  el.innerHTML = '<div class="study-empty">Loading…</div>';
  let stats = null;
  let todayBlocks = { decks: [], blocks: [] };
  try {
    [S.focusHistory, stats, todayBlocks] = await Promise.all([
      jget('/api/study/focus/recent?days=14').then(r => r.sessions),
      jget('/api/study/stats?days=14'),
      jget('/api/study/focus/today-blocks').catch(() => ({ decks: [], blocks: [] })),
    ]);
  } catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'focus' || S.focus) return;

  const last14 = stats.daily.slice(-14);
  const maxMin = Math.max(30, ...last14.map(d => d.focus_min));
  const calCurve = (stats.calibration_curve || []).filter(b => b.n > 0);
  const calSvg = ((data) => {
    if (!data.length) return '';
    const W = 520, H = 150, PL = 32, PB = 22;
    const cw = W - PL, ch = H - PB;
    const bars = data.map(b => {
      const x = PL + (b.predicted / 100) * cw - 7;
      const h = b.accuracy != null ? Math.round(b.accuracy * ch) : 0;
      const y = H - PB - h;
      const o = b.low_n ? '0.35' : '0.85';
      return `<rect x="${x}" y="${y}" width="14" height="${h}" rx="2" fill="var(--accent)" opacity="${o}"/>`;
    }).join('');
    // diagonal perfect-calibration line
    const diag = `<line x1="${PL}" y1="${H - PB}" x2="${W}" y2="${PB}" stroke="var(--text-muted)" stroke-dasharray="3,3" opacity="0.4"/>`;
    // axis labels
    const xLabs = [0, 25, 50, 75, 100].map(v =>
      `<text x="${PL + (v / 100) * cw}" y="${H - 4}" font-size="10" fill="var(--text-muted)" text-anchor="middle">${v}</text>`
    ).join('');
    const yLabs = [0, 0.5, 1].map(v =>
      `<text x="${PL - 4}" y="${H - PB - (v * ch) + 3}" font-size="10" fill="var(--text-muted)" text-anchor="end">${Math.round(v * 100)}</text>`
    ).join('');
    return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;overflow:visible;">${diag}${bars}${xLabs}${yLabs}</svg>`;
  })(calCurve);
  el.innerHTML = `
    <div style="max-width:560px;">
      <div class="study-section-title">Confidence calibration</div>
      ${calCurve.length ? calSvg : '<div class="study-empty">Not enough data yet — answer more questions with confidence.</div>'}
      ${calCurve.length ? `<div style="font-size:11px;opacity:0.6;margin-top:4px;text-align:right;">dashed = perfect calibration · dim = low sample</div>` : ''}

      <div class="study-section-title" style="margin-top:26px;">Start a focus session</div>
      <div class="study-form-row">
        <input class="study-input" id="study-focus-label" placeholder="What are you working on?" style="flex:1;">
      </div>
      ${(todayBlocks.blocks.length || todayBlocks.decks.length) ? `
        <div class="study-form-row" id="study-focus-link-wrap" style="align-items:center;gap:8px;">
          <select class="study-input" id="study-focus-link" style="flex:1;max-width:300px;">
            <option value="">(no specific block)</option>
            ${todayBlocks.blocks.map(b => `<option value="block:${esc(b.exam_id)}:${esc(b.block_key)}:${esc(b.type || '')}">${esc(b.exam_title)} — ${esc(b.type || 'block')} (${esc(b.topics.join(', ') || 'review')}, ${b.minutes}min)</option>`).join('')}
            ${todayBlocks.decks.map(d => `<option value="deck:${esc(d.id)}">${esc(d.name)}</option>`).join('')}
          </select>
          <span class="study-subtle" style="font-size:11px;">optional · link this session to a plan block or subject</span>
        </div>
      ` : ''}
      <div class="study-form-row">
        ${[25, 50, 90].map(m => `<button class="study-btn" data-fmin="${m}">${m} min</button>`).join('')}
        <input class="study-input" id="study-focus-custom" type="number" min="5" max="240" placeholder="custom" style="width:80px;">
        <button class="study-btn primary" id="study-focus-start-custom">Start</button>
      </div>
      <div class="study-section-title" style="margin-top:26px;">Last 14 days (focus minutes)</div>
      <div class="study-bars">
        ${last14.map(d => `<div class="study-bar" style="height:${Math.round((d.focus_min / maxMin) * 100)}%"
          title="${d.date}: ${d.focus_min}min"><i>${d.date.slice(8)}</i></div>`).join('')}
      </div>
      <div class="study-section-title" style="margin-top:26px;">Last 14 days (retrievals)</div>
      <div class="study-bars">
        ${last14.map((d, i) => `<div class="study-bar" style="height:${Math.round((retr[i] / maxRetr) * 100)}%"
          title="${d.date}: ${d.attempts || 0} practice answer(s) + ${d.reviews || 0} card review(s)"><i>${d.date.slice(8)}</i></div>`).join('')}
      </div>
      <div class="study-subtle" style="margin-top:6px;">
        ${t.attempts || 0} practice answers · ${t.reviews || 0} card reviews${okPct === null ? '' : ` · ${okPct}% recalled`}
      </div>
      ${calibHtml}
      <div class="study-section-title" style="margin-top:34px;">Recent sessions</div>
      <div>
        ${S.focusHistory.length ? S.focusHistory.slice(0, 12).map(s => {
          const attr = s.exam_id && s.block_key ? ` · plan block`
            : s.deck_id ? ` · subject`
            : '';
          return `
          <div class="study-row">
            <span class="grow">${esc(s.label || 'Focus')}<span class="study-subtle" style="font-size:11px;">${attr}</span></span>
            <span class="study-subtle">${s.actual_min ?? '…'}min ${s.completed ? '✓' : s.ended_at ? '✗' : '· running'}</span>
          </div>`;
        }).join('') : '<div class="study-empty">No sessions yet.</div>'}
      </div>
    </div>`;

  const start = async (min) => {
    const label = el.querySelector('#study-focus-label').value.trim() || null;
    // Phase 3.2: optional Focus<->Plan link
    const linkSel = el.querySelector('#study-focus-link');
    let deck_id = null, exam_id = null, block_key = null;
    if (linkSel) {
      const v = linkSel.value.trim();
      if (v.startsWith('block:')) {
        const [, ex, bk] = v.split(':');
        exam_id = ex; block_key = bk;
      } else if (v.startsWith('deck:')) {
        deck_id = v.slice(5);
      }
    }
    try {
      const s = await jpost('/api/study/focus/start', { label, planned_min: min, deck_id, exam_id, block_key });
      S.focus = { id: s.id, label, plannedMin: min, startTs: Date.now(), timerId: null };
      renderFocus();
    } catch (e) { toast(e.message, true); }
  };
  el.onclick = (e) => {
    const m = e.target.closest('[data-fmin]')?.dataset.fmin;
    if (m) start(parseInt(m, 10));
  };
  el.querySelector('#study-focus-start-custom').addEventListener('click', () => {
    const v = parseInt(el.querySelector('#study-focus-custom').value || '0', 10);
    if (v >= 5) start(v); else toast('Pick at least 5 minutes', true);
  });
}

async function finishFocus(completed) {
  const f = S.focus;
  if (!f) return;
  clearInterval(f.timerId);
  const elapsedMin = Math.max(completed ? 1 : 0,
    Math.round((Date.now() - f.startTs) / 60000));
  const actual = completed ? Math.min(elapsedMin, f.plannedMin) || f.plannedMin : elapsedMin;
  S.focus = null;
  try {
    await postDurably(
      'focus_finish',
      `/api/study/focus/${f.id}/finish`,
      { actual_min: actual, completed },
      'ff',
      f.id,
    );
    if (completed) toast(`Focus session logged: ${actual} min`);
  } catch (e) { toast(e.message, true); }
  if (_open && _tab === 'focus') renderFocus();
}

// ---------------------------------------------------------------------------

const studyModule = { openPanel, closePanel, togglePanel, isPanelOpen };
export default studyModule;
