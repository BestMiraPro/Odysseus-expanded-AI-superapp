// static/js/codeRunner.js

import * as uiModule from './ui.js';

/**
 * In-browser code runner for Python (Pyodide), JavaScript, and HTML
 */

let pyodideInstance = null;
let pyodideLoading = false;
const pyodideQueue = [];

/**
 * Get or create an output panel below the <pre> element
 */
function getOrCreatePanel(pre) {
  let panel = pre.nextElementSibling;
  if (panel && panel.classList.contains('code-runner-output')) {
    panel.innerHTML = '';
    panel.style.display = 'block';
    return panel;
  }
  panel = document.createElement('div');
  panel.className = 'code-runner-output';
  pre.parentNode.insertBefore(panel, pre.nextSibling);
  return panel;
}

/**
 * Show a loading message in the panel
 */
function showLoading(panel, msg) {
  panel.innerHTML = `<div class="code-runner-loading">${msg}</div>`;
}

/**
 * Show output text in the panel
 */
function showOutput(panel, text, isError) {
  const el = document.createElement('pre');
  el.className = isError ? 'code-runner-pre code-runner-error' : 'code-runner-pre';
  el.textContent = text;
  panel.innerHTML = '';
  panel.appendChild(el);
  // Copy button — visible labeled pill at the top-right of the panel
  // itself (no separate footer / divider, no tiny icon corner).
  if (text) {
    const cbtn = document.createElement('button');
    cbtn.type = 'button';
    cbtn.className = 'code-runner-copy-inline';
    cbtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px;"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy';
    cbtn.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      let ok = false;
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, text.length);
        ok = document.execCommand && document.execCommand('copy');
        ta.remove();
      } catch (_) {}
      if (!ok && navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => {
          if (uiModule.showToast) uiModule.showToast('Copied');
          cbtn.textContent = 'Copied!';
          setTimeout(() => { cbtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px;"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy'; }, 1500);
        }).catch(() => { if (uiModule.showToast) uiModule.showToast('Copy failed'); });
        return;
      }
      if (uiModule.showToast) uiModule.showToast(ok ? 'Copied' : 'Copy failed');
      const orig = cbtn.innerHTML;
      cbtn.textContent = ok ? 'Copied!' : 'Copy failed';
      setTimeout(() => { cbtn.innerHTML = orig; }, 1500);
    });
    // Button lives directly in the panel — no wrapping bar. The panel is
    // position:relative so the button can sit absolute-top-right of it.
    panel.appendChild(cbtn);
  }
  if (isError) {
    setTimeout(() => { if (panel) panel.style.display = 'none'; }, 7000);
  }
}

/**
 * Legacy absolute-positioned copy button — replaced by the inline bar in
 * showOutput. Kept here as no-op so any earlier callers don't crash.
 */
function addCopyBtn_unused(panel, text) {
  if (!text) return;
  const btn = document.createElement('button');
  btn.type = 'button';  // Default <button> type is 'submit' — explicit "button" avoids any accidental form submission.
  btn.className = 'code-runner-copy';
  btn.title = 'Copy output';
  btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    e.preventDefault();
    // Synchronous copy via a hidden textarea + execCommand — this is the
    // single most reliable path across browsers / non-secure contexts /
    // mobile Firefox. Run BEFORE any async navigator.clipboard attempt so
    // the user-gesture context is preserved.
    let ok = false;
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;opacity:0;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      ta.setSelectionRange(0, text.length);
      ok = document.execCommand && document.execCommand('copy');
      ta.remove();
    } catch (_) {}
    // As a backup, also try the modern clipboard API (won't hurt if the
    // legacy path already copied).
    if (!ok && navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); ok = true; } catch (_) {}
    }
    if (uiModule && uiModule.showToast) {
      uiModule.showToast(ok ? 'Copied' : 'Copy failed');
    }
    const _orig = btn.innerHTML;
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
    btn.classList.add('copied');
    setTimeout(() => { btn.innerHTML = _orig; btn.classList.remove('copied'); }, 1500);
  });
  panel.prepend(btn);
}

/**
 * Add a collapse/close button to the panel.
 * Disabled \u2014 the run-output panel is now closed via the unified Code\u2194Run
 * toggle in the editor footer, so a separate X was redundant + cluttered.
 */
function addCloseBtn(_panel) { /* no-op */ }

/**
 * Lazy-load Pyodide from CDN
 */
function loadPyodide() {
  if (pyodideInstance) return Promise.resolve(pyodideInstance);
  if (pyodideLoading) {
    return new Promise((resolve, reject) => {
      pyodideQueue.push({ resolve, reject });
    });
  }
  pyodideLoading = true;

  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/pyodide/v0.27.5/full/pyodide.js';
    script.onload = () => {
      window.loadPyodide({ indexURL: 'https://cdn.jsdelivr.net/pyodide/v0.27.5/full/' })
        .then(py => {
          pyodideInstance = py;
          pyodideLoading = false;
          pyodideQueue.forEach(q => q.resolve(py));
          pyodideQueue.length = 0;
          resolve(py);
        })
        .catch(err => {
          pyodideLoading = false;
          pyodideQueue.forEach(q => q.reject(err));
          pyodideQueue.length = 0;
          reject(err);
        });
    };
    script.onerror = () => {
      pyodideLoading = false;
      const err = new Error('Failed to load Pyodide');
      pyodideQueue.forEach(q => q.reject(err));
      pyodideQueue.length = 0;
      reject(err);
    };
    document.head.appendChild(script);
  });
}

/**
 * Run Python code via Pyodide
 */
/**
 * True when a snippet plots, and therefore needs matplotlib loaded and its
 * figures captured. Deliberately a text test: the alternative is loading a
 * ~15 MB package for every snippet that only prints a number.
 */
export function codeUsesMatplotlib(code) {
  return /\b(?:matplotlib|pyplot|\bplt\.)/.test(String(code || ''));
}

export async function runPython(code, panel) {
  const wantsPlot = codeUsesMatplotlib(code);
  showLoading(panel, 'Loading Python runtime (first time ~10 MB)...');

  let py;
  try {
    py = await loadPyodide();
  } catch (e) {
    showOutput(panel, 'Failed to load Python runtime: ' + e.message, true);
    addCloseBtn(panel);
    return;
  }

  if (wantsPlot) {
    showLoading(panel, 'Loading plotting library (first time ~15 MB)...');
    try {
      // MPLBACKEND must be set before the first matplotlib import: Pyodide's
      // default backend draws to a canvas it owns, and savefig on it produces
      // nothing. Agg renders to a buffer, which is what we want to capture.
      await py.runPythonAsync("import os; os.environ['MPLBACKEND'] = 'AGG'");
      await py.loadPackage(['matplotlib', 'numpy']);
    } catch (e) {
      showOutput(panel, 'Could not load the plotting library: ' + e.message, true);
      addCloseBtn(panel);
      return;
    }
  }

  showLoading(panel, 'Running...');

  // Figures are collected after the snippet returns rather than by hooking
  // plt.show(), so a snippet works whether or not it remembers to call it —
  // models write both. Every figure still open is captured, in creation order.
  const capture = wantsPlot ? `
    try:
        import base64 as _b64, io as _io
        import matplotlib.pyplot as _plt
        for _n in _plt.get_fignums():
            _buf = _io.BytesIO()
            _plt.figure(_n).savefig(_buf, format='png', dpi=110, bbox_inches='tight')
            _images.append(_b64.b64encode(_buf.getvalue()).decode())
        _plt.close('all')
    except Exception as _pe:
        _stderr.write('plot capture failed: ' + str(_pe))
` : '';

  const wrapper = `
import sys, io
_stdout = io.StringIO()
_stderr = io.StringIO()
_images = []
sys.stdout = _stdout
sys.stderr = _stderr
try:
    exec(${JSON.stringify(code)})
${capture}
except Exception as _e:
    _stderr.write(str(_e))
finally:
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
(_stdout.getvalue(), _stderr.getvalue(), _images)
`;

  try {
    // Rendering a figure costs far more than printing, and the first run also
    // unpacks the package, so a plot gets a longer leash than a bare snippet.
    const limitMs = wantsPlot ? 45000 : 10000;
    const result = await Promise.race([
      py.runPythonAsync(wrapper),
      new Promise((_, reject) => setTimeout(
        () => reject(new Error(`Execution timed out (${limitMs / 1000} s)`)), limitMs))
    ]);

    const asJs = result.toJs ? result.toJs() : result;
    const stdout = asJs[0] || '';
    const stderr = asJs[1] || '';
    const images = Array.from(asJs[2] || []);
    if (result.destroy) result.destroy();

    panel.innerHTML = '';
    images.forEach((b64) => {
      const img = document.createElement('img');
      img.className = 'code-runner-plot';
      img.alt = 'Plot generated by the snippet above';
      img.src = 'data:image/png;base64,' + b64;
      panel.appendChild(img);
    });
    if (stderr) {
      showOutput(panel, stderr, true);
    } else if (stdout) {
      showOutput(panel, stdout, false);
    } else if (!images.length) {
      showOutput(panel, '(no output)', false);
    }
  } catch (e) {
    showOutput(panel, e.message, true);
  }
  addCloseBtn(panel);
}

/**
 * Run JavaScript code in a sandboxed iframe
 */
export function runJavaScript(code, panel) {
  showLoading(panel, 'Running...');

  const iframe = document.createElement('iframe');
  iframe.style.display = 'none';
  iframe.sandbox = 'allow-scripts';
  document.body.appendChild(iframe);

  let settled = false;
  const cleanup = () => {
    if (iframe.parentNode) iframe.remove();
  };

  const failsafe = setTimeout(() => {
    if (!settled) {
      settled = true;
      showOutput(panel, 'Execution timed out (10 s)', true);
      addCloseBtn(panel);
      cleanup();
    }
  }, 15000);

  const onMessage = (e) => {
    if (e.source !== iframe.contentWindow) return;
    if (settled) return;
    settled = true;
    clearTimeout(failsafe);
    window.removeEventListener('message', onMessage);

    const data = e.data;
    panel.innerHTML = '';
    if (data.error) {
      showOutput(panel, data.error, true);
    } else if (data.logs && data.logs.length > 0) {
      showOutput(panel, data.logs.join('\n'), false);
    } else {
      showOutput(panel, '(no output)', false);
    }
    addCloseBtn(panel);
    cleanup();
  };

  window.addEventListener('message', onMessage);

  const wrappedCode = `
<!DOCTYPE html><html><body><script>
var _logs = [];
var _origLog = console.log;
console.log = function() { _logs.push([].map.call(arguments, function(a) { try { return typeof a === 'object' ? JSON.stringify(a) : String(a); } catch(e) { return String(a); } }).join(' ')); };
console.warn = function() { _logs.push('[warn] ' + [].map.call(arguments, String).join(' ')); };
console.error = function() { _logs.push('[error] ' + [].map.call(arguments, String).join(' ')); };
try {
  var _timer = setTimeout(function() { parent.postMessage({error:'Execution timed out (10 s)'},'*'); }, 10000);
  ${code.replace(/<\/script>/gi, '<\\/script>')}
  clearTimeout(_timer);
  parent.postMessage({logs: _logs}, '*');
} catch(e) {
  parent.postMessage({error: e.toString()}, '*');
}
<\/script></body></html>`;

  iframe.srcdoc = wrappedCode;
}

/**
 * Run code server-side via POST /api/shell/exec
 */
export async function runServer(code, panel, lang) {
  showLoading(panel, 'Running on server...');
  // Base64-encode the script so newlines survive the shell quoting intact.
  // JSON.stringify turns \n into literal \\n which python3 -c sees as backslash-n;
  // base64 avoids every quoting/escaping pitfall.
  const b64 = btoa(unescape(encodeURIComponent(code)));
  var command;
  if (lang === 'python' || lang === 'py') {
    command = `python3 -c "import base64; exec(base64.b64decode('${b64}').decode('utf-8'))"`;
  } else {
    command = `python3 -c "import base64, subprocess, sys; sys.exit(subprocess.run(['bash','-c',base64.b64decode('${b64}').decode('utf-8')]).returncode)"`;
  }
  try {
    var res = await fetch('/api/shell/exec', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: command }),
    });
    var data = await res.json();
    panel.innerHTML = '';
    if (data.stderr && data.stderr.trim()) {
      showOutput(panel, data.stderr, true);
      if (data.stdout && data.stdout.trim()) {
        var stdoutEl = document.createElement('pre');
        stdoutEl.className = 'code-runner-pre';
        stdoutEl.textContent = data.stdout;
        panel.appendChild(stdoutEl);
      }
    } else if (data.stdout && data.stdout.trim()) {
      showOutput(panel, data.stdout, false);
    } else {
      showOutput(panel, '(no output)' + (data.exit_code ? ' — exit code ' + data.exit_code : ''), !data.exit_code ? false : true);
    }
    if (data.exit_code && data.exit_code !== 0) {
      var exitEl = document.createElement('div');
      exitEl.style.cssText = 'font-size:0.75rem;opacity:0.5;padding:2px 8px;';
      exitEl.textContent = 'Exit code: ' + data.exit_code;
      panel.appendChild(exitEl);
    }
  } catch (e) {
    showOutput(panel, 'Execution failed: ' + e.message, true);
  }
  addCloseBtn(panel);
}

/**
 * Run HTML code in its own popup window
 */
export function runHTML(code, panel) {
  panel.innerHTML = '';

  const win = window.open('', '_blank', 'width=800,height=600,menubar=no,toolbar=no,location=no,status=no');
  if (!win) {
    showOutput(panel, 'Popup blocked — please allow popups for this site.', true);
    addCloseBtn(panel);
    return;
  }
  try { win.opener = null; } catch (_) {}
  win.document.open();
  win.document.write(code);
  win.document.close();

  showOutput(panel, 'Opened in new window', false);
  addCloseBtn(panel);
}

/**
 * Main entry point — called when a Run button is clicked
 */
export function run(btn) {
  const code = btn.getAttribute('data-code');
  const lang = (btn.getAttribute('data-lang') || '').toLowerCase();
  if (!code) return;

  const pre = btn.closest('pre');
  if (!pre) return;

  const panel = getOrCreatePanel(pre);

  if (lang === 'bash' || lang === 'sh' || lang === 'shell' || lang === 'zsh') {
    runServer(code, panel, 'bash');
  } else if (lang === 'python' || lang === 'py') {
    runServer(code, panel, 'python');
  } else if (lang === 'javascript' || lang === 'js') {
    runJavaScript(code, panel);
  } else if (lang === 'html') {
    runHTML(code, panel);
  }
}

/**
 * Draw the plots an AI wrote, instead of showing their source.
 *
 * Mermaid is right for structure, but it cannot plot y = 2*sqrt(x). Asked for a
 * production function with only diagram syntax available, a model draws ASCII
 * art or hands over TikZ to paste into Overleaf. matplotlib is the real answer:
 * the runtime is already here, and it draws the actual curve.
 *
 * Execution is in-browser and sandboxed. It deliberately does NOT use
 * runServer, which shells out to /api/shell/exec: this code is written by a
 * model reading the user's own uploaded documents, so treating it as trusted
 * input to a shell would turn a poisoned file into remote execution. Pyodide
 * has no filesystem and no network into the host.
 *
 * Only plotting snippets run unattended. Anything else stays inert with its
 * source visible, because auto-running arbitrary model code is a different
 * decision from rendering a figure it asked for.
 *
 * Shared by the Study panel and the Tutor tab -- they render the same markdown
 * through different modules, and a plot must work in both.
 */
export function renderPythonPlots(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  const blocks = root.querySelectorAll('pre > code.language-python, pre > code.language-py');
  blocks.forEach((codeEl) => {
    const pre = codeEl.parentElement;
    if (!pre || pre.dataset.plotRunDone) return;
    const src = codeEl.textContent || '';
    if (!codeUsesMatplotlib(src)) return;
    pre.dataset.plotRunDone = '1';

    const panel = document.createElement('div');
    panel.className = 'code-runner-output study-plot-output';
    pre.parentNode.insertBefore(panel, pre.nextSibling);

    // The figure is the explanation; the code is the footnote. Keep it
    // reachable — someone checking the maths should be able to read it.
    pre.hidden = true;
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'study-btn small study-plot-toggle';
    toggle.textContent = 'Show plot code';
    toggle.addEventListener('click', () => {
      pre.hidden = !pre.hidden;
      toggle.textContent = pre.hidden ? 'Show plot code' : 'Hide plot code';
    });
    panel.parentNode.insertBefore(toggle, panel.nextSibling);

    runPython(src, panel).catch(() => { pre.hidden = false; });
  });
}

const codeRunnerModule = {
  run, runPython, runJavaScript, runHTML, runServer, codeUsesMatplotlib,
  renderPythonPlots,
};
export default codeRunnerModule;
