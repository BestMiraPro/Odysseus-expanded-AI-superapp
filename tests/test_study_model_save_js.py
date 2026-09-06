"""B04 — Study must not claim a model was saved when the server refused.

``initModelSelector``'s save closure awaited the settings POST and then showed
"Study model saved" unconditionally. fetch resolves normally for 403, 422 and
500, so every rejection was reported as a success. The initial GETs parsed JSON
without checking status too, so an error body could be read as settings.

Rapid changes were also unserialised: two in-flight POSTs could complete in
either order, leaving the server holding the older selection.

Acceptance from the handoff: 403/422/500 and network failures never show
success; the user gets an actionable error; reverse completion order of two
changes leaves the latest intended selection persisted.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node

PRELUDE = """
const API = '';
const calls = [];
let _modelSaveSeq = 0;
let _modelSavePending = null;
let _modelSaveRunning = false;
let _studyModelConfirmed = { endpointId: '', model: '' };

// Controllable fetch: each entry is {status, ok, body} or {throw: 'msg'}.
function makeFetch(responses, { holdUntil = null } = {}) {
  let i = 0;
  return async (url, opts) => {
    const spec = responses[Math.min(i, responses.length - 1)];
    i += 1;
    const payload = JSON.parse((opts && opts.body) || '{}');
    calls.push({ url, payload, order: calls.length });
    if (spec.delay) await new Promise(r => setTimeout(r, spec.delay));
    if (spec.throw) throw new Error(spec.throw);
    return {
      ok: spec.status >= 200 && spec.status < 300,
      status: spec.status,
      json: async () => spec.body ?? {},
      text: async () => JSON.stringify(spec.body ?? {}),
    };
  };
}
"""


def _run(epilogue, *fns):
    return run_js(PRELUDE, *fns, epilogue=epilogue)


# --------------------------------------------------------------------------
# A rejected save is a failure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 403, 422, 500, 503])
def test_a_rejected_save_is_reported_as_failure(status):
    out = _run(f"""
const fetchImpl = makeFetch([{{ status: {status}, body: {{ detail: 'nope' }} }}]);
const r = await sendStudyModelSave({{ endpointId: 'ep1', model: 'm1' }}, fetchImpl);
console.log(JSON.stringify({{ ok: r.ok, status: r.status }}));
""", "sendStudyModelSave")
    assert out["ok"] is False, f"HTTP {status} was treated as a successful save"
    assert out["status"] == status


def test_a_successful_save_is_reported_as_success():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200, body: {} }]);
const r = await sendStudyModelSave({ endpointId: 'ep1', model: 'm1' }, fetchImpl);
console.log(JSON.stringify({ ok: r.ok, status: r.status, sent: calls[0].payload }));
""", "sendStudyModelSave")
    assert out["ok"] is True
    assert out["sent"] == {"study_endpoint_id": "ep1", "study_model": "m1"}


def test_a_network_failure_is_reported_as_failure():
    out = _run("""
const fetchImpl = makeFetch([{ throw: 'offline' }]);
const r = await sendStudyModelSave({ endpointId: 'ep1', model: 'm1' }, fetchImpl);
console.log(JSON.stringify({ ok: r.ok, detail: String(r.detail || '') }));
""", "sendStudyModelSave")
    assert out["ok"] is False
    assert "offline" in out["detail"]


def test_a_rejection_carries_actionable_detail():
    out = _run("""
const fetchImpl = makeFetch([{ status: 403, body: { detail: 'admin only' } }]);
const r = await sendStudyModelSave({ endpointId: 'ep1', model: 'm1' }, fetchImpl);
console.log(JSON.stringify({ detail: String(r.detail || '') }));
""", "sendStudyModelSave")
    assert "admin only" in out["detail"]


# --------------------------------------------------------------------------
# Serialisation: the latest intended selection must win
# --------------------------------------------------------------------------

def test_rapid_changes_are_serialised_in_order():
    """Two POSTs must not be in flight at once; the later value is written last."""
    out = _run("""
const fetchImpl = makeFetch([
  { status: 200, delay: 40 },
  { status: 200, delay: 0 },
]);
const a = queueStudyModelSave({ endpointId: 'ep1', model: 'first' }, fetchImpl);
const b = queueStudyModelSave({ endpointId: 'ep1', model: 'second' }, fetchImpl);
await Promise.all([a, b]);
console.log(JSON.stringify({
  sent: calls.map(c => c.payload.study_model),
  confirmed: _studyModelConfirmed,
}));
""", "sendStudyModelSave", "queueStudyModelSave")
    assert out["sent"][-1] == "second", (
        f"the last request sent was not the latest selection: {out['sent']}"
    )
    assert out["confirmed"]["model"] == "second"


def test_a_burst_coalesces_to_the_final_selection():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200, delay: 30 }]);
const ps = ['a', 'b', 'c', 'd'].map(m =>
  queueStudyModelSave({ endpointId: 'ep1', model: m }, fetchImpl));
await Promise.all(ps);
console.log(JSON.stringify({
  requests: calls.length,
  last: calls[calls.length - 1].payload.study_model,
  confirmed: _studyModelConfirmed.model,
}));
""", "sendStudyModelSave", "queueStudyModelSave")
    assert out["last"] == "d", "the final selection was not the one persisted"
    assert out["confirmed"] == "d"
    assert out["requests"] < 4, (
        f"a burst of 4 changes sent {out['requests']} requests; it should coalesce"
    )


def test_a_failed_save_does_not_become_the_confirmed_value():
    out = _run("""
const fetchImpl = makeFetch([{ status: 500 }]);
const r = await queueStudyModelSave({ endpointId: 'ep9', model: 'bad' }, fetchImpl);
console.log(JSON.stringify({ ok: r.ok, confirmed: _studyModelConfirmed }));
""", "sendStudyModelSave", "queueStudyModelSave")
    assert out["ok"] is False
    assert out["confirmed"]["model"] == "", "a rejected save was recorded as confirmed"


# --------------------------------------------------------------------------
# The initial GETs must check status too
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 500])
def test_reading_settings_rejects_an_error_response(status):
    out = _run(f"""
const fetchImpl = makeFetch([{{ status: {status}, body: {{ detail: 'no' }} }}]);
let threw = false;
try {{ await fetchStudyJson('/api/auth/settings', fetchImpl); }}
catch (e) {{ threw = true; }}
console.log(JSON.stringify({{ threw }}));
""", "fetchStudyJson")
    assert out["threw"] is True, f"HTTP {status} body was parsed as settings"


def test_reading_settings_returns_the_body_on_success():
    out = _run("""
const fetchImpl = makeFetch([{ status: 200, body: { study_model: 'm7' } }]);
const data = await fetchStudyJson('/api/auth/settings', fetchImpl);
console.log(JSON.stringify({ model: data.study_model }));
""", "fetchStudyJson")
    assert out["model"] == "m7"
