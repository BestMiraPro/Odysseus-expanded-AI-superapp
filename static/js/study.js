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
 *
 * The science core: every question/card attempt is closed-book retrieval,
 * outcomes feed FSRS scheduling (spacing), new questions are served
 * interleaved across topics, and confidence-vs-outcome is tracked for
 * calibration. Scheduling lives server-side (src/fsrs.py, src/study_ai.py).
 */

import * as Modals from './modalManager.js';
import { mdToHtml } from './markdown.js';

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
  ['history', 'History'],
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
    history: renderHistory,
  }[tab];
  if (render) render();
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
          <label class="study-block ${b.done ? 'done' : ''}">
            <input type="checkbox" data-exam="${x.id}" data-key="${esc(b.key)}" ${b.done ? 'checked' : ''}>
            <span class="study-block-type ${esc(b.type)}">${esc(b.type.replace('_', ' '))}</span>
            <span class="study-block-text">${b.topics.map(esc).join(', ')} · ${b.minutes}min</span>
          </label>`).join('')}
      </div>`).join('');
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
  list.addEventListener('click', async (e) => {
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      if (!confirm('Delete this subject with all its materials, questions and cards?')) return;
      try { await jdel(`/api/study/decks/${del.dataset.del}`); renderSubjects(); }
      catch (err) { toast(err.message, true); }
      return;
    }
    const row = e.target.closest('[data-deck]');
    if (row) openSubject(row.dataset.deck);
  });
}

async function openSubject(deckId) {
  const deck = S.decks.find(d => d.id === deckId) || { id: deckId, name: 'Subject' };
  S.subject = { deck, cards: [], materials: [], questions: [], proposals: null,
                qFilter: '', extracting: new Set() };
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
      <span class="study-subtle">${s.questions.length} questions · ${s.cards.length} cards</span>
      <span style="flex:1;"></span>
      <button class="study-btn small" id="study-subj-overview" title="An AI overview of the subject that ties the chapters together">Overview</button>
      <button class="study-btn small" id="study-subj-review" ${!s.cards.length ? 'disabled' : ''}>Review cards</button>
      <button class="study-btn small primary" id="study-subj-practice" ${!s.questions.length ? 'disabled' : ''}>Practice questions</button>
    </div>

    <div class="study-section-title">Materials → question bank</div>
    <div class="study-subtle" style="margin-bottom:8px;">Feed it past papers and problem sets for faithful extraction, or notes/textbook sections for authored questions. Everything becomes closed-book practice, spaced by the scheduler.</div>
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
    </div>
    <div id="study-q-list"></div>

    <div class="study-section-title">Flashcards (atomic facts)</div>
    <div class="study-form-row" style="align-items:stretch;">
      <textarea class="study-textarea" id="study-card-front" placeholder="Front — one specific question or cue" style="flex:1;min-height:50px;"></textarea>
      <textarea class="study-textarea" id="study-card-back" placeholder="Back — shortest complete answer" style="flex:1;min-height:50px;"></textarea>
      <button class="study-btn primary" id="study-card-add" style="align-self:flex-end;">Add</button>
    </div>
    <div class="study-form-row">
      <button class="study-btn small" id="study-gen-cards-btn" title="Drafts atomic flashcards from the newest material">Generate cards from latest material</button>
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
    const mat = s.materials[0];
    if (!mat) { toast('Add a material first', true); return; }
    const btn = e.target;
    btn.disabled = true; btn.textContent = 'Generating…';
    try {
      const res = await jpost('/api/study/ai/generate-cards', {
        material_id: mat.id, count: 12,
      });
      s.proposals = res.cards.map(c => ({ ...c, checked: true }));
      renderProposals();
    } catch (err) { toast(err.message, true); }
    btn.disabled = false; btn.textContent = 'Generate cards from latest material';
  });

  let searchT = null;
  el.querySelector('#study-q-search').addEventListener('input', (e) => {
    clearTimeout(searchT);
    searchT = setTimeout(() => { s.qFilter = e.target.value.trim(); renderQuestionList(); }, 250);
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

// Open an uploaded file inside the app (PDF/text/image render inline; other
// types offer a new-tab/download link). Reused by the practice Consult drawer.
function openFileViewer(fileId, name) {
  if (!fileId) return;
  const url = `${API}/api/upload/${encodeURIComponent(fileId)}?inline=1`;
  const ext = (String(name || '').split('.').pop() || '').toLowerCase();
  const viewable = ['pdf', 'txt', 'md', 'csv', 'png', 'jpg', 'jpeg', 'gif', 'webp'].includes(ext);
  _viewerShell(
    name || 'File',
    `<a class="study-btn small" href="${url}" target="_blank" rel="noopener">Open in new tab</a>`,
    viewable
      ? `<iframe src="${url}" title="${esc(name || 'File')}"></iframe>`
      : `<div class="study-viewer-empty">This file type can’t be previewed inline.<br>Use “Open in new tab” to view or download it.</div>`);
}

// Generate-if-missing then show a Markdown doc (study notes / subject overview)
// in the viewer, with a Regenerate action. `kind` is 'material' or 'deck'.
async function _openMarkdownDoc({ title, getPath, postPath, field, confirmMsg, busyMsg }) {
  let r;
  try { r = await jget(getPath); }
  catch (e) { toast(e.message, true); return; }
  if (!r || !r[field]) {
    if (!confirm(confirmMsg)) return;
    toast(busyMsg);
    try { r = await jpost(postPath, {}); }
    catch (e) { toast(e.message, true); return; }
    reloadSubject();
  }
  const v = _viewerShell(title,
    `<button class="study-btn small" id="study-doc-regen">Regenerate</button>`,
    `<div class="study-viewer-body" id="study-viewer-body"></div>`);
  _renderMarkdownInto(v.querySelector('#study-viewer-body'), r[field]);
  v.querySelector('#study-doc-regen').addEventListener('click', async () => {
    toast(busyMsg);
    try {
      const rr = await jpost(postPath, {});
      _renderMarkdownInto(v.querySelector('#study-viewer-body'), rr[field]);
      reloadSubject();
    } catch (e) { toast(e.message, true); }
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

// When several files are relevant, a chooser of file-name buttons (each opens
// that file in a new tab at its page).
function openFileChooser(locations) {
  const inner = `<div class="study-viewer-body">
    <p class="study-subtle" style="margin-top:0;">Relevant files — open one:</p>
    <div class="study-form-row">
      ${locations.map((l, i) => `<button class="study-btn" data-loc="${i}">${esc(_locLabel(l))}</button>`).join('')}
    </div></div>`;
  const v = _viewerShell('Open file', '', inner);
  v.querySelectorAll('[data-loc]').forEach(b => b.addEventListener('click', () => {
    const l = locations[parseInt(b.dataset.loc, 10)];
    if (l) window.open(`${API}${l.url}`, '_blank', 'noopener');
  }));
}

// Consult = locate the file+page with the content to answer this question.
// First click searches and cites; the button then becomes "Open file".
// Consulting before answering counts like a hint (preserves retrieval effort).
async function consultAction(q, penalize) {
  const p = S.practice;
  if (!p || p.consultBusy) return;
  // Already located -> open the file(s).
  if (p.consult && p.consult.locations && p.consult.locations.length) {
    const locs = p.consult.locations;
    if (locs.length === 1) window.open(`${API}${locs[0].url}`, '_blank', 'noopener');
    else openFileChooser(locs);
    return;
  }
  if (penalize) p.consulted = true;   // looking it up before answering = a hint
  p.consultBusy = true; renderPractice();
  try {
    const r = await jpost(`/api/study/questions/${q.id}/locate`, {});
    p.consult = r;
    if (!r.locations || !r.locations.length) {
      toast('No file in this subject covers it directly — generated a hint instead.', true);
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
  wrap.innerHTML = s.materials.map(m => `
    <div class="study-row">
      <span class="grow"><b>${esc(m.name)}</b>
        <span class="study-subtle"> · ${m.kind} · ${(m.char_count / 1000).toFixed(1)}k chars · ${m.question_count} questions extracted</span></span>
      ${s.extracting.has(m.id)
        ? '<span class="study-subtle">Extracting… (vision can take a few minutes)</span>'
        : `${m.file_id ? `<button class="study-btn small" data-view="${m.id}" title="Open this file inside the app">View</button>` : ''}
           <button class="study-btn small" data-notes="${m.id}" title="${m.has_summary ? 'View AI study notes for this material' : 'Generate AI study notes to consult while practising'}">${m.has_summary ? 'Notes' : 'Make notes'}</button>
           <button class="study-btn small primary" data-extract="${m.id}" title="Pull the actual questions out of a past paper / problem set">Extract questions</button>
           ${m.kind === 'pdf' ? `<button class="study-btn small" data-vextract="${m.id}" title="Renders the PDF pages as images for a vision model — use for formula-heavy or scanned exams. Also runs automatically when text extraction finds nothing.">Extract (vision)</button>` : ''}
           <button class="study-btn small" data-author="${m.id}" title="Write new exam-style questions from notes">Author questions</button>
           ${m.kind !== 'text' ? `<button class="study-btn small" data-reextract="${m.id}" title="Re-read the full file text. Older uploads were capped at 15k characters — use this to pick up the rest.">↻ text</button>` : ''}
           <button class="study-btn small danger" data-delmat="${m.id}">✕</button>`}
    </div>`).join('');
  wrap.onclick = async (e) => {
    const ex = e.target.closest('[data-extract]')?.dataset.extract;
    const vx = e.target.closest('[data-vextract]')?.dataset.vextract;
    const au = e.target.closest('[data-author]')?.dataset.author;
    const del = e.target.closest('[data-delmat]')?.dataset.delmat;
    const rx = e.target.closest('[data-reextract]')?.dataset.reextract;
    const vw = e.target.closest('[data-view]')?.dataset.view;
    const nt = e.target.closest('[data-notes]')?.dataset.notes;
    if (vw) {
      const m = s.materials.find(x => x.id === vw);
      if (m) openFileViewer(m.file_id, m.name);
      return;
    }
    if (nt) {
      const m = s.materials.find(x => x.id === nt);
      if (m) openMaterialNotes(m.id, m.name);
      return;
    }
    if (del) {
      if (!confirm('Remove this material? (Extracted questions stay.)')) return;
      try { await jdel(`/api/study/materials/${del}`); reloadSubject(); }
      catch (err) { toast(err.message, true); }
      return;
    }
    if (rx) {
      try {
        const r = await jpost(`/api/study/materials/${rx}/reextract-text`, {});
        const grew = r.char_count > r.previous;
        toast(grew
          ? `Re-read full text: ${(r.char_count / 1000).toFixed(1)}k chars (was ${(r.previous / 1000).toFixed(1)}k)`
          : `Text unchanged (${(r.char_count / 1000).toFixed(1)}k chars)`);
        reloadSubject();
      } catch (err) { toast(err.message, true); }
      return;
    }
    const id = ex || vx || au;
    if (!id) return;
    if (S.subject.extracting.has(id)) return;   // already running for this material
    // Set-based so several materials can extract in parallel without the
    // single-flag bug that orphaned the earlier task.
    S.subject.extracting.add(id);
    renderMaterialList();
    try {
      const res = await jpost(`/api/study/materials/${id}/extract`,
        { mode: au ? 'author' : 'extract', count: 15, vision: !!vx });
      const cov = res.coverage;
      const covNote = cov && cov.missing && cov.missing.length
        ? ` — could not extract question(s) ${cov.missing.join(', ')}; they may need manual entry`
        : (cov && cov.matched != null ? ` — all ${cov.expected} detected questions covered` : '');
      const dupNote = res.duplicates ? ` (${res.duplicates} already in bank, skipped)` : '';
      const head = res.created === 0 && res.duplicates
        ? 'No new questions — everything was already in the bank'
        : `${res.created} questions added`;
      toast(`${head}${res.vision ? ' (vision)' : ''}${dupNote}${res.chunk_errors ? ` (${res.chunk_errors} batch(es) failed)` : ''}${covNote}`, !!(cov && cov.missing && cov.missing.length));
    } catch (err) { toast(err.message, true); }
    S.subject.extracting.delete(id);
    reloadSubject();   // leaves S.subject.extracting intact for still-running ones
  };
}

function renderQuestionList() {
  const wrap = body()?.querySelector('#study-q-list');
  const s = S.subject;
  if (!wrap || !s) return;
  const f = s.qFilter.toLowerCase();
  const rows = s.questions.filter(q => !f ||
    q.question.toLowerCase().includes(f) || (q.topic || '').toLowerCase().includes(f));
  if (!rows.length) {
    wrap.innerHTML = '<div class="study-empty">No questions yet — extract some from a material above.</div>';
    return;
  }
  wrap.innerHTML = rows.slice(0, 200).map(q => `
    <div class="study-cardrow ${q.suspended ? 'suspended' : ''}">
      <span class="study-qchip ${q.qtype}">${q.qtype}</span>
      <span class="front" style="flex:2;">${esc(q.question)}</span>
      <span class="study-state">${esc(q.topic || '')}${q.topic ? ' · ' : ''}${esc(q.difficulty)}
        · ${esc(q.state)}${q.state !== 'new' ? ` · due ${fmtDue(q.due)}` : ''}${q.lapses ? ` · ${q.lapses}✗` : ''}</span>
      <button class="study-btn small" data-qsusp="${q.id}" title="${q.suspended ? 'Unsuspend' : 'Suspend'}">${q.suspended ? '▶' : '⏸'}</button>
      <button class="study-btn small danger" data-qdel="${q.id}" title="Delete">✕</button>
    </div>`).join('');
  wrap.onclick = async (e) => {
    const su = e.target.closest('[data-qsusp]')?.dataset.qsusp;
    const de = e.target.closest('[data-qdel]')?.dataset.qdel;
    try {
      if (su) {
        const q = s.questions.find(x => x.id === su);
        await jput(`/api/study/questions/${su}`, { suspended: !q.suspended });
        reloadSubject();
      } else if (de) {
        if (!confirm('Delete this question?')) return;
        await jdel(`/api/study/questions/${de}`);
        reloadSubject();
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
        if (!confirm('Delete this card?')) return;
        await jdel(`/api/study/cards/${delId}`);
        reloadSubject();
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
      <div class="study-card-front">${esc(card.front)}</div>
      ${r.revealed ? `<div class="study-card-back">${esc(card.back)}</div>
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

// ---------------------------------------------------------------------------
// PRACTICE (question bank)
// ---------------------------------------------------------------------------

async function startPractice(deckId = null, limit = 12) {
  setTabSilent('practice');
  S.practice = { queue: [], idx: 0, deckId, loading: true,
                 phase: 'answer', confidence: null, choice: null,
                 hints: [], hintBusy: false, answerDraft: '', result: null,
                 consulted: false, consult: null, consultBusy: false,
                 explainText: null, explainBusy: false,
                 log: [], startTs: Date.now(), qShownTs: Date.now() };
  renderPractice();
  try {
    const res = await jget(`/api/study/practice/queue?limit=${limit}${deckId ? `&deck_id=${deckId}` : ''}`);
    S.practice.queue = res.queue;
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

  const progress = Math.round((p.idx / p.queue.length) * 100);
  const isMcq = q.qtype === 'mcq';
  const res = p.result;
  const located = p.consult && p.consult.locations && p.consult.locations.length;
  const consultLabel = p.consultBusy ? 'Locating…' : (located ? 'Open file' : 'Consult');

  el.innerHTML = `
    <div class="study-q-wrap">
      <div class="study-progress"><i style="width:${progress}%"></i></div>
      <div class="study-form-row" style="margin-bottom:10px;">
        <span class="study-qchip ${q.qtype}">${q.qtype}</span>
        ${q.topic ? `<span class="study-qchip">${esc(q.topic)}</span>` : ''}
        <span class="study-qchip ${q.difficulty === 'hard' ? 'hard' : ''}">${esc(q.difficulty)}</span>
        <span style="flex:1;"></span>
        <span class="study-subtle">${p.idx + 1}/${p.queue.length}</span>
      </div>
      <div class="study-card-front" style="font-size:16px;">${esc(q.question)}</div>

      ${isMcq ? `<div style="margin-top:16px;" id="study-opts">
        ${(q.options || []).map((o, i) => {
          let cls = 'study-opt';
          if (!res && p.choice === i) cls += ' sel';
          if (res) {
            if (i === res.correct_index) cls += ' right';
            else if (i === p.choice && !res.correct) cls += ' wrong';
          }
          return `<button class="${cls}" data-opt="${i}" ${res ? 'disabled' : ''}>${esc(o)}</button>`;
        }).join('')}
      </div>` : `
        <textarea class="study-textarea" id="study-prac-answer" style="margin-top:14px;min-height:110px;"
          placeholder="Answer from memory — method and result. No peeking." ${res ? 'disabled' : ''}>${esc(p.answerDraft)}</textarea>`}

      ${p.hints.map((h, i) => `<div class="study-hint"><b>Hint ${i + 1}:</b> ${esc(h)}</div>`).join('')}
      ${located
        ? `<div class="study-hint">📄 Relevant material: ${p.consult.locations.map(l => esc(_locLabel(l))).join(' · ')} — use “Open file”.</div>`
        : ''}
      ${p.consult && !located && p.consult.hint
        ? `<div class="study-hint"><b>Consult hint:</b> ${esc(p.consult.hint)}</div>` : ''}

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
          ${res.grading?.feedback ? `<div style="font-size:12.5px;margin-top:8px;line-height:1.5;">${esc(res.grading.feedback)}</div>` : ''}
          ${res.grading?.followup ? `<div style="font-size:12px;margin-top:8px;opacity:0.75;"><b>Probe:</b> ${esc(res.grading.followup)}</div>` : ''}
          ${res.reference ? `<div style="font-size:12px;margin-top:10px;opacity:0.65;"><b>Reference:</b> ${esc(res.reference)}</div>` : ''}
          ${p.explainText ? `<div style="font-size:12.5px;margin-top:10px;line-height:1.5;border-top:1px solid var(--border);padding-top:8px;">${esc(p.explainText)}</div>` : ''}
        </div>
        <div class="study-form-row" style="margin-top:12px;">
          <button class="study-btn primary" id="study-prac-next">Next →</button>
          ${isMcq && !p.explainText ? `<button class="study-btn" id="study-prac-explain" ${p.explainBusy ? 'disabled' : ''}>${p.explainBusy ? 'Explaining…' : 'Explain options'}</button>` : ''}
          <button class="study-btn" id="study-prac-explain-further" title="Pull the underlying theory from your material, with where to review it">Explain further</button>
          <button class="study-btn" id="study-prac-consult" title="Find which of your files (and page) covers this, then open it (free now that you've answered)">${consultLabel}</button>
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

  el.querySelector('#study-prac-submit')?.addEventListener('click', async () => {
    if (isMcq && p.choice == null) { toast('Pick an option first', true); return; }
    if (!isMcq && !p.answerDraft.trim()) {
      if (!confirm('Empty answer counts as a failed recall. Submit anyway?')) return;
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
  p.result = null; p.choice = null; p.confidence = null;
  p.hints = []; p.answerDraft = ''; p.explainText = null;
  p.consulted = false; p.consult = null; p.consultBusy = false;
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
  el.querySelector('#study-prac-more').addEventListener('click', () => startPractice(p.deckId));
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
    const daysLeft = Math.ceil((new Date(x.exam_date) - Date.now()) / 86400000);
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
        if (!confirm('Delete this exam and its plan?')) return;
        await jdel(`/api/study/exams/${delId}`);
        renderPlan();
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
          <label class="study-block ${done.has(key) ? 'done' : ''}">
            <input type="checkbox" data-key="${esc(key)}" ${done.has(key) ? 'checked' : ''}>
            <span class="study-block-type ${esc(b.type)}">${esc(b.type.replace('_', ' '))}</span>
            <span class="study-block-text">${b.topics.map(esc).join(', ')} · ${b.minutes}min
              <span class="study-block-note">${esc(b.note || '')}</span></span>
          </label>`;
        }).join('')}
      </div>`).join('')}
  `;
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

function renderExamEditor() {
  const el = body();
  const x = S.examEditing;
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

async function renderFocus() {
  const el = body();
  const f = S.focus;

  if (f) {
    const tick = () => {
      if (!_open || _tab !== 'focus' || !S.focus) return;
      const sec = focusRemainingSec();
      const clock = el.querySelector('#study-focus-clock');
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
  try {
    [S.focusHistory, stats] = await Promise.all([
      jget('/api/study/focus/recent?days=14').then(r => r.sessions),
      jget('/api/study/stats?days=14'),
    ]);
  } catch (e) { el.innerHTML = `<div class="study-empty">${esc(e.message)}</div>`; return; }
  if (_tab !== 'focus' || S.focus) return;

  const last14 = stats.daily.slice(-14);
  const maxMin = Math.max(30, ...last14.map(d => d.focus_min));
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
