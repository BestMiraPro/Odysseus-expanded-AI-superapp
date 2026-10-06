// Budget: month-to-date spend on metered (pay-per-token) models and the
// guardrails that limit it. Renders Settings → Budget, keeps a slim banner
// above the chat input when the month nears or passes the cap, and gives the
// chat and Council pages shared money formatting.

const API = '/api/budget';
let _bannerDismissedState = '';

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

export function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'unknown';
  const n = Number(value);
  if (n === 0) return '$0';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function tokens(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

const SOURCE_LABELS = {
  chat: 'Chat', agent: 'Agent', delegation: 'Delegations', teacher: 'Teacher', council: 'Council',
};

async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  let body = null;
  try { body = await res.json(); } catch (_e) { /* empty body */ }
  if (!res.ok) {
    const detail = body && (body.detail?.message || body.detail || body.message);
    throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`);
  }
  return body;
}

export function fetchStatus() { return getJSON(`${API}/status`); }
export function fetchSummary() { return getJSON(API); }
export function saveSettings(patch) {
  return getJSON(`${API}/settings`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  });
}

/** Share the server's model prices with the chat cost display (chatRenderer). */
export async function loadServerPrices() {
  try {
    const data = await getJSON('/api/models/roster');
    const map = {};
    for (const m of data.models || []) {
      if (m.billing !== 'metered' || m.input_per_mtok == null) continue;
      const key = String(m.model).toLowerCase();
      if (map[key] && map[key].source === 'declared') continue;
      map[key] = { input: m.input_per_mtok, output: m.output_per_mtok, source: m.price_source };
    }
    globalThis.__odyServerPrices = map;
  } catch (_e) { /* chat keeps its built-in price table */ }
}

export function openBudgetSettings() {
  if (window.settingsModule && typeof window.settingsModule.open === 'function') {
    window.settingsModule.open('budget');
  }
}

function injectStyles() {
  if (document.getElementById('budget-styles')) return;
  const st = document.createElement('style');
  st.id = 'budget-styles';
  st.textContent = `
.budget-hero { padding: 10px 0 12px; }
.budget-spent { font-size: 26px; font-weight: 650; letter-spacing: -0.01em; }
.budget-sub { font-size: 12px; opacity: 0.7; margin-top: 2px; }
.budget-bar { height: 8px; border-radius: 6px; background: var(--border, rgba(127,127,127,0.25)); overflow: hidden; margin-top: 10px; }
.budget-bar > span { display: block; height: 100%; border-radius: 6px; background: var(--accent, #4a8cff); transition: width .3s; }
.budget-bar.warn > span { background: #d9a400; }
.budget-bar.over > span { background: #d64545; }
.budget-form .settings-row { align-items: center; }
.budget-form input.settings-select { max-width: 140px; }
.budget-help { font-size: 11.5px; opacity: 0.65; margin: -2px 0 8px; line-height: 1.4; }
.budget-actions { display: flex; align-items: center; gap: 10px; margin: 6px 0 4px; }
.budget-save-status { font-size: 12px; opacity: 0.75; }
.budget-save-status.error { color: #d64545; opacity: 1; }
.budget-h3 { font-size: 12.5px; font-weight: 600; margin: 16px 0 6px; opacity: 0.85; }
.budget-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.budget-table th, .budget-table td { text-align: left; padding: 5px 6px; border-bottom: 1px solid var(--border, rgba(127,127,127,0.18)); }
.budget-table th { font-weight: 600; opacity: 0.7; }
.budget-table td.num, .budget-table th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.budget-table td.model { word-break: break-all; }
.budget-empty { font-size: 12px; opacity: 0.6; padding: 6px 0; }
.budget-note { font-size: 11.5px; opacity: 0.6; margin-top: 14px; line-height: 1.45; }
.budget-recent > summary { cursor: pointer; font-size: 12.5px; font-weight: 600; margin: 16px 0 6px; opacity: 0.85; }
.budget-banner { display: flex; align-items: center; gap: 10px; margin: 0 auto 6px; max-width: var(--chat-max-width, 820px);
  width: calc(100% - 24px); padding: 7px 12px; border-radius: 10px; font-size: 12.5px;
  background: rgba(217, 164, 0, 0.12); border: 1px solid rgba(217, 164, 0, 0.45); }
.budget-banner.over { background: rgba(214, 69, 69, 0.12); border-color: rgba(214, 69, 69, 0.5); }
.budget-banner[hidden] { display: none; }
.budget-banner .budget-banner-text { flex: 1; }
.budget-banner button { background: none; border: 1px solid currentColor; border-radius: 7px; color: inherit;
  font-size: 12px; padding: 3px 9px; cursor: pointer; opacity: 0.85; }
.budget-banner button.budget-banner-close { border: none; font-size: 15px; padding: 0 4px; }
.budget-error-actions { margin-top: 8px; }
.budget-error-actions button { background: none; border: 1px solid currentColor; border-radius: 7px; color: inherit;
  font-size: 12px; padding: 3px 10px; cursor: pointer; opacity: 0.85; }
`;
  document.head.appendChild(st);
}

/* ── Settings → Budget ─────────────────────────────────────────────── */

function breakdownTable(rows, firstHeader, labelFor) {
  if (!rows || !rows.length) return '<div class="budget-empty">Nothing yet this month.</div>';
  return `<table class="budget-table"><thead><tr><th>${firstHeader}</th><th class="num">Calls</th>
    <th class="num">Tokens in / out</th><th class="num">Cost</th></tr></thead><tbody>${rows.map(r => `
    <tr><td class="model">${labelFor(r)}</td><td class="num">${r.calls}</td>
    <td class="num">${tokens(r.input_tokens)} / ${tokens(r.output_tokens)}</td>
    <td class="num">${money(r.cost_usd)}</td></tr>`).join('')}</tbody></table>`;
}

function heroHtml(s) {
  const cap = Number(s.monthly_cap_usd) || 0;
  const monthName = (() => {
    try {
      const [y, m] = String(s.month || '').split('-').map(Number);
      return new Date(Date.UTC(y, m - 1, 1)).toLocaleString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' });
    } catch (_e) { return 'this month'; }
  })();
  const parts = [`spent in ${esc(monthName)}`, `${s.calls} metered call${s.calls === 1 ? '' : 's'}`];
  if (s.projected_usd && s.spent_usd) parts.push(`on pace for ${money(s.projected_usd)}`);
  if (s.unpriced_calls) parts.push(`${s.unpriced_calls} unpriced`);
  let capHtml = '<div class="budget-sub">No monthly cap set.</div>';
  if (cap > 0) {
    const pct = Math.min(100, Math.round((s.spent_usd / cap) * 100));
    const cls = s.state === 'over' ? 'over' : s.state === 'warn' ? 'warn' : '';
    const left = s.state === 'over'
      ? (s.cap_action === 'block' ? 'cap reached: metered models are blocked' : 'cap passed (warn only)')
      : `${money(s.remaining_usd)} left`;
    capHtml = `<div class="budget-bar ${cls}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"
      aria-label="Monthly budget used"><span style="width:${pct}%"></span></div>
      <div class="budget-sub">${pct}% of ${money(cap)} cap · ${esc(left)}</div>`;
  }
  return `<div class="budget-hero"><div class="budget-spent">${money(s.spent_usd)}</div>
    <div class="budget-sub">${parts.join(' · ')}</div>${capHtml}</div>`;
}

function formHtml(s) {
  const val = v => (Number(v) > 0 ? String(Number(v)) : '');
  return `<form class="budget-form settings-col" id="budget-form" novalidate>
    <div class="settings-row"><label class="settings-label" for="budget-cap">Monthly cap ($)</label>
      <input id="budget-cap" class="settings-select" type="number" inputmode="decimal" min="0" step="0.01" placeholder="No cap" value="${val(s.monthly_cap_usd)}"></div>
    <div class="settings-row"><label class="settings-label" for="budget-cap-action">When the cap is reached</label>
      <select id="budget-cap-action" class="settings-select" style="max-width:240px">
        <option value="block"${s.cap_action === 'block' ? ' selected' : ''}>Block metered models</option>
        <option value="warn"${s.cap_action === 'warn' ? ' selected' : ''}>Warn only</option>
      </select></div>
    <div class="settings-row"><label class="settings-label" for="budget-limit">Ask before one action above ($)</label>
      <input id="budget-limit" class="settings-select" type="number" inputmode="decimal" min="0" step="0.01" placeholder="Never ask" value="${val(s.action_limit_usd)}"></div>
    <div class="budget-help">A Council turn estimated above this asks you first. An agent delegation above it is refused,
      and the agent gets cheaper options, since an agent can't approve its own spend. Leave empty for no limit.</div>
    <div class="budget-actions"><button type="submit" class="admin-btn-sm" id="budget-save">Save</button>
      <span class="budget-save-status" id="budget-save-status" role="status" aria-live="polite"></span></div>
  </form>`;
}

function recentHtml(rows) {
  if (!rows || !rows.length) return '';
  return `<details class="budget-recent"><summary>Recent metered calls</summary>
    <table class="budget-table"><thead><tr><th>When</th><th>Source</th><th>Model</th><th class="num">Cost</th></tr></thead>
    <tbody>${rows.map(r => {
      const when = new Date(r.at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      const cost = r.cost_usd === null || r.cost_usd === undefined ? 'unpriced' : `${r.estimated ? '≈' : ''}${money(r.cost_usd)}`;
      return `<tr><td>${esc(when)}</td><td>${esc(SOURCE_LABELS[r.source] || r.source)}</td>
        <td class="model">${esc(r.model)}</td><td class="num">${cost}</td></tr>`;
    }).join('')}</tbody></table></details>`;
}

export async function renderPanel(root = document.getElementById('budget-panel-body')) {
  if (!root) return;
  injectStyles();
  loadServerPrices();
  let s;
  try {
    s = await fetchSummary();
  } catch (err) {
    root.innerHTML = `<div class="budget-empty">Could not load the budget: ${esc(err.message)}</div>`;
    return;
  }
  root.innerHTML = `${heroHtml(s)}${formHtml(s)}
    <div class="budget-h3">By source</div>
    ${breakdownTable(s.by_source, 'Source', r => esc(SOURCE_LABELS[r.name] || r.name))}
    <div class="budget-h3">By model</div>
    ${breakdownTable(s.by_model, 'Model', r => `${esc(r.name)}${r.endpoint_name ? ` <span style="opacity:.6">· ${esc(r.endpoint_name)}</span>` : ''}`)}
    ${recentHtml(s.recent)}
    <div class="budget-note">Counted: chat, agent rounds (scheduled tasks included, and stopped turns up to where
      they stopped), agent delegations and Council turns on metered models, priced from your declared costs or
      OpenRouter's public price list. Rows marked ≈ were estimated from text length because the provider reported no
      token counts. Requests running at the same moment can overshoot the cap by about one turn. Not counted yet:
      background work (chat titles, memory, research, Study) and Omnigent. Months follow UTC.</div>`;
  const form = root.querySelector('#budget-form');
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const status = root.querySelector('#budget-save-status');
    const btn = root.querySelector('#budget-save');
    // A number field holding text it cannot parse ("1,000", "50$") reads as
    // "" — that must be an error, not a silently removed cap.
    const num = id => {
      const input = root.querySelector(id);
      if (input.validity && input.validity.badInput) return NaN;
      const raw = input.value.trim();
      return raw === '' ? 0 : Number(raw);
    };
    const patch = {
      monthly_cap_usd: num('#budget-cap'),
      cap_action: root.querySelector('#budget-cap-action').value,
      action_limit_usd: num('#budget-limit'),
    };
    if ([patch.monthly_cap_usd, patch.action_limit_usd].some(v => !Number.isFinite(v) || v < 0)) {
      status.textContent = 'Amounts must be plain numbers, zero or more (e.g. 25 or 0.5).';
      status.classList.add('error');
      return;
    }
    btn.disabled = true;
    status.classList.remove('error');
    status.textContent = 'Saving…';
    try {
      await saveSettings(patch);
      await renderPanel(root);
      const fresh = root.querySelector('#budget-save-status');
      if (fresh) fresh.textContent = 'Saved.';
      refreshBanner(true);
    } catch (err) {
      status.textContent = err.message;
      status.classList.add('error');
      btn.disabled = false;
    }
  });
}

/* ── Chat banner ───────────────────────────────────────────────────── */

function ensureBanner() {
  let el = document.getElementById('budget-banner');
  if (el) return el;
  const bar = document.querySelector('.chat-input-bar');
  if (!bar || !bar.parentNode) return null;
  injectStyles();
  el = document.createElement('div');
  el.id = 'budget-banner';
  el.className = 'budget-banner';
  el.hidden = true;
  el.setAttribute('role', 'status');
  el.innerHTML = `<span class="budget-banner-text"></span>
    <button type="button" class="budget-banner-open">Budget</button>
    <button type="button" class="budget-banner-close" aria-label="Dismiss">×</button>`;
  el.querySelector('.budget-banner-open').addEventListener('click', openBudgetSettings);
  el.querySelector('.budget-banner-close').addEventListener('click', () => {
    _bannerDismissedState = el.dataset.state || '';
    el.hidden = true;
  });
  bar.parentNode.insertBefore(el, bar);
  return el;
}

function bannerText(s) {
  const cap = money(s.monthly_cap_usd);
  if (s.state === 'over') {
    return s.cap_action === 'block'
      ? `Monthly budget reached: ${money(s.spent_usd)} of ${cap}. Metered models are blocked until next month; subscription and local models still work.`
      : `Over this month's budget: ${money(s.spent_usd)} of ${cap}.`;
  }
  return `${Math.round((s.fraction || 0) * 100)}% of this month's ${cap} budget used (${money(s.spent_usd)}).`;
}

let _bannerTimer = null;
export async function refreshBanner(force = false) {
  clearTimeout(_bannerTimer);
  const run = async () => {
    let s;
    try { s = await fetchStatus(); } catch (_e) { return; }
    const el = ensureBanner();
    if (!el) return;
    const show = s.state === 'warn' || s.state === 'over';
    // A dismissed banner stays hidden until the state changes (warn -> over).
    if (!show || (!force && _bannerDismissedState === s.state)) {
      el.hidden = true;
      if (!show) _bannerDismissedState = '';
      return;
    }
    el.dataset.state = s.state;
    el.classList.toggle('over', s.state === 'over');
    el.querySelector('.budget-banner-text').textContent = bannerText(s);
    el.hidden = false;
  };
  if (force) return run();
  _bannerTimer = setTimeout(run, 400);
}

/** Append a "Budget settings" button under a chat error that came from the cap. */
export function decorateBudgetError(bodyEl) {
  if (!bodyEl || bodyEl.querySelector('.budget-error-actions')) return;
  injectStyles();
  const wrap = document.createElement('div');
  wrap.className = 'budget-error-actions';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = 'Open budget settings';
  btn.addEventListener('click', openBudgetSettings);
  wrap.appendChild(btn);
  bodyEl.appendChild(wrap);
}

const budgetModule = { money, fetchStatus, fetchSummary, saveSettings, renderPanel, refreshBanner,
  openBudgetSettings, decorateBudgetError, loadServerPrices };
export default budgetModule;
