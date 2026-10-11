// Effort picker — per-model reasoning effort, next to the model picker.
//
// The level is saved per model for the signed-in user (/api/models/effort) and
// applied server-side to every streamed reply of that model: chat, agents,
// Council and the Study tutor. The button is hidden for models whose provider
// has no effort control.

import uiModule from './ui.js';

const _cache = new Map();   // `${url}::${model}` -> state from the server
let _route = null;          // {model, url} currently shown
let _seq = 0;

const SHORT = { low: 'Low', medium: 'Med', high: 'High', xhigh: 'XHigh', max: 'Max' };

function _els() {
  return {
    wrap: document.getElementById('effort-wrap'),
    btn: document.getElementById('effort-btn'),
    label: document.getElementById('effort-label'),
    menu: document.getElementById('effort-menu'),
  };
}

function _key(route) {
  return `${route.url || ''}::${route.model}`;
}

async function _fetchState(route) {
  const key = _key(route);
  if (_cache.has(key)) return _cache.get(key);
  const q = new URLSearchParams({ model: route.model, url: route.url || '' });
  const r = await fetch(`/api/models/effort?${q}`, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`effort lookup failed (${r.status})`);
  const state = await r.json();
  _cache.set(key, state);
  return state;
}

function _render(state) {
  const { wrap, btn, label } = _els();
  if (!wrap || !btn || !label) return;
  const levels = (state && state.levels) || [];
  if (!levels.length) {
    wrap.hidden = true;
    _closeMenu();
    return;
  }
  wrap.hidden = false;
  const applied = state.applied;
  label.textContent = applied ? SHORT[applied] || applied : '';
  btn.classList.toggle('is-default', !applied);
  const name = String(state.model || '').split('/').pop();
  let title = applied
    ? `Effort for ${name}: ${(levels.find(l => l.id === applied) || {}).label || applied}`
    : `Effort for ${name}: provider default`;
  if (state.effort && applied && state.effort !== applied) {
    title += ` (saved ${state.effort}; this model goes up to ${applied})`;
  }
  btn.title = title;
  btn.setAttribute('aria-label', title);
}

function _closeMenu() {
  const { menu, btn } = _els();
  if (!menu || menu.classList.contains('hidden')) return;
  menu.classList.add('hidden');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

function _openMenu(state) {
  const { menu, btn } = _els();
  if (!menu || !btn) return;
  // One drop-up at a time: close the model list if it is open.
  const modelMenu = document.getElementById('model-picker-menu');
  if (modelMenu) modelMenu.classList.add('hidden');

  menu.replaceChildren();
  const head = document.createElement('div');
  head.className = 'effort-menu-head';
  head.textContent = `Effort · ${String(state.model || '').split('/').pop()}`;
  menu.appendChild(head);

  const levels = state.levels || [];
  const current = state.effort || '';
  // A saved level above this model's range runs clamped; mark what runs.
  const checkedId = !current || levels.some(l => l.id === current) ? current : state.applied;
  [{ id: '', label: 'Default' }].concat(levels).forEach(opt => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'effort-menu-item';
    item.setAttribute('role', 'menuitemradio');
    const selected = opt.id === checkedId;
    item.setAttribute('aria-checked', selected ? 'true' : 'false');
    if (selected) item.classList.add('selected');
    item.textContent = opt.label;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      _choose(opt.id || null);
    });
    menu.appendChild(item);
  });

  const note = document.createElement('div');
  note.className = 'effort-menu-note';
  note.textContent = 'Saved for this model. Used in chat, agents, Council and the Study tutor.';
  menu.appendChild(note);

  menu.classList.remove('hidden');
  btn.setAttribute('aria-expanded', 'true');
  const focusItem = menu.querySelector('.effort-menu-item.selected') || menu.querySelector('.effort-menu-item');
  if (focusItem) focusItem.focus({ preventScroll: true });
}

async function _choose(level) {
  _closeMenu();
  const route = _route;
  if (!route) return;
  try {
    const r = await fetch('/api/models/effort', {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: route.model, url: route.url || '', effort: level }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const state = await r.json();
    // The level is per model, so every endpoint serving it changes too.
    for (const key of [..._cache.keys()]) {
      if (key.endsWith(`::${route.model}`)) _cache.delete(key);
    }
    _cache.set(_key(route), state);
    if (_route && _key(_route) === _key(route)) _render(state);
    const lbl = state.applied
      ? (state.levels.find(l => l.id === state.applied) || {}).label || state.applied
      : 'provider default';
    uiModule.showToast(`Effort for ${route.model.split('/').pop()}: ${lbl}`);
  } catch (e) {
    uiModule.showError('Could not save effort: ' + e.message);
  }
}

/** Show the effort control for the model the composer will send to. */
export async function refreshEffortPicker(route) {
  const { wrap } = _els();
  if (!wrap) return;
  if (!route || !route.model) {
    _route = null;
    wrap.hidden = true;
    _closeMenu();
    return;
  }
  const changed = !_route || _key(_route) !== _key(route);
  _route = { model: route.model, url: route.url || '' };
  if (changed) _closeMenu();
  const seq = ++_seq;
  try {
    const state = await _fetchState(_route);
    if (seq === _seq) _render(state);
  } catch (_) {
    if (seq === _seq) wrap.hidden = true;
  }
}

export function initEffortPicker() {
  const { wrap, btn, menu } = _els();
  if (!wrap || !btn || !menu || wrap.dataset.effortBound === '1') return;
  wrap.dataset.effortBound = '1';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!menu.classList.contains('hidden')) { _closeMenu(); return; }
    if (!_route) return;
    try {
      _openMenu(await _fetchState(_route));
    } catch (_) {
      uiModule.showError('Could not load effort levels');
    }
  });
  menu.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { _closeMenu(); btn.focus(); }
  });
  // Capture phase: the model picker button stops propagation of its own clicks.
  document.addEventListener('pointerdown', (e) => {
    if (!wrap.contains(e.target)) _closeMenu();
  }, true);
}
