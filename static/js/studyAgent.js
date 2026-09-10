/**
 * Study agent tab — chat with the in-app Study agent (routes/study_agent_routes.py).
 *
 * The agent acts through tools (subjects, materials, questions, cards, exams,
 * AI pipelines) and tutors from the learner's own materials. Admins can opt in
 * to code tools per chat ("Allow code changes"). Rendered inside the Study
 * pane's body by study.js; keeps its own state so switching tabs mid-stream
 * doesn't lose the conversation.
 */

import { mdToHtml, renderMermaid, renderMath } from './markdown.js';

const API = window.location.origin;

const A = {
  el: null, esc: (s) => String(s ?? ''), toast: () => {}, decks: [],
  caps: null, threads: [], threadId: null, messages: [],
  streaming: false, abort: null, allowCode: false, deckId: null, prefill: '',
  _raf: null,
};

/** Text to drop into the composer the next time the tab renders. */
export function setAgentPrefill(text) { A.prefill = text || ''; }
/** Subject the next new chat is scoped to. */
export function setAgentScope(deckId) { A.deckId = deckId || null; }

function injectStyles() {
  if (document.getElementById('study-agent-styles')) return;
  const st = document.createElement('style');
  st.id = 'study-agent-styles';
  st.textContent = `
.study-agent { display: flex; gap: 14px; height: 100%; min-height: 420px; }
.study-agent-side { width: 190px; flex: 0 0 190px; border-right: 1px solid var(--border); padding-right: 10px;
  display: flex; flex-direction: column; gap: 6px; overflow-y: auto; }
.study-agent-thread { display: flex; align-items: center; gap: 4px; padding: 6px 8px; border-radius: 7px;
  cursor: pointer; font-size: 12px; border: 1px solid transparent; }
.study-agent-thread:hover { background: rgba(128,128,128,0.1); }
.study-agent-thread.active { border-color: var(--border); background: rgba(128,128,128,0.14); }
.study-agent-thread .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.study-agent-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.study-agent-bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 11.5px; }
.study-agent-log { flex: 1; overflow-y: auto; padding: 4px 6px 4px 0; display: flex; flex-direction: column; gap: 10px; }
.study-agent-msg { max-width: 92%; padding: 9px 12px; border-radius: 10px; font-size: 13px; line-height: 1.5; }
.study-agent-msg.user { align-self: flex-end; background: rgba(91,138,191,0.14); white-space: pre-wrap; }
.study-agent-msg.assistant { align-self: flex-start; border: 1px solid var(--border); }
.study-agent-msg.error { align-self: flex-start; color: var(--red, #e05555); border: 1px solid currentColor; }
.study-agent-msg.thinking { opacity: 0.55; font-style: italic; font-size: 12px; }
.study-agent-tool { align-self: flex-start; max-width: 92%; font-size: 12px; border: 1px dashed var(--border);
  border-radius: 8px; padding: 4px 10px; }
.study-agent-tool summary { cursor: pointer; opacity: 0.8; }
.study-agent-tool.pending summary::after { content: ' …'; }
.study-agent-tool.fail summary { color: var(--red, #e05555); }
.study-agent-tool pre { white-space: pre-wrap; word-break: break-word; margin: 6px 0 2px; font-size: 11.5px;
  max-height: 260px; overflow: auto; }
.study-agent-composer { display: flex; gap: 8px; align-items: flex-end; border-top: 1px solid var(--border); padding-top: 8px; }
.study-agent-composer textarea { flex: 1; min-height: 44px; max-height: 160px; }
.study-agent-hint { font-size: 11px; opacity: 0.5; }
@media (max-width: 768px) { .study-agent { flex-direction: column; } .study-agent-side { width: auto; flex: 0 0 auto;
  border-right: none; border-bottom: 1px solid var(--border); padding: 0 0 8px; flex-direction: row; flex-wrap: wrap; } }
`;
  document.head.appendChild(st);
}

async function jfetch(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || `Request failed (${res.status})`);
  return data;
}

function md(src) {
  try { return mdToHtml(src || '', {}); } catch { return A.esc(src || ''); }
}

// ---------------------------------------------------------------------------
// render
// ---------------------------------------------------------------------------

export async function renderAgentTab({ el, decks, deckId, esc, toast }) {
  injectStyles();
  A.el = el; A.esc = esc || A.esc; A.toast = toast || A.toast; A.decks = decks || [];
  if (deckId && !A.threadId) A.deckId = deckId;
  el.innerHTML = '<div class="study-empty">Loading…</div>';
  try {
    A.caps = await jfetch('/api/study/agent/capabilities');   // cheap; the model can change between visits
    if (!A.decks.length) A.decks = (await jfetch('/api/study/decks')).decks || [];
    A.threads = (await jfetch('/api/study/agent/threads')).threads || [];
  } catch (e) { el.innerHTML = `<div class="study-empty">${A.esc(e.message)}</div>`; return; }
  if (A.threadId && !A.threads.some(t => t.id === A.threadId)) A.threadId = null;
  layout();
  if (A.threadId && !A.messages.length && !A.streaming) await loadThread(A.threadId);
  else renderLog();
}

function layout() {
  const el = A.el;
  const caps = A.caps || {};
  const deckOpts = ['<option value="">All subjects</option>']
    .concat(A.decks.map(d => `<option value="${A.esc(d.id)}" ${A.deckId === d.id ? 'selected' : ''}>${A.esc(d.name)}</option>`))
    .join('');
  el.innerHTML = `
    <div class="study-agent">
      <aside class="study-agent-side">
        <button class="study-btn small primary" id="study-agent-new">+ New chat</button>
        <div id="study-agent-threads"></div>
      </aside>
      <section class="study-agent-main">
        <div class="study-agent-bar">
          <label class="study-subtle">Subject
            <select class="study-select" id="study-agent-scope" style="padding:3px 6px;font-size:11.5px;" title="The subject the agent focuses on; it can still reach the others by name">${deckOpts}</select>
          </label>
          ${caps.code_tools_allowed ? `
          <label class="study-subtle" title="Lets the agent read and change this app's code (${A.esc(caps.code_root || '')}). ${caps.runtime === 'docker' ? 'Docker: edits land in the container copy — see docker-compose.yml to mount your repo.' : ''}">
            <input type="checkbox" id="study-agent-code" ${A.allowCode ? 'checked' : ''}> Allow code changes${caps.runtime === 'docker' ? ' (container copy)' : ''}
          </label>` : ''}
          <span class="study-agent-hint">model: ${A.esc(caps.model || '—')}</span>
        </div>
        <div class="study-agent-log" id="study-agent-log"></div>
        <div class="study-agent-composer">
          <textarea class="study-textarea" id="study-agent-input" placeholder="Ask for an explanation, a drill, or an action: “extract the questions from the 2023 exam”, “explain elasticity from my notes”, “make 10 questions on chapter 3”…"></textarea>
          <button class="study-btn primary" id="study-agent-send">${A.streaming ? 'Stop' : 'Send'}</button>
        </div>
        <div class="study-agent-hint">Enter sends · Shift+Enter newline · destructive actions are confirmed in chat first</div>
      </section>
    </div>`;
  renderThreads();

  el.querySelector('#study-agent-new').addEventListener('click', () => {
    if (A.streaming) return;
    A.threadId = null; A.messages = [];
    renderThreads(); renderLog();
    el.querySelector('#study-agent-input')?.focus();
  });
  el.querySelector('#study-agent-scope').addEventListener('change', (e) => { A.deckId = e.target.value || null; });
  el.querySelector('#study-agent-code')?.addEventListener('change', (e) => { A.allowCode = e.target.checked; });
  const input = el.querySelector('#study-agent-input');
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  el.querySelector('#study-agent-send').addEventListener('click', () => (A.streaming ? stop() : send()));
  if (A.prefill) { input.value = A.prefill; A.prefill = ''; input.focus(); }
}

function renderThreads() {
  const wrap = A.el?.querySelector('#study-agent-threads');
  if (!wrap) return;
  wrap.innerHTML = A.threads.length ? A.threads.map(t => `
    <div class="study-agent-thread ${t.id === A.threadId ? 'active' : ''}" data-thread="${A.esc(t.id)}" title="${A.esc(t.title)}">
      <span class="t">${A.esc(t.title)}</span>
      <button class="study-btn small danger" data-delthread="${A.esc(t.id)}" title="Delete chat" style="padding:1px 5px;">✕</button>
    </div>`).join('') : '<div class="study-empty" style="padding:6px 4px;">No chats yet.</div>';
  wrap.onclick = async (e) => {
    const del = e.target.closest('[data-delthread]');
    if (del) {
      e.stopPropagation();
      if (del.dataset.armed !== '1') {
        del.dataset.armed = '1'; del.classList.add('armed'); del.textContent = 'Delete?';
        setTimeout(() => { del.dataset.armed = ''; del.classList.remove('armed'); del.textContent = '✕'; }, 3500);
        return;
      }
      try {
        await jfetch(`/api/study/agent/threads/${del.dataset.delthread}`, { method: 'DELETE' });
        A.threads = A.threads.filter(t => t.id !== del.dataset.delthread);
        if (A.threadId === del.dataset.delthread) { A.threadId = null; A.messages = []; renderLog(); }
        renderThreads();
      } catch (err) { A.toast(err.message, true); }
      return;
    }
    const row = e.target.closest('[data-thread]');
    if (row && !A.streaming) loadThread(row.dataset.thread);
  };
}

async function loadThread(id) {
  A.threadId = id;
  const t = A.threads.find(x => x.id === id);
  if (t && t.deck_id) {
    A.deckId = t.deck_id;
    const sel = A.el?.querySelector('#study-agent-scope');
    if (sel) sel.value = t.deck_id;
  }
  renderThreads();
  try {
    const r = await jfetch(`/api/study/agent/threads/${id}/messages`);
    A.messages = (r.messages || []).map(m => m.role === 'tool'
      ? { role: 'tool', name: m.name, output: m.output, ok: !(m.output || '').startsWith('ERROR:') }
      : m);
  } catch (e) { A.toast(e.message, true); A.messages = []; }
  renderLog();
}

function renderMessage(m) {
  if (m.role === 'user') return `<div class="study-agent-msg user">${A.esc(m.content)}</div>`;
  if (m.role === 'error') return `<div class="study-agent-msg error">${A.esc(m.content)}</div>`;
  if (m.role === 'thinking') return `<div class="study-agent-msg thinking">${A.esc(m.content.slice(-400))}</div>`;
  if (m.role === 'tool') {
    const args = m.args_preview ? `(${A.esc(m.args_preview)})` : '';
    return `<details class="study-agent-tool ${m.pending ? 'pending' : ''} ${m.ok === false ? 'fail' : ''}">
      <summary>⚙ ${A.esc(m.name)}${args}${m.pending ? '' : (m.ok === false ? ' ✗' : ' ✓')}</summary>
      <pre>${A.esc(m.output || '')}</pre></details>`;
  }
  const calls = (m.tool_calls || []).map(c => `<div class="study-agent-hint">→ ${A.esc(c.name)}</div>`).join('');
  if (!(m.content || '').trim() && !calls) return '';
  return `<div class="study-agent-msg assistant study-md">${md(m.content)}${calls}</div>`;
}

function renderLog() {
  const log = A.el?.querySelector('#study-agent-log');
  if (!log) return;
  if (!A.messages.length) {
    const focus = A.decks.find(d => d.id === A.deckId);
    log.innerHTML = `<div class="study-empty">
      ${focus ? `Focused on <b>${A.esc(focus.name)}</b>. ` : ''}The agent can explain things from your materials (with page citations),
      write or fix questions, add/remove materials and subjects, extract or generate questions, write notes, plan exams —
      and, if you allow it, change this app's code.</div>`;
    return;
  }
  log.innerHTML = A.messages.map(renderMessage).join('');
  log.querySelectorAll('a[href*="/api/upload/"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  // Turn the tutor's ```mermaid fences into diagrams and typeset any formula
  // KaTeX had to defer. Both lazy-load on first use and swallow their own
  // errors, so a missing library leaves readable source rather than a blank.
  // This runs on every renderLog because streaming replaces the whole log, and
  // renderMermaid skips nodes it has already processed.
  try { renderMermaid(log); } catch { /* diagram stays as its source */ }
  try { renderMath(log); } catch { /* formula stays as its source */ }
  log.scrollTop = log.scrollHeight;
}

function scheduleRender() {
  if (A._raf) return;
  A._raf = requestAnimationFrame(() => { A._raf = null; renderLog(); });
}

// ---------------------------------------------------------------------------
// streaming
// ---------------------------------------------------------------------------

function setSendLabel() {
  const b = A.el?.querySelector('#study-agent-send');
  if (b) b.textContent = A.streaming ? 'Stop' : 'Send';
}

function stop() {
  try { A.abort?.abort(); } catch { /* already done */ }
}

async function send() {
  const input = A.el?.querySelector('#study-agent-input');
  const text = (input?.value || '').trim();
  if (!text || A.streaming) return;
  input.value = '';
  A.messages.push({ role: 'user', content: text });
  let current = null;          // assistant bubble being streamed
  let thinking = null;
  A.streaming = true; setSendLabel(); renderLog();
  A.abort = new AbortController();
  try {
    const res = await fetch(`${API}/api/study/agent/chat`, {
      method: 'POST', credentials: 'same-origin', signal: A.abort.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, thread_id: A.threadId, deck_id: A.deckId, allow_code: A.allowCode }),
    });
    if (!res.ok || !res.body) {
      let detail = `Request failed (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    const handle = (ev) => {
      if (ev.type === 'thread') {
        if (!A.threadId) { A.threadId = ev.thread_id; refreshThreads(); }
        return;
      }
      if (ev.delta !== undefined) {
        if (ev.thinking) {
          if (!thinking) { thinking = { role: 'thinking', content: '' }; A.messages.push(thinking); }
          thinking.content += ev.delta;
        } else {
          if (thinking) { A.messages = A.messages.filter(m => m !== thinking); thinking = null; }
          if (!current) { current = { role: 'assistant', content: '' }; A.messages.push(current); }
          current.content += ev.delta;
        }
        scheduleRender();
        return;
      }
      if (ev.type === 'tool_start') {
        if (thinking) { A.messages = A.messages.filter(m => m !== thinking); thinking = null; }
        A.messages.push({ role: 'tool', id: ev.id, name: ev.name, args_preview: ev.args_preview, output: '', pending: true });
        current = null;
      } else if (ev.type === 'tool_output') {
        const card = A.messages.find(m => m.role === 'tool' && m.id === ev.id && m.pending)
          || [...A.messages].reverse().find(m => m.role === 'tool' && m.pending);
        if (card) { card.pending = false; card.ok = ev.ok; card.output = ev.output; }
      } else if (ev.type === 'agent_step') {
        current = null;
      } else if (ev.type === 'error') {
        A.messages.push({ role: 'error', content: ev.message });
      }
      scheduleRender();
    };
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (payload === '[DONE]') continue;
          try { handle(JSON.parse(payload)); } catch { /* ignore malformed */ }
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') A.messages.push({ role: 'error', content: e.message });
    else A.messages.push({ role: 'error', content: 'Stopped.' });
  }
  if (thinking) A.messages = A.messages.filter(m => m !== thinking);
  A.streaming = false; A.abort = null; setSendLabel(); renderLog();
  refreshThreads();
}

async function refreshThreads() {
  try { A.threads = (await jfetch('/api/study/agent/threads')).threads || []; renderThreads(); }
  catch { /* keep the old list */ }
}

export default { renderAgentTab, setAgentPrefill, setAgentScope };
