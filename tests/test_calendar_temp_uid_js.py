"""S7 / #64: the Math.random temp id in calendar.js is a client-local,
transient UI key — not a credential and never transmitted.

Drives the real ``_createEvent`` extracted from static/js/calendar.js under
Node with a scripted fetch. The disposition (false positive for
insecure-randomness) depends on exactly what this pins: the temp id never
leaves the client, grants no access, is never persisted, and is replaced by
the server-issued uid once the create request resolves.
"""

import json
import shutil
from pathlib import Path

import pytest

from tests._study_js_harness import run_js

_CALENDAR_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "calendar.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node binary not on PATH"
)


@needs_node
def test_create_event_temp_uid_stays_client_local_and_is_replaced():
    prelude = """
const _calendars = [{ name: 'Main', href: 'main-href', color: '#112233' }];
const _allEvents = {};
let posted = null;
let fetchUrl = null;
let fetchOpts = null;
globalThis.fetch = async (url, opts) => {
  fetchUrl = url;
  fetchOpts = opts;
  posted = JSON.parse(opts.body);
  return { ok: true, json: async () => ({ uid: 'server-uid-42' }) };
};
const API_BASE = '';
const _saveCache = () => {};
const _open = false;
const _render = () => {};
globalThis.window = { uiModule: { showError: () => {} } };
async function drive() {
  const res = await _createEvent({ summary: 'S7-event', dtstart: '2026-05-01T10:00:00' });
  // allow the fetch .then() chain to run (server uid swap happens async)
  await new Promise(r => setTimeout(r, 0));
  console.log(JSON.stringify({
    resultUid: res.uid,
    postedBody: posted,
    requestUrl: fetchUrl,
    requestHeaders: fetchOpts.headers,
    keys: Object.keys(_allEvents),
    tempKeysRemaining: Object.keys(_allEvents).filter(k => k.startsWith('temp-')).length,
  }));
}
await drive();
"""
    out = run_js(
        prelude,
        "_createEvent",
        "_optimisticEvent",
        source_path=_CALENDAR_JS,
    )

    # Shape: the temp id is timestamp + Math.random, purely local.
    assert out["resultUid"].startswith("temp-")

    # The create request carries the event payload only: no uid, no temp id.
    serialized_request = json.dumps({
        "body": out["postedBody"],
        "url": out["requestUrl"],
        "headers": out["requestHeaders"],
    })
    assert "uid" not in out["postedBody"]
    assert "temp-" not in serialized_request
    assert out["postedBody"]["summary"] == "S7-event"

    # The client key namespace holds only the server-issued uid afterwards.
    assert out["keys"] == ["server-uid-42"]
    assert out["tempKeysRemaining"] == 0