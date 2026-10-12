// Reasoning effort selector for places that keep their own level: Council
// seats, Compare columns, the Study header and Settings roles. (The chat
// composer sets a level per model instead: effortPicker.js.)
// The levels a model accepts come from the backend (src/reasoning_effort.py),
// via /api/models/roster or a place's own payload; a model that accepts none
// gets no selector. "" means the place has no level of its own: replies use
// the level saved for the model in the chat composer, else the model's default.

export const EFFORT_LABELS = {
  none: 'None', minimal: 'Minimal', low: 'Low', medium: 'Medium',
  high: 'High', xhigh: 'Extra high', max: 'Max',
};

const STYLE_ID = 'effort-select-style';
const CSS = `
.effort-select { height: 21px; padding: 0 4px; font-size: 11px; font-family: inherit;
  color: color-mix(in srgb, var(--fg) 75%, transparent); background: transparent;
  border: 1px solid color-mix(in srgb, var(--fg) 18%, transparent); border-radius: 4px;
  cursor: pointer; max-width: 150px; }
.effort-select:hover, .effort-select:focus-visible { border-color: color-mix(in srgb, var(--fg) 40%, transparent); }
.effort-select option { color: var(--fg); background: var(--bg); }
`;

export function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

export function effortLabel(level) {
  return EFFORT_LABELS[level] || level;
}

// Option markup for a <select>; the empty option names the default it stands for.
export function effortOptionsHtml(levels, value, defaultLevel, defaultText) {
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const base = defaultText || (defaultLevel ? `Effort: default (${effortLabel(defaultLevel)})` : 'Effort: default');
  const current = levels.includes(value) ? value : '';
  return [`<option value=""${current ? '' : ' selected'}>${esc(base)}</option>`]
    .concat(levels.map((lv) => `<option value="${esc(lv)}"${lv === current ? ' selected' : ''}>Effort: ${esc(effortLabel(lv))}</option>`))
    .join('');
}

// The same control as markup, for views rendered from HTML strings; "" when the
// model accepts no effort levels. ``attrs`` identifies it to delegated handlers.
export function effortSelectHtml(levels, value, defaultLevel, attrs) {
  if (!Array.isArray(levels) || !levels.length) return '';
  ensureStyle();
  return `<select class="effort-select" ${attrs || ''} aria-label="Reasoning effort" `
    + `title="Reasoning effort: how hard the model thinks before answering. `
    + `Default: the level saved for this model in the chat box, else the model's own.">`
    + `${effortOptionsHtml(levels, value || '', defaultLevel)}</select>`;
}

// A ready <select>, or null when the model accepts no effort levels.
export function createEffortSelect({ levels, value, defaultLevel, defaultText, onChange, title, className }) {
  if (!Array.isArray(levels) || !levels.length) return null;
  ensureStyle();
  const sel = document.createElement('select');
  sel.className = 'effort-select' + (className ? ` ${className}` : '');
  sel.title = title || 'Reasoning effort: how hard the model thinks before answering';
  sel.setAttribute('aria-label', 'Reasoning effort');
  sel.innerHTML = effortOptionsHtml(levels, value || '', defaultLevel, defaultText);
  if (onChange) sel.addEventListener('change', () => onChange(sel.value || ''));
  return sel;
}

// Roster lookup (endpoint_id::model, else a unique model id), shared and cached.
let _roster = null;
let _rosterAt = 0;
let _rosterInflight = null;

export function loadEffortRoster(force = false) {
  if (_rosterInflight) return _rosterInflight;
  if (!force && _roster && Date.now() - _rosterAt < 120000) return Promise.resolve(_roster);
  _rosterInflight = fetch('/api/models/roster', { credentials: 'same-origin' })
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data || !Array.isArray(data.models)) return _roster;
      const map = new Map();
      const bare = new Map();
      for (const m of data.models) {
        const info = { levels: m.effort_levels || [], defaultLevel: m.default_effort || null };
        map.set(`${m.endpoint_id}::${m.model}`, info);
        bare.set(m.model, bare.has(m.model) ? null : info);
      }
      for (const [mid, info] of bare) if (info && !map.has(mid)) map.set(mid, info);
      _roster = map;
      _rosterAt = Date.now();
      return _roster;
    })
    .catch(() => _roster)
    .finally(() => { _rosterInflight = null; });
  return _rosterInflight;
}

export function effortInfoFromRoster(endpointId, model) {
  if (!_roster || !model) return null;
  return _roster.get(`${endpointId || ''}::${model}`) || _roster.get(model) || null;
}
