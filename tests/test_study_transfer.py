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
