"""Study transfer: scope gating, owner filtering, re-owning, merge and rollback.

Two SQLite databases stand in for the two machines. The pull's network fetch
is pointed at the source app's TestClient, with the bearer-token state the
real auth middleware would stamp.
"""

import json
from datetime import datetime

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import routes.study_transfer_routes as st
from core.database import (
    Base, StudyCard, StudyDeck, StudyMaterial, StudyQuestion, StudyReview,
    StudyUserParams,
)


def _db(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}", poolclass=NullPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _seed_source(Session):
    s = Session()
    for owner, deck in (("dinis", "d1"), ("bob", "d2")):
        s.add(StudyDeck(id=deck, owner=owner, name=f"Deck {deck}"))
        s.add(StudyCard(id=f"c-{deck}", owner=owner, deck_id=deck, front="F", back="B",
                        state="review", stability="4.2", difficulty="5.0",
                        due=datetime(2026, 10, 9, 8, 0), reps=3))
        s.add(StudyReview(id=f"r-{deck}", owner=owner, card_id=f"c-{deck}", deck_id=deck,
                          rating=3, reviewed_at=datetime(2026, 9, 30, 21, 0)))
    s.add(StudyMaterial(id="m1", owner="dinis", deck_id="d1", name="PS1", kind="pdf",
                        file_id="aaaa.pdf", content="text"))
    s.add(StudyQuestion(id="q1", owner="dinis", deck_id="d1", material_id="m1", question="Q?"))
    s.add(StudyUserParams(id="p1", owner="dinis", w_json=json.dumps([0.1] * 17)))
    s.commit()
    s.close()


def _source_app(Session, monkeypatch, scopes, owner="dinis"):
    app = FastAPI()

    @app.middleware("http")
    async def _token(request: Request, call_next):
        request.state.api_token = True
        request.state.api_token_owner = owner
        request.state.api_token_scopes = scopes
        request.state.current_user = "api"
        return await call_next(request)

    app.include_router(st.setup_study_transfer_routes())
    return TestClient(app)


def test_export_needs_study_scope(tmp_path, monkeypatch):
    src = _db(tmp_path, "src.db")
    _seed_source(src)
    monkeypatch.setattr(st, "SessionLocal", src)
    for scopes in (["memory:read"], ["chat"], []):
        r = _source_app(src, monkeypatch, scopes).get("/api/study-transfer/export")
        assert r.status_code == 403, scopes


def test_export_returns_only_token_owner_rows(tmp_path, monkeypatch):
    src = _db(tmp_path, "src.db")
    _seed_source(src)
    monkeypatch.setattr(st, "SessionLocal", src)
    monkeypatch.setattr(st, "_figures_dir", lambda mid: str(tmp_path / "nofigs" / mid))
    out = _source_app(src, monkeypatch, ["study:read"]).get("/api/study-transfer/export").json()
    assert out["kind"] == st.TRANSFER_KIND
    assert [d["id"] for d in out["tables"]["decks"]] == ["d1"]
    assert [c["id"] for c in out["tables"]["cards"]] == ["c-d1"]
    assert out["tables"]["cards"][0]["due"] == "2026-10-09T08:00:00"
    assert out["files"] == [{"material_id": "m1", "has_file": True, "figures": []}]


def test_material_file_is_owner_checked(tmp_path, monkeypatch):
    src = _db(tmp_path, "src.db")
    _seed_source(src)
    monkeypatch.setattr(st, "SessionLocal", src)
    r = _source_app(src, monkeypatch, ["study:read"], owner="bob").get(
        "/api/study-transfer/material-file/m1")
    assert r.status_code == 404


class _Uploads:
    max_upload_size = 10 * 1024 * 1024

    def __init__(self):
        self.saved = []
        self.ids = set()

    def save_upload(self, u, client_ip, owner=None):
        self.saved.append((u.filename, u.file.read(), owner))
        self.ids.add("bbbb.pdf")
        return {"id": "bbbb.pdf"}


def _pull_setup(tmp_path, monkeypatch, src, dst, user="admin"):
    """Destination app whose fetches are served by the source app."""
    uploads = _Uploads()
    pdf = tmp_path / "aaaa.pdf"
    pdf.write_bytes(b"%PDF-1.4 problem set")
    import routes.study._common as sc
    # The source's file, plus whatever the destination's upload handler saved.
    def resolve(fid):
        if fid == "aaaa.pdf" or fid in {saved_id for saved_id in uploads.ids}:
            return str(pdf)
        raise HTTPException(404, "Uploaded file not found")
    monkeypatch.setattr(sc, "_resolve_uploaded_file", resolve)
    monkeypatch.setattr(st, "_figures_dir", lambda mid: str(tmp_path / "figs" / mid))
    source = _source_app(src, monkeypatch, ["study:read"])

    async def fake_fetch(url, token, *, params=None, max_bytes=0, scope=""):
        path = "/" + url.split("/", 3)[3]
        monkeypatch.setattr(st, "SessionLocal", src)
        try:
            r = source.get(path, params=params)
        finally:
            monkeypatch.setattr(st, "SessionLocal", dst)
        if r.status_code != 200:
            raise HTTPException(502, f"Source returned HTTP {r.status_code}")
        return r.content

    monkeypatch.setattr(st, "fetch_from_source", fake_fetch)
    monkeypatch.setattr(st, "SessionLocal", dst)
    monkeypatch.setattr(st, "require_admin", lambda request: None)
    monkeypatch.setattr(st, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(st.setup_study_transfer_routes(upload_handler=uploads))
    return TestClient(app), uploads


def test_pull_copies_everything_reowned_and_is_idempotent(tmp_path, monkeypatch):
    src, dst = _db(tmp_path, "src.db"), _db(tmp_path, "dst.db")
    _seed_source(src)
    client, uploads = _pull_setup(tmp_path, monkeypatch, src, dst)
    body = {"source_url": "192.168.1.20:7000", "token": "ody_abc"}

    preview = client.post("/api/study-transfer/pull", json={**body, "dry_run": True}).json()
    assert preview["dry_run"] and preview["files"] == 1
    s = dst()
    assert s.query(StudyDeck).count() == 0
    s.close()

    out = client.post("/api/study-transfer/pull", json=body).json()
    added = {x["name"]: x["added"] for x in out["sections"]}
    assert added["subjects"] == 1 and added["flashcards"] == 1 and added["practice questions"] == 1
    assert added["card reviews"] == 1 and added["fitted FSRS weights"] == 1
    assert out["files_added"] == 1 and out["failed"] == []
    assert uploads.saved == [("PS1.pdf", b"%PDF-1.4 problem set", "admin")]

    s = dst()
    card = s.query(StudyCard).one()
    assert card.owner == "admin" and card.stability == "4.2"
    assert card.due == datetime(2026, 10, 9, 8, 0)
    assert s.query(StudyMaterial).one().file_id == "bbbb.pdf"
    assert {d.owner for d in s.query(StudyDeck).all()} == {"admin"}
    s.close()

    again = client.post("/api/study-transfer/pull", json=body).json()
    assert sum(x["added"] for x in again["sections"]) == 0
    assert again["files_added"] == 0

    # A file that went missing here is fetched again on the next pull.
    uploads.ids.clear()
    retry = client.post("/api/study-transfer/pull", json=body).json()
    assert sum(x["added"] for x in retry["sections"]) == 0
    assert retry["files_added"] == 1


def test_local_fsrs_weights_are_kept(tmp_path, monkeypatch):
    src, dst = _db(tmp_path, "src.db"), _db(tmp_path, "dst.db")
    _seed_source(src)
    s = dst()
    s.add(StudyUserParams(id="local", owner="admin", w_json="[9]"))
    s.commit()
    s.close()
    client, _ = _pull_setup(tmp_path, monkeypatch, src, dst)
    client.post("/api/study-transfer/pull", json={"source_url": "h", "token": "ody_abc"})
    s = dst()
    assert [p.id for p in s.query(StudyUserParams).all()] == ["local"]
    s.close()


def test_failed_insert_rolls_back_everything(tmp_path, monkeypatch):
    src, dst = _db(tmp_path, "src.db"), _db(tmp_path, "dst.db")
    _seed_source(src)
    client, _ = _pull_setup(tmp_path, monkeypatch, src, dst)
    real = st._dict_to_row

    def broken(model, row, owner):
        out = real(model, row, owner)
        if model is StudyCard:
            out["front"] = None  # NOT NULL: the insert fails after decks went in
        return out

    monkeypatch.setattr(st, "_dict_to_row", broken)
    r = client.post("/api/study-transfer/pull", json={"source_url": "h", "token": "ody_abc"})
    assert r.status_code == 500 and "rolled back" in r.json()["detail"]
    s = dst()
    assert s.query(StudyDeck).count() == 0
    s.close()


def test_malformed_export_writes_nothing():
    with pytest.raises(HTTPException) as e:
        st._validate_tables({"decks": [{"name": "no id"}]})
    assert e.value.status_code == 502
    with pytest.raises(HTTPException):
        st._validate_tables({"cards": "not a list"})


def test_newer_source_columns_are_dropped():
    row = st._dict_to_row(StudyDeck, {"id": "d", "name": "N", "owner": "x",
                                      "column_from_the_future": 1}, "admin")
    assert row == {"id": "d", "name": "N", "owner": "admin"}


def test_pull_requires_ody_token(tmp_path, monkeypatch):
    src, dst = _db(tmp_path, "src.db"), _db(tmp_path, "dst.db")
    client, _ = _pull_setup(tmp_path, monkeypatch, src, dst)
    r = client.post("/api/study-transfer/pull", json={"source_url": "h", "token": "secret"})
    assert r.status_code == 400


# ── bundle (file) transfer: no network between the machines ──────────────

import io
import zipfile


def _bundle_setup(tmp_path, monkeypatch, src):
    """Write a bundle from the source DB as the source machine would."""
    pdf = tmp_path / "aaaa.pdf"
    pdf.write_bytes(b"%PDF-1.4 problem set")
    figs = tmp_path / "srcfigs"
    (figs / "m1").mkdir(parents=True)
    (figs / "m1" / "0.jpg").write_bytes(b"\xff\xd8figure\xff\xd9")
    import routes.study._common as sc
    monkeypatch.setattr(sc, "_resolve_uploaded_file", lambda fid: str(pdf) if fid == "aaaa.pdf" else (_ for _ in ()).throw(HTTPException(404)))
    monkeypatch.setattr(st, "SessionLocal", src)
    monkeypatch.setattr(st, "_figures_dir", lambda mid: str(figs / mid))
    out = tmp_path / "bundle.zip"
    counts = st.write_bundle("dinis", str(out))
    return out, counts


def _import_client(tmp_path, monkeypatch, dst, user="admin"):
    uploads = _Uploads()
    import routes.study._common as sc
    monkeypatch.setattr(sc, "_resolve_uploaded_file", lambda fid: fid if fid in uploads.ids else (_ for _ in ()).throw(HTTPException(404)))
    monkeypatch.setattr(st, "SessionLocal", dst)
    # Real figure-path logic, rooted in a temp uploads dir.
    import src.constants as constants
    monkeypatch.setattr(constants, "UPLOAD_DIR", str(tmp_path / "dstuploads"))
    monkeypatch.setattr(st, "_figures_dir", st.__dict__["_real_figures_dir"])
    monkeypatch.setattr(st, "require_admin", lambda request: None)
    monkeypatch.setattr(st, "get_current_user", lambda request: user)
    app = FastAPI()
    app.include_router(st.setup_study_transfer_routes(upload_handler=uploads))
    return TestClient(app), uploads


def test_bundle_round_trip(tmp_path, monkeypatch):
    src, dst = _db(tmp_path, "src.db"), _db(tmp_path, "dst.db")
    _seed_source(src)
    bundle, counts = _bundle_setup(tmp_path, monkeypatch, src)
    assert counts == {"files": 1, "figures": 1, "missing": 0}
    with zipfile.ZipFile(bundle) as zf:
        assert sorted(zf.namelist()) == ["figures/m1/0.jpg", "files/m1", "study.json"]
        manifest = json.loads(zf.read("study.json"))
    assert [d["id"] for d in manifest["tables"]["decks"]] == ["d1"]  # owner-filtered

    client, uploads = _import_client(tmp_path, monkeypatch, dst)
    data = bundle.read_bytes()
    preview = client.post("/api/study-transfer/import", data={"dry_run": "true"},
                          files={"bundle": ("odysseus-study.zip", data, "application/zip")}).json()
    assert preview["dry_run"] and preview["files"] == 1
    s = dst()
    assert s.query(StudyDeck).count() == 0
    s.close()

    out = client.post("/api/study-transfer/import",
                      files={"bundle": ("odysseus-study.zip", data, "application/zip")}).json()
    added = {x["name"]: x["added"] for x in out["sections"]}
    assert added["subjects"] == 1 and added["flashcards"] == 1 and added["card reviews"] == 1
    assert out["files_added"] == 1 and out["figures_added"] == 1 and out["failed"] == []
    assert uploads.saved == [("PS1.pdf", b"%PDF-1.4 problem set", "admin")]
    assert (tmp_path / "dstuploads" / ".study_figures" / "m1" / "0.jpg").read_bytes() == b"\xff\xd8figure\xff\xd9"
    s = dst()
    card = s.query(StudyCard).one()
    assert card.owner == "admin" and card.stability == "4.2" and card.due == datetime(2026, 10, 9, 8, 0)
    assert s.query(StudyMaterial).one().file_id == "bbbb.pdf"
    s.close()

    again = client.post("/api/study-transfer/import",
                        files={"bundle": ("odysseus-study.zip", data, "application/zip")}).json()
    assert sum(x["added"] for x in again["sections"]) == 0 and again["files_added"] == 0


def test_bundle_download_route_is_owner_scoped(tmp_path, monkeypatch):
    src = _db(tmp_path, "src.db")
    _seed_source(src)
    _bundle_setup(tmp_path, monkeypatch, src)
    r = _source_app(src, monkeypatch, ["study:read"], owner="bob").get("/api/study-transfer/bundle")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        manifest = json.loads(zf.read("study.json"))
    assert [d["id"] for d in manifest["tables"]["decks"]] == ["d2"]
    assert "files/m1" not in zf.namelist()
    r = _source_app(src, monkeypatch, ["memory:read"]).get("/api/study-transfer/bundle")
    assert r.status_code == 403


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("data,needle", [
    (b"not a zip at all", "not a Study bundle"),
    (_zip({"other.json": "{}"}), "no study.json"),
    (_zip({"study.json": "{nope"}), "not valid JSON"),
    (_zip({"study.json": json.dumps({"kind": "odysseus-memory-transfer"})}), "not a Study bundle"),
])
def test_bad_bundles_are_rejected_and_write_nothing(tmp_path, monkeypatch, data, needle):
    dst = _db(tmp_path, "dst.db")
    client, _ = _import_client(tmp_path, monkeypatch, dst)
    r = client.post("/api/study-transfer/import", files={"bundle": ("x.zip", data, "application/zip")})
    assert r.status_code == 400 and needle in r.json()["detail"]
    s = dst()
    assert s.query(StudyDeck).count() == 0
    s.close()


def test_crafted_member_names_are_never_used_as_paths(tmp_path, monkeypatch):
    # A zip carrying traversal names alongside a manifest whose material id
    # is itself a traversal: the importer only opens names it builds from
    # ids (basename'd), and figures land under the figures root.
    manifest = {"kind": st.TRANSFER_KIND, "tables": {
        "decks": [{"id": "d9", "name": "D"}],
        "materials": [{"id": "../../evil", "deck_id": "d9", "name": "x", "file_id": "f.pdf"}],
    }, "files": [{"material_id": "../../evil", "has_file": True, "figures": [0]}]}
    data = _zip({"study.json": json.dumps(manifest),
                 "../../evil.txt": "pwned", "files/evil": "%PDF", "figures/evil/0.jpg": "img"})
    work = tmp_path / "work"
    work.mkdir()
    dst = _db(work, "dst.db")
    client, _ = _import_client(work, monkeypatch, dst)
    client.post("/api/study-transfer/import", files={"bundle": ("x.zip", data, "application/zip")})
    root = work / "dstuploads" / ".study_figures"
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".jpg", ".txt")]
    assert written == [root / "evil" / "0.jpg"]


def test_oversized_member_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "MAX_BUNDLE_MEMBER_BYTES", 10)
    zf = zipfile.ZipFile(io.BytesIO(_zip({"study.json": json.dumps({"kind": st.TRANSFER_KIND, "pad": "x" * 50})})))
    with pytest.raises(HTTPException) as e:
        st._read_member(zf, "study.json")
    assert "too large" in e.value.detail
