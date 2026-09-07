"""U05 — a learner must be able to tell what actually reached the server.

Rating a flashcard advances the session before the save completes and only
raised a transient error toast, and background retries discarded their result.
A learner could finish a session with no idea which answers landed.

The durable queue (B06/B07) already knows: items waiting, items that failed
permanently, a paused auth state, and whether localStorage refused the write.
This turns that into one persistent, honest status line — and keeps a failed
storage write distinct, because there reload persistence is not guaranteed.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import extract_const, needs_node, run_js

pytestmark = needs_node

FNS = ("queueStorageKey", "loadRetryQueue", "queueStorageHealthy", "queueIsPaused",
       "summariseSubmissionQueue", "submissionStatusText")
CONSTS = ("RETRY_QUEUE_PREFIX", "SINGLE_USER_QUEUE_OWNER")

PRELUDE = """
const store = new Map();
const localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, v),
  removeItem: (k) => store.delete(k),
};
let _queueOwner = 'alice';
let _queuePaused = false;
let _queueStorageFailed = false;

function seed(items) {
  store.set(queueStorageKey(_queueOwner), JSON.stringify(items));
}
"""


def _run(epilogue):
    consts = "\n".join(extract_const(c) for c in CONSTS)
    return run_js(PRELUDE + consts + "\n", *FNS, epilogue=epilogue)


def _status(setup):
    return _run(f"""
{setup}
const s = summariseSubmissionQueue();
console.log(JSON.stringify({{ summary: s, text: submissionStatusText(s) }}));
""")


# --------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------

def test_an_empty_queue_reports_everything_saved():
    out = _status("seed([]);")
    assert out["summary"]["pending"] == 0
    assert out["summary"]["attention"] == 0
    assert "saved" in out["text"].lower()


def test_waiting_items_are_counted_as_pending():
    out = _status("seed([{ id: 'a' }, { id: 'b' }, { id: 'c' }]);")
    assert out["summary"]["pending"] == 3
    assert "3" in out["text"]
    assert "sync" in out["text"].lower()


def test_a_permanently_failed_item_needs_attention_not_a_retry_count():
    out = _status("seed([{ id: 'a' }, { id: 'b', permanent: true }]);")
    assert out["summary"]["pending"] == 1
    assert out["summary"]["attention"] == 1
    assert "attention" in out["text"].lower(), (
        "a permanently failed answer was reported as merely pending"
    )


def test_attention_takes_priority_over_pending_in_the_message():
    out = _status("seed([{ id: 'a' }, { id: 'b' }, { id: 'c', permanent: true }]);")
    assert "attention" in out["text"].lower()


def test_an_auth_pause_is_reported_as_needing_sign_in():
    out = _status("_queuePaused = true; seed([{ id: 'a' }]);")
    assert out["summary"]["paused"] is True
    assert "sign in" in out["text"].lower()


def test_a_storage_failure_gets_its_own_message():
    """Distinct because reload persistence is not guaranteed here."""
    out = _status("_queueStorageFailed = true; seed([{ id: 'a' }]);")
    assert out["summary"]["storageFailed"] is True
    assert "reload" in out["text"].lower(), (
        f"a failed local write was not called out: {out['text']!r}"
    )


def test_storage_failure_outranks_every_other_state():
    out = _status(
        "_queueStorageFailed = true; _queuePaused = true;"
        " seed([{ id: 'a', permanent: true }]);"
    )
    assert "reload" in out["text"].lower()


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------

@pytest.mark.parametrize("count, singular", [(1, True), (2, False)])
def test_the_pending_message_agrees_in_number(count, singular):
    items = ", ".join("{ id: 'x%d' }" % i for i in range(count))
    out = _status(f"seed([{items}]);")
    text = out["text"].lower()
    assert ("1 answer waiting" in text) is singular, text
    if not singular:
        assert "answers waiting" in text


@pytest.mark.parametrize("count, singular", [(1, True), (3, False)])
def test_the_attention_message_agrees_in_number(count, singular):
    items = ", ".join("{ id: 'x%d', permanent: true }" % i for i in range(count))
    out = _status(f"seed([{items}]);")
    text = out["text"].lower()
    assert ("1 answer needs attention" in text) is singular, text


def test_the_queue_of_another_account_is_not_counted():
    out = _status("""
store.set(queueStorageKey('bob'), JSON.stringify([{ id: 'b1' }, { id: 'b2' }]));
seed([{ id: 'a1' }]);
""")
    assert out["summary"]["pending"] == 1, "another account's pending work was counted"
