import * as Modals from './modalManager.js';
import { runProviderDeviceFlow, formatDeviceFlowError } from './providerDeviceFlow.js';

const MODAL_ID = 'omnigent-modal';
const BODY_ID = 'omnigent-body';

let _loaded = false;
let _state = {
  status: null,
  providers: [],
  sessions: [],
  error: '',
};

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
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
    restoreFn: () => {
      modal.classList.remove('hidden');
      refresh();
    },
    closeFn: () => {
      modal.classList.add('hidden');
    },
  });
  return modal;
}

function statusLabel(status) {
  if (!status?.installed) return ['not-installed', 'Not installed'];
  if (status.running) return ['running', 'Running'];
  return ['idle', 'Installed'];
}

function providerAction(provider) {
  if (provider.id === 'chatgpt-subscription') {
    return '<button class="omnigent-link-btn" data-omnigent-action="link-chatgpt">Link ChatGPT</button>';
  }
  if (provider.id === 'api-endpoint') {
    return '<button class="omnigent-link-btn" data-omnigent-action="models">Open endpoints</button>';
  }
  if (provider.id === 'glm-free-web') {
    return `<a class="omnigent-link-btn" href="${esc(provider.url)}" target="_blank" rel="noopener">Open GLM website</a>`;
  }
  return '';
}

function renderProvider(provider) {
  const supported = provider.supported !== false;
  const note = provider.note || provider.description || provider.setup_hint || '';
  return `
    <div class="omnigent-provider" data-provider-id="${esc(provider.id)}">
      <div class="omnigent-provider-head">
        <div>
          <div class="omnigent-provider-title">${esc(provider.label)}</div>
          <div class="omnigent-provider-kind">${esc(provider.kind || 'provider')}</div>
        </div>
        <span class="omnigent-provider-pill ${supported ? 'supported' : 'unsupported'}">${supported ? 'Ready' : 'Note'}</span>
      </div>
      <div class="omnigent-provider-note">${esc(note)}</div>
      ${provider.id === 'glm-free-web'
        ? '<div class="omnigent-provider-note emphasis">GLM free website is kept as a note because it is not an API connector.</div>'
        : ''}
      <div class="omnigent-provider-actions">${providerAction(provider)}</div>
    </div>`;
}

function renderSessions(sessions) {
  if (!sessions.length) {
    return '<div class="omnigent-empty">No active Omnigent sessions reported.</div>';
  }
  return sessions.slice(0, 6).map(session => `
    <div class="omnigent-session">
      <span>${esc(session.title || session.name || session.id || 'Session')}</span>
      <small>${esc(session.status || session.state || '')}</small>
    </div>`).join('');
}

function render() {
  const modal = ensureModal();
  const body = modal.querySelector(`#${BODY_ID}`);
  if (!body) return;
  const status = _state.status || {};
  const [statusClass, label] = statusLabel(status);
  const install = status.install || {};
  const command = status.command ? String(status.command).replace(/\\/g, '/') : '';

  body.innerHTML = `
    <div class="omnigent-shell">
      <section class="omnigent-hero">
        <div class="omnigent-hero-main">
          <div class="omnigent-kicker">AI agent workspace</div>
          <h3>Omnigent inside Odysseus</h3>
          <div class="omnigent-status-row">
            <span class="omnigent-status ${statusClass}">${label}</span>
            ${command ? `<span class="omnigent-command">${esc(command)}</span>` : ''}
          </div>
        </div>
        <div class="omnigent-actions">
          <button class="admin-btn-sm" data-omnigent-action="refresh">Refresh</button>
          <button class="admin-btn-add" data-omnigent-action="start" ${status.running ? 'disabled' : ''}>Start</button>
          <button class="admin-btn-delete" data-omnigent-action="stop" ${!status.running ? 'disabled' : ''}>Stop</button>
        </div>
      </section>

      <section class="omnigent-grid">
        <div class="omnigent-card">
          <div class="omnigent-card-title">Bridge bundle</div>
          <p>Download the Odysseus agent bundle, then run it with an Odysseus token in Omnigent.</p>
          <div class="omnigent-code">
            <code>ODYSSEUS_URL=${esc(location.origin)} ODYSSEUS_API_TOKEN=... omnigent run odysseus/config.yaml</code>
          </div>
          <div class="omnigent-card-actions">
            <a class="admin-btn-add" href="/api/omnigent/bundle.tar.gz" download="odysseus-omnigent-bundle.tar.gz">Download bundle</a>
            <button class="admin-btn-sm" data-omnigent-action="integrations">Create token</button>
          </div>
        </div>

        <div class="omnigent-card">
          <div class="omnigent-card-title">Install</div>
          <p>${esc(install.recommended || 'uv tool install omnigent')}</p>
          <div class="omnigent-muted">${esc((install.alternatives || []).join(' | '))}</div>
          ${status.error ? `<div class="omnigent-error">${esc(status.error)}</div>` : ''}
        </div>
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Account and model options</h4>
        </div>
        <div class="omnigent-provider-grid">
          ${_state.providers.map(renderProvider).join('')}
        </div>
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Sessions</h4>
          <span>${esc(String(_state.sessions.length))}</span>
        </div>
        <div class="omnigent-session-list">${renderSessions(_state.sessions)}</div>
      </section>
    </div>`;

  wireActions(body);
}

async function refresh() {
  _state.error = '';
  const [status, providerPayload, sessionPayload] = await Promise.all([
    fetchJson('/api/omnigent/status', {}),
    fetchJson('/api/omnigent/providers', { providers: [] }),
    fetchJson('/api/omnigent/sessions', { sessions: [] }),
  ]);
  _state.status = status;
  _state.providers = Array.isArray(providerPayload.providers) ? providerPayload.providers : [];
  _state.sessions = Array.isArray(sessionPayload.sessions) ? sessionPayload.sessions : [];
  _loaded = true;
  render();
}

async function postServer(action) {
  const toast = window.uiModule?.showToast;
  try {
    const res = await fetch(`/api/omnigent/server/${action}`, { method: 'POST', credentials: 'same-origin' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Omnigent server request failed');
    _state.status = data;
    toast?.(`Omnigent ${action === 'start' ? 'started' : 'stopped'}`);
  } catch (err) {
    toast?.(err?.message || 'Omnigent request failed');
  }
  await refresh();
}

async function linkChatGPTSubscription() {
  const toast = window.uiModule?.showToast;
  try {
    toast?.('Opening ChatGPT sign-in...');
    const result = await runProviderDeviceFlow('chatgpt-subscription', {
      onWaiting: () => toast?.('Waiting for ChatGPT approval...'),
    });
    if (result.status === 'authorized') {
      toast?.('ChatGPT Subscription linked');
      return;
    }
    toast?.(result.status === 'expired' ? 'ChatGPT sign-in expired' : 'ChatGPT sign-in was not completed');
  } catch (err) {
    toast?.(formatDeviceFlowError(err, 'ChatGPT sign-in failed'));
  }
}

function openSettings() {
  document.getElementById('user-bar-settings')?.click();
  setTimeout(() => {
    document.querySelector('[data-settings-tab="integrations"]')?.click();
  }, 120);
}

function wireActions(root) {
  root.querySelectorAll('[data-omnigent-action]').forEach(btn => {
    if (btn.dataset.omnigentBound) return;
    btn.dataset.omnigentBound = '1';
    btn.addEventListener('click', async (event) => {
      const action = event.currentTarget.dataset.omnigentAction;
      if (action === 'refresh') await refresh();
      else if (action === 'start') await postServer('start');
      else if (action === 'stop') await postServer('stop');
      else if (action === 'link-chatgpt') await linkChatGPTSubscription();
      else if (action === 'settings' || action === 'integrations') openSettings();
      else if (action === 'models') {
        document.getElementById('user-bar-settings')?.click();
        setTimeout(() => document.querySelector('[data-settings-tab="models"]')?.click(), 120);
      }
    });
  });
}

export async function open() {
  const modal = ensureModal();
  modal.classList.remove('hidden', 'modal-minimized');
  modal.style.display = '';
  render();
  if (!_loaded) await refresh();
  else refresh();
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
