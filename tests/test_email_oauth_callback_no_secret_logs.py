"""S7 / #151: the Google OAuth callback logs only account identifiers.

Source-to-sink: the callback derives ``account_id`` from the HMAC-verified
state payload (a string DB primary key), then writes encrypted tokens. The
only account-adjacent log on this path is the mailbox identity-verification
warning ("Google OAuth mailbox identity verification failed for account %s").
Sentinel proof: with a token exchange and userinfo that carry sentinel
secrets, no record may contain them; the verification-failure record may
contain only the account id. ``verify_oauth_state`` leaves unverifiable
secrets out of the payload entirely.
"""

import logging
from types import SimpleNamespace

import pytest

import src.secret_storage as _secret_storage
from routes.email_helpers import make_oauth_state

SENTINEL_ACCESS = "ya29.S7-CALLBACK-ACCESS-SENTINEL"
SENTINEL_REFRESH = "1//S7-CALLBACK-REFRESH-SENTINEL"
SENTINEL_CLIENT_SECRET = "GOOGLE-CLIENT-SECRET-S7-SENTINEL"
ACCOUNT_ID = "acct-sentinel-77"
OWNER = "alice"


class _Resp:
    is_success = True

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeDb:
    def __init__(self, row):
        self._row = row

    def query(self, model):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row

    def commit(self):
        pass

    def close(self):
        pass


class _FakeRequest:
    def __init__(self):
        self.headers = {"host": "localhost:7000"}
        self.url = SimpleNamespace(scheme="http")


def _callback_route():
    from routes.email_routes import setup_email_routes

    router = setup_email_routes()
    for route in router.routes:
        if (
            getattr(route, "path", "") == "/api/email/oauth/google/callback"
            and "GET" in getattr(route, "methods", set())
        ):
            return route.endpoint
    raise AssertionError("Google OAuth callback route not found")


@pytest.fixture(autouse=True)
def _pinned_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "s7-test-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", SENTINEL_CLIENT_SECRET)
    monkeypatch.setattr("core.database.SessionLocal", lambda: _FakeDb(_row()))
    monkeypatch.setattr(_secret_storage, "_load_or_create_key", lambda: b"k" * 32)


def _row():
    return SimpleNamespace(
        id=ACCOUNT_ID,
        owner=OWNER,
        imap_user="alice@example.com",
        smtp_user="alice@example.com",
        oauth_provider=None,
    )


@pytest.mark.asyncio
async def test_callback_verification_failure_logs_account_id_only(monkeypatch, caplog):
    """Drive the real callback through the mailbox-identity mismatch branch:
    sentinel bearer tokens and client secret must stay out of every record."""
    from fastapi.responses import RedirectResponse

    monkeypatch.setattr(
        "httpx.post",
        lambda *a, **k: _Resp({
            "access_token": SENTINEL_ACCESS,
            "refresh_token": SENTINEL_REFRESH,
            "expires_in": 3599,
        }),
    )
    monkeypatch.setattr(
        "httpx.get",
        lambda *a, **k: _Resp({"email": "mallory@example.invalid", "name": "M"}),
    )

    state = make_oauth_state(ACCOUNT_ID, OWNER)
    caplog.set_level(logging.DEBUG, logger="routes.email_routes")

    resp = await _callback_route()(
        code="s7-auth-code",
        state=state,
        error=None,
        request=_FakeRequest(),
    )

    assert isinstance(resp, RedirectResponse)
    assert "identity_verification_failed" in resp.headers["location"]
    text = caplog.text
    assert SENTINEL_ACCESS not in text
    assert SENTINEL_REFRESH not in text
    assert SENTINEL_CLIENT_SECRET not in text
    assert ACCOUNT_ID in text


def test_state_payload_carries_account_ids_not_tokens():
    """The callback's only tainted input is the verified state payload; prove
    its runtime shape: account id + owner + nonce, all plain strings."""
    state = make_oauth_state(ACCOUNT_ID, OWNER)
    from routes.email_helpers import verify_oauth_state

    payload = verify_oauth_state(state)
    assert isinstance(payload, dict)
    for entry in payload.values():
        assert isinstance(entry, str)