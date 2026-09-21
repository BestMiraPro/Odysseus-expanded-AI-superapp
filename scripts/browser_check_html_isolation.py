# -*- coding: utf-8 -*-
"""Real-browser acceptance for the HTML runner preview isolation (plan S1).

No automation dependency: writes a temporary harness page into static/js/,
serves the repo over http.server, and drives headless Edge/Chrome against the
REAL codeRunner.js runHTML. The probe payload run inside the preview reports,
per plan §S1:

- its own DOM works (script execution preserved);
- reading the app's localStorage sentinel fails (opaque origin);
- reading parent.document fails (no inherited authenticated origin);
- a synthetic same-origin API request is rejected;
- the preview iframe's origin is the opaque string "null".

Exit 0 = all checks passed; 1 = any failed or no browser found.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PAGE = """<!doctype html>
<meta charset="utf-8">
<div id="result">pending</div>
<div id="host-slot"></div>
<script type="module">
window.__isoReport = (obj) => {
  document.getElementById('result').textContent = JSON.stringify(obj);
  document.title = 'html-isolation-done';
};

async function main() {
  const results = {};
  window.localStorage.setItem('__app_sentinel', 'ORIGIN-SECRET-SENTINEL');

  // Attach before any frame exists so no probe message can race the listener.
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (d && d.probe && d.probe.indexOf('html-isolation') === 0) {
      delete results.timeout;
      results.probe = d.results;
      results.channel = d.probe;
      if (results.frameOrigin !== undefined) window.__isoReport(results);
    }
  });

  const { buildIsolatedPreviewFrame } = await import('./codeRunner.js');

  // A same-origin host document stands in for the popup's wrapper document
  // (both are the app origin the preview must not inherit). The preview
  // itself is built by the production function under test.
  const host = document.createElement('iframe');
  host.src = 'about:blank';
  document.getElementById('host-slot').appendChild(host);
  await new Promise((resolve) => {
    if (host.contentDocument && host.contentDocument.readyState === 'complete') resolve();
    host.addEventListener('load', resolve);
  });

  const payload = '<body><script>' +
    'var r = {};' +
    'try { document.body.dataset.ran = "1"; r.ownDom = true; } catch(e) { r.ownDom = String(e); }' +
    'try { r.localStorage = localStorage.getItem("__app_sentinel"); } catch(e) { r.localStorage = "SecurityError:" + e.name; }' +
    'try { r.parentDoc = !!parent.document.documentElement; } catch(e) { r.parentDoc = "SecurityError:" + e.name; }' +
    'var done = false;' +
    'function report() { if (done || typeof r.api === "undefined") return; done = true;' +
    '  try { top.postMessage({ probe: "html-isolation", results: r }, "*"); } catch(e) {} }' +
    'fetch("/api/__isolation_probe__", { method: "POST", credentials: "same-origin" })' +
    '  .then(() => { r.api = "REACHED"; report(); })' +
    '  .catch((e) => { r.api = "blocked:" + e.name; report(); });' +
    'setTimeout(() => { r.api = r.api || "unreported"; report(); }, 800);' +
    '<\\/script></body>';

  const frame = buildIsolatedPreviewFrame(host.contentDocument, payload);
  results.frameSandbox = frame.getAttribute('sandbox');
  results.frameSrcdoc = frame.getAttribute('srcdoc');
  try {
    results.frameOrigin = frame.contentWindow.origin;
  } catch (err) {
    // Modern Chromium throws SecurityError for opaque origins — the same
    // proof the old "null" string was.
    results.frameOrigin = 'opaque';
  }
  try {
    void frame.contentWindow.localStorage;
    results.harnessStorageAccess = 'ALLOWED';
  } catch (err) {
    results.harnessStorageAccess = 'SecurityError';
  }

  setTimeout(() => {
    if (!document.getElementById('result').textContent.includes('probe')) {
      results.timeout = true;
      window.__isoReport(results);
    }
  }, 6000);
}

main().catch((e) => {
  window.__isoReport({ fatal: String((e && (e.stack || e.message)) || e) });
});
</script>
"""


def _find_browser():
    for exe in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ):
        if exe and Path(exe).exists():
            return exe
    return None


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main() -> int:
    browser = _find_browser()
    if not browser:
        print("no headless browser found (Edge/Chrome) — skipped")
        return 1

    page = REPO / "static" / "js" / "_tmp_html_isolation_harness.html"
    page.write_text(PAGE, encoding="utf-8")
    handler = lambda *a, **k: _QuietHandler(*a, directory=str(REPO), **k)
    httpd = socketserver.TCPServer(("127.0.0.1", 8766), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.4)
        url = ("http://127.0.0.1:8766/static/js/"
               "_tmp_html_isolation_harness.html")
        proc = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--disable-popup-blocking", "--virtual-time-budget=15000",
             "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        dom = proc.stdout
        m = re.search(r'<div id="result">(\{.*?\})</div>', dom, re.DOTALL)
        if not m:
            print("harness reported nothing; dom tail:")
            print(dom[-400:])
            return 1
        results = json.loads(m.group(1))
        checks = {
            "sandbox-attr": results.get("frameSandbox") == "allow-scripts",
            "srcdoc-carries-code": bool(results.get("frameSrcdoc")),
            "own-dom-works": results.get("probe", {}).get("ownDom") is True,
            "localStorage-blocked": str(
                results.get("probe", {}).get("localStorage")
            ).startswith("SecurityError"),
            "parent-doc-blocked": str(
                results.get("probe", {}).get("parentDoc")
            ).startswith("SecurityError"),
            "api-rejected": str(
                results.get("probe", {}).get("api", "")
            ).startswith("blocked"),
            "opaque-origin": results.get("frameOrigin") in ("null", "opaque"),
            "harness-cannot-reach-frame-storage": results.get(
                "harnessStorageAccess") == "SecurityError",
            "no-timeout": not results.get("timeout", False),
        }
        failed = [k for k, v in checks.items() if not v]
        for k, v in sorted(checks.items()):
            print(f"  [{'ok' if v else 'FAIL'}] {k}")
        if failed:
            print("probe:", json.dumps(results.get("probe"), ensure_ascii=False))
            print("html isolation check failed:", ", ".join(failed))
            return 1
        print("html isolation check passed")
        return 0
    finally:
        httpd.shutdown()
        try:
            page.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())