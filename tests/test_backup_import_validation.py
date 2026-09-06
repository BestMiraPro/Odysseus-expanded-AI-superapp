"""Backup import must validate row shapes before it writes anything.

``import_data`` checked only the outer object and each section's container
type, then assumed the fields inside rows were strings and applied sections
sequentially. A row like ``{"text": 123}`` raised ``AttributeError`` on
``.strip()`` mid-import — after earlier sections had already been persisted,
leaving a half-applied restore with no report of what landed.

The contract these tests pin:

* every section is validated up front; a malformed payload writes nothing;
* the rejection says which section and row were wrong;
* valid payloads still import exactly as before.
"""

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.backup_routes as br


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _setup(monkeypatch, user="alice", memories=(), skills=()):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: user)

    writes = {"memories": None, "presets": None, "settings": None,
              "features": None, "preferences": None, "skills": []}

    mem = MagicMock()
    mem.load_all.return_value = list(memories)
    mem.load_all_for_update.return_value = list(memories)
    mem.save.side_effect = lambda entries: writes.__setitem__("memories", entries)

    skills_mgr = MagicMock()
    skills_mgr.load_all.return_value = list(skills)
    skills_mgr.add_skill.side_effect = lambda **kw: (
        writes["skills"].append(kw) or {"id": kw.get("name") or "s1", "name": kw.get("name")}
    )

    presets = MagicMock()
    presets.get_all.return_value = {}
    presets.save.side_effect = lambda cur: writes.__setitem__("presets", cur)

    monkeypatch.setattr(br, "load_settings", lambda: {})
    monkeypatch.setattr(br, "save_settings", lambda cur: writes.__setitem__("settings", cur))
    monkeypatch.setattr(br, "load_features", lambda: {})
    monkeypatch.setattr(br, "save_features", lambda cur: writes.__setitem__("features", cur))

    router = br.setup_backup_routes(mem, presets, skills_mgr)
    endpoint = next(
        r.endpoint for r in router.routes
        if r.path == "/api/import" and "POST" in getattr(r, "methods", set())
    )
    return endpoint, writes


def _nothing_written(writes):
    return (
        writes["memories"] is None
        and writes["presets"] is None
        and writes["settings"] is None
        and writes["features"] is None
        and writes["skills"] == []
    )


# --------------------------------------------------------------------------
# Malformed rows are rejected, not crashed on
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_text", [123, ["a"], {"a": 1}, True])
def test_non_string_memory_text_is_a_400(monkeypatch, bad_text):
    endpoint, writes = _setup(monkeypatch)
    body = {"memories": [{"text": bad_text}]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert "memories" in str(exc.value.detail).lower()
    assert _nothing_written(writes)


def test_non_string_skill_title_is_a_400(monkeypatch):
    endpoint, writes = _setup(monkeypatch)
    body = {"skills": [{"title": 42}]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert "skills" in str(exc.value.detail).lower()
    assert _nothing_written(writes)


def test_non_dict_row_in_memories_is_a_400(monkeypatch):
    endpoint, writes = _setup(monkeypatch)
    body = {"memories": ["just a string"]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert _nothing_written(writes)


def test_settings_must_have_string_keys(monkeypatch):
    endpoint, writes = _setup(monkeypatch)
    # JSON cannot produce this, but a caller posting a decoded object can.
    body = {"settings": {1: "x"}}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert _nothing_written(writes)


def test_preset_values_must_be_objects_or_lists(monkeypatch):
    endpoint, writes = _setup(monkeypatch)
    body = {"presets": {"p1": "not a preset"}}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert _nothing_written(writes)


# --------------------------------------------------------------------------
# The point of validating first: a bad late section cancels the whole import
# --------------------------------------------------------------------------

def test_a_bad_late_section_leaves_earlier_sections_unwritten(monkeypatch):
    """The regression the review named: memories used to land before the
    malformed section further down was ever reached."""
    endpoint, writes = _setup(monkeypatch)
    body = {
        "memories": [{"text": "perfectly fine"}],
        "settings": {"theme": "dark"},
        "preferences": "not an object, and it comes last",
    }

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert writes["memories"] is None, "memories were written before validation failed"
    assert writes["settings"] is None
    assert _nothing_written(writes)


def test_rejection_names_the_offending_section_and_row(monkeypatch):
    endpoint, _writes = _setup(monkeypatch)
    body = {"memories": [{"text": "ok"}, {"text": "ok too"}, {"text": 99}]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    detail = str(exc.value.detail)
    assert "memories" in detail.lower()
    assert "2" in detail, f"row index missing from {detail!r}"


# --------------------------------------------------------------------------
# Valid payloads are unaffected
# --------------------------------------------------------------------------

def test_valid_payload_still_imports_every_section(monkeypatch):
    endpoint, writes = _setup(monkeypatch)
    body = {
        "memories": [{"text": "remember this"}],
        "skills": [{"title": "Do the thing", "name": "do-thing"}],
        "presets": {"p1": {"a": 1}, "p2": [1, 2]},
        "settings": {"theme": "dark"},
        "features": {"beta": True},
        "preferences": {"lang": "en"},
    }
    monkeypatch.setattr(br, "_load_prefs_for_user", None, raising=False)

    result = asyncio.run(endpoint(_Req(body)))

    assert result["ok"] is True
    assert writes["memories"] is not None
    assert any(m.get("text") == "remember this" for m in writes["memories"])
    assert len(writes["skills"]) == 1
    assert writes["presets"] == {"p1": {"a": 1}, "p2": [1, 2]}
    assert writes["settings"] == {"theme": "dark"}
    assert writes["features"] == {"beta": True}


def test_empty_payload_is_still_a_soft_no_op(monkeypatch):
    endpoint, writes = _setup(monkeypatch)
    result = asyncio.run(endpoint(_Req({})))
    assert result["ok"] is False
    assert _nothing_written(writes)


def test_unknown_sections_are_ignored(monkeypatch):
    """Forward compatibility: a newer export's extra keys must not 400."""
    endpoint, writes = _setup(monkeypatch)
    body = {"memories": [{"text": "hi"}], "future_section": {"anything": [1, 2]}}

    result = asyncio.run(endpoint(_Req(body)))
    assert result["ok"] is True
    assert writes["memories"] is not None


# --------------------------------------------------------------------------
# Partial-import policy: an infrastructure failure names what already landed
# --------------------------------------------------------------------------

def test_a_store_failure_midway_reports_what_was_applied(monkeypatch):
    endpoint, writes = _setup(monkeypatch)

    def explode(_cur):
        raise OSError("No space left on device")

    monkeypatch.setattr(br, "save_settings", explode)
    body = {
        "memories": [{"text": "landed first"}],
        "settings": {"theme": "dark"},
    }

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))

    assert exc.value.status_code == 500
    detail = str(exc.value.detail)
    assert "No space left on device" in detail
    assert "memories" in detail, "the completed section is not reported"
    assert "Re-run the import" in detail
    # The section that did complete really did complete.
    assert writes["memories"] is not None


def test_an_unreadable_memory_store_still_returns_503(monkeypatch):
    """The pre-existing guard must not be swallowed by the new handler."""
    from services.memory import MemoryStoreUnreadable

    endpoint, writes = _setup(monkeypatch)
    mem_error = MemoryStoreUnreadable("corrupt")

    router_mem = MagicMock()
    router_mem.load_all_for_update.side_effect = mem_error
    presets = MagicMock()
    presets.get_all.return_value = {}
    skills_mgr = MagicMock()
    skills_mgr.load_all.return_value = []
    router = br.setup_backup_routes(router_mem, presets, skills_mgr)
    endpoint = next(
        r.endpoint for r in router.routes
        if r.path == "/api/import" and "POST" in getattr(r, "methods", set())
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req({"memories": [{"text": "hi"}]})))
    assert exc.value.status_code == 503
