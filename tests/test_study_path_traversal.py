"""Regression test confirming _resolve_uploaded_file rejects path-traversal.

The function already uses:
  - basename() to strip any path
  - realpath() on the resolved candidate
  - _confined() prefix check against the uploads root

This test locks in that behaviour.
"""

from __future__ import annotations

import os
import pytest
import tempfile

from routes.study_routes import _resolve_uploaded_file
from fastapi import HTTPException


@pytest.fixture
def isolated_upload_dir(monkeypatch):
    """Provide a temporary directory as UPLOAD_DIR."""
    with tempfile.TemporaryDirectory() as upload_dir:
        monkeypatch.setattr("src.constants.UPLOAD_DIR", upload_dir)
        yield upload_dir


def test_basename_strips_traversal(isolated_upload_dir):
    """Passing a file_id with '..' or '/' yields only the basename."""
    upload_dir = isolated_upload_dir
    safe_file = os.path.join(upload_dir, "myfile.pdf")
    with open(safe_file, "w") as fh:
        fh.write("content")

    # A traversal payload should resolve to the safe file in the same directory
    # or fail entirely — never escape.
    payload = "../../../etc/passwd"
    with pytest.raises(HTTPException) as exc_info:
        _resolve_uploaded_file(payload)
    assert exc_info.value.status_code == 404


def test_confined_prefix_check_rejects_outside(isolated_upload_dir):
    """Even if the basename matches, the resolved path must be inside UPLOAD_DIR."""
    upload_dir = isolated_upload_dir
    outside = tempfile.NamedTemporaryFile(delete=False)
    outside.close()
    try:
        # Symlink attack attempt: basename is the symlink name, realpath
        # resolves outside UPLOAD_DIR. _confined rejects it.
        symlink = os.path.join(upload_dir, "link.pdf")
        try:
            os.symlink(outside.name, symlink)
        except OSError:
            pytest.skip("Symlinks not supported in this temp dir")

        with pytest.raises(HTTPException) as exc_info:
            _resolve_uploaded_file("link.pdf")
        assert exc_info.value.status_code == 404
    finally:
        os.unlink(outside.name)
