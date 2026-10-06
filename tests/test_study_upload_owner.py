"""Attaching an uploaded file to a subject checks who uploaded it.

create_material_record resolved any upload id it was given, so a user could
attach another user's file - and then read, transcribe and export it through
Study - by naming its id. It now makes the same owner check as
routes/upload_routes.download_file.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi import HTTPException

from tests.helpers.sqlite_db import make_temp_sqlite

TEXT = "Price elasticity of demand measures responsiveness to price. " * 3


@pytest.fixture
def env(monkeypatch, tmp_path):
    import core.database as cd
    from core.database import StudyDeck
    from routes import study_routes as sr

    SessionLocal, engine, tmp = make_temp_sqlite(cd.Base.metadata)
    tmp.close()
    monkeypatch.setattr(sr, "SessionLocal", SessionLocal)
    monkeypatch.setattr("src.constants.UPLOAD_DIR", str(tmp_path))

    index = {}
    for owner, fid in (("alice", "alice-notes.txt"), ("bob", "bob-notes.txt")):
        path = tmp_path / fid
        path.write_text(TEXT, encoding="utf-8")
        index[fid] = {"id": fid, "owner": owner, "path": str(path), "name": fid}
    (tmp_path / "uploads.json").write_text(json.dumps(index), encoding="utf-8")

    s = SessionLocal()
    for owner in ("alice", None):
        s.add(StudyDeck(id=f"d-{owner}", owner=owner, name="Micro", new_per_day=15,
                        retention="0.9"))
    s.commit()
    s.close()
    yield sr, tmp_path
    engine.dispose()
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def test_own_upload_is_attached(env):
    sr, _ = env
    row = sr.create_material_record("alice", "d-alice", file_id="alice-notes.txt")
    assert row["char_count"] > 30


def test_someone_elses_upload_is_not_found(env):
    sr, _ = env
    with pytest.raises(HTTPException) as exc:
        sr.create_material_record("alice", "d-alice", file_id="bob-notes.txt")
    assert exc.value.status_code == 404


def test_an_upload_missing_from_the_index_is_not_found(env):
    """No record, no owner to match - as in the upload route."""
    sr, tmp_path = env
    (tmp_path / "stray.txt").write_text(TEXT, encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        sr.create_material_record("alice", "d-alice", file_id="stray.txt")
    assert exc.value.status_code == 404


def test_without_auth_there_is_no_owner_to_check(env):
    sr, _ = env
    row = sr.create_material_record(None, "d-None", file_id="bob-notes.txt")
    assert row["char_count"] > 30
