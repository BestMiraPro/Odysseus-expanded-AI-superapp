/**
 * Ask AI mini panel (protected practice coach).
 *
 * One instance per question OCCURRENCE inside a practice session (a later
 * re-drill of the same question starts a new chat). The instance owns its
 * dialogue DOM — study.js never re-renders the panel on send/reply/stop;
 * it mounts the element once and reattaches it across same-question
 * renderPractice() calls. Conversation state lives here, never in the
 * tutor's singleton.
 *
 * The turn is streamed from POST /api/study/questions/{id}/ask (stream=true):
 * fixed status events + one atomic, server-reviewed reply. Nothing here can
 * display answer-bearing tool data because the stream never carries it.
 */

import { mdToHtml } from './markdown.js';
import { consumeStudyEvents, enrichStudyMessage } from './studyChat.js';

const API = window.location.origin;
const FOLLOW_PX = 48; // px from the bottom at which reading == following

/**
 * Remember who inside the coach (if anyone) held focus, plus their selection,
 * before an outer re-render replaces the practice view — replacing the parent
 * disconnects the active element and browsers move focus away.
 */
export function captureCoachFocus(element) {
  const ae = (typeof document !== 'undefined' && document.activeElement) || null;
  if (ae && typeof ae.focus === 'function' && element && element.contains(ae)) {
    return { el: ae,
             start: ae.selectionStart ?? null,
             end: ae.selectionEnd ?? null };
  }
  return null;
}

/** Put focus and selection back after a reattachment (never scrolls). */
export function restoreCoachFocus(state) {
  if (!state) return;
  try { state.el.focus({ preventScroll: true }); } catch { /* element gone */ }
  if (state.start != null && typeof state.el.setSelectionRange === 'function') {
    try { state.el.setSelectionRange(state.start, state.end); }
    catch { /* non-text control */ }
  }
}

export function createPracticeCoach({ questionId, getContext, esc, toast }) {
  esc = esc || ((s) => String(s ?? ''));
  toast = toast || (() => {});

  let generation = 0;      // bumped by dispose(); stale continuations check it
  let threadSeq = 0;       // bumped by conversation changes; same contract
  let historyLoading = false; // true while the selected thread's history loads; sends block
  let detached = true;     // false while the panel is mounted in the page
  let busy = false;        // exactly one active send per instance
  let controller = null;   // AbortController for the active send
  let threadId = null;     // null = "New conversation."
  let threads = [];
  let follow = true;       // stick to the bottom as new content arrives
  let lastRequest = null;  // { message } — Retry resends it as a new turn
  let lastRetryable = false;
  let observer = null;     // delayed math/diagram/plot height changes

  const md = (src) => {
    try { return mdToHtml(src || '', {}); } catch { return esc(src || ''); }
  };

  const root = document.createElement('div');
  root.className = 'study-coach';
  root.innerHTML = `
    <div class="study-coach-head">Ask AI — understand it without revealing the answer.</div>
    <div class="study-coach-log" role="log" aria-label="Ask AI conversation"></div>
    <div class="study-coach-status" aria-live="polite"></div>
    <div class="study-coach-actions">
      <button class="study-btn small study-coach-jump" hidden>Jump to latest</button>
      <button class="study-btn small study-coach-retry" hidden>Retry</button>
    </div>
    <div class="study-coach-composer">
      <select class="study-select study-coach-history" aria-label="Conversation history"></select>
      <textarea class="study-textarea study-coach-input" rows="2"
        placeholder="Stuck? Ask about the wording, a concept, or how to start…"></textarea>
      <button class="study-btn primary study-coach-send">Send</button>
    </div>`;

  const log = root.querySelector('.study-coach-log');
  const status = root.querySelector('.study-coach-status');
  const jump = root.querySelector('.study-coach-jump');
  const retry = root.querySelector('.study-coach-retry');
  const select = root.querySelector('.study-coach-history');
  const input = root.querySelector('.study-coach-input');
  const sendBtn = root.querySelector('.study-coach-send');

  // ------------------------------------------------------------------ scroll

  function nearBottom() {
    if (!log) return true;
    return log.scrollHeight - log.clientHeight - log.scrollTop <= FOLLOW_PX;
  }

  function scrollBottom() {
    if (log) log.scrollTop = log.scrollHeight;
  }

  function syncJump() {
    if (!jump) return;
    // Reading history: show the way back while new content sits below.
    jump.hidden = follow || nearBottom();
  }

  log.addEventListener('scroll', () => {
    follow = nearBottom();
    syncJump();
  });

  jump.addEventListener('click', () => {
    follow = true;
    scrollBottom();
    syncJump();
    log?.focus?.();
  });

  if (typeof ResizeObserver !== 'undefined' && log) {
    observer = new ResizeObserver(() => {
      if (detached) return;
      if (follow) scrollBottom();
      else syncJump();  // content grew below the fold: show Jump to latest
    });
    // Observer of log PLUS every message node: once the log reaches its
    // height cap, later math/diagram/plot growth changes scrollHeight without
    // resizing the container, and only a message-node observation follows it.
    try { observer.observe(log); } catch { /* observer optional */ }
  }

  // ------------------------------------------------------------------ render

  function appendMessage(role, content) {
    const node = document.createElement('div');
    node.className = `study-coach-msg ${role} study-md`;
    if (role === 'student') {
      node.textContent = content;
    } else {
      node.innerHTML = md(content);
      enrichStudyMessage(node);
    }
    log.appendChild(node);
    try { observer?.observe(node); } catch { /* observer optional */ }
    if (!detached) {
      if (role === 'student') {
        // The learner's own message is always brought into view.
        follow = true;
        scrollBottom();
      } else if (follow) {
        scrollBottom();
      }
    }
    syncJump();
    return node;
  }

  function setStatus(text) {
    if (!status) return;
    status.textContent = text || '';
    status.classList.toggle('busy', !!text);
  }

  function showError(message, retryable) {
    lastRetryable = !!retryable;
    appendMessage('error', message);
    if (retry) retry.hidden = !lastRetryable;
  }

  function clearRetry() {
    lastRetryable = false;
    if (retry) retry.hidden = true;
  }

  function syncSendEnabled() {
    if (sendBtn) sendBtn.disabled = historyLoading;
  }

  function rebuildFromHistory(messages) {
    while (log.firstChild) log.removeChild(log.firstChild);
    for (const m of (messages || [])) {
      if (m.role === 'user') appendMessage('student', m.content || '');
      else if (m.role === 'assistant') appendMessage('assistant', m.content || '');
    }
    follow = true;
    scrollBottom();
    syncJump();
  }

  // ------------------------------------------------------------------ http

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

  async function refreshThreads() {
    const gen = generation;
    try {
      threads = (await jfetch(`/api/study/agent/threads?question_id=${encodeURIComponent(questionId)}`)).threads || [];
    } catch (e) {
      if (gen !== generation) return;
      setStatus(esc(e.message));
      return;
    }
    if (gen !== generation) return;
    renderHistorySelect();
  }

  function renderHistorySelect() {
    const current = threadId;
    select.innerHTML = `<option value="">New conversation.</option>`
      + threads.map((t) => `<option value="${esc(t.id)}" ${t.id === current ? 'selected' : ''}>${esc(t.title || 'Chat')}</option>`).join('');
  }

  async function loadThread(id) {
    // (gen, seq) tokens: dispose invalidates generation; switching
    // conversations bumps seq. A late response for a thread that is no
    // longer selected must never replace the displayed one. While the
    // selected history loads, sends block (historyLoading) so the pending
    // response cannot wipe a newly sent exchange that matches the same
    // gen/seq/thread checks.
    const seq = threadSeq;
    const gen = generation;
    historyLoading = true;
    syncSendEnabled();
    setStatus('Loading conversation…');
    try {
      const r = await jfetch(`/api/study/agent/threads/${encodeURIComponent(id)}/messages`);
      if (gen !== generation || seq !== threadSeq || threadId !== id) return;
      rebuildFromHistory(r.messages || []);
      setStatus('');
    } catch (e) {
      if (gen !== generation || seq !== threadSeq || threadId !== id) return;
      setStatus('');
      showError(e.message, false);
    } finally {
      if (gen === generation && seq === threadSeq && threadId === id) {
        historyLoading = false;
        syncSendEnabled();
      }
    }
  }

  // ------------------------------------------------------------------ send

  function contextPayload() {
    // Read the LIVE practice state at send time — never a snapshot from the
    // initial render, never a result that does not exist yet.
    const ctx = (typeof getContext === 'function' ? getContext() : null) || {};
    const payload = {
      message: '',
      thread_id: threadId || undefined,
      stream: true,
      hints: Array.isArray(ctx.hints) ? ctx.hints.slice(0, 3) : [],
      consulted: !!ctx.consulted,
    };
    if (typeof ctx.draft === 'string' && ctx.draft.trim()) payload.draft = ctx.draft;
    if (ctx.choice != null) payload.choice_index = ctx.choice;
    if (ctx.submissionId) payload.submission_id = ctx.submissionId;
    return payload;
  }

async function send() {
    if (busy || detached) return;
    if (historyLoading) return; // block until the selected history loads
    const text = (input?.value || '').trim();
    if (!text) return;
    const gen = generation;
    const seq = threadSeq;  // the conversation selected when the send started
    input.value = ''; // only after the request is captured
    lastRequest = { message: text };
    clearRetry();
    setStatus('');
    appendMessage('student', text);
    busy = true;
    sendBtn.textContent = 'Stop';
    controller = new AbortController();
    try {
      const body = contextPayload();
      body.message = text;
      const res = await fetch(`${API}/api/study/questions/${encodeURIComponent(questionId)}/ask`, {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok || !res.body) {
        let detail = `Request failed (${res.status})`;
        try { detail = ((await res.json()).detail) || detail; } catch { /* keep */ }
        throw new Error(detail);
      }
      await consumeStudyEvents(res, (ev) => {
        if (gen !== generation || seq !== threadSeq) return;
        if (ev.type === 'thread') {
          if (!threadId) { threadId = ev.thread_id; refreshThreads(); }
        } else if (ev.type === 'status') {
          setStatus(ev.message);
        } else if (ev.type === 'reply') {
          setStatus('');
          appendMessage('assistant', ev.content);
          if (ev.retryable) { lastRetryable = true; retry.hidden = false; }
        } else if (ev.type === 'error') {
          setStatus('');
          showError(ev.message, !!ev.retryable);
        }
        // model_info and anything unexpected: deliberately ignored.
      });
    } catch (e) {
      if (gen !== generation || seq !== threadSeq) return;
      if (e.name === 'AbortError') {
        setStatus('');
        return;
      }
      showError(e.message, true);
    } finally {
      if (gen === generation && seq === threadSeq) {
        busy = false;
        controller = null;
        sendBtn.textContent = 'Send';
      }
    }
    if (gen !== generation || seq !== threadSeq) return;
    if (!threadId) refreshThreads(); // pick up a thread created even on error turns
  }

  function stop() {
    try { controller?.abort(); } catch { /* no active request */ }
  }

  function retrySend() {
    if (busy || historyLoading || !lastRequest) return;
    if (!input) return;
    input.value = lastRequest.message;
    send(); // an explicit NEW turn — the server history shows both
  }

  // ------------------------------------------------------------------ wiring

  select.addEventListener('change', () => {
    const id = select.value || null;
    if (id === threadId) return;
    if (busy) {
      stop(); // switching history aborts the active request...
      busy = false;               // ...and its completion callbacks must not
      sendBtn.textContent = 'Send';  // mutate the NEW selection afterwards
      controller = null;
    }
    threadSeq += 1;   // invalidates every pending history/send continuation
    threadId = id;
    clearRetry();
    setStatus('');
    if (id) {
      // Clear the pending view at once: the stale conversation must not
      // linger while the new history loads, and a blocked send means the
      // resolving history cannot wipe a newer local exchange.
      rebuildFromHistory([]);
      loadThread(id);
    } else {
      historyLoading = false;
      syncSendEnabled();
      rebuildFromHistory([]);
    }
  });

  sendBtn.addEventListener('click', () => (busy ? stop() : send()));
  input.addEventListener('keydown', (e) => {
    // Practice shortcuts (1-9, H/C/N, Enter-submit) are the panel's around
    // it; never let them see keys typed into the composer.
    e.stopPropagation();
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      send();
    }
  });
  retry.addEventListener('click', retrySend);

  refreshThreads();

  // ------------------------------------------------------------------ api

  return {
    element: root,

    /** study.js reattached us into a live practice view. */
    update() {
      // The panel's own DOM node was preserved (and reattached) as-is, so
      // composer text, focus and selection inside it survived the parent
      // re-render without any reconstruction.
      detached = false;
      syncJump();
    },

    /** Practice tab left: abort active work but keep the conversation. */
    pause() {
      detached = true;
      if (busy) stop();
    },

    /** New question / session / pane close: permanently retire this chat. */
    dispose() {
      generation += 1;
      detached = true;
      try { controller?.abort(); } catch { /* already done */ }
      try { observer?.disconnect(); } catch { /* observer optional */ }
      busy = false;
      controller = null;
    },
  };
}