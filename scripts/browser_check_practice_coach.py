# -*- coding: utf-8 -*-
"""Real-browser check for the Ask AI mini panel (plan tasks 5/6, section 10).

No automation dependency: writes a temporary harness page into static/js/,
serves the repo over http.server, and drives headless Edge/Chrome against it.
The page runs the REAL studyPracticeCoach.js + studyChat.js against a mocked
fetch whose /ask response is a genuine ReadableStream — status bytes are split
mid-UTF-8-character to prove the shared parser handles split chunks, and the
reply only arrives after the reader scrolled up, exercising the follow logic
against real DOM layout.

Covers section 10 browser cases:
  1. overflow log + send at bottom -> same log node survives, message visible
  2. scroll up while "Reading materials" shows -> position fixed, Jump shown
  3. outer practice re-render -> composer text survives reattachment

Usage:
    python scripts/browser_check_practice_coach.py [--keep] [--verbose]

Exit code 0 = all checks passed; 1 = any failed or no browser found.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

START_EVENTS = []

PAGE = """<!doctype html>
<meta charset="utf-8">
<style>
  body { font: 14px sans-serif; }
  .study-coach-log { max-height: 260px; overflow-y: auto; display: flex;
    flex-direction: column; gap: 6px; padding: 0 4px; }
  .study-coach-msg { font-size: 12px; line-height: 1.5; padding: 5px 8px;
    border-radius: 7px; }
  .study-coach-msg.student { background: #eee; align-self: flex-end; }
  .study-coach-msg.assistant { background: #e8f0fa; align-self: flex-start; }
  .study-coach-jump[hidden] { display: none; }
</style>
<div id="result">pending</div>
<script type="module">
// Split the status payload mid-UTF-8 ('\u2113' = 3 bytes; cut after the 1st).
const statusPayload = new TextEncoder().encode(
  'data: {"type":"status","message":"Reading materia\\u2113s"}\\r\\n\\r\\n');
const cut = (function () {
  const idx = statusPayload.findIndex((b) => b === 0xe2); // first byte of \u2113
  return idx === -1 ? statusPayload.length : idx + 1;
})();

let releaseReply = null;
const replyGate = new Promise((r) => { releaseReply = r; });

const chunks = (function* () {
  yield statusPayload.slice(0, cut);
  yield new Promise((r) => { window._midStatus = r; }); // pause mid-stream
  yield statusPayload.slice(cut);
  yield replyGate;                              // reply waits for the reader
  yield new TextEncoder().encode(
    'data: {"type":"reply","content":"the approved reply"}\\n\\n');
  yield new TextEncoder().encode('data: [DONE]\\n\\n');
})();

const baseFetch = window.fetch;
window.fetch = (url, opts = {}) => {
  const u = String(url);
  if (u.includes('/threads?')) {
    return Promise.resolve({
      ok: true,
      json: async () => ({
        threads: [{ id: 't-1', title: 'Saved chat', question_id: 'q-1' }],
      }),
    });
  }
  if (u.includes('/messages')) {
    const msgs = [];
    for (let i = 0; i < 30; i++) {
      msgs.push({ role: 'user', content: 'old turn ' + i });
      msgs.push({ role: 'assistant', content: 'old reply ' + i });
    }
    return Promise.resolve({ ok: true, json: async () => ({ messages: msgs }) });
  }
  if (u.includes('/ask')) {
    const stream = new ReadableStream({
      async pull(controller) {
        while (true) {
          const next = chunks.next();
          if (next.done) { controller.close(); return; }
          if (next.value instanceof Uint8Array) {
            controller.enqueue(next.value);
            return;
          }
          await next.value; // gate: keep this pull alive through the wait
        }
      },
    });
    return Promise.resolve({ ok: true, body: stream });
  }
  return Promise.resolve({ ok: false, status: 500,
                           json: async () => ({ detail: 'unmocked url ' + u }) });
};

const { createPracticeCoach, captureCoachFocus, restoreCoachFocus } =
  await import('./studyPracticeCoach.js');
const results = {};

const coach = createPracticeCoach({
  questionId: 'q-1',
  getContext: () => ({ draft: '', hints: [], consulted: false }),
  esc: (s) => String(s ?? ''),
  toast: () => {},
});
document.body.appendChild(coach.element);
coach.update();

const log = coach.element.querySelector('.study-coach-log');
const input = coach.element.querySelector('.study-coach-input');
const send = coach.element.querySelector('.study-coach-send');
const jump = coach.element.querySelector('.study-coach-jump');
const select = coach.element.querySelector('.study-coach-history');

const tick = (ms) => new Promise((r) => setTimeout(r, ms));

await tick(40);                                   // threads listing settles
select.value = 't-1';
select.dispatchEvent(new Event('change'));
await tick(40);                                   // history load settles
results.loadedHistory = log.children.length === 60;
results.overflowing = log.scrollHeight > log.clientHeight;

// Case 1: send from the bottom; the log DOM node must survive.
const logNode = log;
input.value = 'browser question';
send.dispatchEvent(new Event('click'));
await tick(40);                                   // stream started, status pending
results.busyLabel = send.textContent === 'Stop';
results.sameNodeAfterSend = log === logNode;
results.questionAppended = Array.from(log.children).some(
  (c) => c.textContent === 'browser question');
// The stream is paused mid-status; scroll up to read history (case 2).
log.scrollTop = 150;
log.dispatchEvent(new Event('scroll'));
const posWhileReading = log.scrollTop;
results.readerPositionKept = posWhileReading === 150;
results.jumpShownWhileReading = !jump.hidden;

window._midStatus();                              // let the rest of the status land
await tick(40);
releaseReply();                                   // now the reply can arrive
await tick(40);
results.posAfterReplyKept = log.scrollTop === posWhileReading;
results.replyArrived = Array.from(log.children).some(
  (c) => c.textContent.includes('the approved reply'));
results.jumpStillShown = !jump.hidden;

jump.dispatchEvent(new Event('click'));
const atBottom = log.scrollTop >= log.scrollHeight - log.clientHeight - 1;
results.jumpScrollsDown = atBottom;
results.jumpHidesAfter = jump.hidden;

// Case 3: a same-question renderPractice() re-render replaces the practice DOM
// around the SAME panel instance. Mirrors static/js/study.js order: snapshot
// outer scroll + coach log scroll + capture focus BEFORE el.innerHTML,
// reattach the same instance, restore log scroll + focus, reset outer scroll.
const practiceEl = document.createElement('div');
practiceEl.style.maxHeight = '400px';
practiceEl.style.overflowY = 'auto';
document.body.appendChild(practiceEl);
practiceEl.appendChild(coach.element);
input.value = 'half typed sentence';
log.scrollTop = 120;
log.dispatchEvent(new Event('scroll')); // reader mid-history: not following
practiceEl.scrollTop = 250;
input.focus();
input.setSelectionRange(2, 7);
const keepOuterScroll = practiceEl.scrollTop;
const keepInnerScroll = log.scrollTop;
const keepDraft = input.value;
const focusState = captureCoachFocus(coach.element);
results.focusCapturedInside = !!focusState;
practiceEl.innerHTML = '<div class="study-q-wrap"><div id="study-prac-coach"></div></div>';
practiceEl.querySelector('#study-prac-coach').replaceWith(coach.element);
coach.update();
if (keepInnerScroll != null) log.scrollTop = keepInnerScroll;
restoreCoachFocus(focusState);
practiceEl.scrollTop = keepOuterScroll;
results.textSurvivesRerender = input.value === 'half typed sentence';
results.draftPreserved = input.value === keepDraft;
results.focusRestoredAfterRerender = document.activeElement === input;
results.selectionRestored = results.focusRestoredAfterRerender &&
  input.selectionStart === 2 && input.selectionEnd === 7;
results.innerScrollKept = log.scrollTop === keepInnerScroll;
results.outerScrollKept = practiceEl.scrollTop === keepOuterScroll;

// Case 4 (review finding 4): a rendered message grows late (math/diagram/
// plot) AFTER the log is already at its height cap.
const lastMsg = log.children[log.children.length - 1];
log.scrollTop = log.scrollHeight;      // reader at the bottom
lastMsg.style.height = '420px';        // late render grows the message
await tick(80);                        // ResizeObserver delivers
const bottomGap = log.scrollHeight - log.clientHeight - log.scrollTop;
results.growthFollowedAtBottom = bottomGap <= 48;

// Reader scrolled up: growth keeps their position and Jump stays offered.
log.scrollTop = 160;
log.dispatchEvent(new Event('scroll'));
const posBeforeGrowth = log.scrollTop;
lastMsg.style.height = '820px';
await tick(80);
results.growthKeepsReaderPosition = log.scrollTop === posBeforeGrowth ||
  log.scrollTop === posBeforeGrowth;
results.jumpOfferedAfterGrowth = !jump.hidden;

document.getElementById('result').textContent = JSON.stringify(results);
document.title = 'coach-browser-harness-done';
</script>
"""


def _find_browser():
    for exe in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ):
        if exe and Path(exe).exists() or (exe and not Path(exe).exists() and False):
            return exe
    return None


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _serve(repo: Path, port: int):
    os_module = __import__("os")
    handler = lambda *a, **k: _QuietHandler(*a, directory=str(repo), **k)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def main() -> int:
    keep = "--keep" in sys.argv
    browser = _find_browser()
    if not browser:
        print("no headless browser found (Edge/Chrome) — browser check skipped")
        return 1

    page = REPO / "static" / "js" / "_tmp_coach_browser_harness.html"
    page.write_text(PAGE, encoding="utf-8")
    httpd = None
    try:
        httpd = _serve(REPO, 8765)
        time.sleep(0.4)
        url = f"http://127.0.0.1:8765/static/js/_tmp_coach_browser_harness.html"
        proc = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--virtual-time-budget=15000", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        dom = proc.stdout
        m = re.search(r'<div id="result">(\{.*?\})</div>', dom, re.DOTALL)
        if not m:
            print("harness did not report results; dom tail:")
            print(proc.stdout[-500:])
            print("stderr tail:", proc.stderr[-300:])
            return 1
        results = json.loads(m.group(1))
        failures = [k for k, v in results.items() if v is not True]
        for k, v in sorted(results.items()):
            print(f"  [{'ok' if v else 'FAIL'}] {k}")
        if failures:
            print("browser check failed:", ", ".join(failures))
            return 1
        print("browser check passed")
        return 0
    finally:
        if httpd:
            httpd.shutdown()
        if not keep:
            try:
                page.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())