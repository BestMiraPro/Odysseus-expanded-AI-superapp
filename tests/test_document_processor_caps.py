"""The text-extraction cap is overridable so the Study module can keep the
full document while chat keeps its context-protecting default."""
import os
import tempfile

from src.document_processor import _process_text_file, _truncate_inline


def _tmp_txt(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, text.encode("utf-8"))
    os.close(fd)
    return path


def test_text_default_caps_long_file():
    path = _tmp_txt("x" * 40000)
    try:
        out = _process_text_file(path)            # default -1 -> 30k cap
        assert out.count("x") < 40000
    finally:
        os.remove(path)


def test_text_max_chars_none_keeps_full():
    path = _tmp_txt("y" * 40000)
    try:
        out = _process_text_file(path, max_chars=None)   # Study path
        assert out.count("y") >= 40000                   # full content kept
    finally:
        os.remove(path)


def test_text_explicit_cap():
    path = _tmp_txt("z" * 5000)
    try:
        out = _process_text_file(path, max_chars=1000)
        assert out.count("z") <= 1100               # ~cap (+ boundary scan slack)
    finally:
        os.remove(path)


def test_truncate_inline_caps_and_marks():
    body, marker = _truncate_inline("a" * 20000, 15000)
    assert len(body) == 15000 and marker
    body2, marker2 = _truncate_inline("short", 15000)
    assert body2 == "short" and marker2 == ""
