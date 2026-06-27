import * as Modals from './modalManager.js';

const MODAL_ID = 'omnigent-modal';
const BODY_ID = 'omnigent-body';

let _loaded = false;
let _state = { status: null, launching: false, error: '' };

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

function omnigentUiUrl() {
  // Omnigent's web UI is bridged from its in-container loopback port to :6868.
  return `${location.protocol}//${location.hostname}:6868`;
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
  return modal;
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
  render();
  try {
    const data = await postJson('/api/omnigent/launch', {});
    _state.status = { ...(_state.status || {}), ...data };
    if (data.running) {
      const n = data.api_models?.models || 0;
      toast?.(n ? `Omnigent ready — ${n} API models installed` : 'Omnigent ready — click Open Omnigent');
    } else if (data.installed === false) {
      toast?.('Omnigent not available — check setup');
    } else {
      toast?.(data.error || 'Launch attempted — check Omnigent');
    }
  } catch (err) {
    toast?.(err?.message || 'Launch failed');
  } finally {
    _state.launching = false;
    render();
  }
}

function render() {
  const modal = ensureModal();
  const body = modal.querySelector(`#${BODY_ID}`);
  if (!body) return;
  const s = _state.status || {};
  const installed = s.installed !== false;
  const running = !!s.running;

  body.innerHTML = `
    <div class="omnigent-shell">
      <section class="omnigent-hero">
        <div class="omnigent-hero-main">
          <div class="omnigent-kicker">Omnigent</div>
          <h3>Launch Omnigent</h3>
          <div class="omnigent-status-row">
            <span class="omnigent-status ${running ? 'running' : (installed ? 'idle' : 'not-installed')}">${running ? 'Running' : (installed ? 'Ready' : 'Not installed')}</span>
          </div>
        </div>
        <button class="admin-btn-sm" data-omnigent-action="refresh">Refresh</button>
      </section>

      ${running
        ? `<a class="admin-btn-add omnigent-launch-btn" href="${omnigentUiUrl()}" target="_blank" rel="noopener">Open Omnigent ↗</a>`
        : `<button class="admin-btn-add omnigent-launch-btn" data-omnigent-action="launch" ${(installed && !_state.launching) ? '' : 'disabled'}>${_state.launching ? 'Launching… (first boot ~30s)' : 'Launch Omnigent'}</button>`}

      ${s.api_models?.models ? `<p style="margin-top:10px;color:#3ba55d;font-weight:600;">✓ ${esc(String(s.api_models.models))} API model${s.api_models.models === 1 ? '' : 's'} installed${s.api_models.default_model ? ` · default <code>${esc(s.api_models.default_model)}</code>` : ''}</p>` : ''}

      <p class="omnigent-provider-note" style="margin-top:14px;line-height:1.55;">
        Launch installs your Odysseus <strong>API models</strong> into Omnigent (marked as API) and opens its chat UI.
        Your <strong>Claude/ChatGPT subscriptions</strong> still need <code>omni</code> in WSL2 (to log in + work on your own files).
      </p>

      ${installed ? '' : `
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
  _state.status = await fetchJson('/api/omnigent/status', {});
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
      else if (action === 'copy-install') await copyText(installCommand(_state.status || {}), 'Install command');
    });
  });
}

export async function open() {
  const modal = ensureModal();
  modal.classList.remove('hidden', 'modal-minimized');
  modal.style.display = '';
  render();
  await refresh();
  // The button "just launches": auto-boot Omnigent when it isn't already up.
  const s = _state.status || {};
  if (s.installed !== false && !s.running && !_state.launching) {
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
