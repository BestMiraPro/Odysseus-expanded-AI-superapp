import * as Modals from './modalManager.js';
import { runProviderDeviceFlow, formatDeviceFlowError } from './providerDeviceFlow.js';

const MODAL_ID = 'omnigent-modal';
const BODY_ID = 'omnigent-body';

let _loaded = false;
let _state = {
  status: null,
  providers: [],
  agents: [],
  orchestrator: { backend: 'claude-subscription', model: null, workspace: '' },
  modelEndpoints: [],
  agentForm: null,
  claudeHelp: false,
  launching: false,
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

async function putJson(url, payload = {}) {
  const res = await fetch(url, {
    method: 'PUT',
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
  if (provider.id === 'claude-subscription') {
    return '<button class="omnigent-link-btn" data-omnigent-action="link-claude">Link Claude</button>';
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

const BACKEND_LABELS = {
  'claude-subscription': 'Claude (subscription)',
  'chatgpt-subscription': 'ChatGPT (subscription)',
  'api': 'API endpoint',
};

function backendOptions(selected) {
  return Object.entries(BACKEND_LABELS).map(([id, label]) =>
    `<option value="${id}" ${id === selected ? 'selected' : ''}>${esc(label)}</option>`).join('');
}

function modelOptions(selected) {
  const opts = ['<option value="">— pick a model —</option>'];
  for (const ep of _state.modelEndpoints) {
    for (const m of ep.models || []) {
      const val = `${ep.id}::${m}`;
      opts.push(`<option value="${esc(val)}" ${val === selected ? 'selected' : ''}>${esc(ep.name)} · ${esc(m)}</option>`);
    }
  }
  return opts.join('');
}

function orchModelValue() {
  const m = _state.orchestrator?.model;
  return m ? `${m.endpoint_id}::${m.model}` : '';
}

function renderOrchestrator() {
  const o = _state.orchestrator || {};
  const isApi = o.backend === 'api';
  return `
    <section class="omnigent-section">
      <div class="omnigent-section-head"><h4>Orchestrator</h4><span>Coordinates the crew</span></div>
      <div class="omnigent-orch-row">
        <select id="omnigent-orch-backend" class="omnigent-select">${backendOptions(o.backend)}</select>
        <select id="omnigent-orch-model" class="omnigent-select" ${isApi ? '' : 'style="display:none"'}>${modelOptions(orchModelValue())}</select>
        <input id="omnigent-orch-workspace" class="omnigent-input" placeholder="Workspace dir (optional)" value="${esc(o.workspace || '')}">
        <button class="admin-btn-sm" data-omnigent-action="save-orchestrator">Save</button>
      </div>
    </section>`;
}

function renderAgentRow(a) {
  const model = a.model?.model ? ` · ${esc(a.model.model)}` : '';
  return `
    <div class="omnigent-agent-row" data-agent-id="${esc(a.id)}">
      <div class="omnigent-agent-main">
        <span class="omnigent-agent-name">${esc(a.name)}</span>
        <small>${esc(BACKEND_LABELS[a.backend] || a.backend)}${model}</small>
        <p>${esc(a.role || '')}</p>
      </div>
      <span class="omnigent-worker-status ${a.enabled ? 'ready' : 'cancelled'}">${a.enabled ? 'On' : 'Off'}</span>
      <div class="omnigent-agent-actions">
        <button class="omnigent-link-btn" data-omnigent-action="edit-agent" data-agent-id="${esc(a.id)}">Edit</button>
        <button class="omnigent-link-btn" data-omnigent-action="delete-agent" data-agent-id="${esc(a.id)}">Delete</button>
      </div>
    </div>`;
}

function renderAgents() {
  const rows = _state.agents.map(renderAgentRow).join('') ||
    '<div class="omnigent-empty">No agents yet. Add one to build your crew.</div>';
  return `
    <section class="omnigent-section">
      <div class="omnigent-section-head">
        <h4>Agents</h4>
        <button class="admin-btn-add" data-omnigent-action="add-agent">Add agent</button>
      </div>
      ${renderAgentForm()}
      <div class="omnigent-agent-list">${rows}</div>
    </section>`;
}

async function saveOrchestrator() {
  const toast = window.uiModule?.showToast;
  const backend = document.getElementById('omnigent-orch-backend')?.value || 'claude-subscription';
  const workspace = document.getElementById('omnigent-orch-workspace')?.value || '';
  let model = null;
  const raw = document.getElementById('omnigent-orch-model')?.value || '';
  if (backend === 'api' && raw.includes('::')) {
    const [endpoint_id, m] = raw.split('::');
    model = { endpoint_id, model: m };
  }
  try {
    _state.orchestrator = await putJson('/api/omnigent/orchestrator', { backend, model, workspace });
    toast?.('Orchestrator saved');
    render();
  } catch (err) { toast?.(err?.message || 'Save failed'); }
}

function agentModelValue(a) {
  return a?.model ? `${a.model.endpoint_id}::${a.model.model}` : '';
}

function openAgentForm(existing) {
  _state.agentForm = existing
    ? { id: existing.id, name: existing.name, role: existing.role || '', backend: existing.backend, model: existing.model || null }
    : { id: null, name: '', role: '', backend: 'claude-subscription', model: null };
  render();
  setTimeout(() => document.getElementById('omnigent-agent-name')?.focus(), 0);
}

function closeAgentForm() {
  _state.agentForm = null;
  render();
}

function renderAgentForm() {
  const f = _state.agentForm;
  if (!f) return '';
  const isApi = f.backend === 'api';
  return `
    <div class="omnigent-agent-form">
      <input id="omnigent-agent-name" class="omnigent-input" placeholder="Agent name" value="${esc(f.name)}">
      <textarea id="omnigent-agent-role" class="omnigent-input" rows="2" placeholder="Role / instructions (what this agent does)">${esc(f.role)}</textarea>
      <div class="omnigent-orch-row">
        <select id="omnigent-agent-backend" class="omnigent-select">${backendOptions(f.backend)}</select>
        <select id="omnigent-agent-model" class="omnigent-select" ${isApi ? '' : 'style="display:none"'}>${modelOptions(agentModelValue(f))}</select>
      </div>
      <div class="omnigent-card-actions" style="display:flex;">
        <button class="admin-btn-add" data-omnigent-action="save-agent">${f.id ? 'Save changes' : 'Add agent'}</button>
        <button class="admin-btn-sm" data-omnigent-action="cancel-agent">Cancel</button>
      </div>
    </div>`;
}

async function saveAgentForm() {
  const f = _state.agentForm;
  if (!f) return;
  const toast = window.uiModule?.showToast;
  const name = (document.getElementById('omnigent-agent-name')?.value || '').trim();
  const role = document.getElementById('omnigent-agent-role')?.value || '';
  const backend = document.getElementById('omnigent-agent-backend')?.value || 'claude-subscription';
  if (!name) { toast?.('Agent name is required'); return; }
  let model = null;
  const raw = document.getElementById('omnigent-agent-model')?.value || '';
  if (backend === 'api' && raw.includes('::')) {
    const [endpoint_id, m] = raw.split('::');
    model = { endpoint_id, model: m };
  }
  const payload = { name, role, backend, model };
  if (!f.id) payload.enabled = true;
  try {
    if (f.id) await putJson(`/api/omnigent/agents/${encodeURIComponent(f.id)}`, payload);
    else await postJson('/api/omnigent/agents', payload);
    _state.agentForm = null;
    toast?.(f.id ? 'Agent updated' : 'Agent added');
    await refresh();
  } catch (err) { toast?.(err?.message || 'Save failed'); }
}

function renderClaudeHelp() {
  if (!_state.claudeHelp) return '';
  return `
    <div class="omnigent-claude-help">
      <strong>Link your Claude subscription</strong>
      <p>Claude Pro/Max runs through Claude Code in your Omnigent runtime (WSL2). Anthropic has no browser sign-in for it like ChatGPT, so link it where Omnigent runs:</p>
      <ol class="omnigent-claude-steps">
        <li>Open your Omnigent / WSL2 shell.</li>
        <li>Run <code>claude</code>, then type <code>/login</code> and approve in the browser (uses your Claude subscription).</li>
        <li>Back here, set an agent's backend to <em>Claude (subscription)</em>.</li>
      </ol>
      <div class="omnigent-card-actions" style="display:flex;">
        <button class="omnigent-link-btn" data-omnigent-action="copy-claude-login">Copy login command</button>
        <a class="omnigent-link-btn" href="https://claude.ai" target="_blank" rel="noopener">Open claude.ai</a>
      </div>
    </div>`;
}

function linkClaudeSubscription() {
  _state.claudeHelp = !_state.claudeHelp;
  render();
}

async function deleteAgent(id) {
  const toast = window.uiModule?.showToast;
  if (!window.confirm('Delete this agent?')) return;
  try {
    await fetch(`/api/omnigent/agents/${encodeURIComponent(id)}`,
      { method: 'DELETE', credentials: 'same-origin' });
    toast?.('Agent deleted');
    await refresh();
  } catch (err) { toast?.(err?.message || 'Delete failed'); }
}

function omnigentUiUrl() {
  // Omnigent's web UI is bridged from its in-container loopback port to :6868
  // on the Odysseus host.
  return `${location.protocol}//${location.hostname}:6868`;
}

async function launchCrew() {
  const toast = window.uiModule?.showToast;
  _state.launching = true;
  render();
  try {
    const data = await postJson('/api/omnigent/launch', {});
    _state.status = { ...(_state.status || {}), ...data };
    if (data.running) {
      window.open(omnigentUiUrl(), '_blank');
      toast?.('Omnigent launched');
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
  const liveAgents = _state.agents.filter(a => a.enabled).length;

  body.innerHTML = `
    <div class="omnigent-shell">
      <section class="omnigent-hero">
        <div class="omnigent-hero-main">
          <div class="omnigent-kicker">Omnigent crew</div>
          <h3>Configure &amp; launch your crew</h3>
          <div class="omnigent-status-row">
            <span class="omnigent-status ${running ? 'running' : (installed ? 'idle' : 'not-installed')}">${running ? 'Omnigent running' : (installed ? 'Omnigent ready' : 'Omnigent not installed')}</span>
            ${running ? `<a class="omnigent-link-btn" href="${omnigentUiUrl()}" target="_blank" rel="noopener">Open Omnigent</a>` : ''}
          </div>
        </div>
        <button class="admin-btn-sm" data-omnigent-action="refresh">Refresh</button>
      </section>

      ${renderOrchestrator()}
      ${renderAgents()}

      <button class="admin-btn-add omnigent-launch-btn" data-omnigent-action="launch" ${(!liveAgents || _state.launching) ? 'disabled' : ''}>${_state.launching ? 'Launching… (first boot can take ~30s)' : 'Launch crew'}</button>

      ${installed ? '' : `
        <div class="omnigent-claude-help">
          <strong>Omnigent isn't installed where Odysseus runs</strong>
          <p>Launch boots Omnigent's own chat UI. Install it where Odysseus can reach it — or ask me to bundle it into Odysseus for true one-click.</p>
          <div class="omnigent-code"><code>${esc(installCommand(s))}</code></div>
          <button class="omnigent-link-btn" data-omnigent-action="copy-install">Copy install command</button>
        </div>`}

      <details class="omnigent-advanced">
        <summary>Accounts &amp; advanced</summary>
        <div class="omnigent-provider-grid">${_state.providers.map(renderProvider).join('')}</div>
        ${renderClaudeHelp()}
        <div class="omnigent-card-actions">
          <button class="admin-btn-sm" data-omnigent-action="integrations">Create token</button>
        </div>
      </details>

      ${_state.error ? `<div class="omnigent-error">${esc(_state.error)}</div>` : ''}
    </div>`;

  wireActions(body);
}

async function refresh() {
  _state.error = '';
  const [status, providerPayload, agentPayload, orchestratorPayload, modelPayload] = await Promise.all([
    fetchJson('/api/omnigent/status', {}),
    fetchJson('/api/omnigent/providers', { providers: [] }),
    fetchJson('/api/omnigent/agents', { agents: [] }),
    fetchJson('/api/omnigent/orchestrator', { backend: 'claude-subscription', model: null, workspace: '' }),
    fetchJson('/api/omnigent/model-options', { endpoints: [] }),
  ]);
  _state.status = status;
  _state.providers = Array.isArray(providerPayload.providers) ? providerPayload.providers : [];
  _state.agents = Array.isArray(agentPayload.agents) ? agentPayload.agents : [];
  _state.orchestrator = orchestratorPayload || _state.orchestrator;
  _state.modelEndpoints = Array.isArray(modelPayload.endpoints) ? modelPayload.endpoints : [];
  _loaded = true;
  render();
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
      else if (action === 'copy-install') {
        await copyText(installCommand(_state.status || {}), 'External install command');
      } else if (action === 'copy-bridge') {
        await copyText(bridgeCommand(), 'Bridge command');
      } else if (action === 'link-chatgpt') {
        await linkChatGPTSubscription();
      } else if (action === 'integrations') {
        openSettings('integrations');
      } else if (action === 'models') {
        openSettings('models');
      } else if (action === 'save-orchestrator') {
        await saveOrchestrator();
      } else if (action === 'add-agent') {
        await openAgentForm(null);
      } else if (action === 'edit-agent') {
        await openAgentForm(_state.agents.find(a => a.id === target.dataset.agentId));
      } else if (action === 'delete-agent') {
        await deleteAgent(target.dataset.agentId || '');
      } else if (action === 'launch') {
        await launchCrew();
      } else if (action === 'save-agent') {
        await saveAgentForm();
      } else if (action === 'cancel-agent') {
        closeAgentForm();
      } else if (action === 'link-claude') {
        linkClaudeSubscription();
      } else if (action === 'copy-claude-login') {
        await copyText('claude\n/login', 'Claude login command');
      }
    });
  });

  const ob = root.querySelector('#omnigent-orch-backend');
  if (ob && !ob.dataset.bound) {
    ob.dataset.bound = '1';
    ob.addEventListener('change', () => {
      const m = root.querySelector('#omnigent-orch-model');
      if (m) m.style.display = ob.value === 'api' ? '' : 'none';
    });
  }
  const ab = root.querySelector('#omnigent-agent-backend');
  if (ab && !ab.dataset.bound) {
    ab.dataset.bound = '1';
    ab.addEventListener('change', () => {
      const m = root.querySelector('#omnigent-agent-model');
      if (m) m.style.display = ab.value === 'api' ? '' : 'none';
    });
  }
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
