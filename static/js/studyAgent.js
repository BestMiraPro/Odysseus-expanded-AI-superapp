/**
 * Study agent tab — chat with the in-app Study agent (routes/study_agent_routes.py).
 *
 * The agent acts through tools (subjects, materials, questions, cards, exams,
 * AI pipelines) and tutors from the learner's own materials. Admins can opt in
 * to code tools per chat ("Allow code changes"). Rendered inside the Study
 * pane's body by study.js; keeps its own state so switching tabs mid-stream
 * doesn't lose the conversation.
 *
 * "Teach me" starts a teaching session: a Socratic tutor with a skill map the
 * server keeps and grades (src/study_socratic.py). The map shows live above
 * the chat; tool outputs are hidden there so a lookup cannot spoil a question.
 */

import { mdToHtml } from './markdown.js';
import { consumeStudyEvents, enrichStudyMessage } from './studyChat.js';

const API = window.location.origin;

const A = {
  el: null, esc: (s) => String(s ?? ''), toast: () => {}, decks: [],
  caps: null, threads: [], threadId: null, messages: [],
  streaming: false, abort: null, allowCode: false, deckId: null, prefill: '',
  tutor: null,          // the open thread's teaching session (public skill map), or null
  teachForm: false,     // the "Teach me" topic form is showing
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
.study-agent-side > .study-btn { width: 100%; }
.study-agent-pill { align-self: flex-start; font-size: 11px; opacity: 0.5; padding: 0 4px; }
.study-agent-pill.pending::after { content: ' …'; }
.study-tutor { border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; font-size: 12px;
  display: flex; flex-direction: column; gap: 6px; }
.study-tutor-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.study-tutor-head .topic { font-weight: 600; font-size: 13px; }
.study-tutor-chip { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid currentColor; opacity: 0.85; }
.study-tutor-chip.complete { color: var(--green, #4f9e60); }
.study-tutor-chip.final { color: var(--accent, #5b8abf); }
.study-tutor-skills { display: flex; flex-direction: column; gap: 3px; max-height: 170px; overflow-y: auto; max-width: 560px; }
.study-tutor-row { display: grid; grid-template-columns: minmax(0, 1fr) 96px 30px; gap: 8px; align-items: center; }
.study-tutor-row .n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.study-tutor-row.focus .n { font-weight: 600; }
.study-tutor-row .lv { text-align: right; font-variant-numeric: tabular-nums; opacity: 0.8; }
.study-tutor-bar { position: relative; height: 6px; border-radius: 3px; background: rgba(128,128,128,0.2); overflow: hidden; }
.study-tutor-bar > span { position: absolute; inset: 0 auto 0 0; background: var(--accent, #5b8abf); }
.study-tutor-row.done .study-tutor-bar > span { background: var(--green, #4f9e60); }
.study-tutor-bar > i { position: absolute; top: 0; bottom: 0; width: 1px; background: currentColor; opacity: 0.45; }
.study-tutor-cmds { display: flex; gap: 6px; flex-wrap: wrap; }
.study-tutor details { font-size: 11.5px; }
.study-tutor details summary { cursor: pointer; opacity: 0.75; }
.study-tutor details ul { margin: 4px 0 0 16px; padding: 0; }
.study-teach-form { display: flex; flex-direction: column; gap: 8px; max-width: 520px; font-size: 13px; }
.study-teach-form input[type=text] { width: 100%; }
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

const AGENT_PLACEHOLDER = 'Ask for an explanation, a drill, or an action: “extract the questions from the 2023 exam”, '
  + '“explain elasticity from my notes”, “make 10 questions on chapter 3”…';
const TUTOR_PLACEHOLDER = 'Your answer — or “hint”, “show progress”, “harder”, “easier”, “just tell me”';
const TUTOR_PHASES = { diagnostic: 'Diagnostic', teaching: 'Learning', final: 'Final exam', complete: 'Mastered' };
// Quick commands the tutor understands (they are sent as ordinary messages).
const TUTOR_COMMANDS = [
  ['Show progress', 'show progress'], ['Harder', 'harder'], ['Easier', 'easier'], ['Just tell me', 'just tell me'],
];

/** The skill-map panel for a teaching session (pure: state in, HTML out). */
function tutorPanelHtml(t, esc) {
  if (!t) return '';
  const target = Number(t.target) || 4;
  const rows = (t.skills || []).map(s => {
    const lvl = Number(s.level) || 0;
    const done = s.placed && lvl >= target;
    const cls = `study-tutor-row${s.focus ? ' focus' : ''}${done ? ' done' : ''}`;
    const width = s.placed ? Math.round(100 * lvl / 5) : 0;
    return `<div class="${cls}" title="${esc(s.placed ? `${lvl}/5 · ${s.label}` : 'not placed yet')}">
      <span class="n">${s.focus ? '▸ ' : ''}${esc(s.name)}</span>
      <span class="study-tutor-bar"><span style="width:${width}%"></span><i style="left:${Math.round(100 * target / 5)}%"></i></span>
      <span class="lv">${s.placed ? `${lvl}/5` : '?'}</span></div>`;
  }).join('');
  const miscon = (t.skills || []).flatMap(s => (s.misconceptions || []).map(m => `<li><b>${esc(s.name)}:</b> ${esc(m)}</li>`));
  const phase = t.phase || 'diagnostic';
  const result = t.result && phase === 'complete'
    ? `<div class="study-subtle">Passed${(t.result.weakest || []).length ? ` · weakest: ${esc(t.result.weakest.join(', '))}` : ''}</div>`
    : '';
  const cmds = phase === 'complete' ? '' : `<div class="study-tutor-cmds">${TUTOR_COMMANDS.map(([label, text]) =>
    `<button class="study-btn small" data-tutor-cmd="${esc(text)}">${esc(label)}</button>`).join('')}</div>`;
  return `<div class="study-tutor">
    <div class="study-tutor-head">
      <span class="topic">🎓 ${esc(t.topic)}</span>
      <span class="study-tutor-chip ${esc(phase)}">${esc(TUTOR_PHASES[phase] || phase)}</span>
      <span class="study-subtle">target ${target}/5 · ${esc(t.overall ?? 0)}% overall${t.difficulty ? ` · ${esc(t.difficulty_label)}` : ''}</span>
    </div>
    ${rows ? `<div class="study-tutor-skills">${rows}</div>` : '<div class="study-subtle">The tutor is breaking the topic into skills…</div>'}
    ${miscon.length ? `<details><summary>Misconceptions spotted (${miscon.length})</summary><ul>${miscon.join('')}</ul></details>` : ''}
    ${result}${cmds}
  </div>`;
}

// What a tool did, in words, for the teaching-session log (no output shown).
const TUTOR_TOOL_LABELS = {
  search_materials: 'searched your materials', get_material: 'read your materials',
  list_materials: 'looked at your materials', list_questions: 'looked at your question bank',
  get_question: 'looked at your question bank', add_cards: 'saved flashcards',
  tutor_record_check: 'updated your skill map', tutor_place: 'placed you on the skill map',
  tutor_set_skill_map: 'mapped the skills', tutor_finish: 'graded the final exam',
};

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
        <button class="study-btn small" id="study-agent-teach" title="A Socratic teaching session: a short diagnostic, a skill map from 0 to 5, one question at a time, surprise re-tests and a final exam">🎓 Teach me</button>
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
        <div id="study-agent-tutor"></div>
        <div class="study-agent-log" id="study-agent-log"></div>
        <div class="study-agent-composer">
          <textarea class="study-textarea" id="study-agent-input" placeholder="${A.esc(AGENT_PLACEHOLDER)}"></textarea>
          <button class="study-btn primary" id="study-agent-send">${A.streaming ? 'Stop' : 'Send'}</button>
        </div>
        <div class="study-agent-hint">Enter sends · Shift+Enter newline · destructive actions are confirmed in chat first</div>
      </section>
    </div>`;
  renderThreads();
  renderTutor();

  el.querySelector('#study-agent-new').addEventListener('click', () => {
    if (A.streaming) return;
    A.threadId = null; A.messages = []; A.tutor = null; A.teachForm = false;
    renderThreads(); renderTutor(); renderLog();
    el.querySelector('#study-agent-input')?.focus();
  });
  el.querySelector('#study-agent-teach').addEventListener('click', () => {
    if (A.streaming) return;
    A.threadId = null; A.messages = []; A.tutor = null; A.teachForm = true;
    renderThreads(); renderTutor(); renderLog();
    el.querySelector('#study-teach-topic')?.focus();
  });
  el.querySelector('#study-agent-tutor').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tutor-cmd]');
    if (b && !A.streaming) send(b.dataset.tutorCmd);
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
      <span class="t">${t.mode === 'socratic' ? '🎓 ' : ''}${A.esc(t.title)}</span>
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
        if (A.threadId === del.dataset.delthread) { A.threadId = null; A.messages = []; A.tutor = null; renderTutor(); renderLog(); }
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
  A.teachForm = false;
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
      ? { role: 'tool', name: m.name, output: m.output, ok: m.ok ?? !(m.output || '').startsWith('ERROR:') }
      : m);
    A.tutor = r.tutor || null;
  } catch (e) { A.toast(e.message, true); A.messages = []; A.tutor = null; }
  renderTutor();
  renderLog();
}

function renderTutor() {
  const box = A.el?.querySelector('#study-agent-tutor');
  if (box) box.innerHTML = tutorPanelHtml(A.tutor, A.esc);
  const input = A.el?.querySelector('#study-agent-input');
  if (input) input.placeholder = A.tutor ? TUTOR_PLACEHOLDER : AGENT_PLACEHOLDER;
}

// Tool calls a model writes as text run server-side, but their markup streams
// here first and would stay on screen as raw JSON until a reload.
const TOOL_MARKUP = [['```tool_call', '```'], ['<tool_call>', '</tool_call>'], ['<invoke', '</invoke>']];

function hideToolMarkup(text) {
  const lower = text.toLowerCase();
  let out = '';
  let pos = 0;
  while (pos < text.length) {
    let next = null;
    for (const [open, close] of TOOL_MARKUP) {
      const i = lower.indexOf(open, pos);
      if (i >= 0 && (!next || i < next.i)) next = { i, open, close };
    }
    if (!next) { out += text.slice(pos); break; }
    out += text.slice(pos, next.i);
    const end = lower.indexOf(next.close, next.i + next.open.length);
    if (end < 0) break; // still streaming: hide the unfinished block too
    pos = end + next.close.length;
  }
  return out.replace(/<\/?function_calls>/gi, '').trim();
}

function renderMessage(m) {
  if (m.role === 'user') return `<div class="study-agent-msg user">${A.esc(m.content)}</div>`;
  if (m.role === 'error') return `<div class="study-agent-msg error">${A.esc(m.content)}</div>`;
  if (m.role === 'thinking') return `<div class="study-agent-msg thinking">${A.esc(m.content.slice(-400))}</div>`;
  if (m.role === 'tool' && A.tutor) {
    // Teaching session: say that something happened, never what it returned.
    const label = TUTOR_TOOL_LABELS[m.name] || (String(m.name || '').startsWith('tutor_') ? 'updated the session' : 'checked something');
    return `<div class="study-agent-pill ${m.pending ? 'pending' : ''}">· ${A.esc(label)}${m.ok === false ? ' (failed)' : ''}</div>`;
  }
  if (m.role === 'tool') {
    const args = m.args_preview ? `(${A.esc(m.args_preview)})` : '';
    return `<details class="study-agent-tool ${m.pending ? 'pending' : ''} ${m.ok === false ? 'fail' : ''}">
      <summary>⚙ ${A.esc(m.name)}${args}${m.pending ? '' : (m.ok === false ? ' ✗' : ' ✓')}</summary>
      <pre>${A.esc(m.output || '')}</pre></details>`;
  }
  // A session's pills already say what ran; raw tool names would only add noise.
  const calls = A.tutor ? '' : (m.tool_calls || []).map(c => `<div class="study-agent-hint">→ ${A.esc(c.name)}</div>`).join('');
  const content = hideToolMarkup(m.content || '');
  if (!content && !calls) return '';
  return `<div class="study-agent-msg assistant study-md">${md(content)}${calls}</div>`;
}

function renderLog() {
  const log = A.el?.querySelector('#study-agent-log');
  if (!log) return;
  if (!A.messages.length && A.teachForm) {
    const focus = A.decks.find(d => d.id === A.deckId);
    log.innerHTML = `<form class="study-teach-form" id="study-teach-form">
      <div><b>🎓 Teach me</b> — a strict Socratic tutor. It starts with a few questions to find your level, builds a
      skill map from 0 to 5, then teaches by asking one question at a time. Levels are earned: the app, not the model,
      decides when you go up, earlier skills come back as surprise re-tests, and it ends with a final exam.
      ${focus ? `Questions come from your <b>${A.esc(focus.name)}</b> materials and practice history.` : 'Pick a subject above to ground it in your materials.'}</div>
      <input type="text" class="study-input" id="study-teach-topic" maxlength="200" placeholder="Topic, e.g. price elasticity of demand" required>
      <label class="study-subtle"><input type="checkbox" id="study-teach-full"> Aim for full mastery (5/5: edge cases and teaching it back)</label>
      <div><button class="study-btn primary" type="submit">Start session</button></div>
    </form>`;
    log.querySelector('#study-teach-form').addEventListener('submit', (e) => { e.preventDefault(); startTeaching(); });
    return;
  }
  if (!A.messages.length) {
    const focus = A.decks.find(d => d.id === A.deckId);
    log.innerHTML = `<div class="study-empty">
      ${focus ? `Focused on <b>${A.esc(focus.name)}</b>. ` : ''}The agent can explain things from your materials (with page citations),
      write or fix questions, add/remove materials and subjects, extract or generate questions, write notes, plan exams —
      and, if you allow it, change this app's code. To learn a topic properly, start a <b>🎓 Teach me</b> session.</div>`;
    return;
  }
  log.innerHTML = A.messages.map(renderMessage).join('');
  log.querySelectorAll('a[href*="/api/upload/"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  // Turn the tutor's ```mermaid fences into diagrams and typeset any formula
  // KaTeX had to defer, plus the python plots. All of these lazy-load on first
  // use and swallow their own errors, so a missing library leaves readable
  // source rather than a blank. This runs on every renderLog because streaming
  // replaces the whole log; the shared enrich skips what it already processed.
  enrichStudyMessage(log);
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

/** Start a teaching session from the "Teach me" form, then open it. */
async function startTeaching() {
  if (A.streaming) return;
  const topic = (A.el?.querySelector('#study-teach-topic')?.value || '').trim();
  if (!topic) return;
  const full = !!A.el?.querySelector('#study-teach-full')?.checked;
  let t;
  try {
    t = await jfetch('/api/study/agent/threads', {
      method: 'POST', body: JSON.stringify({ deck_id: A.deckId, teach_topic: topic, full_mastery: full }),
    });
  } catch (e) { A.toast(e.message, true); return; }
  A.threadId = t.id; A.messages = []; A.teachForm = false;
  A.tutor = { topic: t.title, phase: 'diagnostic', target: full ? 5 : 4, overall: 0, skills: [] };
  A.threads = [t, ...A.threads.filter(x => x.id !== t.id)];
  renderThreads(); renderTutor();
  await send(`Teach me ${topic}.${full ? ' I want full mastery.' : ''}`);
}

async function send(override) {
  const input = A.el?.querySelector('#study-agent-input');
  const typed = override === undefined;
  const text = (typed ? (input?.value || '') : String(override)).trim();
  if (!text || A.streaming) return;
  if (typed && input) input.value = '';
  A.teachForm = false;
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
    const handle = (ev) => {
      if (ev.type === 'thread') {
        if (!A.threadId) { A.threadId = ev.thread_id; refreshThreads(); }
        return;
      }
      if (ev.type === 'tutor_state') {
        A.tutor = ev.state || null; renderTutor();
        return;
      }
      if (ev.type === 'mode') { refreshThreads(); return; }
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
    await consumeStudyEvents(res, handle);
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
