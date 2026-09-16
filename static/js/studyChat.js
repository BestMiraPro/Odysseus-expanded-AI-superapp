/**
 * Shared, stateless chat plumbing extracted from the tutor tab so the Ask AI
 * mini panel can reuse it without sharing the tutor's singleton state (its
 * `A` object, its unconditional scroll-to-bottom, its tab lifecycle).
 */

import { renderMermaid, renderMath } from './markdown.js';
import { renderPythonPlots } from './codeRunner.js';

/**
 * Consume an SSE response body, invoking onEvent(parsedJson) per event.
 *
 * Handles split UTF-8 chunks, many events per chunk, CRLF framing, [DONE],
 * and malformed blocks — a bad line is skipped without losing the valid
 * events after it. Stops at [DONE] or end of stream.
 */
export async function consumeStudyEvents(response, onEvent) {
  if (!response || !response.body || !response.body.getReader) {
    throw new Error(response ? `Request failed (${response.status})` : 'No response body');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    buf = buf.replace(/\r\n/g, '\n');
    let idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of block.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') return;
        let ev;
        try { ev = JSON.parse(payload); } catch { continue; }
        if (ev) onEvent(ev);
      }
    }
  }
  // A final event without its terminator is still an event.
  const tail = buf.trim();
  if (tail.startsWith('data:') && tail.slice(5).trim() !== '[DONE]') {
    try { onEvent(JSON.parse(tail.slice(5).trim())); } catch { /* truncated */ }
  }
}

/**
 * Turn the mermaid placeholders / deferred math / python plots inside ONE
 * newly appended message node into their rendered forms. Idempotent like the
 * tutor's whole-log pass: renderers skip nodes they already processed.
 */
export function enrichStudyMessage(element) {
  if (!element) return;
  try { renderMermaid(element); } catch { /* diagram stays as its source */ }
  try { renderMath(element); } catch { /* formula stays as its source */ }
  try { renderPythonPlots(element); } catch { /* plot stays as its source */ }
  element.querySelectorAll?.('a[href*="/api/upload/"]').forEach((a) => {
    a.target = '_blank';
    a.rel = 'noopener';
  });
}