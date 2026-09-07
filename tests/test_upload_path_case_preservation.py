"""Reserving an upload must not rewrite its recorded path.

``reserve_upload`` deduplicated candidate rows through a set of
``os.path.normcase(os.path.realpath(stored_path))`` — the right key for
comparison — and then returned that *key* as the upload's path.
``os.path.normcase`` lowercases on Windows, so the reservation replaced the
recorded path with a lowercased copy.

Two consequences, neither visible on Linux where normcase is a no-op:

* the stored metadata no longer matches the real filename;
* ``path_changed = current_info.get("path") != path`` became true on every
  reservation, rewriting the index each time.

The earlier failure was read as Windows fixture noise. It was a real defect.
"""

from __future__ import annotations

import json
import os

import pytest

from src.upload_handler import UploadHandler


UPLOAD_ID = "a" * 32


@pytest.fixture
def handler_with_upload(tmp_path):
    """An indexed upload whose directory has deliberate mixed case."""
    upload_dir = tmp_path / "UpLoads" / "2026" / "06" / "09"
    upload_dir.mkdir(parents=True)
    upload_path = upload_dir / f"{UPLOAD_ID}.txt"
    upload_path.write_text("contents", encoding="utf-8")

    root = tmp_path / "UpLoads"
    handler = UploadHandler(str(tmp_path), str(root))
    handler._atomic_write_json(
        str(root / "uploads.json"),
        {
            "alice:hash-a": {
                "id": f"{UPLOAD_ID}.txt",
                "path": str(upload_path),
                "mime": "text/plain",
                "size": upload_path.stat().st_size,
                "name": "note.txt",
                "hash": "hash-a",
                "original_name": "note.txt",
                "uploaded_at": "2026-06-09T10:00:00",
                "last_accessed": "2026-06-09T10:00:00",
                "client_ip": "127.0.0.1",
                "owner": "alice",
            },
        },
    )
    return handler, root, upload_path


def test_the_resolved_path_keeps_its_original_case(handler_with_upload):
    handler, _root, upload_path = handler_with_upload

    resolved = handler.resolve_upload(f"{UPLOAD_ID}.txt", owner="alice")

    assert resolved is not None
    assert resolved["path"] == str(upload_path), (
        "the reservation returned a case-folded path instead of the stored one"
    )


def test_the_stored_index_is_not_case_folded(handler_with_upload):
    handler, root, upload_path = handler_with_upload

    handler.resolve_upload(f"{UPLOAD_ID}.txt", owner="alice")

    index = json.loads((root / "uploads.json").read_text(encoding="utf-8"))
    stored = index["alice:hash-a"]["path"]
    assert stored == str(upload_path), (
        f"the index path was rewritten: {stored!r}"
    )


def test_the_resolved_path_still_points_at_the_real_file(handler_with_upload):
    handler, _root, upload_path = handler_with_upload

    resolved = handler.resolve_upload(f"{UPLOAD_ID}.txt", owner="alice")

    assert os.path.isfile(resolved["path"])
    assert os.path.samefile(resolved["path"], upload_path)


def test_repeated_reservations_are_stable(handler_with_upload):
    """path_changed must not fire every time and rewrite the index."""
    handler, root, _upload_path = handler_with_upload

    first = handler.resolve_upload(f"{UPLOAD_ID}.txt", owner="alice")["path"]
    after_first = (root / "uploads.json").read_text(encoding="utf-8")
    second = handler.resolve_upload(f"{UPLOAD_ID}.txt", owner="alice")["path"]
    after_second = (root / "uploads.json").read_text(encoding="utf-8")

    assert first == second
    assert after_first == after_second, "the index was rewritten by a no-op reservation"


def test_another_owner_still_cannot_resolve_it(handler_with_upload):
    """Preserving case must not loosen the ownership check."""
    handler, _root, _upload_path = handler_with_upload
    assert handler.resolve_upload(f"{UPLOAD_ID}.txt", owner="mallory") is None
