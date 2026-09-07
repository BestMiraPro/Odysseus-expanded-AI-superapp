"""B06 + B07 — one contract for durable Study submissions.

B06: ``postDurably`` minted a fresh idempotency key on every call, so a manual
retry after a lost response was a *different* key. Server-side idempotency
deduplicates identical keys and therefore could not deduplicate those two — the
grade could be recorded twice, altering history and scheduling.

B07: the queue lived under one origin-wide key with no owner, so an item
queued by account A could be transmitted under account B. ``jfetch`` discarded
the HTTP status, so a permanent 400/404/409 retried forever. Storage failures
were swallowed, meaning an "allegedly durable" answer could vanish on reload.

The two are one design: a logical submission has a stable identity and an
immutable payload, and the queue that carries it is account-scoped and
classifies failures.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import extract_const, needs_node, run_js

pytestmark = needs_node

QUEUE_FNS = (
    "queueStorageKey", "loadRetryQueue", "saveRetryQueue", "makeIdempotencyKey",
    "retryDelayMs", "classifyQueueError", "beginDurablePost", "removeRetryItem",
    "updateRetryItem", "recordQueueFailure", "postDurably", "flushRetryQueue",
)
# Real constants from study.js, so the tests track the shipped values.
QUEUE_CONSTS = ("RETRY_QUEUE_PREFIX", "SINGLE_USER_QUEUE_OWNER",
                "PERMANENT_QUEUE_STATUSES")

PRELUDE = """
const API = '';
const sent = [];
let _storageFails = false;

// Module-scope consts shadow the real globals; Node 24 makes navigator and
// crypto read-only, so they cannot be assigned on globalThis.
const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { if (_storageFails) throw new Error('quota'); store.set(k, v); },
  removeItem: (k) => { store.delete(k); },
};
const navigator = { onLine: true };
let _uuid = 0;
const crypto = { randomUUID: () => 'uuid-' + (++_uuid) };
const window = { addEventListener() {} };

let _queueOwner = 'alice';
let _queuePaused = false;
let _queueStorageFailed = false;
let _retryTimer = null;
let _retryFlushing = false;

// Scripted responses keyed by call index.
let RESPONSES = [];
function setResponses(list) { RESPONSES = list.slice(); }

async function jpost(path, body) {
  const spec = RESPONSES.length ? RESPONSES.shift() : { status: 200 };
  sent.push({ path, body, owner: _queueOwner });
  if (spec.status >= 200 && spec.status < 300) return { ok: true };
  const err = new Error(spec.detail || ('Request failed (' + spec.status + ')'));
  err.status = spec.status;
  if (spec.retryAfter) err.retryAfter = spec.retryAfter;
  throw err;
}
function scheduleRetryFlush() { /* timers are driven explicitly in tests */ }
"""


def _run(epilogue):
    consts = "\n".join(extract_const(c) for c in QUEUE_CONSTS)
    return run_js(PRELUDE + consts + "\n", *QUEUE_FNS, epilogue=epilogue)


# --------------------------------------------------------------------------
# B06 — one logical submission, one idempotency key
# --------------------------------------------------------------------------

def test_a_manual_retry_reuses_the_same_idempotency_key():
    """The reported trigger: the server commits, the response is lost, the
    student clicks Check again."""
    out = _run("""
setResponses([{ status: 500 }, { status: 200 }]);
try { await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1'); } catch {}
try { await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1'); } catch {}
console.log(JSON.stringify({
  keys: sent.map(s => s.body.idempotency_key),
  count: sent.length,
}));
""")
    assert len(out["keys"]) == 2
    assert out["keys"][0] == out["keys"][1], (
        "the manual retry used a different key, so the server cannot deduplicate it"
    )


def test_a_retry_resends_the_original_payload_snapshot():
    """A retry must replay the attempt that was made, not a newer edit."""
    out = _run("""
setResponses([{ status: 500 }, { status: 200 }]);
try { await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1'); } catch {}
try { await postDurably('attempt', '/a', { choice: 9 }, 'att', 'q1', 'sub-1'); } catch {}
console.log(JSON.stringify({ choices: sent.map(s => s.body.choice) }));
""")
    assert out["choices"] == [1, 1], (
        f"the retry sent a mutated payload: {out['choices']}"
    )


def test_a_deliberate_new_attempt_gets_a_new_key():
    out = _run("""
setResponses([{ status: 200 }, { status: 200 }]);
await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1');
await postDurably('attempt', '/a', { choice: 2 }, 'att', 'q1', 'sub-2');
console.log(JSON.stringify({ keys: sent.map(s => s.body.idempotency_key) }));
""")
    assert out["keys"][0] != out["keys"][1], "a genuine new attempt reused an old key"


def test_two_rapid_clicks_create_one_queued_submission():
    out = _run("""
setResponses([{ status: 500 }, { status: 500 }]);
const a = postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1').catch(() => {});
const b = postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1').catch(() => {});
await Promise.all([a, b]);
console.log(JSON.stringify({ queued: loadRetryQueue().length }));
""")
    assert out["queued"] == 1, f"a double click queued {out['queued']} submissions"


def test_a_successful_send_leaves_nothing_queued():
    out = _run("""
setResponses([{ status: 200 }]);
await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1');
console.log(JSON.stringify({ queued: loadRetryQueue().length }));
""")
    assert out["queued"] == 0


# --------------------------------------------------------------------------
# B07 — account scoping
# --------------------------------------------------------------------------

def test_the_queue_is_scoped_per_account():
    out = _run("""
const a = queueStorageKey('alice');
const b = queueStorageKey('bob');
const single = queueStorageKey(null);
console.log(JSON.stringify({ a, b, single, distinct: a !== b && a !== single }));
""")
    assert out["distinct"] is True, "two accounts shared one queue key"
    assert "alice" in out["a"] and "bob" in out["b"]


def test_a_queued_item_is_not_sent_under_another_account():
    out = _run("""
setResponses([{ status: 500 }, { status: 200 }]);
try { await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1'); } catch {}
const before = sent.length;
_queueOwner = 'bob';                 // account switch
await flushRetryQueue();
console.log(JSON.stringify({ before, after: sent.length, owners: sent.map(s => s.owner) }));
""")
    assert out["after"] == out["before"], (
        "an item queued by alice was transmitted after switching to bob"
    )


def test_returning_to_the_account_recovers_its_pending_work():
    out = _run("""
setResponses([{ status: 500 }, { status: 200 }]);
try { await postDurably('attempt', '/a', { choice: 1 }, 'att', 'q1', 'sub-1'); } catch {}
_queueOwner = 'bob';
await flushRetryQueue();
_queueOwner = 'alice';
// Clear the backoff window; this test is about recovery on return, not timing.
for (const it of loadRetryQueue()) updateRetryItem(it.id, { next_try: 0 });
await flushRetryQueue();
console.log(JSON.stringify({ sent: sent.length, queued: loadRetryQueue().length }));
""")
    assert out["sent"] == 2, "alice's pending work did not resume on return"
    assert out["queued"] == 0


def test_unscoped_legacy_entries_are_not_claimed_by_the_next_account():
    out = _run("""
localStorage.setItem('study:durable-posts:v1', JSON.stringify([
  { id: 'legacy', kind: 'attempt', path: '/a', payload: { idempotency_key: 'legacy' } },
]));
await flushRetryQueue();
console.log(JSON.stringify({ sent: sent.length }));
""")
    assert out["sent"] == 0, (
        "a v1 entry with no owner was attributed to whichever account logged in"
    )


# --------------------------------------------------------------------------
# B07 — failure classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 403, 404, 409, 410, 422])
def test_permanent_failures_stop_retrying(status):
    out = _run(f"""
setResponses([{{ status: {status} }}, {{ status: 200 }}]);
try {{ await postDurably('attempt', '/a', {{ c: 1 }}, 'att', 'q1', 'sub-1'); }} catch {{}}
const first = sent.length;
await flushRetryQueue();
const q = loadRetryQueue();
console.log(JSON.stringify({{
  first, after: sent.length,
  permanent: q.length ? !!q[0].permanent : null,
}}));
""")
    assert out["after"] == out["first"], f"HTTP {status} was retried"
    assert out["permanent"] is True, f"HTTP {status} was not marked as needing attention"


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_failures_retry_with_the_same_key(status):
    out = _run(f"""
setResponses([{{ status: {status} }}, {{ status: 200 }}]);
try {{ await postDurably('attempt', '/a', {{ c: 1 }}, 'att', 'q1', 'sub-1'); }} catch {{}}
updateRetryItem(loadRetryQueue()[0].id, {{ next_try: 0 }});
await flushRetryQueue();
console.log(JSON.stringify({{
  keys: sent.map(s => s.body.idempotency_key), queued: loadRetryQueue().length,
}}));
""")
    assert len(out["keys"]) == 2, f"HTTP {status} was not retried"
    assert out["keys"][0] == out["keys"][1], "the retry changed the idempotency key"
    assert out["queued"] == 0


def test_an_auth_failure_pauses_dispatch():
    out = _run("""
setResponses([{ status: 401 }, { status: 200 }]);
try { await postDurably('attempt', '/a', { c: 1 }, 'att', 'q1', 'sub-1'); } catch {}
const first = sent.length;
await flushRetryQueue();
console.log(JSON.stringify({ first, after: sent.length, paused: _queuePaused }));
""")
    assert out["paused"] is True, "a 401 did not pause the queue"
    assert out["after"] == out["first"], "the queue kept dispatching after a 401"


def test_a_network_error_is_retryable():
    out = _run("""
console.log(JSON.stringify({
  network: classifyQueueError({ status: 0 }),
  auth: classifyQueueError({ status: 401 }),
  permanent: classifyQueueError({ status: 409 }),
  transient: classifyQueueError({ status: 503 }),
}));
""")
    assert out["network"] == "retryable"
    assert out["auth"] == "auth"
    assert out["permanent"] == "permanent"
    assert out["transient"] == "retryable"


# --------------------------------------------------------------------------
# B07 — storage failure must be visible, ordering must hold
# --------------------------------------------------------------------------

def test_a_storage_failure_is_reported_not_swallowed():
    out = _run("""
_storageFails = true;
const ok = saveRetryQueue([{ id: 'x' }]);
console.log(JSON.stringify({ ok }));
""")
    assert out["ok"] is False, (
        "a failed write returned success; the answer would vanish on reload"
    )


def test_pending_ratings_for_one_card_keep_their_order():
    """An older failed rating must not be applied after a newer one."""
    out = _run("""
setResponses([{ status: 500 }, { status: 500 }, { status: 200 }, { status: 200 }]);
try { await postDurably('review', '/r', { rating: 1 }, 'rv', 'card-1', 'sub-a'); } catch {}
try { await postDurably('review', '/r', { rating: 4 }, 'rv', 'card-1', 'sub-b'); } catch {}
for (const it of loadRetryQueue()) updateRetryItem(it.id, { next_try: 0 });
await flushRetryQueue();
console.log(JSON.stringify({ ratings: sent.map(s => s.body.rating) }));
""")
    assert out["ratings"] == [1, 4, 1, 4], (
        f"pending ratings for one card were reordered: {out['ratings']}"
    )


def test_a_blocked_entity_does_not_let_a_later_rating_jump_ahead():
    out = _run("""
setResponses([{ status: 500 }, { status: 500 }, { status: 500 }, { status: 200 }]);
try { await postDurably('review', '/r', { rating: 1 }, 'rv', 'card-1', 'sub-a'); } catch {}
try { await postDurably('review', '/r', { rating: 4 }, 'rv', 'card-1', 'sub-b'); } catch {}
for (const it of loadRetryQueue()) updateRetryItem(it.id, { next_try: 0 });
await flushRetryQueue();
const tail = sent.slice(2).map(s => s.body.rating);
console.log(JSON.stringify({ tail }));
""")
    assert out["tail"] == [1], (
        f"the newer rating was sent while the older one was still failing: {out['tail']}"
    )
