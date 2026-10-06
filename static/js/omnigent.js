import * as Modals from './modalManager.js';

const MODAL_ID = 'omnigent-modal';
const BODY_ID = 'omnigent-body';

let _loaded = false;
let _state = {
  status: null,
  launching: false,
  busy: '',            // 'apply' | 'restart' | 'stop' while a request runs
  error: '',
  crew: null,          // GET /api/omnigent/orchestrator
  crewError: '',
  draft: null,         // unsaved {orchestrator, reasoning_effort, max_workers}
  showRoster: false,
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

async function fetchJson(url, fallback) {
  try {
    const res = await fetch(url, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return await res.json();
  } catch (err) {
    _state.error = err?.message || 'Request failed';
    return fallback;
  }
}

async function postJson(url, payload = {}) {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || 'Request failed');
  return data;
}

function installCommand(status) {
  return status?.install?.recommended || 'curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh';
}

function omnigentUiUrl(status) {
  // Omnigent's web UI is bridged from its in-container loopback port by socat,
  // which speaks plain HTTP, so the link is http: even when Odysseus is served
  // over HTTPS.
  const port = Number(status?.ui_port) || 6868;
  return `http://${location.hostname}:${port}`;
}

function ensureModal() {
  let modal = document.getElementById(MODAL_ID);
  if (!modal) {
    modal = document.createElement('div');
    modal.id = MODAL_ID;
    modal.className = 'modal hidden';
    modal.innerHTML = `
      <div class="modal-content omnigent-modal-content" role="dialog" aria-label="Omnigent">
        <div class="modal-header omnigent-modal-header">
          <h4>Omnigent</h4>
          <button class="close-btn" id="close-omnigent-modal" aria-label="Close Omnigent">x</button>
        </div>
        <div id="${BODY_ID}" class="modal-body omnigent-body"></div>
      </div>`;
    document.body.appendChild(modal);
  }
  const close = modal.querySelector('#close-omnigent-modal');
  if (close && !close.dataset.omnigentBound) {
    close.dataset.omnigentBound = '1';
    close.addEventListener('click', () => closeModal());
  }
  Modals.injectMinimizeButton(modal, MODAL_ID);
  Modals.register(MODAL_ID, {
    railBtnId: 'rail-omnigent',
    sidebarBtnId: 'tool-omnigent-btn',
    label: 'Omnigent',
    icon: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4"/><path d="M12 17v4"/><path d="M4.9 6.9l2.8 2.8"/><path d="M16.3 16.3l2.8 2.8"/><path d="M3 12h4"/><path d="M17 12h4"/><circle cx="12" cy="12" r="5"/></svg>',
    restoreFn: () => { modal.classList.remove('hidden'); refresh(); },
    closeFn: () => { modal.classList.add('hidden'); },
  });
  injectStyles();
  return modal;
}

function injectStyles() {
  if (document.getElementById('omnigent-crew-styles')) return;
  const st = document.createElement('style');
  st.id = 'omnigent-crew-styles';
  st.textContent = `
.omnigent-crew { margin-top: 16px; border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
.omnigent-crew h5 { margin: 0 0 4px; font-size: 13px; }
.omnigent-crew .sub { font-size: 11.5px; opacity: 0.65; line-height: 1.5; margin: 0 0 10px; }
.omnigent-crew-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }
.omnigent-crew-row label { font-size: 11px; text-transform: uppercase; letter-spacing: .5px; opacity: .6; }
.omnigent-crew select, .omnigent-crew input { padding: 5px 7px; font-size: 12.5px; border-radius: 6px;
  border: 1px solid var(--border); background: var(--bg); color: var(--fg); max-width: 100%; }
.omnigent-crew select#omnigent-orchestrator { flex: 1 1 260px; min-width: 0; }
.omnigent-crew input[type=number] { width: 64px; }
.omnigent-roster { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
.omnigent-roster th, .omnigent-roster td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border); }
.omnigent-roster th { font-weight: 600; opacity: .6; font-size: 11px; }
.omnigent-roster-wrap { max-height: 260px; overflow: auto; }
.omnigent-star { color: #e0b03c; }
.omnigent-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.omnigent-warn { font-size: 11.5px; line-height: 1.5; margin-top: 12px; padding: 8px 10px; border-radius: 8px;
  background: color-mix(in srgb, #e0a252 14%, transparent); }
`;
  document.head.appendChild(st);
}

async function copyText(text, label) {
  const toast = window.uiModule?.showToast;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const input = document.createElement('textarea');
      input.value = text;
      input.style.position = 'fixed';
      input.style.opacity = '0';
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      input.remove();
    }
    toast?.(`${label} copied`);
  } catch (err) {
    toast?.(`Could not copy ${label.toLowerCase()}`);
  }
}

async function launchCrew() {
  const toast = window.uiModule?.showToast;
  _state.launching = true;
  _state.error = '';
  render();
  try {
    const data = await postJson('/api/omnigent/launch', {});
    _state.status = { ...(_state.status || {}), ...data };
    if (data.running) {
      const n = data.api_models?.workers || 0;
      toast?.(n ? `Omnigent ready — crew has ${n} sub-agents` : 'Omnigent ready — click Open Omnigent');
    } else if (data.installed === false) {
      toast?.('Omnigent not available — check setup');
    } else {
      _state.error = data.error || 'Launch attempted, but the server did not come up. Check the Omnigent logs.';
    }
  } catch (err) {
    _state.error = err?.message || 'Launch failed';
  } finally {
    _state.launching = false;
    render();
  }
}

async function loadCrew() {
  try {
    const res = await fetch('/api/omnigent/orchestrator', { credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.detail || 'Unavailable'), { status: res.status });
    _state.crew = data;
    _state.crewError = '';
    _state.draft = {
      orchestrator: data.settings?.orchestrator || 'claude',
      reasoning_effort: data.settings?.reasoning_effort || 'high',
      max_workers: data.settings?.max_workers || 40,
    };
  } catch (err) {
    _state.crew = null;
    _state.crewError = err.status === 403 ? 'Only an admin can choose the crew orchestrator.' : (err.message || 'Could not load the crew');
  }
}

async function applyCrew() {
  const toast = window.uiModule?.showToast;
  const running = !!_state.status?.running;
  _state.busy = 'apply';
  render();
  try {
    const data = await postJson('/api/omnigent/orchestrator', { ..._state.draft, apply: true });
    _state.status = { ...(_state.status || {}), ...data };
    toast?.(running ? 'Crew updated — Omnigent restarted' : 'Crew updated — it will be used on the next launch');
    await loadCrew();
  } catch (err) {
    toast?.(err?.message || 'Could not update the crew');
  } finally {
    _state.busy = '';
    render();
  }
}

async function serverAction(action) {
  const toast = window.uiModule?.showToast;
  _state.busy = action;
  render();
  try {
    const data = await postJson(action === 'stop' ? '/api/omnigent/server/stop' : '/api/omnigent/server/restart', {});
    _state.status = { ...(_state.status || {}), ...data };
    toast?.(action === 'stop' ? 'Omnigent stopped' : 'Models re-synced — Omnigent restarted');
  } catch (err) {
    toast?.(err?.message || 'Request failed');
  } finally {
    _state.busy = '';
    render();
  }
}

function optionLabel(o) {
  const bits = [o.recommended ? '★ ' : '', o.label];
  if (o.cost_band) bits.push(` (${o.cost_band})`);
  return bits.join('');
}

function renderCrew() {
  if (_state.crewError) {
    return `<section class="omnigent-crew"><h5>Crew</h5><p class="sub">${esc(_state.crewError)}</p></section>`;
  }
  const crew = _state.crew;
  if (!crew) return `<section class="omnigent-crew"><h5>Crew</h5><p class="sub">Loading models…</p></section>`;
  const d = _state.draft || {};
  const groups = [];
  for (const o of crew.options || []) {
    let g = groups.find(x => x.name === o.group);
    if (!g) { g = { name: o.group, options: [] }; groups.push(g); }
    g.options.push(o);
  }
  for (const g of groups) g.options.sort((a, b) => Number(b.recommended) - Number(a.recommended));
  const current = (crew.options || []).find(o => o.id === d.orchestrator);
  const isCodex = String(d.orchestrator || '').startsWith('codex');
  const changed = crew.settings && (d.orchestrator !== crew.settings.orchestrator
    || d.reasoning_effort !== crew.settings.reasoning_effort || Number(d.max_workers) !== Number(crew.settings.max_workers));
  const running = !!_state.status?.running;
  const workers = crew.workers || [];
  return `<section class="omnigent-crew" aria-label="Crew">
    <h5>Universal crew</h5>
    <p class="sub">One crew. The model you pick here leads it, and every other connected model is on its roster as a sub-agent. The orchestrator sees each worker's tier, strengths, ★ recommended (newest of its family) and price, so it can pick the right one per task.</p>
    <div class="omnigent-crew-row">
      <label for="omnigent-orchestrator">Orchestrator</label>
      <select id="omnigent-orchestrator">
        ${groups.map(g => `<optgroup label="${esc(g.name)}">${g.options.map(o =>
          `<option value="${esc(o.id)}" ${o.id === d.orchestrator ? 'selected' : ''} title="${esc(o.cost_label || '')}">${esc(optionLabel(o))}</option>`).join('')}</optgroup>`).join('')}
      </select>
    </div>
    ${current?.cost_label ? `<p class="sub" style="margin-top:-4px">${esc(current.cost_label)}${current.tier ? ` · ${esc(current.tier)}` : ''}</p>` : ''}
    <div class="omnigent-crew-row">
      ${isCodex ? `<label for="omnigent-effort">Reasoning</label>
        <select id="omnigent-effort">${(crew.reasoning_efforts || []).map(e =>
          `<option value="${esc(e)}" ${e === d.reasoning_effort ? 'selected' : ''}>${esc(e)}</option>`).join('')}</select>` : ''}
      <label for="omnigent-max-workers">Max API workers</label>
      <input id="omnigent-max-workers" type="number" min="1" max="${esc(crew.max_workers_limit || 40)}" value="${esc(d.max_workers)}">
      <button class="admin-btn-sm" data-omnigent-action="toggle-roster" aria-expanded="${_state.showRoster}">${_state.showRoster ? 'Hide' : 'Show'} roster (${workers.length})</button>
    </div>
    ${_state.showRoster ? `<div class="omnigent-roster-wrap"><table class="omnigent-roster">
      <thead><tr><th></th><th>Worker</th><th>Via</th><th>Tier</th><th>Cost</th></tr></thead>
      <tbody>${workers.map(w => `<tr>
        <td>${w.recommended ? '<span class="omnigent-star" title="Recommended: newest of its family">★</span>' : ''}</td>
        <td>${esc(w.model)}${(w.traits || []).length ? ` <span style="opacity:.55">· ${esc(w.traits.join(', '))}</span>` : ''}</td>
        <td>${esc(w.endpoint)}</td><td>${esc(w.tier || '')}</td><td>${esc(w.cost_label || '')}</td></tr>`).join('')}
      </tbody></table></div>
      ${crew.omitted ? `<p class="sub">${esc(crew.omitted)} more model(s) left off by the worker cap.</p>` : ''}` : ''}
    <div class="omnigent-actions">
      <button class="admin-btn-add" data-omnigent-action="apply" ${(!changed || _state.busy) ? 'disabled' : ''}>
        ${_state.busy === 'apply' ? 'Applying…' : running ? 'Apply & restart Omnigent' : 'Apply'}</button>
    </div>
  </section>`;
}

function render() {
  const modal = ensureModal();
  const body = modal.querySelector(`#${BODY_ID}`);
  if (!body) return;
  const s = _state.status || {};
  const known = !!_state.status && Object.keys(_state.status).length > 0;
  const installed = known ? s.installed !== false : false;
  const running = !!s.running;
  const api = s.api_models || {};

  body.innerHTML = `
    <div class="omnigent-shell">
      <section class="omnigent-hero">
        <div class="omnigent-hero-main">
          <div class="omnigent-kicker">Omnigent</div>
          <h3>Launch Omnigent</h3>
          <div class="omnigent-status-row">
            <span class="omnigent-status ${running ? 'running' : (installed ? 'idle' : 'not-installed')}">${running ? 'Running' : (!known ? 'Unknown' : installed ? 'Ready' : 'Not installed')}</span>
          </div>
        </div>
        <button class="admin-btn-sm" data-omnigent-action="refresh">Refresh</button>
      </section>

      ${running
        ? `<a class="admin-btn-add omnigent-launch-btn" href="${esc(omnigentUiUrl(s))}" target="_blank" rel="noopener">Open Omnigent ↗</a>
           <div class="omnigent-actions">
             <button class="admin-btn-sm" data-omnigent-action="restart" ${_state.busy ? 'disabled' : ''} title="Pick up new endpoints, keys and models">${_state.busy === 'restart' ? 'Re-syncing…' : 'Re-sync models'}</button>
             <button class="admin-btn-sm" data-omnigent-action="stop" ${_state.busy ? 'disabled' : ''}>${_state.busy === 'stop' ? 'Stopping…' : 'Stop'}</button>
           </div>`
        : `<button class="admin-btn-add omnigent-launch-btn" data-omnigent-action="launch" ${(installed && !_state.launching) ? '' : 'disabled'}>${_state.launching ? 'Launching… (first boot ~30s)' : 'Launch Omnigent'}</button>`}

      ${api.workers ? `<p style="margin-top:10px;color:#3ba55d;font-weight:600;">✓ Crew ready: ${esc(String(api.workers))} sub-agents from ${esc(String(api.endpoints || 0))} endpoint(s)</p>` : ''}
      ${api.error ? `<div class="omnigent-error">${esc(api.error)}</div>` : ''}

      ${renderCrew()}

      <div class="omnigent-warn">
        <strong>Subscriptions:</strong> the <code>claude-code</code> and <code>codex</code> workers (and a Claude or Codex orchestrator) use the logins inside Omnigent — run <code>claude login</code> / <code>codex login</code> there once.
        <strong>Safety:</strong> crew agents run unsandboxed with a shell where Odysseus runs; only give them work you would run yourself.
      </div>

      ${installed || !known ? '' : `
        <div class="omnigent-claude-help">
          <strong>Omnigent isn't installed where Odysseus runs.</strong>
          <div class="omnigent-code"><code>${esc(installCommand(s))}</code></div>
          <button class="omnigent-link-btn" data-omnigent-action="copy-install">Copy install command</button>
        </div>`}

      ${_state.error ? `<div class="omnigent-error">${esc(_state.error)}</div>` : ''}
    </div>`;

  wireActions(body);
}

async function refresh() {
  _state.error = '';
  _state.status = await fetchJson('/api/omnigent/status', null);
  await loadCrew();
  _loaded = true;
  render();
}

function wireActions(root) {
  root.querySelectorAll('[data-omnigent-action]').forEach(btn => {
    if (btn.dataset.omnigentBound) return;
    btn.dataset.omnigentBound = '1';
    btn.addEventListener('click', async (event) => {
      const action = event.currentTarget.dataset.omnigentAction;
      if (action === 'refresh') await refresh();
      else if (action === 'launch') await launchCrew();
      else if (action === 'apply') await applyCrew();
      else if (action === 'restart' || action === 'stop') await serverAction(action);
      else if (action === 'toggle-roster') { _state.showRoster = !_state.showRoster; render(); }
      else if (action === 'copy-install') await copyText(installCommand(_state.status || {}), 'Install command');
    });
  });
  const orch = root.querySelector('#omnigent-orchestrator');
  if (orch) orch.addEventListener('change', (e) => { _state.draft.orchestrator = e.target.value; render(); });
  const effort = root.querySelector('#omnigent-effort');
  if (effort) effort.addEventListener('change', (e) => { _state.draft.reasoning_effort = e.target.value; render(); });
  const maxw = root.querySelector('#omnigent-max-workers');
  if (maxw) maxw.addEventListener('change', (e) => { _state.draft.max_workers = Number(e.target.value) || 1; render(); });
}

export async function open() {
  const modal = ensureModal();
  modal.classList.remove('hidden', 'modal-minimized');
  modal.style.display = '';
  render();
  await refresh();
  // The button "just launches": auto-boot Omnigent when it isn't already up.
  // Only when the status is actually known — a failed status fetch must not
  // trigger a 30s rewrite-and-restart.
  const s = _state.status;
  if (s && s.installed !== false && !s.running && !_state.launching && !_state.crewError) {
    launchCrew();
  }
}

export function close() {
  closeModal();
}

function closeModal() {
  const modal = document.getElementById(MODAL_ID);
  if (modal) modal.classList.add('hidden');
  Modals.unregister(MODAL_ID);
}

export default { open, close, refresh };
