"""U03 — preview the restore scope, and report partial results honestly.

The admin UI uploaded the parsed file the moment it was selected: no
confirmation, no statement of what would change. And `imported` was appended
only after a whole section finished, so if the third skill failed the error
could not say that the first two had been saved.

Two capabilities here:

* a preview that validates and summarises **without writing anything**;
* per-row progress, so a mid-section failure reports what actually landed.
"""

from __future__ import annotations

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


def _setup(monkeypatch, user="alice", add_skill=None):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: user)

    writes = {"memories": None, "settings": None, "features": None,
              "presets": None, "skills": []}

    mem = MagicMock()
    mem.load_all.return_value = []
    mem.load_all_for_update.return_value = []
    mem.save.side_effect = lambda e: writes.__setitem__("memories", e)

    skills_mgr = MagicMock()
    skills_mgr.load_all.return_value = []
    if add_skill is None:
        def add_skill(**kw):
            writes["skills"].append(kw.get("name"))
            return {"id": kw.get("name"), "name": kw.get("name")}
    skills_mgr.add_skill.side_effect = add_skill

    presets = MagicMock()
    presets.get_all.return_value = {}
    presets.save.side_effect = lambda c: writes.__setitem__("presets", c)

    monkeypatch.setattr(br, "load_settings", lambda: {})
    monkeypatch.setattr(br, "save_settings", lambda c: writes.__setitem__("settings", c))
    monkeypatch.setattr(br, "load_features", lambda: {})
    monkeypatch.setattr(br, "save_features", lambda c: writes.__setitem__("features", c))

    router = br.setup_backup_routes(mem, presets, skills_mgr)
    routes = {}
    for r in router.routes:
        if "POST" in getattr(r, "methods", set()):
            routes[r.path] = r.endpoint
    return routes, writes


PAYLOAD = {
    "version": 1,
    "exported_at": "2026-09-01T10:00:00",
    "memories": [{"text": "one"}, {"text": "two"}],
    "skills": [{"title": "A", "name": "a"}, {"title": "B", "name": "b"}],
    "presets": {"p1": {"x": 1}},
    "settings": {"theme": "dark"},
    "unknown_future": {"nested": True},
}


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------

def test_preview_summarises_recognised_sections_with_counts(monkeypatch):
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import/preview"](_Req(PAYLOAD)))

    sections = {s["name"]: s for s in result["sections"]}
    assert sections["memories"]["count"] == 2
    assert sections["skills"]["count"] == 2
    assert sections["presets"]["count"] == 1
    assert sections["settings"]["count"] == 1


def test_preview_reports_the_export_metadata(monkeypatch):
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import/preview"](_Req(PAYLOAD)))
    assert result["version"] == 1
    assert result["exported_at"] == "2026-09-01T10:00:00"


def test_preview_lists_sections_it_will_ignore(monkeypatch):
    """A newer export's extra keys are skipped; say so rather than silently."""
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import/preview"](_Req(PAYLOAD)))
    assert "unknown_future" in result["ignored"]
    assert "memories" not in result["ignored"]


def test_preview_states_how_each_section_is_applied(monkeypatch):
    """Merge vs replace matters: the operator is about to overwrite settings."""
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import/preview"](_Req(PAYLOAD)))
    modes = {s["name"]: s["mode"] for s in result["sections"]}
    assert modes["memories"] == "merge"
    assert modes["settings"] == "merge"
    assert modes["presets"] == "replace-matching"


def test_preview_flags_sections_shared_across_users(monkeypatch):
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import/preview"](_Req(PAYLOAD)))
    shared = {s["name"]: s.get("shared", False) for s in result["sections"]}
    assert shared["settings"] is True, "app settings are shared; say so"
    assert shared["memories"] is False


def test_preview_writes_nothing(monkeypatch):
    routes, writes = _setup(monkeypatch)
    asyncio.run(routes["/api/import/preview"](_Req(PAYLOAD)))
    assert writes["memories"] is None
    assert writes["skills"] == []
    assert writes["settings"] is None
    assert writes["presets"] is None


def test_preview_rejects_a_malformed_payload(monkeypatch):
    """Preview must use the same validator, or it would promise a bad import."""
    routes, writes = _setup(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes["/api/import/preview"](_Req({"skills": [{"tags": 42}]})))
    assert exc.value.status_code == 400
    assert writes["skills"] == []


# --------------------------------------------------------------------------
# Structured results
# --------------------------------------------------------------------------

def test_import_returns_per_section_counts(monkeypatch):
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import"](_Req(PAYLOAD)))

    assert result["ok"] is True
    sections = {s["name"]: s for s in result["sections"]}
    assert sections["memories"]["added"] == 2
    assert sections["skills"]["added"] == 2


def test_import_keeps_the_legacy_message_and_list(monkeypatch):
    """Older clients read `imported` and `message`; do not break them."""
    routes, _writes = _setup(monkeypatch)
    result = asyncio.run(routes["/api/import"](_Req(PAYLOAD)))
    assert isinstance(result["imported"], list)
    assert result["imported"]
    assert "Imported:" in result["message"]


def test_a_skipped_duplicate_is_counted_separately(monkeypatch):
    routes, _writes = _setup(monkeypatch)
    body = {"memories": [{"text": "same"}, {"text": "SAME"}]}
    result = asyncio.run(routes["/api/import"](_Req(body)))
    sections = {s["name"]: s for s in result["sections"]}
    assert sections["memories"]["added"] == 1
    assert sections["memories"]["skipped"] == 1


# --------------------------------------------------------------------------
# A mid-section failure must report what actually landed
# --------------------------------------------------------------------------

def test_a_failure_partway_through_a_section_reports_the_rows_that_landed(monkeypatch):
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("No space left on device")
        return {"id": kw.get("name"), "name": kw.get("name")}

    routes, _writes = _setup(monkeypatch, add_skill=flaky)
    body = {"skills": [{"title": t, "name": t} for t in ("a", "b", "c", "d")]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes["/api/import"](_Req(body)))

    assert exc.value.status_code == 500
    detail = str(exc.value.detail)
    assert "2" in detail, (
        f"the two skills that were saved are not reported: {detail!r}"
    )
    assert "skills" in detail
    assert "No space left on device" in detail
