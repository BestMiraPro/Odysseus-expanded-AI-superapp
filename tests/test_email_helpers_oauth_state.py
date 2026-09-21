"""S7 / #298: OAuth state tokens are HMAC-SHA256 authentication codes.

CodeQL's weak-sensitive-data-hashing rule fires on the ``hashlib.sha256`` in
``routes/email_helpers.py:79`` (and its verification twin), but the hash is a
*keyed message authentication code over a nonce-bearing payload*, verified
with ``hmac.compare_digest`` — not a stored password hash. It exists so the
Google OAuth callback can prove the flow was initiated by an authenticated,
owning user (CSRF / state-forgery protection).

The disposition is a false positive and depends on exactly the contract these
tests pin: tokens round-trip, every field participates in the MAC, tampering
and wrong-key tokens fail closed, and malformed input returns None instead of
raising. The HMAC must be kept; replacing it with bcrypt would break the
callback's ability to verify state synchronously.
"""

import base64
import json

import pytest

import src.secret_storage as _secret_storage
from routes.email_helpers import make_oauth_state, verify_oauth_state


@pytest.fixture(autouse=True)
def _fixed_app_key(monkeypatch):
    # Both helpers resolve _load_or_create_key at call time, so a fixed key
    # keeps every test hermetic: no data/.app_key is created or read.
    monkeypatch.setattr(_secret_storage, "_load_or_create_key", lambda: b"k" * 32)


def test_state_round_trips_account_owner_and_nonce():
    state = make_oauth_state("acct-1", "alice")
    payload = verify_oauth_state(state)

    assert isinstance(payload, dict)
    assert payload["a"] == "acct-1"
    assert payload["o"] == "alice"
    # token_hex(16) -> 32 hex chars
    assert isinstance(payload["n"], str) and len(payload["n"]) == 32


def test_fresh_nonce_makes_every_token_unique():
    states = {make_oauth_state("acct-1", "alice") for _ in range(8)}
    assert len(states) == 8


def test_account_and_owner_are_covered_by_the_mac():
    for account, owner in (("acct-2", "alice"), ("acct-1", "bob")):
        s1 = make_oauth_state("acct-1", "alice")
        s2 = make_oauth_state(account, owner)
        payload1 = json.loads(base64.urlsafe_b64decode(s1.encode()).decode().rsplit("|", 1)[0])
        payload2 = json.loads(base64.urlsafe_b64decode(s2.encode()).decode().rsplit("|", 1)[0])
        assert (payload2["a"], payload2["o"]) == (account, owner)
        assert s1 != s2
        # each token verifies only against its own payload
        assert verify_oauth_state(s1)["a"] == "acct-1"
        assert verify_oauth_state(s2)["a"] == account


def test_tampered_payload_is_rejected_before_parsing():
    state = make_oauth_state("acct-1", "alice")
    raw = base64.urlsafe_b64decode(state.encode()).decode()
    payload, sig = raw.rsplit("|", 1)

    # Mutations that still produce structurally different payloads must die at
    # the MAC check, not later: signing a different payload is a forgery.
    for tampered in (payload[:-1] + ("A" if payload[-1] != "A" else "B"),
                     "x" + payload):
        forged = base64.urlsafe_b64encode(f"{tampered}|{sig}".encode()).decode()
        assert verify_oauth_state(forged) is None


def test_wrong_signing_key_is_rejected(monkeypatch):
    state = make_oauth_state("acct-1", "alice")  # signed with b"k"*32
    monkeypatch.setattr(_secret_storage, "_load_or_create_key", lambda: b"j" * 32)
    assert verify_oauth_state(state) is None


@pytest.mark.parametrize("bad", [
    "",
    "not base64 at all!",
    base64.urlsafe_b64encode(b"no-separator-here").decode(),
    base64.urlsafe_b64encode(b"{broken json}|deadbeef").decode(),
    base64.urlsafe_b64encode(b"{}|deadbeef").decode(),
    None,
])
def test_malformed_tokens_return_none_and_never_raise(bad):
    assert verify_oauth_state(bad) is None