/**
 * AI Council — put one question to several models at once.
 *
 * Stage 1  Opinions   every seated model answers independently (streamed side by side)
 * Stage 2  Review     each model ranks the others' answers blind (Response A, B, …)
 * Stage 3  Synthesis  the chairman writes the council's answer from all of it
 *
 * Seats can be any Odysseus model endpoint: a Claude subscription (through the
 * Claude Code CLI), a ChatGPT subscription (OpenAI device sign-in) or any
 * API/local endpoint. Both subscriptions can be connected from this page; once
 * connected they are ordinary endpoints usable everywhere else in Odysseus too.
 *
 * Runs live on the server: closing or minimizing this page does not stop a
 * deliberation, and reopening the thread picks up its saved result.
 */

import * as Modals from './modalManager.js';
import { mdToHtml, renderMath } from './markdown.js';
import { providerLogo } from './providers.js';
import { formatDeviceFlowError, runProviderDeviceFlow } from './providerDeviceFlow.js';

const PANE_ID = 'council-pane';
const STORE_KEY = 'council:lastSession';
const STAGES = [['opinions', 'Opinions'], ['review', 'Peer review'], ['synthesis', 'Synthesis']];

const ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="17" r="2.5"/><circle cx="19" cy="17" r="2.5"/><path d="M12 7.5v4"/><path d="M12 11.5 6.8 15.3"/><path d="M12 11.5l5.2 3.8"/></svg>';

let _open = false;
let _pane = null;
let _keyHandler = null;
let _focusReturn = null;

const S = {
  roster: null,          // /api/council/roster
  sessions: [],
  session: null,         // {id, title, config, turns}
  members: [],           // [{endpoint_id, model}]
  chairman: null,        // {endpoint_id, model} | null → first member
  mode: 'full',
  live: null,            // streaming turn (same shape as a saved turn + runtime bits)
  picker: false,
  connect: { claude: { busy: false, msg: '', err: false }, chatgpt: { busy: false, msg: '', err: false, code: '', url: '' } },
  pollTimer: null,
};

// ---------------------------------------------------------------------------
// utils
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function jfetch(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || `Request failed (${res.status})`;
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return data;
}
const jget = (p) => jfetch(p);
const jpost = (p, body) => jfetch(p, { method: 'POST', body: JSON.stringify(body || {}) });
const jpatch = (p, body) => jfetch(p, { method: 'PATCH', body: JSON.stringify(body || {}) });
const jdel = (p) => jfetch(p, { method: 'DELETE' });

function toast(msg) { try { window.uiModule?.showToast?.(msg); } catch { /* optional */ } }

function storeGet() { try { return localStorage.getItem(STORE_KEY) || ''; } catch { return ''; } }
function storeSet(v) { try { v ? localStorage.setItem(STORE_KEY, v) : localStorage.removeItem(STORE_KEY); } catch { /* private mode */ } }

function md(text) {
  try { return mdToHtml(text || '', {}); } catch { return `<pre>${esc(text)}</pre>`; }
}

function fmtMs(ms) {
  if (!ms && ms !== 0) return '';
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

function seatKey(seat) { return seat ? `${seat.endpoint_id}::${seat.model}` : ''; }

function endpointById(id) {
  return (S.roster?.endpoints || []).find(e => e.id === id) || null;
}

function seatInfo(seat) {
  const ep = endpointById(seat?.endpoint_id);
  return {
    model: seat?.model || '',
    endpoint: ep?.name || 'Unavailable endpoint',
    kind: ep?.kind || 'missing',
    provider: ep?.provider || '',
    available: !!(ep && ep.models.includes(seat.model)),
  };
}

function logoFor(model) {
  const svg = providerLogo(model || '');
  return svg ? `<span class="council-logo" aria-hidden="true">${svg}</span>` : `<span class="council-logo council-logo-dot" aria-hidden="true"></span>`;
}

const KIND_LABEL = { subscription: 'Subscription', api: 'API', local: 'Local', missing: 'Missing' };
const PROVIDER_SHORT = {
  'claude-subscription': 'Claude sub',
  'chatgpt-subscription': 'ChatGPT sub',
  copilot: 'Copilot',
};

function badge(kind, provider) {
  const text = PROVIDER_SHORT[provider] || KIND_LABEL[kind] || kind;
  return `<span class="council-badge kind-${esc(kind)}">${esc(text)}</span>`;
}

// ---------------------------------------------------------------------------
// styles
// ---------------------------------------------------------------------------

function injectStyles() {
  if (document.getElementById('council-styles')) return;
  const st = document.createElement('style');
  st.id = 'council-styles';
  st.textContent = `
.council-pane { position: fixed; inset: 4vh 5vw; z-index: 160; display: flex; flex-direction: column;
  background: var(--panel, var(--bg)); color: var(--fg); border: 1px solid var(--border);
  border-radius: 12px; box-shadow: 0 18px 60px rgba(0,0,0,0.45); overflow: hidden; }
.council-backdrop { position: fixed; inset: 0; z-index: 159; background: rgba(0,0,0,0.35); }
.council-pane.hidden, .council-backdrop.hidden { display: none !important; }
@media (max-width: 768px) { .council-pane { inset: 0; border-radius: 0; } }
.council-header { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
  border-bottom: 1px solid var(--border); flex-shrink: 0; }
.council-title { font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 7px; }
.council-header-spacer { flex: 1; }
.council-x { background: none; border: 1px solid transparent; color: var(--fg); cursor: pointer;
  font-size: 14px; padding: 2px 8px; border-radius: 6px; opacity: 0.7; }
.council-x:hover { opacity: 1; border-color: var(--border); }
/* minmax(0, …): a plain 1fr track grows to its widest child and the pane
   scrolls sideways on a phone. */
.council-layout { flex: 1; display: grid; grid-template-columns: 230px minmax(0, 1fr); min-height: 0; }
.council-side { border-right: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; min-width: 0; }
.council-side-top { padding: 10px; display: flex; gap: 6px; }
.council-sessions { flex: 1; overflow-y: auto; padding: 0 6px 8px; }
.council-session { display: flex; align-items: center; gap: 6px; padding: 7px 8px; border-radius: 7px;
  cursor: pointer; font-size: 12.5px; }
.council-session:hover { background: color-mix(in srgb, var(--fg) 7%, transparent); }
.council-session.active { background: color-mix(in srgb, var(--accent, var(--red, #888)) 18%, transparent); }
.council-session .grow { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.council-session .del { opacity: 0; background: none; border: none; color: inherit; cursor: pointer; font-size: 12px; }
.council-session:hover .del { opacity: 0.6; }
.council-session .del:hover { opacity: 1; }
.council-connect { border-top: 1px solid var(--border); padding: 10px; display: flex; flex-direction: column; gap: 10px;
  font-size: 12px; max-height: 55%; overflow-y: auto; }
.council-conn-h { display: flex; align-items: center; gap: 6px; font-weight: 600; }
.council-conn-h .state { margin-left: auto; font-weight: 500; font-size: 11px; opacity: 0.75; }
.council-conn-h .state.on { color: var(--ok, #3ba55d); opacity: 1; }
.council-conn-body { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
.council-conn-body p { margin: 0; opacity: 0.72; line-height: 1.45; }
.council-conn-body code { font-size: 11px; }
.council-conn-msg { font-size: 11.5px; line-height: 1.4; }
.council-conn-msg.err { color: var(--danger, #e05252); }
.council-conn-code { font-size: 15px; letter-spacing: 1.5px; font-weight: 700; }
.council-input { width: 100%; box-sizing: border-box; padding: 6px 8px; font-size: 12px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg); color: var(--fg); }
.council-btn { padding: 6px 10px; font-size: 12px; border-radius: 7px; border: 1px solid var(--border);
  background: var(--bg); color: var(--fg); cursor: pointer; white-space: nowrap; }
.council-btn:hover:not(:disabled) { border-color: color-mix(in srgb, var(--fg) 40%, transparent); }
.council-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.council-btn.primary { background: var(--accent, var(--red, #c0392b)); border-color: transparent; color: #fff; font-weight: 600; }
.council-btn.ghost { background: none; }
.council-btn.small { padding: 3px 8px; font-size: 11px; }
.council-main { display: flex; flex-direction: column; min-height: 0; min-width: 0; overflow: hidden; }
.council-seats { padding: 10px 14px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; position: relative; }
.council-seat-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.council-seat-label { font-size: 11px; text-transform: uppercase; letter-spacing: .6px; opacity: 0.55; margin-right: 4px; }
.council-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 6px 4px 8px; border-radius: 999px;
  border: 1px solid var(--border); font-size: 12px; max-width: min(420px, 100%); min-width: 0; background: var(--bg); }
.council-chip.missing { border-style: dashed; opacity: 0.6; }
/* The model name is what identifies a seat; the endpoint name gives way first. */
.council-chip .name { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 240px; }
.council-chip .ep { flex: 0 100 auto; min-width: 0; opacity: 0.55; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 130px; }
.council-chip > .council-logo, .council-chip > .council-badge, .council-chip > button { flex-shrink: 0; }
.council-chip button { background: none; border: none; color: inherit; cursor: pointer; opacity: 0.55; font-size: 12px; padding: 0 3px; }
.council-chip button:hover { opacity: 1; }
.council-logo { display: inline-flex; width: 14px; height: 14px; flex-shrink: 0; }
.council-logo svg { width: 14px; height: 14px; }
.council-logo-dot::before { content: ''; width: 7px; height: 7px; margin: auto; border-radius: 50%; background: currentColor; opacity: .45; }
.council-badge { font-size: 9.5px; text-transform: uppercase; letter-spacing: .4px; padding: 1px 5px; border-radius: 4px;
  background: color-mix(in srgb, var(--fg) 9%, transparent); opacity: 0.85; white-space: nowrap; }
.council-badge.kind-subscription { background: color-mix(in srgb, #b07cff 22%, transparent); }
.council-badge.kind-local { background: color-mix(in srgb, #3ba55d 22%, transparent); }
.council-select { padding: 4px 6px; font-size: 12px; border-radius: 6px; border: 1px solid var(--border);
  background: var(--bg); color: var(--fg); max-width: min(320px, 100%); min-width: 0; }
.council-mode { display: inline-flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.council-mode button { background: none; border: none; color: var(--fg); padding: 4px 10px; font-size: 12px; cursor: pointer; opacity: 0.65; }
.council-mode button.on { opacity: 1; background: color-mix(in srgb, var(--fg) 10%, transparent); font-weight: 600; }
.council-picker { position: absolute; left: 14px; top: 100%; margin-top: 4px; z-index: 5; width: min(560px, calc(100% - 28px));
  max-height: 55vh; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.25); padding: 8px; }
.council-picker-group { font-size: 10.5px; text-transform: uppercase; letter-spacing: .6px; opacity: 0.5; margin: 8px 6px 4px; }
.council-picker-ep { padding: 6px; border-radius: 8px; }
.council-picker-ep .h { font-size: 12px; font-weight: 600; display: flex; gap: 6px; align-items: center; margin-bottom: 5px; }
.council-picker-models { display: flex; flex-wrap: wrap; gap: 5px; }
.council-picker-models button { display: inline-flex; gap: 5px; align-items: center; font-size: 11.5px; padding: 3px 8px;
  border-radius: 999px; border: 1px solid var(--border); background: none; color: var(--fg); cursor: pointer; }
.council-picker-models button:hover:not(:disabled) { border-color: var(--accent, var(--red, #888)); }
.council-picker-models button:disabled { opacity: 0.4; cursor: default; }
.council-scroll { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 22px; }
.council-empty { margin: auto; max-width: 520px; text-align: center; opacity: 0.8; line-height: 1.6; font-size: 13px; }
.council-empty h3 { margin: 0 0 8px; font-size: 16px; }
.council-empty ol { text-align: left; display: inline-block; margin: 8px 0 0; padding-left: 18px; }
.council-turn { display: flex; flex-direction: column; gap: 10px; }
.council-q { align-self: flex-end; max-width: 80%; padding: 9px 12px; border-radius: 12px 12px 3px 12px;
  background: color-mix(in srgb, var(--fg) 8%, transparent); white-space: pre-wrap; font-size: 13.5px; line-height: 1.5; }
.council-strip { display: flex; align-items: center; gap: 6px; font-size: 11.5px; flex-wrap: wrap; }
.council-step { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--border); opacity: 0.55; }
.council-step.running { opacity: 1; border-color: var(--accent, var(--red, #888)); }
.council-step.done { opacity: 0.9; }
.council-step.skipped { opacity: 0.35; text-decoration: line-through; }
.council-step .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; opacity: 0.4; }
.council-step.running .dot { background: var(--accent, var(--red, #888)); opacity: 1; animation: council-pulse 1.1s ease-in-out infinite; }
.council-step.done .dot { background: var(--ok, #3ba55d); opacity: 1; }
@keyframes council-pulse { 50% { transform: scale(0.55); } }
@media (prefers-reduced-motion: reduce) { .council-step.running .dot { animation: none; } }
.council-strip .status { margin-left: auto; opacity: 0.7; }
.council-strip .status.err { color: var(--danger, #e05252); opacity: 1; }
.council-section { border: 1px solid var(--border); border-radius: 10px; margin: 0; padding: 0; background: none; overflow: visible; }
.council-section > summary { cursor: pointer; padding: 8px 12px; font-size: 12.5px; font-weight: 600; list-style: none;
  display: flex; gap: 8px; align-items: center; }
.council-section > summary::-webkit-details-marker { display: none; }
.council-section > summary::before { content: '▸'; opacity: 0.5; transition: transform .15s; }
.council-section[open] > summary::before { transform: rotate(90deg); }
.council-section > summary .sub { font-weight: 400; opacity: 0.6; }
.council-section[open] > :not(summary) { animation: none; }
.council-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; padding: 0 10px 10px; }
.council-card { border: 1px solid var(--border); border-radius: 9px; display: flex; flex-direction: column; min-width: 0;
  background: var(--bg); max-height: 460px; }
.council-card.err { border-color: color-mix(in srgb, var(--danger, #e05252) 60%, var(--border)); }
.council-card-h { display: flex; align-items: center; gap: 6px; padding: 7px 10px; border-bottom: 1px solid var(--border);
  font-size: 12px; min-width: 0; }
.council-card-h .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.council-card-h .ep { opacity: 0.55; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.council-card-h .state { margin-left: auto; font-size: 10.5px; opacity: 0.65; white-space: nowrap; }
.council-card-h .label { font-weight: 700; font-size: 11px; padding: 0 5px; border-radius: 4px;
  background: color-mix(in srgb, var(--fg) 10%, transparent); }
.council-card-b { padding: 8px 12px; overflow-y: auto; font-size: 13px; line-height: 1.55; min-height: 40px; }
.council-card-b > :first-child { margin-top: 0; }
.council-card-b > :last-child { margin-bottom: 0; }
.council-card-b.pending::after { content: '…'; opacity: 0.5; }
.council-errtext { color: var(--danger, #e05252); font-size: 12px; }
.council-rank { width: calc(100% - 20px); margin: 0 10px 10px; border-collapse: collapse; font-size: 12px; }
.council-rank th, .council-rank td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border); }
.council-rank th { font-weight: 600; opacity: 0.6; font-size: 11px; }
.council-rank tr.top td { font-weight: 600; }
.council-final { border: 1px solid color-mix(in srgb, var(--accent, var(--red, #888)) 55%, var(--border)); border-radius: 12px;
  overflow: hidden; }
.council-final-h { display: flex; align-items: center; gap: 7px; padding: 8px 12px; font-size: 12px;
  background: color-mix(in srgb, var(--accent, var(--red, #888)) 10%, transparent); }
.council-final-h .t { font-weight: 700; }
.council-final-h .ep { opacity: 0.6; }
.council-final-h .tools { margin-left: auto; display: flex; gap: 4px; }
.council-final-b { padding: 10px 14px; font-size: 14px; line-height: 1.6; }
.council-final-b > :first-child { margin-top: 0; }
.council-final-b > :last-child { margin-bottom: 0; }
.council-composer { border-top: 1px solid var(--border); padding: 10px 14px; display: flex; gap: 8px; align-items: flex-end; }
.council-composer textarea { flex: 1; resize: none; min-height: 40px; max-height: 200px; padding: 9px 11px; font: inherit;
  font-size: 13.5px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg); color: var(--fg); }
.council-composer .hint { font-size: 11px; opacity: 0.55; }
.council-warn { font-size: 12px; padding: 7px 10px; border-radius: 8px;
  background: color-mix(in srgb, #e0a252 15%, transparent); }
@media (max-width: 860px) {
  .council-layout { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .council-side { border-right: none; border-bottom: 1px solid var(--border); max-height: 32vh; }
  .council-grid { grid-template-columns: 1fr; }
  .council-q { max-width: 92%; }
}
`;
  document.head.appendChild(st);
}

// ---------------------------------------------------------------------------
// pane lifecycle
// ---------------------------------------------------------------------------

export function openPanel() {
  if (_open) return;
  _open = true;
  injectStyles();
  _focusReturn = document.activeElement;

  const backdrop = document.createElement('div');
  backdrop.id = 'council-backdrop';
  backdrop.className = 'council-backdrop';
  backdrop.addEventListener('click', () => minimizePanel());
  document.body.appendChild(backdrop);

  _pane = document.createElement('div');
  _pane.id = PANE_ID;
  _pane.className = 'council-pane';
  _pane.setAttribute('role', 'dialog');
  _pane.setAttribute('aria-label', 'AI Council');
  _pane.innerHTML = `
    <div class="council-header">
      <span class="council-title">${ICON} Council</span>
      <span class="council-header-spacer"></span>
      <button class="council-x" id="council-min-btn" title="Minimize" aria-label="Minimize Council">–</button>
      <button class="council-x" id="council-close-btn" title="Close (Esc)" aria-label="Close Council">✕</button>
    </div>
    <div class="council-layout">
      <aside class="council-side">
        <div class="council-side-top">
          <button class="council-btn primary" id="council-new" style="flex:1">+ New council</button>
        </div>
        <div class="council-sessions" id="council-sessions" role="list"></div>
        <div class="council-connect" id="council-connect"></div>
      </aside>
      <section class="council-main">
        <div class="council-seats" id="council-seats"></div>
        <div class="council-scroll" id="council-scroll"></div>
        <div class="council-composer">
          <div style="flex:1;display:flex;flex-direction:column;gap:4px;">
            <textarea id="council-input" rows="2" placeholder="Ask the council…" aria-label="Question for the council"></textarea>
            <span class="hint" id="council-hint">Enter to convene · Shift+Enter for a new line</span>
          </div>
          <button class="council-btn primary" id="council-send">Convene</button>
        </div>
      </section>
    </div>`;
  document.body.appendChild(_pane);
  document.getElementById('tool-council-btn')?.classList.add('active');

  _pane.querySelector('#council-close-btn').addEventListener('click', () => closePanel());
  _pane.querySelector('#council-min-btn').addEventListener('click', () => minimizePanel());
  _pane.querySelector('#council-new').addEventListener('click', () => newSession());
  _pane.querySelector('#council-send').addEventListener('click', () => convene());
  const input = _pane.querySelector('#council-input');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); convene(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(200, input.scrollHeight)}px`;
  });
  _pane.addEventListener('click', onPaneClick);
  _pane.addEventListener('change', onPaneChange);

  _keyHandler = (e) => {
    if (!_open || !_pane || _pane.classList.contains('hidden')) return;
    if (e.key !== 'Escape') return;
    if (S.picker) { S.picker = false; renderSeats(); return; }
    if (_pane.contains(document.activeElement) || document.activeElement === document.body) closePanel();
  };
  document.addEventListener('keydown', _keyHandler);
  ensureChip();
  init();
  setTimeout(() => input.focus(), 30);
}

function ensureChip() {
  try {
    if (Modals.isRegistered && Modals.isRegistered(PANE_ID)) return;
    Modals.register(PANE_ID, {
      railBtnId: 'rail-council',
      sidebarBtnId: 'tool-council-btn',
      label: 'Council',
      icon: ICON,
      restoreFn: () => {
        document.getElementById('council-backdrop')?.classList.remove('hidden');
        if (!_open) openPanel();
        else refreshCurrent();
      },
      closeFn: () => { forceClose(); },
    });
  } catch { /* modal manager optional */ }
}

function minimizePanel() {
  ensureChip();
  document.getElementById('council-backdrop')?.classList.add('hidden');
  if (Modals.minimize) Modals.minimize(PANE_ID);
  else closePanel();
  restoreFocus();
}

function restoreFocus() {
  const t = _focusReturn;
  _focusReturn = null;
  if (t && typeof t.focus === 'function' && t.isConnected !== false) { try { t.focus(); } catch { /* gone */ } }
}

function forceClose() {
  _open = false;
  stopPolling();
  // An in-flight stream keeps running server-side; drop only our listener.
  if (S.live?.reader) { try { S.live.reader.cancel(); } catch { /* closed */ } }
  S.live = null;
  if (_keyHandler) { document.removeEventListener('keydown', _keyHandler); _keyHandler = null; }
  document.getElementById('tool-council-btn')?.classList.remove('active');
  try { Modals.unregister(PANE_ID); } catch { /* not registered */ }
  document.getElementById(PANE_ID)?.remove();
  document.getElementById('council-backdrop')?.remove();
  _pane = null;
  restoreFocus();
}

export function closePanel() { forceClose(); }

export function togglePanel() {
  try {
    if (Modals.isMinimized && Modals.isMinimized(PANE_ID)) { Modals.restore(PANE_ID); return; }
  } catch { /* optional */ }
  _open ? closePanel() : openPanel();
}

export function isPanelOpen() { return _open; }

const $ = (sel) => _pane?.querySelector(sel);

// ---------------------------------------------------------------------------
// data
// ---------------------------------------------------------------------------

async function init() {
  await Promise.all([loadRoster(), loadSessions()]);
  const last = storeGet();
  const target = S.sessions.find(s => s.id === last) || S.sessions[0];
  if (target) await openSession(target.id);
  else { applyConfig({}); renderAll(); }
}

async function loadRoster() {
  try { S.roster = await jget('/api/council/roster'); }
  catch (e) { S.roster = { endpoints: [], connections: {}, can_connect: false, error: e.message }; }
}

async function loadSessions() {
  try { S.sessions = (await jget('/api/council/sessions')).sessions || []; }
  catch { S.sessions = []; }
}

function applyConfig(cfg) {
  S.members = Array.isArray(cfg?.members) ? cfg.members.filter(m => m && m.endpoint_id && m.model).slice(0, 8) : [];
  S.chairman = cfg?.chairman && cfg.chairman.endpoint_id ? cfg.chairman : null;
  S.mode = cfg?.mode === 'quick' ? 'quick' : 'full';
  if (!S.members.length) S.members = defaultSeats();
}

function defaultSeats() {
  // First model of each subscription, then fill with API models: a useful
  // council without any clicking.
  const eps = S.roster?.endpoints || [];
  const seats = [];
  for (const ep of eps.filter(e => e.kind === 'subscription')) {
    if (ep.models[0]) seats.push({ endpoint_id: ep.id, model: ep.models[0] });
  }
  for (const ep of eps.filter(e => e.kind !== 'subscription')) {
    if (seats.length >= 3) break;
    if (ep.models[0]) seats.push({ endpoint_id: ep.id, model: ep.models[0] });
  }
  return seats.slice(0, 3);
}

async function openSession(id) {
  try {
    const data = await jget(`/api/council/sessions/${encodeURIComponent(id)}`);
    S.session = data;
    storeSet(data.id);
    applyConfig(data.config || {});
  } catch (e) {
    toast(e.message);
    S.session = null;
    storeSet('');
    applyConfig({});
  }
  renderAll();
  scrollToEnd();
  maybePoll();
}

async function refreshCurrent() {
  await loadRoster();
  if (S.session && !S.live) {
    try { S.session = await jget(`/api/council/sessions/${encodeURIComponent(S.session.id)}`); } catch { /* keep */ }
  }
  renderAll();
  maybePoll();
}

async function newSession() {
  try {
    const data = await jpost('/api/council/sessions', { config: currentConfig() });
    S.sessions.unshift(data);
    S.session = data;
    storeSet(data.id);
    renderAll();
    $('#council-input')?.focus();
  } catch (e) { toast(e.message); }
}

async function deleteSession(id) {
  const s = S.sessions.find(x => x.id === id);
  if (!confirm(`Delete "${s?.title || 'this council'}" and all its answers?`)) return;
  try {
    await jdel(`/api/council/sessions/${encodeURIComponent(id)}`);
    S.sessions = S.sessions.filter(x => x.id !== id);
    if (S.session?.id === id) {
      S.session = null;
      storeSet('');
      if (S.sessions[0]) { await openSession(S.sessions[0].id); return; }
    }
    renderAll();
  } catch (e) { toast(e.message); }
}

function currentConfig() {
  return { members: S.members, chairman: S.chairman, mode: S.mode };
}

let _saveTimer = null;
function saveConfigSoon() {
  if (!S.session) return;
  clearTimeout(_saveTimer);
  const id = S.session.id;
  _saveTimer = setTimeout(() => {
    jpatch(`/api/council/sessions/${encodeURIComponent(id)}`, { config: currentConfig() }).catch(() => {});
  }, 400);
}

// A run started elsewhere (or before a reload) shows as "running": poll until it lands.
function maybePoll() {
  stopPolling();
  if (!S.session || S.live) return;
  const running = (S.session.turns || []).some(t => t.status === 'running');
  if (!running) return;
  S.pollTimer = setTimeout(async () => {
    S.pollTimer = null;
    if (!_open || !S.session || S.live) return;
    try {
      S.session = await jget(`/api/council/sessions/${encodeURIComponent(S.session.id)}`);
      renderTurns();
    } catch { /* retry next tick */ }
    maybePoll();
  }, 4000);
}

function stopPolling() {
  if (S.pollTimer) { clearTimeout(S.pollTimer); S.pollTimer = null; }
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function renderAll() {
  if (!_pane) return;
  renderSessions();
  renderConnect();
  renderSeats();
  renderTurns();
  renderComposer();
}

function renderSessions() {
  const box = $('#council-sessions');
  if (!box) return;
  if (!S.sessions.length) {
    box.innerHTML = '<div class="council-session" style="cursor:default;opacity:.55">No councils yet</div>';
    return;
  }
  box.innerHTML = S.sessions.map(s => `
    <div class="council-session ${S.session?.id === s.id ? 'active' : ''}" role="listitem" data-session="${esc(s.id)}" tabindex="0">
      <span class="grow" title="${esc(s.title)}">${esc(s.title)}</span>
      <button class="del" data-del-session="${esc(s.id)}" title="Delete" aria-label="Delete council">✕</button>
    </div>`).join('');
}

function renderConnect() {
  const box = $('#council-connect');
  if (!box) return;
  const c = S.roster?.connections || {};
  const canConnect = !!S.roster?.can_connect;
  const claude = c.claude || {};
  const gpt = c.chatgpt || {};
  const cs = S.connect.claude;
  const gs = S.connect.chatgpt;

  let claudeBody = '';
  if (claude.connected) {
    claudeBody = `<p>${claude.mode === 'host' ? "Using this machine's Claude Code login." : 'Using your Claude setup token.'}
      ${(claude.models || []).length} models, usable everywhere in Odysseus.</p>
      ${canConnect ? '<button class="council-btn small ghost" data-act="claude-disconnect">Disconnect</button>' : ''}`;
  } else if (!claude.cli_installed) {
    claudeBody = `<p>Needs the Claude Code CLI where Odysseus runs:</p>
      <code>npm install -g @anthropic-ai/claude-code</code>
      <button class="council-btn small" data-act="refresh-roster">I installed it</button>`;
  } else if (!canConnect) {
    claudeBody = '<p>Ask an admin to connect a Claude subscription.</p>';
  } else {
    claudeBody = `<p>In a terminal run <code>claude setup-token</code>, sign in with your Claude account, and paste the token:</p>
      <input class="council-input" id="council-claude-token" type="password" autocomplete="off" placeholder="sk-ant-oat01-…" aria-label="Claude setup token">
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="council-btn small primary" data-act="claude-token" ${cs.busy ? 'disabled' : ''}>Connect</button>
        <button class="council-btn small" data-act="claude-host" ${cs.busy ? 'disabled' : ''} title="Use the Claude login already on the Odysseus machine (claude auth login)">Use this machine's login</button>
      </div>`;
  }
  if (cs.msg) claudeBody += `<div class="council-conn-msg ${cs.err ? 'err' : ''}">${esc(cs.msg)}</div>`;

  let gptBody = '';
  if (gpt.connected) {
    gptBody = `<p>Signed in with your OpenAI account. ${(gpt.models || []).length} models, usable everywhere in Odysseus. Manage it in Settings → Models.</p>`;
  } else if (!canConnect) {
    gptBody = '<p>Ask an admin to connect a ChatGPT subscription.</p>';
  } else if (gs.code) {
    gptBody = `<p>Enter this code on the OpenAI page:</p>
      <div class="council-conn-code">${esc(gs.code)}</div>
      <a class="council-btn small" href="${esc(gs.url)}" target="_blank" rel="noopener">Open OpenAI sign-in ↗</a>`;
  } else {
    gptBody = `<p>Sign in with the OpenAI account that has your ChatGPT plan.</p>
      <button class="council-btn small primary" data-act="chatgpt-connect" ${gs.busy ? 'disabled' : ''}>Connect ChatGPT</button>`;
  }
  if (gs.msg) gptBody += `<div class="council-conn-msg ${gs.err ? 'err' : ''}">${esc(gs.msg)}</div>`;

  box.innerHTML = `
    <div>
      <div class="council-conn-h">${providerLogo('claude') ? `<span class="council-logo">${providerLogo('claude')}</span>` : ''}Claude
        <span class="state ${claude.connected ? 'on' : ''}">${claude.connected ? '● Connected' : '○ Not connected'}</span></div>
      <div class="council-conn-body">${claudeBody}</div>
    </div>
    <div>
      <div class="council-conn-h">${providerLogo('gpt') ? `<span class="council-logo">${providerLogo('gpt')}</span>` : ''}ChatGPT
        <span class="state ${gpt.connected ? 'on' : ''}">${gpt.connected ? '● Connected' : '○ Not connected'}</span></div>
      <div class="council-conn-body">${gptBody}</div>
    </div>`;
}

function chairmanSeat() {
  return S.chairman || S.members[0] || null;
}

function seatChip(seat, idx) {
  const info = seatInfo(seat);
  return `<span class="council-chip ${info.available ? '' : 'missing'}" title="${esc(`${info.model} · ${info.endpoint}`)}">
    ${logoFor(info.model)}<span class="name">${esc(info.model)}</span><span class="ep">${esc(info.endpoint)}</span>
    ${badge(info.kind, info.provider)}
    <button data-remove-seat="${idx}" aria-label="Remove ${esc(info.model)}">✕</button></span>`;
}

function allSeatOptions(selectedKey) {
  const groups = { subscription: [], api: [], local: [] };
  for (const ep of S.roster?.endpoints || []) (groups[ep.kind] || groups.api).push(ep);
  const names = { subscription: 'Subscriptions', api: 'API', local: 'Local' };
  let html = '';
  for (const [kind, eps] of Object.entries(groups)) {
    for (const ep of eps) {
      html += `<optgroup label="${esc(`${ep.name} · ${names[kind]}`)}">${ep.models.map(m => {
        const key = `${ep.id}::${m}`;
        return `<option value="${esc(key)}" ${key === selectedKey ? 'selected' : ''}>${esc(m)}</option>`;
      }).join('')}</optgroup>`;
    }
  }
  return html;
}

function renderSeats() {
  const box = $('#council-seats');
  if (!box) return;
  const maxMembers = S.roster?.max_members || 8;
  const chair = chairmanSeat();
  const chairKey = seatKey(chair);
  const chairKnown = chair && seatInfo(chair).available;
  const noModels = !(S.roster?.endpoints || []).length;
  box.innerHTML = `
    ${noModels ? `<div class="council-warn">No models yet. Connect Claude or ChatGPT on the left, or add an API model in Settings → Models.</div>` : ''}
    <div class="council-seat-row">
      <span class="council-seat-label">Members</span>
      ${S.members.map((m, i) => seatChip(m, i)).join('')}
      <button class="council-btn small" data-act="toggle-picker" ${S.members.length >= maxMembers || noModels ? 'disabled' : ''} aria-expanded="${S.picker}">+ Add member</button>
    </div>
    <div class="council-seat-row">
      <span class="council-seat-label">Chairman</span>
      <select class="council-select" id="council-chair" aria-label="Chairman model">
        ${chairKnown ? '' : `<option value="" selected>${chair ? 'Unavailable — pick one' : 'Pick a chairman'}</option>`}
        ${allSeatOptions(chairKey)}
      </select>
      <span class="council-seat-label" style="margin-left:10px">Mode</span>
      <span class="council-mode" role="radiogroup" aria-label="Council mode">
        <button data-mode="full" class="${S.mode === 'full' ? 'on' : ''}" role="radio" aria-checked="${S.mode === 'full'}" title="Opinions, blind peer review and ranking, then synthesis">Full</button>
        <button data-mode="quick" class="${S.mode === 'quick' ? 'on' : ''}" role="radio" aria-checked="${S.mode === 'quick'}" title="Opinions, then synthesis (no peer review)">Quick</button>
      </span>
    </div>
    ${S.picker ? renderPicker() : ''}`;
}

function renderPicker() {
  const taken = new Set(S.members.map(seatKey));
  const eps = S.roster?.endpoints || [];
  const group = (kind, title) => {
    const list = eps.filter(e => e.kind === kind);
    if (!list.length) return '';
    return `<div class="council-picker-group">${title}</div>` + list.map(ep => `
      <div class="council-picker-ep">
        <div class="h">${esc(ep.name)} ${badge(ep.kind, ep.provider)}</div>
        <div class="council-picker-models">${ep.models.map(m => {
          const key = `${ep.id}::${m}`;
          return `<button data-add-seat="${esc(key)}" ${taken.has(key) ? 'disabled' : ''}>${logoFor(m)}${esc(m)}</button>`;
        }).join('')}</div>
      </div>`).join('');
  };
  return `<div class="council-picker" role="dialog" aria-label="Add a council member">
    ${group('subscription', 'Subscriptions')}${group('api', 'API models')}${group('local', 'Local models')}
  </div>`;
}

function renderComposer() {
  const btn = $('#council-send');
  const input = $('#council-input');
  const hint = $('#council-hint');
  if (!btn) return;
  const running = !!S.live || (S.session?.turns || []).some(t => t.status === 'running');
  const ready = S.members.length > 0 && chairmanSeat() && S.members.every(m => seatInfo(m).available)
    && seatInfo(chairmanSeat()).available;
  btn.disabled = running || !ready;
  btn.textContent = running ? 'Deliberating…' : 'Convene';
  if (input) input.disabled = false;
  if (hint) {
    hint.textContent = !S.members.length ? 'Seat at least one member first.'
      : !ready ? 'A seat points at a model that is no longer available — remove or replace it.'
      : `${S.members.length} member${S.members.length === 1 ? '' : 's'} · ${S.mode === 'full' ? 'opinions → blind review → synthesis' : 'opinions → synthesis'} · Enter to convene`;
  }
}

function seatsOfTurn(turn) {
  const cfg = turn.config || {};
  return { members: cfg.members || [], chairman: cfg.chairman || null, mode: cfg.mode || 'full' };
}

function stageState(turn, stage) {
  if (turn.stages && turn.stages[stage]) return turn.stages[stage];
  const { mode } = seatsOfTurn(turn);
  const okCount = (turn.opinions || []).filter(o => !o.error && o.text).length;
  if (stage === 'opinions') return (turn.opinions || []).length ? 'done' : 'pending';
  if (stage === 'review') {
    if (mode !== 'full' || okCount < 2) return turn.status === 'running' ? 'pending' : 'skipped';
    return (turn.reviews || []).length ? 'done' : 'pending';
  }
  return turn.final ? 'done' : 'pending';
}

function statusText(turn) {
  switch (turn.status) {
    case 'running': return 'Deliberating…';
    case 'done': return turn.error ? turn.error : '';
    case 'cancelled': return 'Stopped';
    case 'interrupted': return 'Interrupted (Odysseus restarted)';
    case 'error': return turn.error || 'Failed';
    default: return '';
  }
}

function memberCard(stage, seat, idx, entry, label) {
  const text = entry?.text || '';
  const err = entry?.error;
  const streaming = entry?.streaming;
  const state = err ? 'error' : streaming ? (entry?.thinking && !text ? 'thinking…' : 'writing…')
    : entry?.ms != null ? fmtMs(entry.ms) : (entry ? '' : 'waiting…');
  return `<div class="council-card ${err ? 'err' : ''}" data-stage="${stage}" data-member="${idx}">
    <div class="council-card-h">
      ${label ? `<span class="label" title="Anonymous label used in peer review">${esc(label)}</span>` : ''}
      ${logoFor(seat?.model)}<span class="name">${esc(seat?.model || '?')}</span>
      <span class="ep">${esc(seat?.endpoint_name || '')}</span>
      <span class="state">${esc(state)}</span>
    </div>
    <div class="council-card-b ${!text && !err ? 'pending' : ''}">${err ? `<div class="council-errtext">${esc(err)}</div>` : md(text)}</div>
  </div>`;
}

function sameScore(a, b) {
  return !!(a && b) && a.avg_rank === b.avg_rank && a.first_votes === b.first_votes;
}

function tied(ranking) { return ranking.length > 1 && sameScore(ranking[0], ranking[1]); }

function rankingSummary(ranking, seats) {
  if (!ranking.length) return 'blind ranking';
  if (tied(ranking)) {
    // Two members can only rank each other once self-votes are dropped, so a
    // tie there is the expected outcome, not a verdict.
    return seats.members.length === 2 ? 'tie (two members cannot separate)' : 'tie at the top';
  }
  return `top: Response ${esc(ranking[0].label)} (${esc(seats.members[ranking[0].member]?.model || '?')})`;
}

function renderTurn(turn, live = false) {
  const seats = seatsOfTurn(turn);
  const memberLabel = {};
  for (const [label, idx] of Object.entries(turn.labels || {})) memberLabel[idx] = label;
  const ops = {};
  for (const o of turn.opinions || []) ops[o.member] = o;
  const revs = {};
  for (const r of turn.reviews || []) revs[r.member] = r;
  const running = turn.status === 'running';
  const status = statusText(turn);
  const statusErr = ['error', 'interrupted'].includes(turn.status) || (turn.status === 'done' && turn.error);

  const strip = STAGES.map(([key, name]) => {
    const st = stageState(turn, key);
    return `<span class="council-step ${st}"><span class="dot"></span>${name}</span>`;
  }).join('<span style="opacity:.35">→</span>');

  const opinionsOpen = live || running || !turn.final;
  const opinions = `<details class="council-section" data-sec="opinions" ${opinionsOpen ? 'open' : ''}>
    <summary>Opinions <span class="sub">${seats.members.length} member${seats.members.length === 1 ? '' : 's'}</span></summary>
    <div class="council-grid">${seats.members.map((seat, i) => memberCard('opinions', seat, i, ops[i], memberLabel[i])).join('')}</div>
  </details>`;

  let review = '';
  const showReview = seats.mode === 'full' && ((turn.reviews || []).length || (turn.stages && turn.stages.review && turn.stages.review !== 'pending'));
  if (showReview) {
    const ranking = (turn.ranking || []).filter(r => r.avg_rank != null);
    const table = ranking.length ? `<table class="council-rank"><thead><tr><th>#</th><th>Answer</th><th>Model</th><th>Avg rank</th><th>1st-place votes</th></tr></thead><tbody>
      ${ranking.map((r, i) => {
        const seat = seats.members[r.member] || {};
        return `<tr class="${i === 0 && !tied(ranking) ? 'top' : ''}"><td>${i > 0 && sameScore(r, ranking[i - 1]) ? '=' : i + 1}</td><td>Response ${esc(r.label)}</td><td>${logoFor(seat.model)} ${esc(seat.model || '?')}</td><td>${esc(r.avg_rank)}</td><td>${esc(r.first_votes)}</td></tr>`;
      }).join('')}</tbody></table>
      <div style="font-size:11px;opacity:.55;margin:-4px 12px 10px">Each model ranked the answers without knowing who wrote them; votes on its own answer are not counted.</div>` : '';
    const reviewers = seats.members.map((seat, i) => ({ seat, i })).filter(({ i }) => revs[i] || (turn.stages?.review === 'running' && ops[i] && !ops[i].error));
    review = `<details class="council-section" data-sec="review" ${live || running ? 'open' : ''}>
      <summary>Peer review <span class="sub">${rankingSummary(ranking, seats)}</span></summary>
      ${table}
      <div class="council-grid">${reviewers.map(({ seat, i }) => memberCard('review', seat, i, revs[i], memberLabel[i] ? `by ${memberLabel[i]}` : '')).join('')}</div>
    </details>`;
  }

  const chair = seats.chairman || {};
  const finalText = turn.final || '';
  const finalErr = turn.status === 'done' && turn.error ? turn.error : '';
  const showFinal = finalText || (turn.stages && turn.stages.synthesis && turn.stages.synthesis !== 'pending') || (!running && turn.status !== 'running' && turn.error);
  const final = showFinal ? `<section class="council-final" data-final>
    <div class="council-final-h">${ICON}<span class="t">Council answer</span>
      <span class="ep">chairman ${esc(chair.model || '')}${chair.endpoint_name ? ` · ${esc(chair.endpoint_name)}` : ''}</span>
      <span class="tools">${finalText ? `<button class="council-btn small ghost" data-copy-final="${esc(turn.id)}">Copy</button>` : ''}</span>
    </div>
    <div class="council-final-b ${!finalText ? 'pending' : ''}">${finalErr ? `<div class="council-errtext" style="margin-bottom:8px">${esc(finalErr)} Showing the top-ranked answer instead.</div>` : ''}${md(finalText)}</div>
  </section>` : '';

  return `<article class="council-turn" data-turn="${esc(turn.id)}">
    <div class="council-q">${esc(turn.question)}</div>
    <div class="council-strip">${strip}
      <span class="status ${statusErr ? 'err' : ''}">${esc(status)}</span>
      ${running ? `<button class="council-btn small" data-stop="${esc(turn.id)}">Stop</button>` : ''}
      ${!running ? `<button class="council-btn small ghost" data-del-turn="${esc(turn.id)}" title="Delete this question">Delete</button>` : ''}
    </div>
    ${opinions}${review}${final}
  </article>`;
}

function renderTurns() {
  const box = $('#council-scroll');
  if (!box) return;
  const turns = [...(S.session?.turns || [])];
  // A live run belongs to the thread it was asked in, not whichever is open.
  const live = S.live && S.live.sessionId === S.session?.id ? S.live : null;
  if (live && !turns.some(t => t.id === live.id)) turns.push(live);
  const html = turns.map(t => renderTurn(live && t.id === live.id ? live : t, !!(live && t.id === live.id))).join('');
  if (!html) {
    box.innerHTML = `<div class="council-empty">
      <h3>Convene a council</h3>
      Seat a few models above — Claude and ChatGPT subscriptions sit next to your API and local models.
      <ol>
        <li>Every member answers your question on its own.</li>
        <li>Each member ranks the others' answers blind (Full mode).</li>
        <li>The chairman writes one answer from all of it.</li>
      </ol>
    </div>`;
  } else {
    box.innerHTML = html;
    try { renderMath(box); } catch { /* optional */ }
  }
  renderComposer();
}

function scrollToEnd() {
  const box = $('#council-scroll');
  if (box) box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------------------
// live streaming
// ---------------------------------------------------------------------------

const _dirty = new Set();
let _rafPending = false;

function markDirty(stage, member) {
  _dirty.add(`${stage}|${member}`);
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(flushDirty);
}

function flushDirty() {
  _rafPending = false;
  const live = S.live;
  const box = $('#council-scroll');
  if (!live || !box || live.sessionId !== S.session?.id) { _dirty.clear(); return; }
  const article = box.querySelector(`[data-turn="${CSS.escape(live.id)}"]`);
  if (!article) { _dirty.clear(); return; }
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  for (const key of _dirty) {
    const [stage, member] = key.split('|');
    if (stage === 'synthesis') {
      const body = article.querySelector('[data-final] .council-final-b');
      if (body) { body.classList.toggle('pending', !live.final); body.innerHTML = md(live.final); }
      continue;
    }
    const entry = (stage === 'opinions' ? live.opinions : live.reviews).find(e => String(e.member) === member);
    const card = article.querySelector(`.council-card[data-stage="${stage}"][data-member="${member}"]`);
    if (!card || !entry) continue;
    const body = card.querySelector('.council-card-b');
    body.classList.toggle('pending', !entry.text && !entry.error);
    body.innerHTML = entry.error ? `<div class="council-errtext">${esc(entry.error)}</div>` : md(entry.text);
    body.scrollTop = body.scrollHeight;
    const state = card.querySelector('.state');
    if (state) state.textContent = entry.error ? 'error' : entry.streaming ? (entry.thinking && !entry.text ? 'thinking…' : 'writing…') : fmtMs(entry.ms);
    card.classList.toggle('err', !!entry.error);
  }
  _dirty.clear();
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

function liveEntry(stage, member) {
  const list = stage === 'opinions' ? S.live.opinions : S.live.reviews;
  let entry = list.find(e => e.member === member);
  if (!entry) { entry = { member, text: '', streaming: true }; list.push(entry); }
  return entry;
}

function onEvent(ev) {
  const live = S.live;
  if (!live) return;
  switch (ev.type) {
    case 'turn':
      live.id = ev.turn_id;
      live.config = { members: ev.members, chairman: ev.chairman, mode: ev.mode };
      renderTurns();
      scrollToEnd();
      break;
    case 'stage':
      live.stages[ev.stage] = ev.status === 'start' ? 'running' : 'done';
      if (ev.labels) live.labels = ev.labels;
      if (ev.stage === 'review' && ev.status === 'start') live.reviews = [];
      renderTurns();
      if (ev.status === 'start') scrollToEnd();
      break;
    case 'thinking':
      if (ev.stage === 'synthesis') break;
      liveEntry(ev.stage, ev.member).thinking = true;
      markDirty(ev.stage, ev.member);
      break;
    case 'delta':
      if (ev.stage === 'synthesis') { live.final += ev.text; markDirty('synthesis', 'chair'); break; }
      liveEntry(ev.stage, ev.member).text += ev.text;
      markDirty(ev.stage, ev.member);
      break;
    case 'member_done':
    case 'member_error': {
      if (ev.stage === 'synthesis') {
        if (ev.type === 'member_error') live.chairError = ev.error;
        break;
      }
      const entry = liveEntry(ev.stage, ev.member);
      entry.streaming = false;
      entry.ms = ev.ms;
      if (ev.type === 'member_error') entry.error = ev.error;
      markDirty(ev.stage, ev.member);
      break;
    }
    case 'ranking':
      live.ranking = ev.ranking || [];
      live.labels = ev.labels || live.labels;
      renderTurns();
      break;
    case 'done':
      live.status = ev.status || 'done';
      if (typeof ev.final === 'string' && ev.final) live.final = ev.final;
      live.error = ev.error || null;
      break;
    default:
      break;
  }
}

async function convene() {
  const input = $('#council-input');
  const question = (input?.value || '').trim();
  if (!question || S.live) return;
  if (!S.members.length) { toast('Seat at least one member'); return; }
  const chair = chairmanSeat();
  if (!S.session) {
    try {
      const data = await jpost('/api/council/sessions', { config: currentConfig() });
      S.sessions.unshift(data);
      S.session = data;
      storeSet(data.id);
    } catch (e) { toast(e.message); return; }
  }
  S.picker = false;
  S.live = {
    id: `live-${Date.now()}`, sessionId: S.session.id, question, status: 'running', config: { members: [], chairman: null, mode: S.mode },
    opinions: [], reviews: [], ranking: [], labels: {}, final: '', error: null,
    stages: { opinions: 'running', review: 'pending', synthesis: 'pending' }, reader: null,
  };
  // Show the seats immediately, before the server's first event.
  S.live.config.members = S.members.map((m, i) => ({ index: i, model: m.model, endpoint_name: seatInfo(m).endpoint }));
  S.live.config.chairman = { model: chair.model, endpoint_name: seatInfo(chair).endpoint };
  input.value = '';
  input.style.height = 'auto';
  renderSeats();
  renderTurns();
  scrollToEnd();

  const sessionId = S.session.id;
  try {
    const res = await fetch(`/api/council/sessions/${encodeURIComponent(sessionId)}/ask`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, members: S.members, chairman: chair, mode: S.mode }),
    });
    if (!res.ok || !res.body) {
      let msg = `Request failed (${res.status})`;
      try { const d = await res.json(); msg = d.detail || d.error || msg; } catch { /* not json */ }
      throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    }
    const reader = res.body.getReader();
    if (S.live) S.live.reader = reader;
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data:')) continue;
          try { onEvent(JSON.parse(line.slice(5).trim())); } catch { /* partial */ }
        }
      }
    }
  } catch (e) {
    if (S.live && S.live.status === 'running' && !S.live.id.startsWith('live-')) {
      // Lost the stream but the run continues server-side; the poll picks it up.
    } else if (S.live) {
      toast(e.message || 'Council failed');
      if (input && !input.value) input.value = question;
    }
  } finally {
    const wasLive = S.live;
    S.live = null;
    if (_open && S.session?.id === sessionId) {
      try { S.session = await jget(`/api/council/sessions/${encodeURIComponent(sessionId)}`); } catch { /* keep */ }
      const idx = S.sessions.findIndex(s => s.id === sessionId);
      if (idx >= 0 && S.session) S.sessions[idx] = { ...S.sessions[idx], title: S.session.title };
      renderAll();
      if (wasLive) scrollToEnd();
      maybePoll();
    }
  }
}

// ---------------------------------------------------------------------------
// connections
// ---------------------------------------------------------------------------

async function connectClaude(mode) {
  const cs = S.connect.claude;
  const token = mode === 'token' ? ($('#council-claude-token')?.value || '').trim() : '';
  if (mode === 'token' && !token) { cs.msg = 'Paste the token printed by claude setup-token.'; cs.err = true; renderConnect(); return; }
  cs.busy = true; cs.err = false; cs.msg = 'Checking with a tiny test call…';
  renderConnect();
  try {
    const data = await jpost('/api/claude-subscription/connect', { mode, token: token || null });
    cs.msg = `Connected — ${(data.endpoint?.models || []).length} Claude models available.`;
    await afterConnect('claude', data.endpoint);
  } catch (e) {
    cs.err = true; cs.msg = e.message;
  } finally {
    cs.busy = false;
    renderConnect();
  }
}

async function disconnectClaude() {
  if (!confirm('Disconnect the Claude subscription from Odysseus? Seats using it will stop working until you reconnect.')) return;
  try {
    await jpost('/api/claude-subscription/disconnect', {});
    S.connect.claude.msg = 'Disconnected.'; S.connect.claude.err = false;
    await loadRoster();
    renderAll();
  } catch (e) { toast(e.message); }
}

async function connectChatGPT() {
  const gs = S.connect.chatgpt;
  gs.busy = true; gs.err = false; gs.msg = 'Starting OpenAI sign-in…'; gs.code = ''; gs.url = '';
  renderConnect();
  try {
    const result = await runProviderDeviceFlow('chatgpt-subscription', {
      // The link is shown in the panel; a programmatic window.open after an
      // await is usually popup-blocked and would double up when it is not.
      openWindow: () => {},
      onStart: ({ start, authUrl }) => {
        gs.code = start.user_code || '';
        gs.url = authUrl || '';
        gs.msg = 'Waiting for you to approve in the OpenAI tab…';
        renderConnect();
      },
    });
    gs.code = ''; gs.url = '';
    if (result.status === 'authorized') {
      gs.msg = `Connected — ${(result.endpoint?.models || []).length} ChatGPT models available.`;
      await afterConnect('chatgpt', result.endpoint);
    } else {
      gs.err = true;
      gs.msg = result.status === 'expired' ? 'Sign-in expired. Try again.' : `Sign-in failed (${result.error || 'denied'}).`;
    }
  } catch (e) {
    gs.code = ''; gs.url = '';
    gs.err = true; gs.msg = formatDeviceFlowError(e, 'ChatGPT sign-in failed');
  } finally {
    gs.busy = false;
    renderConnect();
  }
}

async function afterConnect(which, endpoint) {
  await loadRoster();
  // Seat the new subscription's first model if it is not on the council yet.
  const ep = endpoint?.id ? endpointById(endpoint.id) : null;
  if (ep && ep.models[0] && !S.members.some(m => m.endpoint_id === ep.id) && S.members.length < (S.roster?.max_members || 8)) {
    S.members.push({ endpoint_id: ep.id, model: ep.models[0] });
    saveConfigSoon();
  }
  // Same refresh the Settings → Models form does, so the chat model picker
  // shows the new subscription without a reload.
  try { await window.modelsModule?.refreshModels?.(true); } catch { /* optional */ }
  try { window.dispatchEvent(new CustomEvent('ge:model-endpoints-updated', { detail: { source: `council-${which}` } })); } catch { /* optional */ }
  try { window.sessionModule?.updateModelPicker?.(); } catch { /* optional */ }
  renderAll();
}

// ---------------------------------------------------------------------------
// events
// ---------------------------------------------------------------------------

async function onPaneClick(e) {
  const t = e.target;
  const sess = t.closest('[data-session]');
  const delSess = t.closest('[data-del-session]');
  if (delSess) { e.stopPropagation(); await deleteSession(delSess.dataset.delSession); return; }
  if (sess && !S.live) { if (sess.dataset.session !== S.session?.id) await openSession(sess.dataset.session); return; }
  if (sess && S.live) { toast('Wait for the council to finish, or stop it first'); return; }

  const rm = t.closest('[data-remove-seat]');
  if (rm) {
    const idx = Number(rm.dataset.removeSeat);
    const removed = S.members.splice(idx, 1)[0];
    if (S.chairman && removed && seatKey(S.chairman) === seatKey(removed) && !S.members.some(m => seatKey(m) === seatKey(removed))) S.chairman = null;
    saveConfigSoon(); renderSeats(); renderComposer();
    return;
  }
  const add = t.closest('[data-add-seat]');
  if (add) {
    const [endpoint_id, ...rest] = add.dataset.addSeat.split('::');
    S.members.push({ endpoint_id, model: rest.join('::') });
    if (S.members.length >= (S.roster?.max_members || 8)) S.picker = false;
    saveConfigSoon(); renderSeats(); renderComposer();
    return;
  }
  const mode = t.closest('[data-mode]');
  if (mode) { S.mode = mode.dataset.mode === 'quick' ? 'quick' : 'full'; saveConfigSoon(); renderSeats(); renderComposer(); return; }

  const stop = t.closest('[data-stop]');
  if (stop && S.session) {
    stop.disabled = true;
    const turnId = S.live && S.live.id === stop.dataset.stop ? S.live.id : stop.dataset.stop;
    try { await jpost(`/api/council/sessions/${encodeURIComponent(S.session.id)}/turns/${encodeURIComponent(turnId)}/stop`, {}); }
    catch (err) { toast(err.message); }
    return;
  }
  const delTurn = t.closest('[data-del-turn]');
  if (delTurn && S.session) {
    if (!confirm('Delete this question and its answers?')) return;
    try {
      await jdel(`/api/council/sessions/${encodeURIComponent(S.session.id)}/turns/${encodeURIComponent(delTurn.dataset.delTurn)}`);
      S.session.turns = (S.session.turns || []).filter(x => x.id !== delTurn.dataset.delTurn);
      renderTurns();
    } catch (err) { toast(err.message); }
    return;
  }
  const copy = t.closest('[data-copy-final]');
  if (copy) {
    const turn = (S.session?.turns || []).find(x => x.id === copy.dataset.copyFinal);
    const text = turn?.final || '';
    try { await navigator.clipboard.writeText(text); toast('Answer copied'); }
    catch { toast('Could not copy'); }
    return;
  }

  const act = t.closest('[data-act]')?.dataset.act;
  if (!act) {
    if (S.picker && !t.closest('.council-picker')) { S.picker = false; renderSeats(); }
    return;
  }
  if (act === 'toggle-picker') { S.picker = !S.picker; renderSeats(); }
  else if (act === 'claude-token') await connectClaude('token');
  else if (act === 'claude-host') await connectClaude('host');
  else if (act === 'claude-disconnect') await disconnectClaude();
  else if (act === 'chatgpt-connect') await connectChatGPT();
  else if (act === 'refresh-roster') { await loadRoster(); renderAll(); }
}

function onPaneChange(e) {
  if (e.target.id === 'council-chair') {
    const val = e.target.value;
    if (!val) return;
    const [endpoint_id, ...rest] = val.split('::');
    S.chairman = { endpoint_id, model: rest.join('::') };
    saveConfigSoon();
    renderComposer();
  }
}

const councilModule = { openPanel, closePanel, togglePanel, isPanelOpen };
export default councilModule;
