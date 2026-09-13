"""scripts/fetch_pyodide.py must never put an unverified byte where the app serves it.

The browser's Python runtime is served from static/lib/pyodide/<version>/ and is
fetched rather than committed (26.8 MB). Whatever lands in that directory is
executed in every user's browser, so the fetch has one job above convenience:
a file either matches the pinned manifest exactly, or it is not written.

These cover the manifest's own validation (a tampered manifest must not be able
to write outside the runtime directory), the download's verify-before-rename
discipline, the retry policy (network errors retried; a wrong hash never, since
more attempts at the wrong bytes cannot produce the right ones), and the exit
codes the Docker build relies on.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fetch_pyodide", REPO / "scripts" / "fetch_pyodide.py")
fp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fp)


def _meta(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _manifest(files: dict) -> dict:
    return {"version": "9.9.9", "source": "https://cdn.example/pyodide/",
            "files": {name: _meta(data) for name, data in files.items()}}


def _write(tmp_path: Path, manifest: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


class _Opener:
    """Stands in for urllib.request.urlopen: each call yields the next payload,
    raising it if it is an exception."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def __call__(self, url, timeout=None):
        self.calls += 1
        payload = self.payloads.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return io.BytesIO(payload)


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["../evil.js", "sub/evil.js", "..", "sub\\evil.js", ""])
def test_a_manifest_cannot_name_a_path_outside_the_runtime_dir(tmp_path, name):
    manifest = _manifest({"ok.js": b"x"})
    manifest["files"][name] = _meta(b"y")
    with pytest.raises(ValueError):
        fp.load_manifest(_write(tmp_path, manifest))


@pytest.mark.parametrize("source", [
    "http://cdn.example/pyodide/",     # not TLS
    "https://cdn.example/pyodide",     # not a directory: names would be glued on
    "file:///etc/",
])
def test_a_manifest_source_must_be_an_https_directory(tmp_path, source):
    manifest = _manifest({"ok.js": b"x"})
    manifest["source"] = source
    with pytest.raises(ValueError):
        fp.load_manifest(_write(tmp_path, manifest))


def test_a_manifest_entry_needs_a_real_hash(tmp_path):
    manifest = _manifest({"ok.js": b"x"})
    manifest["files"]["ok.js"]["sha256"] = "abc"
    with pytest.raises(ValueError):
        fp.load_manifest(_write(tmp_path, manifest))


def test_the_committed_manifest_is_well_formed():
    manifest = fp.load_manifest()
    assert manifest["source"].startswith("https://")
    assert len(manifest["files"]) == 18
    for meta in manifest["files"].values():
        assert len(meta["sha256"]) == 64 and meta["size"] > 0


def test_it_pins_every_file_the_loader_needs_at_startup():
    """The script tag, plus the three requests the CSP blocked, plus the
    interpreter's JS glue -- all needed before any package can load."""
    files = fp.load_manifest()["files"]
    for name in ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm",
                 "python_stdlib.zip", "pyodide-lock.json"):
        assert name in files, f"{name} is missing; the runtime cannot start without it"


def test_it_pins_the_plotting_stack():
    names = list(fp.load_manifest()["files"])
    for package in ("matplotlib-", "numpy-", "matplotlib_pyodide-", "pillow-", "kiwisolver-"):
        assert any(n.startswith(package) and n.endswith(".whl") for n in names), (
            f"no {package}*.whl pinned; a plot would fail to import"
        )


# --------------------------------------------------------------------------
# Download: verify before it becomes visible
# --------------------------------------------------------------------------

def test_verified_bytes_land_and_leave_no_partial_file(tmp_path, monkeypatch):
    data = b"runtime" * 1000
    monkeypatch.setattr(fp.urllib.request, "urlopen", _Opener([data]))
    target = tmp_path / "pyodide.js"

    fp.download("https://cdn.example/pyodide.js", target, _meta(data))

    assert target.read_bytes() == data
    assert not (tmp_path / "pyodide.js.part").exists()


def test_wrong_bytes_are_rejected_and_nothing_is_left_to_serve(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.urllib.request, "urlopen", _Opener([b"tampered"]))
    target = tmp_path / "pyodide.js"

    with pytest.raises(fp.IntegrityError):
        fp.download("https://cdn.example/pyodide.js", target, _meta(b"genuine"))

    assert not target.exists(), "a file that failed verification was left in place"
    assert not (tmp_path / "pyodide.js.part").exists()


def test_a_wrong_hash_is_never_retried(tmp_path, monkeypatch):
    opener = _Opener([b"tampered"] * fp.ATTEMPTS)
    monkeypatch.setattr(fp.urllib.request, "urlopen", opener)
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)

    with pytest.raises(fp.IntegrityError):
        fp.download("https://cdn.example/x", tmp_path / "x", _meta(b"genuine"))

    assert opener.calls == 1


def test_a_right_size_wrong_content_is_still_rejected(tmp_path, monkeypatch):
    """Size alone is not verification."""
    monkeypatch.setattr(fp.urllib.request, "urlopen", _Opener([b"evil"]))
    with pytest.raises(fp.IntegrityError):
        fp.download("https://cdn.example/x", tmp_path / "x", _meta(b"good"))


def test_network_errors_are_retried_then_reported(tmp_path, monkeypatch):
    opener = _Opener([urllib.error.URLError("down")] * fp.ATTEMPTS)
    monkeypatch.setattr(fp.urllib.request, "urlopen", opener)
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)
    target = tmp_path / "x"

    with pytest.raises(fp.NetworkError):
        fp.download("https://cdn.example/x", target, _meta(b"data"))

    assert opener.calls == fp.ATTEMPTS
    assert not target.exists()
    assert not (tmp_path / "x.part").exists()


def test_a_transient_failure_then_success_lands_the_file(tmp_path, monkeypatch):
    data = b"data"
    opener = _Opener([urllib.error.URLError("blip"), data])
    monkeypatch.setattr(fp.urllib.request, "urlopen", opener)
    monkeypatch.setattr(fp.time, "sleep", lambda s: None)
    target = tmp_path / "x"

    fp.download("https://cdn.example/x", target, _meta(data))

    assert target.read_bytes() == data
    assert opener.calls == 2


# --------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------

def test_check_mode_reports_without_fetching(tmp_path):
    manifest = _manifest({"a.js": b"aaa", "b.wasm": b"bbb", "c.zip": b"ccc"})
    (tmp_path / "a.js").write_bytes(b"aaa")            # valid
    (tmp_path / "b.wasm").write_bytes(b"tampered")     # corrupt
    fetched = []

    report = fp.sync(manifest, tmp_path, fetch=False,
                     downloader=lambda *a: fetched.append(a))

    assert report["ok"] == ["a.js"]
    assert report["bad"] == ["b.wasm"]
    assert report["missing"] == ["c.zip"]
    assert fetched == [], "--check downloaded something"


def test_fetch_mode_replaces_a_corrupt_file(tmp_path):
    manifest = _manifest({"b.wasm": b"bbb"})
    (tmp_path / "b.wasm").write_bytes(b"tampered")

    def downloader(url, target, meta):
        assert url == "https://cdn.example/pyodide/b.wasm"
        target.write_bytes(b"bbb")

    report = fp.sync(manifest, tmp_path, fetch=True, downloader=downloader)

    assert report["fetched"] == ["b.wasm"]
    assert (tmp_path / "b.wasm").read_bytes() == b"bbb"


def test_already_verified_files_are_not_downloaded_again(tmp_path):
    manifest = _manifest({"a.js": b"aaa"})
    (tmp_path / "a.js").write_bytes(b"aaa")
    report = fp.sync(manifest, tmp_path, fetch=True,
                     downloader=lambda *a: pytest.fail("re-downloaded a verified file"))
    assert report["ok"] == ["a.js"]


# --------------------------------------------------------------------------
# Exit codes the Docker build depends on
# --------------------------------------------------------------------------

def _point_at(monkeypatch, manifest, dest):
    monkeypatch.setattr(fp, "load_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(fp, "destination", lambda *a, **k: dest)


def test_check_fails_when_the_runtime_is_absent_and_says_how_to_fix_it(
        tmp_path, monkeypatch, capsys):
    _point_at(monkeypatch, _manifest({"pyodide.js": b"x"}), tmp_path / "absent")

    assert fp.main(["--check"]) == 1
    assert "fetch_pyodide.py" in capsys.readouterr().err
    assert not (tmp_path / "absent").exists(), "--check created the directory"


def test_check_passes_when_everything_verifies(tmp_path, monkeypatch):
    dest = tmp_path / "rt"
    dest.mkdir()
    (dest / "pyodide.js").write_bytes(b"x")
    _point_at(monkeypatch, _manifest({"pyodide.js": b"x"}), dest)

    assert fp.main(["--check"]) == 0


def test_an_integrity_failure_fails_the_build(tmp_path, monkeypatch):
    _point_at(monkeypatch, _manifest({"pyodide.js": b"x"}), tmp_path / "rt")

    def boom(*a, **k):
        raise fp.IntegrityError("tampered")

    monkeypatch.setattr(fp, "sync", boom)
    assert fp.main([]) == 1


def test_a_network_failure_is_distinguishable_from_tampering(tmp_path, monkeypatch):
    _point_at(monkeypatch, _manifest({"pyodide.js": b"x"}), tmp_path / "rt")

    def down(*a, **k):
        raise fp.NetworkError("unreachable")

    monkeypatch.setattr(fp, "sync", down)
    assert fp.main([]) == 2


# --------------------------------------------------------------------------
# The copy in this checkout, when one has been fetched
# --------------------------------------------------------------------------

_LOCAL = fp.destination(fp.load_manifest())


@pytest.mark.skipif(not _LOCAL.exists(), reason="runtime not fetched in this checkout")
def test_a_fetched_local_runtime_verifies_against_the_manifest():
    report = fp.sync(fp.load_manifest(), _LOCAL, fetch=False)
    assert not report["missing"] and not report["bad"], report


@pytest.mark.skipif(not (_LOCAL / "pyodide-lock.json").exists(),
                    reason="runtime not fetched in this checkout")
def test_every_pinned_wheel_agrees_with_the_runtime_lock_file():
    """Two records of the same fact must agree: the manifest's wheel hashes and
    the ones Pyodide itself will check at loadPackage time."""
    lock = json.loads((_LOCAL / "pyodide-lock.json").read_text(encoding="utf-8"))
    by_file = {p["file_name"]: p["sha256"] for p in lock["packages"].values()}
    for name, meta in fp.load_manifest()["files"].items():
        if name.endswith(".whl"):
            assert by_file.get(name) == meta["sha256"], name
