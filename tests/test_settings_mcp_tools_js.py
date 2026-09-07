"""B08 — MCP tool toggles must not display permissions the server refused.

``showMcpForm`` sent the enable/disable PATCH and the tool-list PATCH without
checking ``res.ok``, then updated the enabled count regardless. A rejected
request looked exactly like a saved one.

Every checkbox change also fired its own independent PATCH carrying the whole
disabled list, so two rapid toggles could complete in either order and leave
the server holding the earlier choice.

Acceptance: a failed PATCH never appears saved, network errors are handled, and
rapid changes converge to the last intended complete set on both sides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

SETTINGS_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "settings.js"

FNS = ("sendMcpToolUpdate", "queueMcpToolUpdate", "sendMcpServerEnabled")

PRELUDE = """
const calls = [];
const _mcpToolSaves = new Map();

function makeFetch(responses) {
  let i = 0;
  return async (url, opts) => {
    const spec = responses[Math.min(i, responses.length - 1)];
    i += 1;
    let payload = null;
    try { payload = opts && opts.body ? JSON.parse(opts.body) : null; } catch { payload = 'form'; }
    calls.push({ url, method: (opts && opts.method) || 'GET', payload });
    if (spec.delay) await new Promise(r => setTimeout(r, spec.delay));
    if (spec.throw) throw new Error(spec.throw);
    return {
      ok: spec.status >= 200 && spec.status < 300,
      status: spec.status,
      json: async () => spec.body ?? {},
    };
  };
}
"""


def _run(epilogue):
    return run_js(PRELUDE, *FNS, epilogue=epilogue, source_path=SETTINGS_JS)


# --------------------------------------------------------------------------
# A refused PATCH is a failure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_a_rejected_tool_update_is_reported_as_failure(status):
    out = _run(f"""
const fetchImpl = makeFetch([{{ status: {status}, body: {{ detail: 'refused' }} }}]);
const r = await sendMcpToolUpdate('srv1', ['a'], fetchImpl);
console.log(JSON.stringify({{ ok: r.ok, status: r.status }}));
""")
    assert out["ok"] is False, f"HTTP {status} was treated as saved"
    assert out["status"] == status


def test_a_successful_tool_update_reports_success_and_sends_the_full_list():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200 }]);
const r = await sendMcpToolUpdate('srv1', ['a', 'b'], fetchImpl);
console.log(JSON.stringify({ ok: r.ok, sent: calls[0].payload, method: calls[0].method }));
""")
    assert out["ok"] is True
    assert out["sent"] == {"disabled": ["a", "b"]}
    assert out["method"] == "PATCH"


def test_a_network_error_is_reported_as_failure():
    out = _run("""
const fetchImpl = makeFetch([{ throw: 'connection reset' }]);
const r = await sendMcpToolUpdate('srv1', [], fetchImpl);
console.log(JSON.stringify({ ok: r.ok, detail: String(r.detail || '') }));
""")
    assert out["ok"] is False
    assert "connection reset" in out["detail"]


@pytest.mark.parametrize("status", [403, 500])
def test_a_rejected_enable_toggle_is_reported_as_failure(status):
    out = _run(f"""
const fetchImpl = makeFetch([{{ status: {status} }}]);
const r = await sendMcpServerEnabled('srv1', false, fetchImpl);
console.log(JSON.stringify({{ ok: r.ok, status: r.status }}));
""")
    assert out["ok"] is False, f"a {status} on enable/disable looked applied"


def test_a_successful_enable_toggle_reports_success():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200 }]);
const r = await sendMcpServerEnabled('srv1', true, fetchImpl);
console.log(JSON.stringify({ ok: r.ok, method: calls[0].method }));
""")
    assert out["ok"] is True
    assert out["method"] == "PATCH"


# --------------------------------------------------------------------------
# Rapid changes converge on the last intended set
# --------------------------------------------------------------------------

def test_rapid_toggles_serialise_and_the_last_set_wins():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200, delay: 40 }, { status: 200 }]);
const a = queueMcpToolUpdate('srv1', ['a'], fetchImpl);
const b = queueMcpToolUpdate('srv1', ['a', 'b'], fetchImpl);
await Promise.all([a, b]);
console.log(JSON.stringify({
  sent: calls.map(c => c.payload.disabled),
  last: calls[calls.length - 1].payload.disabled,
}));
""")
    assert out["last"] == ["a", "b"], (
        f"the server was left holding an earlier set: {out['sent']}"
    )


def test_a_burst_coalesces_to_the_final_set():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200, delay: 30 }]);
const sets = [['a'], ['a','b'], ['a','b','c'], []];
await Promise.all(sets.map(s => queueMcpToolUpdate('srv1', s, fetchImpl)));
console.log(JSON.stringify({
  requests: calls.length,
  last: calls[calls.length - 1].payload.disabled,
}));
""")
    assert out["last"] == [], "the final intended set was not the one persisted"
    assert out["requests"] < 4, (
        f"4 rapid toggles sent {out['requests']} requests; they should coalesce"
    )


def test_two_servers_do_not_block_each_other():
    """Coalescing is per server; one slow server must not stall another."""
    out = _run("""
const fetchImpl = makeFetch([{ status: 200 }, { status: 200 }]);
await Promise.all([
  queueMcpToolUpdate('srv1', ['a'], fetchImpl),
  queueMcpToolUpdate('srv2', ['b'], fetchImpl),
]);
console.log(JSON.stringify({ urls: calls.map(c => c.url) }));
""")
    assert len(out["urls"]) == 2
    assert any("srv1" in u for u in out["urls"])
    assert any("srv2" in u for u in out["urls"])


def test_a_failed_update_is_surfaced_by_the_queue():
    out = _run("""
const fetchImpl = makeFetch([{ status: 500, body: { detail: 'server down' } }]);
const r = await queueMcpToolUpdate('srv1', ['a'], fetchImpl);
console.log(JSON.stringify({ ok: r.ok, detail: String(r.detail || '') }));
""")
    assert out["ok"] is False
    assert "server down" in out["detail"]
