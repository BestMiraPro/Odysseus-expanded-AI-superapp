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

const TABS = [
  ['today', 'Today'], ['subjects', 'Subjects'], ['review', 'Cards'],
  ['practice', 'Practice'], ['plan', 'Plan'], ['focus', 'Focus'],
  ['history', 'History'], ['agent', 'Agent'],
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
.study-conf { display: flex; gap: 6px; align-items: center; margin: 14px 0 4px; }
.study-conf button { font-size: 11px; padding: 4px 10px; border-radius: 99px;
  border: 1px solid var(--border); background: none; color: var(--fg); cursor: pointer; opacity: 0.7; }
.study-conf button.sel { opacity: 1; border-color: var(--accent, #5b8abf); color: var(--accent, #5b8abf); }
.study-hint { border-left: 2px solid var(--accent, #5b8abf); padding: 6px 10px; margin: 8px 0;
  font-size: 12.5px; opacity: 0.85; background: rgba(91,138,191,0.06); border-radius: 0 6px 6px 0; }
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
  _pane.innerHTML = `
    <div class="study-header">
      <span class="study-title">${ICON} Study</span>
      <div class="study-tabs" id="study-tabs">
        ${TABS.map(([k, label]) => `<button class="study-tab" data-tab="${k}">${label}</button>`).join('')}
      </div>
      <span class="study-header-spacer"></span>
      <span class="study-model-wrap" id="study-model-wrap" title="Model used for extraction, grading and hints. 'Same as chat' falls back to the utility/default model.">
        <select id="study-ep-select"><option value="">Same as chat</option></select>
        <select id="study-model-select"><option value="">model…</option></select>
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
    if (e.key === 'Escape') {
      // Close an open file viewer first, leaving the study pane open.
      const v = (_pane || document).querySelector('#study-viewer');
      if (v) { v.remove(); return; }
      closePanel();
      return;
    }
    if (_tab === 'review') reviewKeydown(e);
    else if (_tab === 'practice') practiceKeydown(e);
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

async function initModelSelector() {
  const epSel = _pane?.querySelector('#study-ep-select');
  const mSel = _pane?.querySelector('#study-model-select');
  if (!epSel || !mSel) return;
  try {
    const [eps, settings] = await Promise.all([
      fetch(`${API}/api/model-endpoints`, { credentials: 'same-origin' }).then(r => r.json()),
      fetch(`${API}/api/auth/settings`, { credentials: 'same-origin' }).then(r => r.json()),
    ]);
    _endpoints = Array.isArray(eps) ? eps : [];
    epSel.innerHTML = '<option value="">Same as chat</option>' + _endpoints.map(ep =>
      `<option value="${esc(ep.id)}">${esc(ep.name)}${ep.online === false ? ' (offline)' : ''}</option>`).join('');
    epSel.value = settings.study_endpoint_id || '';
    fillStudyModels(settings.study_model || '');
  } catch (e) {
    console.warn('study model selector init failed', e);
    return;
  }

  const save = async () => {
    try {
      await fetch(`${API}/api/auth/settings`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          study_endpoint_id: epSel.value || '',
          study_model: mSel.value || '',
        }),
      });
      toast(epSel.value ? 'Study model saved' : 'Study model: same as chat');
    } catch (e) { toast('Could not save model (admin only?): ' + e.message, true); }
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
  _pane.querySelectorAll('.study-tab').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tab === tab));
  const render = {
    today: renderToday, subjects: renderSubjects, review: renderReview,
    practice: renderPractice, plan: renderPlan, focus: renderFocus,
    history: renderHistory, agent: renderAgent,
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
function openAgent(deckId, prefill) {
  if (deckId) setAgentScope(deckId);
  if (prefill) setAgentPrefill(prefill);
  setTab('agent');
}

function setTabSilent(tab) {
  _tab = tab;
  const b = body();
  if (b) b.onclick = null;
  _pane?.querySelectorAll('.study-tab').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tab === tab));
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
  const conf = e.confidence ? ` · ${esc(e.confidence)}` : '';
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
  el.innerHTML = `
    <div class="study-chips">
      <div class="study-chip"><b>${o.due_total}</b><span>cards due</span></div>
      <div class="study-chip"><b>${o.q_due_total ?? 0}</b><span>questions due</span></div>
      <div class="study-chip"><b>${t.streak_days}</b><span>day streak</span></div>
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
    <div id="study-deck-list"></div>
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
    </div>
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

// Show a Markdown doc (study notes / subject overview) in the viewer, with a
// Regenerate action. When none exists yet the viewer offers a Generate button
// (no window.confirm — browsers may suppress it). `kind` is 'material' or 'deck'.
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
          : `${m.file_id ? `<button class="study-btn small" data-openfile="${m.id}" title="Open this file in a new browser tab">Open</button>` : ''}
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
      if (m) openFileTab(m.file_id);
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
        const front = prompt('Front:', c.front); if (front === null) return;
        const back = prompt('Back:', c.back); if (back === null) return;
        await jput(`/api/study/cards/${editId}`, { front, back });
        reloadSubject();
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
    await jpost(`/api/study/cards/${card.id}/review`, { rating, duration_ms: duration });
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

// Practice shortcuts: 1-9 pick an MCQ option, Enter checks the answer (or
// goes to the next question once graded); inside the answer box Ctrl/Cmd+Enter
// submits so Enter can still add a newline.
function practiceKeydown(e) {
  const p = S.practice;
  if (!p || p.loading) return;
  const el = body();
  if (!el) return;
  if (e.target.closest('input, select')) return;
  const inText = !!e.target.closest('textarea');
  if (e.key === 'Enter') {
    if (inText && !(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    (el.querySelector('#study-prac-next') || el.querySelector('#study-prac-submit'))?.click();
    return;
  }
  if (inText) return;
  if (/^[1-9]$/.test(e.key) && !p.result) {
    const opt = el.querySelector(`[data-opt="${parseInt(e.key, 10) - 1}"]`);
    if (opt) { e.preventDefault(); opt.click(); }
  }
}

// ---------------------------------------------------------------------------
// PRACTICE (question bank)
// ---------------------------------------------------------------------------

// `scope`: optional {materialId, topics (array|csv), label} — practice only one
// material's questions, or an exam-plan block's topics.
async function startPractice(deckId = null, limit = 12, scope = null) {
  setTabSilent('practice');
  S.practice = { queue: [], idx: 0, deckId, scope: scope || null, loading: true,
                 phase: 'answer', confidence: null, choice: null, emptyArmed: false,
                 hints: [], hintBusy: false, answerDraft: '', result: null,
                 consulted: false, consult: null, consultBusy: false,
                 prereqs: null, prereqsFor: null, prereqsBusy: false,
                 explainText: null, explainBusy: false,
                 log: [], startTs: Date.now(), qShownTs: Date.now() };
  renderPractice();
  try {
    const params = new URLSearchParams({ limit: String(limit) });
    if (deckId) params.set('deck_id', deckId);
    if (scope?.materialId) params.set('material_id', scope.materialId);
    if (scope?.topics) params.set('topics', Array.isArray(scope.topics) ? scope.topics.join(',') : scope.topics);
    const res = await jget(`/api/study/practice/queue?${params}`);
    S.practice.queue = res.queue;
    if (res.topic_fallback) toast('No questions matched those topics — practising the whole subject instead');
  } catch (e) { toast(e.message, true); }
  S.practice.loading = false;
  renderPractice();
}

async function renderPractice() {
  const el = body();
  const p = S.practice;

  if (!p) {
    el.innerHTML = '<div class="study-empty">Loading…</div>';
    let decks = [];
    try { decks = (await jget('/api/study/decks')).decks; } catch { }
    if (_tab !== 'practice' || S.practice) return;
    const totalQ = decks.reduce((a, d) => a + (d.q_due ?? 0) + (d.q_new ?? 0), 0);
    el.innerHTML = `
      <div class="study-card-stage">
        <div style="font-size:15px;margin-bottom:6px;">Question practice</div>
        <div class="study-subtle" style="max-width:480px;margin:0 auto 18px;">Exam-format retrieval from your extracted question banks. Due questions come first (spacing); new ones are mixed across topics (interleaving). Hints cost you — a hinted success reschedules sooner.</div>
        <div class="study-rate-row" style="margin-top:0;">
          <button class="study-btn primary" id="study-practice-all" ${totalQ === 0 ? 'disabled' : ''}>Practice everything</button>
        </div>
        <div style="margin-top:22px;">${decks.map(d => `
          <div class="study-row" style="max-width:460px;margin:0 auto 6px;">
            <span class="grow" style="text-align:left;">${esc(d.name)}</span>
            <span class="study-badge q">${d.q_due ?? 0} due</span>
            <span class="study-badge new">${d.q_new ?? 0} new</span>
            <button class="study-btn small" data-prac="${d.id}" ${(d.q_due ?? 0) + (d.q_new ?? 0) === 0 ? 'disabled' : ''}>Start</button>
          </div>`).join('')}</div>
        ${totalQ === 0 ? '<div class="study-empty" style="margin-top:14px;">No questions yet — go to Subjects, add a material, and extract questions from it.</div>' : ''}
      </div>`;
    el.querySelector('#study-practice-all')?.addEventListener('click', () => startPractice(null));
    el.onclick = (e) => {
      const id = e.target.closest('[data-prac]')?.dataset.prac;
      if (id) startPractice(id);
    };
    return;
  }

  if (p.loading) { el.innerHTML = '<div class="study-empty">Loading questions…</div>'; return; }

  const q = p.queue[p.idx];
  if (!q) { return renderPracticeSummary(); }

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
          <span class="study-subtle">Before you check — how confident?</span>
          ${['sure', 'unsure', 'guess'].map(cv => `
            <button data-conf="${cv}" class="${p.confidence === cv ? 'sel' : ''}">${cv}</button>`).join('')}
        </div>
        <div class="study-form-row" style="margin-top:10px;">
          <button class="study-btn primary" id="study-prac-submit">Check answer</button>
          <button class="study-btn" id="study-prac-hint" ${p.hints.length >= 3 || p.hintBusy ? 'disabled' : ''}>
            ${p.hintBusy ? 'Thinking…' : `Hint (${p.hints.length}/3)`}</button>
          <button class="study-btn" id="study-prac-consult" title="Find which of your files (and page) covers this, then open it. Consulting before you answer counts like a hint.">${consultLabel}</button>
          <button class="study-btn" id="study-prac-skip">Skip</button>
        </div>` : `
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
        <div class="study-form-row" style="margin-top:12px;">
          <button class="study-btn primary" id="study-prac-next">Next →</button>
          ${isMcq && !p.explainText ? `<button class="study-btn" id="study-prac-explain" ${p.explainBusy ? 'disabled' : ''}>${p.explainBusy ? 'Explaining…' : 'Explain options'}</button>` : ''}
          <button class="study-btn" id="study-prac-explain-further" title="Pull the underlying theory from your material, with where to review it">Explain further</button>
          <button class="study-btn" id="study-prac-consult" title="Find which of your files (and page) covers this, then open it (free now that you've answered)">${consultLabel}</button>
          <button class="study-btn" id="study-prac-ask" title="Discuss this question with the Study agent (tutor grounded in your materials)">Ask the tutor</button>
        </div>`}
    </div>`;

  // handlers
  el.querySelectorAll('[data-opt]').forEach(b => b.addEventListener('click', () => {
    if (p.result) return;
    p.choice = parseInt(b.dataset.opt, 10);
    renderPractice();
  }));
  el.querySelectorAll('[data-conf]').forEach(b => b.addEventListener('click', () => {
    p.confidence = p.confidence === b.dataset.conf ? null : b.dataset.conf;
    renderPractice();
  }));
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

  el.querySelector('#study-prac-skip')?.addEventListener('click', () => {
    p.log.push({ q, skipped: true });
    advancePractice();
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
      p.result = await jpost(`/api/study/questions/${q.id}/attempt`, {
        choice_index: isMcq ? p.choice : null,
        answer: isMcq ? null : p.answerDraft,
        confidence: p.confidence,
        // Consulting before answering counts like a hint (retrieval was assisted).
        hints_used: p.hints.length + (p.consulted ? 1 : 0),
        duration_ms: Date.now() - p.qShownTs,
      });
      p.log.push({ q, result: p.result, confidence: p.confidence, hints: p.hints.length });
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
  p.qShownTs = Date.now();
  renderPractice();
}

function renderPracticeSummary() {
  const el = body();
  const p = S.practice;
  const answered = p.log.filter(l => l.result);
  const ok = answered.filter(l => l.result.correct === true || (l.result.score ?? 0) >= 60);
  const sureWrong = answered.filter(l => l.confidence === 'sure' &&
    !(l.result.correct === true || (l.result.score ?? 0) >= 60));
  const guessRight = answered.filter(l => l.confidence === 'guess' &&
    (l.result.correct === true || (l.result.score ?? 0) >= 60));
  const hintsTotal = answered.reduce((a, l) => a + (l.hints || 0), 0);
  const mins = Math.max(1, Math.round((Date.now() - p.startTs) / 60000));
  const weakTopics = [...new Set(answered
    .filter(l => !(l.result.correct === true || (l.result.score ?? 0) >= 60))
    .map(l => l.q.topic).filter(Boolean))];

  el.innerHTML = `
    <div class="study-card-stage">
      <div style="font-size:22px;margin-bottom:6px;">Practice complete</div>
      <div style="opacity:0.65;font-size:13px;">${answered.length} answered · ${answered.length ? Math.round(ok.length / answered.length * 100) : 0}% success · ${hintsTotal} hints · ~${mins} min</div>
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
        <button class="study-btn primary" id="study-prac-home">Back to Today</button>
      </div>
    </div>`;
  el.querySelector('#study-prac-more').addEventListener('click', () => startPractice(p.deckId, 12, p.scope));
  el.querySelector('#study-prac-home').addEventListener('click', () => { S.practice = null; setTab('today'); });
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
          ${exam.deck_id && b.type !== 'mock' && d.date >= today ? `<button class="study-btn small" data-plan-practice="${esc(exam.deck_id)}" data-topics="${esc(b.topics.join(','))}" title="Practice this block's topics from the linked subject">Practice</button>` : ''}
          </div>`;
        }).join('')}
      </div>`).join('')}
  `;
  container.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-plan-practice]');
    if (btn) startPractice(btn.dataset.planPractice, 12, { topics: btn.dataset.topics, label: btn.dataset.topics });
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
    return `<div class="study-section-title" style="margin-top:26px;">Calibration</div>
      <div class="study-subtle">Tag your confidence when you answer — after a few
      sessions this shows how often "sure" really means right.</div>`;
  }
  const o = c.overall;
  const pct = (v) => v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`;
  const swr = o.sure_wrong_rate;
  const rows = (c.buckets || []).filter(b => b.attempts).map(b => `
    <div class="study-row">
      <span class="grow">Said <b>${esc(b.confidence)}</b> <span class="study-subtle">(${b.attempts})</span></span>
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
    <div class="study-section-title" style="margin-top:26px;">Calibration (last ${c.days} days)</div>
    <div class="study-chips" style="margin-bottom:10px;">
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
  let stats = null, calib = null;
  try {
    [S.focusHistory, stats, calib] = await Promise.all([
      jget('/api/study/focus/recent?days=14').then(r => r.sessions),
      jget('/api/study/stats?days=14'),
      jget('/api/study/calibration?days=90').catch(() => null),
    ]);
  } catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'focus' || S.focus) return;

  const last14 = stats.daily.slice(-14);
  const maxMin = Math.max(30, ...last14.map(d => d.focus_min));
  // Retrievals = card reviews + practice answers. Practice is where most
  // retrieval happens, so a chart of card reviews alone under-reports the work.
  const retr = last14.map(d => (d.reviews || 0) + (d.attempts || 0));
  const maxRetr = Math.max(5, ...retr);
  const t = stats.totals || {};
  const okPct = (t.success_rate === null || t.success_rate === undefined)
    ? null : Math.round(t.success_rate * 100);
  const calibHtml = _calibrationHtml(calib);
  el.innerHTML = `
    <div style="max-width:560px;">
      <div class="study-section-title">Start a focus session</div>
      <div class="study-form-row">
        <input class="study-input" id="study-focus-label" placeholder="What are you working on?" style="flex:1;">
      </div>
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
        ${S.focusHistory.length ? S.focusHistory.slice(0, 12).map(s => `
          <div class="study-row">
            <span class="grow">${esc(s.label || 'Focus')}</span>
            <span class="study-subtle">${s.actual_min ?? '…'}min ${s.completed ? '✓' : s.ended_at ? '✗' : '· running'}</span>
          </div>`).join('') : '<div class="study-empty">No sessions yet.</div>'}
      </div>
    </div>`;

  const start = async (min) => {
    const label = el.querySelector('#study-focus-label').value.trim() || null;
    try {
      const s = await jpost('/api/study/focus/start', { label, planned_min: min });
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
    await jpost(`/api/study/focus/${f.id}/finish`, { actual_min: actual, completed });
    if (completed) toast(`Focus session logged: ${actual} min`);
  } catch (e) { toast(e.message, true); }
  if (_open && _tab === 'focus') renderFocus();
}

// ---------------------------------------------------------------------------

const studyModule = { openPanel, closePanel, togglePanel, isPanelOpen };
export default studyModule;
