import * as Modals from './modalManager.js';
import { runProviderDeviceFlow, formatDeviceFlowError } from './providerDeviceFlow.js';

const MODAL_ID = 'omnigent-modal';
const BODY_ID = 'omnigent-body';

let _loaded = false;
let _state = {
  status: null,
  providers: [],
  sessions: [],
  externalSessions: [],
  workers: [],
  nativeWorkers: [],
  presets: [],
  selectedPreset: 'balanced',
  selectedRun: null,
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

function nativeStatusLabel() {
  const native = _state.status?.native || {};
  if (!native.available) return ['not-installed', 'Native crew unavailable'];
  if (!native.model_ready) return ['idle', 'Native crew ready'];
  return ['running', 'Native crew model-ready'];
}

function installCommand(status) {
  return status?.install?.recommended || 'curl -fsSL https://raw.githubusercontent.com/omnigent-ai/omnigent/main/scripts/install_oss.sh | sh';
}

function bridgeCommand() {
  return `ODYSSEUS_URL=${location.origin} ODYSSEUS_API_TOKEN=... omnigent run odysseus/config.yaml`;
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
  return '<button class="omnigent-link-btn" data-omnigent-action="copy-install">Copy external install</button>';
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
        <span class="omnigent-provider-pill ${supported ? 'supported' : 'unsupported'}">${supported ? 'Optional' : 'Note'}</span>
      </div>
      <div class="omnigent-provider-note">${esc(note)}</div>
      ${provider.id === 'glm-free-web'
        ? '<div class="omnigent-provider-note emphasis">GLM free website is kept as a note because it is not an API connector.</div>'
        : ''}
      <div class="omnigent-provider-actions">${providerAction(provider)}</div>
    </div>`;
}

function renderPreset(preset) {
  const active = preset.id === _state.selectedPreset ? 'active' : '';
  return `
    <button class="omnigent-preset ${active}" data-omnigent-action="select-preset" data-preset-id="${esc(preset.id)}">
      <span>${esc(preset.label)}</span>
      <small>${esc(preset.description || '')}</small>
    </button>`;
}

function workerStatusLabel(worker) {
  if (worker.status === 'completed') return 'Done';
  if (worker.status === 'running') return 'Running';
  if (worker.status === 'ready') return 'Ready';
  if (worker.status === 'cancelled') return 'Cancelled';
  if (worker.status === 'linkable') return 'Link';
  if (worker.status === 'note') return 'Note';
  return worker.available === false ? 'Missing' : 'Queued';
}

function renderWorker(worker) {
  const status = worker.status || (worker.available ? 'ready' : 'missing');
  return `
    <div class="omnigent-worker" data-worker-id="${esc(worker.id)}">
      <div class="omnigent-worker-main">
        <span class="omnigent-worker-name">${esc(worker.label || worker.id)}</span>
        <small>${esc(worker.source || '')}</small>
      </div>
      <span class="omnigent-worker-status ${esc(status)}">${esc(workerStatusLabel({ ...worker, status }))}</span>
      <p>${esc(worker.output || worker.current_step || worker.hint || '')}</p>
    </div>`;
}

function renderTimeline(run) {
  const events = Array.isArray(run?.timeline) ? run.timeline : [];
  if (!events.length) {
    return '<div class="omnigent-empty">Start a crew run to see the timeline.</div>';
  }
  return events.map(event => `
    <div class="omnigent-timeline-item">
      <span>${esc(event.label || event.kind || 'Event')}</span>
      <p>${esc(event.detail || '')}</p>
    </div>`).join('');
}

function renderRuns() {
  const sessions = _state.sessions || [];
  if (!sessions.length) {
    return '<div class="omnigent-empty">No native crew runs yet.</div>';
  }
  const selectedId = _state.selectedRun?.id || '';
  return sessions.slice(0, 8).map(session => `
    <button class="omnigent-session ${session.id === selectedId ? 'active' : ''}" data-omnigent-action="select-run" data-run-id="${esc(session.id)}">
      <span>${esc(session.title || session.id || 'Crew run')}</span>
      <small>${esc(session.status || '')}</small>
    </button>`).join('');
}

function currentWorkers() {
  if (_state.selectedRun?.workers?.length) return _state.selectedRun.workers;
  return _state.nativeWorkers.length ? _state.nativeWorkers : _state.workers;
}

function render() {
  const modal = ensureModal();
  const body = modal.querySelector(`#${BODY_ID}`);
  if (!body) return;
  const status = _state.status || {};
  const native = status.native || {};
  const [statusClass, statusText] = nativeStatusLabel();
  const selectedRun = _state.selectedRun;
  const selectedPreset = _state.presets.find(p => p.id === _state.selectedPreset) || _state.presets[0];
  const install = installCommand(status);
  const bridge = bridgeCommand();
  const modelHint = native.model_ready
    ? 'Odysseus has a configured model endpoint for model-backed crew execution.'
    : 'Add a model endpoint when you want model-backed execution; native planning still works now.';

  body.innerHTML = `
    <div class="omnigent-shell">
      <section class="omnigent-hero">
        <div class="omnigent-hero-main">
          <div class="omnigent-kicker">Native crew</div>
          <h3>Run an Odysseus crew</h3>
          <div class="omnigent-status-row">
            <span class="omnigent-status ${statusClass}">${statusText}</span>
            <span class="omnigent-command">${esc(modelHint)}</span>
          </div>
        </div>
        <div class="omnigent-actions">
          <button class="admin-btn-sm" data-omnigent-action="refresh">Refresh</button>
          <button class="admin-btn-add" data-omnigent-action="create-run">Launch crew</button>
          <button class="admin-btn-delete" data-omnigent-action="cancel-run" ${selectedRun ? '' : 'disabled'}>Cancel</button>
        </div>
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Goal</h4>
          <span>${esc(selectedPreset?.label || 'Balanced')}</span>
        </div>
        <textarea id="omnigent-goal-input" class="omnigent-goal-input" rows="3" placeholder="What should the crew do?">${esc(selectedRun?.goal || '')}</textarea>
        <div class="omnigent-preset-row">
          ${_state.presets.map(renderPreset).join('')}
        </div>
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Worker roster</h4>
          <span>${esc(String(currentWorkers().length))}</span>
        </div>
        <div class="omnigent-worker-grid">
          ${currentWorkers().map(renderWorker).join('') || '<div class="omnigent-empty">No workers reported yet.</div>'}
        </div>
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Crew timeline</h4>
          <span>${esc(selectedRun?.status || 'idle')}</span>
        </div>
        <div class="omnigent-timeline">${renderTimeline(selectedRun)}</div>
        ${selectedRun?.summary ? `<div class="omnigent-console-lines"><div>${esc(selectedRun.summary)}</div></div>` : ''}
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Recent runs</h4>
          <span>${esc(String(_state.sessions.length))}</span>
        </div>
        <div class="omnigent-session-list">${renderRuns()}</div>
      </section>

      <section class="omnigent-section">
        <div class="omnigent-section-head">
          <h4>Account and model options</h4>
        </div>
        <div class="omnigent-provider-grid">
          ${_state.providers.map(renderProvider).join('')}
        </div>
        <details class="omnigent-advanced">
          <summary>Advanced bridge</summary>
          <p>Native crew runs inside Odysseus. Use this only when you want an external Omnigent CLI to call scoped Odysseus tools.</p>
          <div class="omnigent-code"><code>${esc(bridge)}</code></div>
          <div class="omnigent-code"><code>${esc(install)}</code></div>
          <div class="omnigent-card-actions">
            <a class="admin-btn-add" href="/api/omnigent/bundle.tar.gz" download="odysseus-omnigent-bundle.tar.gz">Download bundle</a>
            <button class="admin-btn-sm" data-omnigent-action="integrations">Create token</button>
            <button class="omnigent-link-btn" data-omnigent-action="copy-bridge">Copy bridge command</button>
            <button class="omnigent-link-btn" data-omnigent-action="copy-install">Copy external install</button>
          </div>
        </details>
      </section>

      ${_state.error ? `<div class="omnigent-error">${esc(_state.error)}</div>` : ''}
    </div>`;

  wireActions(body);
}

async function refresh() {
  _state.error = '';
  const [status, providerPayload, sessionPayload, workerPayload] = await Promise.all([
    fetchJson('/api/omnigent/status', {}),
    fetchJson('/api/omnigent/providers', { providers: [] }),
    fetchJson('/api/omnigent/sessions', { sessions: [] }),
    fetchJson('/api/omnigent/workers', { workers: [], native_workers: [], presets: [] }),
  ]);
  _state.status = status;
  _state.providers = Array.isArray(providerPayload.providers) ? providerPayload.providers : [];
  _state.sessions = Array.isArray(sessionPayload.sessions) ? sessionPayload.sessions : [];
  _state.externalSessions = Array.isArray(sessionPayload.external_sessions) ? sessionPayload.external_sessions : [];
  _state.workers = Array.isArray(workerPayload.workers) ? workerPayload.workers : [];
  _state.nativeWorkers = Array.isArray(workerPayload.native_workers) ? workerPayload.native_workers : [];
  _state.presets = Array.isArray(workerPayload.presets) ? workerPayload.presets : [];
  if (!_state.presets.length && status.native?.presets?.length) _state.presets = status.native.presets;
  if (!_state.presets.find(p => p.id === _state.selectedPreset)) {
    _state.selectedPreset = _state.presets[0]?.id || 'balanced';
  }
  _loaded = true;
  render();
}

async function launchCrew() {
  const toast = window.uiModule?.showToast;
  const input = document.getElementById('omnigent-goal-input');
  const goal = (input?.value || '').trim();
  if (!goal) {
    toast?.('Add a goal for the crew');
    return;
  }
  try {
    const created = await postJson('/api/omnigent/runs', {
      goal,
      preset: _state.selectedPreset || 'balanced',
    });
    const started = await postJson(`/api/omnigent/runs/${encodeURIComponent(created.id)}/start`);
    _state.selectedRun = started;
    toast?.('Native crew completed');
    await refresh();
    _state.selectedRun = started;
    render();
  } catch (err) {
    toast?.(err?.message || 'Crew launch failed');
  }
}

async function cancelSelectedRun() {
  const toast = window.uiModule?.showToast;
  if (!_state.selectedRun?.id) return;
  try {
    _state.selectedRun = await postJson(`/api/omnigent/runs/${encodeURIComponent(_state.selectedRun.id)}/cancel`);
    toast?.('Crew cancelled');
    await refresh();
    render();
  } catch (err) {
    toast?.(err?.message || 'Cancel failed');
  }
}

async function selectRun(runId) {
  const toast = window.uiModule?.showToast;
  try {
    const run = await fetchJson(`/api/omnigent/runs/${encodeURIComponent(runId)}`, null);
    if (run) {
      _state.selectedRun = run;
      _state.selectedPreset = run.preset || _state.selectedPreset;
      render();
    }
  } catch (err) {
    toast?.(err?.message || 'Could not open run');
  }
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

function openSettings(tab = 'integrations') {
  document.getElementById('user-bar-settings')?.click();
  setTimeout(() => {
    document.querySelector(`[data-settings-tab="${tab}"]`)?.click();
  }, 120);
}

function wireActions(root) {
  root.querySelectorAll('[data-omnigent-action]').forEach(btn => {
    if (btn.dataset.omnigentBound) return;
    btn.dataset.omnigentBound = '1';
    btn.addEventListener('click', async (event) => {
      const target = event.currentTarget;
      const action = target.dataset.omnigentAction;
      if (action === 'refresh') await refresh();
      else if (action === 'create-run') await launchCrew();
      else if (action === 'cancel-run') await cancelSelectedRun();
      else if (action === 'select-preset') {
        _state.selectedPreset = target.dataset.presetId || 'balanced';
        render();
      } else if (action === 'select-run') {
        await selectRun(target.dataset.runId || '');
      } else if (action === 'copy-install') {
        await copyText(installCommand(_state.status || {}), 'External install command');
      } else if (action === 'copy-bridge') {
        await copyText(bridgeCommand(), 'Bridge command');
      } else if (action === 'link-chatgpt') {
        await linkChatGPTSubscription();
      } else if (action === 'integrations') {
        openSettings('integrations');
      } else if (action === 'models') {
        openSettings('models');
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
