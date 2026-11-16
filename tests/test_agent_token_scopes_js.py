"""Agent tokens must only gain the scopes the user switched on.

Settings → Integrations → Claude/Codex/Omnigent agent → Create token makes a
token with ``chat`` only, but the Configure toggles for the new token were
rendered with every tool scope pre-checked "as a preview". Switching on
Council and clicking Save then PATCHed all of them, so a token meant for
chat + council was saved with 15 scopes, including ``email:send`` and
``calendar:write``.

These tests run the real ``showAgentForm`` closure from settings.js under Node
against a minimal DOM, then drive Create, the toggles and Save.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._study_js_harness import extract_const, needs_node, run_js

pytestmark = needs_node

SETTINGS_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "settings.js"

PRELUDE = """
const calls = [];
let listedTokens = [];
let formInputs = [];
let inlineInputs = [];
let elements = new Map();

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
const window = {
  location: { origin: 'http://localhost' },
  spinnerModule: { createWhirlpool: () => ({ element: { style: {} }, destroy() {} }) },
};
const document = { createTextNode: () => ({}) };
async function renderList() {}
function notifyIntegrationsChanged() {}

function scopeInputs(html) {
  return [...html.matchAll(/<input\\b[^>]*>/g)].map(m => m[0])
    .filter(tag => /class="[^"]*\\buf-codex-scope\\b/.test(tag))
    .map(tag => ({
      dataset: {
        tokenId: tag.match(/data-token-id="([^"]*)"/)[1],
        scope: tag.match(/data-scope="([^"]*)"/)[1],
      },
      checked: /\\schecked\\b/.test(tag),
      addEventListener(type, fn) { this['on' + type] = fn; },
    }));
}

function fakeElement(id) {
  let html = '';
  return {
    id, style: {}, dataset: {}, listeners: {},
    get innerHTML() { return html; },
    set innerHTML(v) {
      html = v;
      if (id === 'uf-codex-inline-scopes') inlineInputs = scopeInputs(v);
    },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    appendChild() {}, setAttribute() {},
  };
}

const el = id => {
  if (!elements.has(id)) elements.set(id, fakeElement(id));
  return elements.get(id);
};

const formEl = {
  dataset: {}, style: {},
  set innerHTML(v) { elements = new Map(); formInputs = scopeInputs(v); inlineInputs = []; },
  querySelectorAll(sel) {
    const pool = sel.startsWith('#uf-codex-inline-scopes') ? inlineInputs : formInputs.concat(inlineInputs);
    return sel.endsWith(':checked') ? pool.filter(i => i.checked) : pool;
  },
};

// Mirrors routes/api_token_routes.py: the token gets exactly the scopes sent.
async function fetch(url, opts = {}) {
  const method = opts.method || 'GET';
  const body = opts.body instanceof FormData ? Object.fromEntries(opts.body)
    : opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, method, body });
  let payload = {};
  if (method === 'GET') payload = listedTokens;
  if (method === 'POST') payload = { id: 'new1', name: body.name, token: 'ody_new', token_prefix: 'ody_new', scopes: body.scopes.split(',') };
  if (method === 'PATCH') payload = { id: url.split('/').pop(), scopes: body.scopes };
  return { ok: true, json: async () => payload };
}

async function until(done) {
  for (let i = 0; i < 100 && !done(); i++) await new Promise(r => setTimeout(r, 0));
}

const checkedScopes = inputs => inputs.filter(i => i.checked).map(i => i.dataset.scope);
"""


def _show_agent_form_source() -> str:
    src = SETTINGS_JS.read_text(encoding="utf-8")
    # Nested inside the integrations initialiser: opens and closes at 2 spaces.
    match = re.search(
        r"^  async function showAgentForm\(.*?^  \}$", src, re.DOTALL | re.MULTILINE
    )
    assert match, "showAgentForm not found in settings.js"
    return match.group(0)


def _run(epilogue: str) -> dict:
    src = SETTINGS_JS.read_text(encoding="utf-8")
    prelude = "\n\n".join(
        [PRELUDE, extract_const("AGENT_CONFIGS", src), _show_agent_form_source()]
    )
    return run_js(prelude, epilogue=epilogue, source_path=SETTINGS_JS)


@pytest.mark.parametrize("kind", ["claude", "codex", "omnigent"])
@pytest.mark.parametrize("switched_on", [[], ["council"], ["todos:read", "council"]])
def test_new_agent_token_saves_only_the_scopes_switched_on(kind, switched_on):
    out = _run(f"""
await showAgentForm({kind!r});
el('uf-codex-create-btn').listeners.click[0]();
await until(() => el('uf-codex-save').style.display === 'inline-flex');
const before = checkedScopes(inlineInputs);
for (const scope of {switched_on!r}) inlineInputs.find(i => i.dataset.scope === scope).checked = true;
await el('uf-codex-save').listeners.click[0]();
const post = calls.find(c => c.method === 'POST');
const patch = calls.find(c => c.method === 'PATCH');
console.log(JSON.stringify({{
  created: post.body.scopes, toggles: inlineInputs.length, before,
  saved: patch.body.scopes, patchUrl: patch.url,
}}));
""")
    assert out["created"] == "chat"
    assert out["toggles"] > 0
    assert out["before"] == [], "new token toggles must start at chat only"
    assert out["patchUrl"] == "/api/tokens/new1"
    assert sorted(out["saved"]) == sorted(["chat", *switched_on])


def test_existing_agent_token_shows_and_patches_its_real_scopes():
    out = _run("""
listedTokens = [{ id: 't1', name: 'Claude Agent', token_prefix: 'ody_abcd', scopes: ['chat', 'todos:read', 'council'] }];
await showAgentForm('claude', 't1');
const shown = checkedScopes(formInputs);
const council = formInputs.find(i => i.dataset.scope === 'council');
council.checked = false;
await council.onchange();
const patch = calls.find(c => c.method === 'PATCH');
console.log(JSON.stringify({ shown, saved: patch.body.scopes, patchUrl: patch.url }));
""")
    assert sorted(out["shown"]) == ["council", "todos:read"]
    assert out["patchUrl"] == "/api/tokens/t1"
    assert sorted(out["saved"]) == ["chat", "todos:read"]
