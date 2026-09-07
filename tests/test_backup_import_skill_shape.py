"""B05 — the import validator must accept only shapes the store can take.

The first pass validated a handful of string fields and nothing else, so these
payloads all passed validation and then failed *inside* the import, after
earlier memories and skills had already been written:

    {"skills": [{"title": "Example", "id": ["bad"]}]}          unhashable id
    {"skills": [{"title": "Example", "confidence": "nope"}]}   float() raises
    {"skills": [{"title": "Example", "tags": 42}]}             list() raises

A string in a list field is worse than an error: ``list("abc")`` silently
becomes ``['a', 'b', 'c']``.

Unlike tests/test_backup_import_validation.py, which stubs add_skill with a
permissive lambda, the last group here drives a real SkillsManager over a real
directory — the normalisation boundary that actually raises.
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


VALID_SKILL = {"title": "Photosynthesis basics", "name": "photosynthesis"}
VALID_MEMORY = {"text": "chlorophyll absorbs red and blue light"}


def _setup(monkeypatch, skills_manager=None, user="alice"):
    monkeypatch.setattr(br, "require_admin", lambda request: None)
    monkeypatch.setattr(br, "get_current_user", lambda request: user)

    writes = {"memories": None, "settings": None, "features": None, "presets": None}

    mem = MagicMock()
    mem.load_all.return_value = []
    mem.load_all_for_update.return_value = []
    mem.save.side_effect = lambda e: writes.__setitem__("memories", e)

    if skills_manager is None:
        skills_manager = MagicMock()
        skills_manager.load_all.return_value = []
        skills_manager.add_skill.side_effect = lambda **kw: {"id": "s1", "name": kw.get("name")}

    presets = MagicMock()
    presets.get_all.return_value = {}
    presets.save.side_effect = lambda c: writes.__setitem__("presets", c)

    monkeypatch.setattr(br, "load_settings", lambda: {})
    monkeypatch.setattr(br, "save_settings", lambda c: writes.__setitem__("settings", c))
    monkeypatch.setattr(br, "load_features", lambda: {})
    monkeypatch.setattr(br, "save_features", lambda c: writes.__setitem__("features", c))

    router = br.setup_backup_routes(mem, presets, skills_manager)
    endpoint = next(
        r.endpoint for r in router.routes
        if r.path == "/api/import" and "POST" in getattr(r, "methods", set())
    )
    return endpoint, writes


# --------------------------------------------------------------------------
# Every malformed skill field is a 400 with zero writes
# --------------------------------------------------------------------------

MALFORMED = [
    ("id", ["bad"]),                     # unhashable: `sid in existing_ids` raises
    ("id", {"a": 1}),
    ("id", 123),                         # the store keys on strings
    ("confidence", "not-a-number"),      # float() raises
    ("confidence", True),                # bool is not a meaningful confidence
    ("confidence", float("inf")),        # float() succeeds, the value is nonsense
    ("confidence", float("nan")),
    ("confidence", 2.5),                 # outside the documented 0..1 range
    ("confidence", -0.5),
    ("tags", 42),                        # list() raises
    ("tags", "biology"),                 # list() silently yields ['b','i','o',...]
    ("tags", [1, 2]),                    # elements must be strings
    ("steps", "one then two"),
    ("procedure", {"a": 1}),
    ("pitfalls", 7),
    ("verification", "check it"),
    ("platforms", "linux"),
    ("requires_toolsets", 3),
    ("fallback_for_toolsets", "shell"),
    ("status", 5),
    ("version", 1.0),
    ("teacher_model", ["m"]),
    ("when_to_use", 9),
]


@pytest.mark.parametrize("field, value", MALFORMED, ids=[f"{f}={v!r}" for f, v in MALFORMED])
def test_a_malformed_skill_field_is_rejected_before_any_write(monkeypatch, field, value):
    """A valid memory and a valid first skill must not land before the failure."""
    endpoint, writes = _setup(monkeypatch)
    body = {
        "memories": [VALID_MEMORY],
        "skills": [dict(VALID_SKILL), {**VALID_SKILL, "name": "second", field: value}],
    }

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))

    assert exc.value.status_code == 400, f"{field}={value!r} was not rejected"
    detail = str(exc.value.detail)
    assert "skills" in detail.lower()
    assert "1" in detail, f"row index missing from {detail!r}"
    assert field in detail, f"field name missing from {detail!r}"
    assert writes["memories"] is None, "memories were written before validation failed"


# --------------------------------------------------------------------------
# Valid shapes still import
# --------------------------------------------------------------------------

WELL_FORMED = [
    ("confidence", 0.0),
    ("confidence", 1),
    ("confidence", 0.85),
    ("tags", []),
    ("tags", ["biology", "cells"]),
    ("id", "skill-123"),
    ("procedure", ["step one", "step two"]),
    ("status", "draft"),
    ("version", "1.0.0"),
]


@pytest.mark.parametrize("field, value", WELL_FORMED, ids=[f"{f}={v!r}" for f, v in WELL_FORMED])
def test_well_formed_skill_fields_are_accepted(monkeypatch, field, value):
    endpoint, _writes = _setup(monkeypatch)
    body = {"skills": [{**VALID_SKILL, field: value}]}
    result = asyncio.run(endpoint(_Req(body)))
    assert result["ok"] is True


def test_null_fields_are_accepted_and_left_to_the_stores_defaults(monkeypatch):
    """Older exports omit fields or write them as null; both must still import."""
    endpoint, _writes = _setup(monkeypatch)
    body = {"skills": [{**VALID_SKILL, "tags": None, "confidence": None,
                        "procedure": None, "id": None}]}
    result = asyncio.run(endpoint(_Req(body)))
    assert result["ok"] is True


def test_a_legacy_export_without_optional_fields_imports(monkeypatch):
    endpoint, _writes = _setup(monkeypatch)
    body = {"skills": [{"title": "Old skill", "description": "from an old export"}]}
    result = asyncio.run(endpoint(_Req(body)))
    assert result["ok"] is True


# --------------------------------------------------------------------------
# The real store boundary — no permissive mock
# --------------------------------------------------------------------------

def _real_skills_manager(tmp_path):
    from services.memory.skills import SkillsManager

    return SkillsManager(str(tmp_path))


def _skill_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "skills").rglob("SKILL.md"))


@pytest.mark.parametrize("field, value", [
    ("id", ["bad"]),
    ("confidence", "not-a-number"),
    ("tags", 42),
])
def test_the_real_store_is_never_reached_with_a_bad_shape(monkeypatch, tmp_path, field, value):
    """These are the three payloads the handoff reproduced against the validator."""
    manager = _real_skills_manager(tmp_path)
    endpoint, _writes = _setup(monkeypatch, skills_manager=manager)
    body = {"skills": [dict(VALID_SKILL), {**VALID_SKILL, "name": "second", field: value}]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))

    assert exc.value.status_code == 400
    assert _skill_files(tmp_path) == [], "a skill was written before the payload was rejected"


def test_a_valid_payload_reaches_the_real_store(monkeypatch, tmp_path):
    """The validator must not be so strict that a good export stops importing."""
    manager = _real_skills_manager(tmp_path)
    endpoint, _writes = _setup(monkeypatch, skills_manager=manager)
    body = {"skills": [{
        "title": "Photosynthesis basics",
        "name": "photosynthesis",
        "description": "how plants convert light",
        "tags": ["biology"],
        "confidence": 0.9,
        "procedure": ["absorb light", "split water"],
        "status": "draft",
        "version": "1.0.0",
    }]}

    result = asyncio.run(endpoint(_Req(body)))

    assert result["ok"] is True
    assert _skill_files(tmp_path) == ["SKILL.md"], "the valid skill was not written"


def test_a_string_list_field_would_have_been_silently_split(monkeypatch, tmp_path):
    """Guards the quiet corruption, not just the loud crash.

    list("biology") is ['b','i','o','l','o','g','y'] — no exception, seven junk
    tags. It has to be rejected at the boundary.
    """
    manager = _real_skills_manager(tmp_path)
    endpoint, _writes = _setup(monkeypatch, skills_manager=manager)
    body = {"skills": [{**VALID_SKILL, "tags": "biology"}]}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(_Req(body)))
    assert exc.value.status_code == 400
    assert _skill_files(tmp_path) == []
