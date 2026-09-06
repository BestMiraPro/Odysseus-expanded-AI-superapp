"""The crash watchdog's endpoint cleanup must stay off the event loop.

``_serve_crash_watchdog`` probed the served endpoint with a synchronous
``urllib.request.urlopen(..., timeout=3)`` and then ran a blocking SQLAlchemy
session, all directly inside a coroutine. A slow or blackholed endpoint stalled
every other request for the probe's full timeout.
"""

import asyncio
import threading

import pytest

from routes import cookbook_routes


def test_cleanup_helper_is_module_level():
    """It has to be reachable to be testable and to be handed to a thread."""
    assert callable(getattr(cookbook_routes, "_drop_endpoint_after_crash", None))


@pytest.mark.asyncio
async def test_endpoint_cleanup_allows_event_loop_to_progress(monkeypatch):
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    progressed = []

    def slow_cleanup(endpoint_id, session_id, exit_code):
        # Stand in for the real probe + DB work: blocking, and slow.
        loop.call_soon_threadsafe(started.set)
        progressed.append(release.wait(timeout=3))
        return True

    monkeypatch.setattr(cookbook_routes, "_drop_endpoint_after_crash", slow_cleanup)

    async def other_request():
        await started.wait()
        release.set()

    other = asyncio.create_task(other_request())
    try:
        await cookbook_routes._run_endpoint_cleanup("ep-1", "sess-1", 7)
        await asyncio.wait_for(other, timeout=3)
        assert progressed == [True], "watchdog cleanup blocked unrelated async requests"
    finally:
        release.set()
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)


def test_reachable_endpoint_is_kept(monkeypatch):
    """A stale exit marker must not drop an endpoint that is actually serving."""
    deleted = []
    endpoint = _FakeEndpoint("ep-1", "http://127.0.0.1:9999/v1")
    _install_fake_db(monkeypatch, endpoint, deleted)
    monkeypatch.setattr(cookbook_routes, "_probe_endpoint_alive", lambda url, timeout=3: True)

    assert cookbook_routes._drop_endpoint_after_crash("ep-1", "sess-1", 7) is False
    assert deleted == []


def test_unreachable_endpoint_is_dropped(monkeypatch):
    deleted = []
    endpoint = _FakeEndpoint("ep-1", "http://127.0.0.1:9999/v1")
    _install_fake_db(monkeypatch, endpoint, deleted)
    monkeypatch.setattr(cookbook_routes, "_probe_endpoint_alive", lambda url, timeout=3: False)

    assert cookbook_routes._drop_endpoint_after_crash("ep-1", "sess-1", 7) is True
    assert deleted == [endpoint]


def test_missing_endpoint_is_not_an_error(monkeypatch):
    deleted = []
    _install_fake_db(monkeypatch, None, deleted)
    monkeypatch.setattr(cookbook_routes, "_probe_endpoint_alive", lambda url, timeout=3: False)

    assert cookbook_routes._drop_endpoint_after_crash("gone", "sess-1", 7) is False
    assert deleted == []


def test_probe_url_is_derived_from_the_endpoint_base_url(monkeypatch):
    seen = []
    endpoint = _FakeEndpoint("ep-1", "http://127.0.0.1:8000/v1/")
    _install_fake_db(monkeypatch, endpoint, [])

    def probe(url, timeout=3):
        seen.append(url)
        return True

    monkeypatch.setattr(cookbook_routes, "_probe_endpoint_alive", probe)
    cookbook_routes._drop_endpoint_after_crash("ep-1", "sess-1", 7)
    assert seen == ["http://127.0.0.1:8000/v1/models"]


# --------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------

class _FakeEndpoint:
    def __init__(self, ep_id, base_url):
        self.id = ep_id
        self.name = "served-model"
        self.base_url = base_url


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row, deleted):
        self._row = row
        self._deleted = deleted
        self.committed = False

    def query(self, _model):
        return _FakeQuery(self._row)

    def delete(self, row):
        self._deleted.append(row)

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _install_fake_db(monkeypatch, row, deleted):
    import core.database as cdb

    monkeypatch.setattr(cdb, "SessionLocal", lambda: _FakeSession(row, deleted))
