"""Study transfer — copy a user's whole Study app from another machine.

Two ways, with the same merge rules:

* **Bundle (no network between the machines).** ``GET /api/study-transfer/bundle``
  on the old machine downloads one .zip; ``POST /api/study-transfer/import``
  on the new machine merges it. Carry the file on a USB stick or a cloud
  drive. Neither machine has to accept a connection from the other, which is
  what you want on a shared or public network.
* **Network pull**, shaped like the memory transfer
  (routes/memory_transfer_routes.py), when the old machine is reachable:

* ``GET  /api/study-transfer/export`` — SOURCE machine. Every Study row the
  caller owns: subjects, cards, practice questions, materials, exams and
  plans, focus sessions, review and attempt history, fitted FSRS weights and
  Study-agent chats, plus a list of the files those materials point at.
  Reachable with an ``ody_`` API token that has ``study:read``.
* ``GET  /api/study-transfer/material-file/{id}`` and
  ``/api/study-transfer/figure/{id}/{idx}`` — SOURCE machine. The uploaded
  PDF behind a material and its extracted figures.
* ``POST /api/study-transfer/pull`` — DESTINATION machine, admin only. Fetches
  all of the above and merges it in.

Both merge paths are admin only. The bundle download is available to the
logged-in user for their own rows (or an ``ody_`` token with ``study:read``).

Merging is by row id: a row whose id already exists here is left alone, so a
second pull or import only adds what is new and never overwrites progress made on this
machine. Rows are re-owned to the pulling user (the source's usernames mean
nothing here). The database part is one transaction, so a failure leaves no
half-imported subject behind. Files follow, each saved through the normal
upload handler; a file that fails is reported and its material still has the
extracted text it was studied from.
"""

import asyncio
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import DateTime

from core.database import (
    SessionLocal,
    StudyAgentMessage,
    StudyAgentThread,
    StudyAttempt,
    StudyCard,
    StudyDeck,
    StudyExam,
    StudyFocusSession,
    StudyMaterial,
    StudyQuestion,
    StudyReview,
    StudyUserParams,
)
from core.middleware import require_admin
from routes.codex_routes import _scope_owner
from routes.memory_transfer_routes import (
    fetch_from_source,
    parse_transfer_payload,
    source_base_url,
)
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)

TRANSFER_KIND = "odysseus-study-transfer"
TRANSFER_VERSION = 1
STUDY_READ_SCOPES = {"study:read"}
# Study rows are text, but a big question bank with its attempt history and
# agent chats is far larger than a memory store.
MAX_EXPORT_BYTES = 256 * 1024 * 1024

# Insert order: parents before the rows that point at them (decks before
# cards/materials/questions, exams before focus sessions that cite them).
TABLES = (
    ("decks", StudyDeck),
    ("exams", StudyExam),
    ("materials", StudyMaterial),
    ("cards", StudyCard),
    ("questions", StudyQuestion),
    ("focus_sessions", StudyFocusSession),
    ("reviews", StudyReview),
    ("attempts", StudyAttempt),
    ("user_params", StudyUserParams),
    ("agent_threads", StudyAgentThread),
    ("agent_messages", StudyAgentMessage),
)
# What the Study app calls each table, for the preview and the result.
LABELS = {
    "decks": "subjects", "exams": "exams and plans", "materials": "materials",
    "cards": "flashcards", "questions": "practice questions",
    "focus_sessions": "focus sessions", "reviews": "card reviews",
    "attempts": "question attempts", "user_params": "fitted FSRS weights",
    "agent_threads": "Study chats", "agent_messages": "Study chat messages",
}
_FIGURE_RE = re.compile(r"^(\d+)\.jpg$")


def _figures_dir(material_id: str) -> str:
    from routes.study._common import _study_figures_dir
    return _study_figures_dir(material_id)


_real_figures_dir = _figures_dir  # tests patch _figures_dir and restore this


def _export_owner(request: Request):
    """API tokens need study:read; browser sessions read the logged-in user's
    rows (None with auth disabled, which means every row — as Study does)."""
    if getattr(request.state, "api_token", False):
        return _scope_owner(request, STUDY_READ_SCOPES)
    _scope_owner(request, STUDY_READ_SCOPES)  # require_user for cookie callers
    return get_current_user(request)


def _row_to_dict(model, obj) -> Dict[str, Any]:
    out = {}
    for col in model.__table__.columns:
        value = getattr(obj, col.key, None)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        out[col.key] = value
    return out


def _dict_to_row(model, row: Dict[str, Any], owner) -> Dict[str, Any]:
    """Keep only this build's columns, parse datetimes, re-own the row.

    Columns the source has and this build lacks are dropped, so a newer source
    can still be pulled into an older install.
    """
    out = {}
    for col in model.__table__.columns:
        if col.key not in row:
            continue
        value = row[col.key]
        if value is not None and isinstance(col.type, DateTime):
            if not isinstance(value, str):
                raise HTTPException(400, f"Bad {model.__tablename__}.{col.key} value")
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise HTTPException(400, f"Bad {model.__tablename__}.{col.key} value")
        out[col.key] = value
    out["owner"] = owner
    return out


def _validate_tables(tables: Any) -> Dict[str, List[dict]]:
    if not isinstance(tables, dict):
        raise HTTPException(502, "Source sent a malformed Study export")
    clean = {}
    for name, _model in TABLES:
        rows = tables.get(name) or []
        if not isinstance(rows, list):
            raise HTTPException(502, f"Source sent a malformed Study export ({name})")
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise HTTPException(502, f"Source sent a malformed Study export ({name} row {i})")
        clean[name] = rows
    return clean


def _existing_ids(db, model, ids: List[str]) -> set:
    found = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        found.update(r[0] for r in db.query(model.id).filter(model.id.in_(chunk)).all())
    return found


def _plan(db, tables: Dict[str, List[dict]], user) -> Dict[str, List[dict]]:
    """Which incoming rows are new here. A user's fitted FSRS weights are one
    row per owner, so local weights win over the source's."""
    new = {}
    for name, model in TABLES:
        rows = tables[name]
        have = _existing_ids(db, model, [r["id"] for r in rows])
        fresh = [r for r in rows if r["id"] not in have]
        if model is StudyUserParams and fresh:
            q = db.query(StudyUserParams.id)
            q = q.filter(StudyUserParams.owner == user) if user is not None else q.filter(StudyUserParams.owner.is_(None))
            if q.first():
                fresh = []
            else:
                fresh = fresh[:1]
        new[name] = fresh
    return new


def _upload_name(material: Dict[str, Any]) -> str:
    """A filename that keeps the upload's extension, so type detection works."""
    file_id = str(material.get("file_id") or "")
    ext = os.path.splitext(file_id)[1]
    name = os.path.basename(str(material.get("name") or "")) or file_id or "material"
    if ext and not name.lower().endswith(ext.lower()):
        name += ext
    return name


def _materials_missing_files(db, source_materials, new_ids, user) -> List[dict]:
    """Already-imported materials whose file never arrived (a failed earlier
    pull), so pulling again retries just those files."""
    from routes.study._common import _resolve_uploaded_file
    out = []
    for m in source_materials:
        if m["id"] in new_ids or not m.get("file_id"):
            continue
        local = db.query(StudyMaterial).filter(StudyMaterial.id == m["id"]).first()
        if local is None or (user is not None and local.owner != user):
            continue
        try:
            if local.file_id:
                _resolve_uploaded_file(local.file_id)
                continue
        except HTTPException:
            pass
        out.append(m)
    return out


def build_export(owner) -> Dict[str, Any]:
    """Every Study row ``owner`` has (all rows when None), plus which materials
    carry a file and figures. The payload both the network export and the
    bundle carry."""
    db = SessionLocal()
    try:
        tables = {}
        for name, model in TABLES:
            q = db.query(model)
            if owner is not None:
                q = q.filter(model.owner == owner)
            tables[name] = [_row_to_dict(model, obj) for obj in q.all()]
    finally:
        db.close()

    files = []
    for m in tables["materials"]:
        entry = {"material_id": m["id"], "has_file": bool(m.get("file_id")), "figures": []}
        try:
            entry["figures"] = sorted(
                int(match.group(1)) for f in os.listdir(_figures_dir(m["id"]))
                if (match := _FIGURE_RE.match(f))
            )
        except OSError:
            pass
        if entry["has_file"] or entry["figures"]:
            files.append(entry)

    return {
        "kind": TRANSFER_KIND,
        "version": TRANSFER_VERSION,
        "exported_at": datetime.now().isoformat(),
        "tables": tables,
        "files": files,
    }


# ── bundle (file) format ──────────────────────────────────────────────
#
# A zip so it can be carried on a USB stick or a cloud drive, with no network
# path between the machines at all:
#   study.json                    the export payload above
#   files/<material_id>           the material's uploaded file
#   figures/<material_id>/<n>.jpg extracted figures
# The importer only ever opens these exact names (built from ids it parsed),
# never paths taken from the archive, so a crafted zip cannot write anywhere.
BUNDLE_MANIFEST = "study.json"
MAX_BUNDLE_BYTES = int(os.getenv("ODYSSEUS_STUDY_BUNDLE_MAX_BYTES", str(4 * 1024 ** 3)))
MAX_BUNDLE_MEMBER_BYTES = 1024 ** 3


def _bundle_file_name(material_id: str) -> str:
    return f"files/{os.path.basename(material_id)}"


def _bundle_figure_name(material_id: str, idx: int) -> str:
    return f"figures/{os.path.basename(material_id)}/{int(idx)}.jpg"


def write_bundle(owner, out_path: str) -> Dict[str, int]:
    from routes.study._common import _resolve_uploaded_file
    payload = build_export(owner)
    counts = {"files": 0, "figures": 0, "missing": 0}
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        file_ids = {m["id"]: m.get("file_id") for m in payload["tables"]["materials"]}
        for entry in payload["files"]:
            mid = entry["material_id"]
            if entry.get("has_file"):
                try:
                    zf.write(_resolve_uploaded_file(file_ids[mid]), _bundle_file_name(mid))
                    counts["files"] += 1
                except HTTPException:
                    entry["has_file"] = False  # gone on this machine; text still travels
                    counts["missing"] += 1
            for idx in entry.get("figures") or []:
                path = os.path.join(_figures_dir(mid), f"{int(idx)}.jpg")
                if os.path.isfile(path):
                    zf.write(path, _bundle_figure_name(mid, idx))
                    counts["figures"] += 1
        payload["bundle"] = True
        zf.writestr(BUNDLE_MANIFEST, json.dumps(payload, ensure_ascii=False))
    return counts


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise HTTPException(404, f"{name} is not in the bundle")
    if info.file_size > MAX_BUNDLE_MEMBER_BYTES:
        raise HTTPException(400, f"{name} in the bundle is too large")
    with zf.open(info) as fh:
        data = fh.read(MAX_BUNDLE_MEMBER_BYTES + 1)
    if len(data) > MAX_BUNDLE_MEMBER_BYTES:
        raise HTTPException(400, f"{name} in the bundle is too large")
    return data


def open_bundle(fileobj) -> tuple:
    """Open an uploaded bundle and return (zipfile, payload)."""
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile:
        raise HTTPException(400, "That file is not a Study bundle (expected the .zip from Download Study Bundle)")
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_BUNDLE_BYTES:
        raise HTTPException(400, "Bundle is larger than the import limit")
    raw = _read_member(zf, BUNDLE_MANIFEST) if BUNDLE_MANIFEST in zf.namelist() else None
    if raw is None:
        raise HTTPException(400, "That file is not a Study bundle (no study.json inside)")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "The bundle's study.json is not valid JSON")
    if not isinstance(payload, dict) or payload.get("kind") != TRANSFER_KIND:
        raise HTTPException(400, "That file is not a Study bundle")
    return zf, payload


def setup_study_transfer_routes(upload_handler=None) -> APIRouter:
    router = APIRouter(prefix="/api/study-transfer", tags=["study-transfer"])

    # ── source side ────────────────────────────────────────────────────

    @router.get("/export")
    def export_study(request: Request):
        return build_export(_export_owner(request))

    @router.get("/bundle")
    def download_bundle(request: Request):
        """The whole Study app as one .zip, to carry to another machine."""
        owner = _export_owner(request)
        fd, path = tempfile.mkstemp(prefix="odysseus-study-", suffix=".zip")
        os.close(fd)
        try:
            write_bundle(owner, path)
        except Exception:
            os.unlink(path)
            raise
        name = f"odysseus-study-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
        return FileResponse(path, media_type="application/zip", filename=name,
                            background=BackgroundTask(os.unlink, path))

    def _owned_material(request: Request, material_id: str):
        owner = _export_owner(request)
        db = SessionLocal()
        try:
            m = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
            if m is None or (owner is not None and m.owner != owner):
                raise HTTPException(404, "Material not found")
            return m.file_id
        finally:
            db.close()

    @router.get("/material-file/{material_id}")
    def export_material_file(request: Request, material_id: str):
        file_id = _owned_material(request, material_id)
        if not file_id:
            raise HTTPException(404, "Material has no file")
        from routes.study._common import _resolve_uploaded_file
        return FileResponse(_resolve_uploaded_file(file_id))

    @router.get("/figure/{material_id}/{idx}")
    def export_figure(request: Request, material_id: str, idx: int):
        _owned_material(request, material_id)
        path = os.path.join(_figures_dir(material_id), f"{int(idx)}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, "Figure not found")
        return FileResponse(path, media_type="image/jpeg")

    # ── destination side ───────────────────────────────────────────────

    async def _copy_files(get_file, get_figure, materials, files, user) -> Dict[str, Any]:
        """Bring over each material's file and figures. Best-effort per file.

        ``get_file(mid)`` / ``get_figure(mid, idx)`` return bytes: from the
        network for a pull, from the zip for a bundle.
        """
        report = {"files_added": 0, "figures_added": 0, "failed": []}
        by_id = {m["id"]: m for m in materials}
        for entry in files:
            mid = entry.get("material_id")
            material = by_id.get(mid)
            if material is None:
                continue  # already here with its file; keep the local copy
            if entry.get("has_file") and upload_handler is not None:
                try:
                    data = await get_file(mid)
                    upload = UploadFile(file=io.BytesIO(data), filename=_upload_name(material))
                    # Rate limiting is per client key; one key per material keeps
                    # a large library from tripping the 60-a-minute browser cap.
                    saved = await asyncio.to_thread(
                        upload_handler.save_upload, upload, f"study-transfer:{mid}", user)
                    db = SessionLocal()
                    try:
                        db.query(StudyMaterial).filter(StudyMaterial.id == mid).update(
                            {"file_id": saved["id"]}, synchronize_session=False)
                        db.commit()
                    finally:
                        db.close()
                    report["files_added"] += 1
                except HTTPException as e:
                    report["failed"].append({"material": material.get("name"), "reason": e.detail})
                except Exception as e:
                    logger.warning("Study transfer: file for %s failed: %s", mid, type(e).__name__)
                    report["failed"].append({"material": material.get("name"), "reason": "could not save the file"})
            for idx in entry.get("figures") or []:
                try:
                    idx = int(idx)
                    out_dir = _figures_dir(mid)
                    target = os.path.join(out_dir, f"{idx}.jpg")
                    if os.path.exists(target):
                        continue
                    data = await get_figure(mid, idx)
                    os.makedirs(out_dir, exist_ok=True)
                    with open(target, "wb") as fh:
                        fh.write(data)
                    report["figures_added"] += 1
                except Exception:
                    report["failed"].append({"material": material.get("name"), "reason": f"figure {idx}"})
        return report

    async def _apply(payload, user, get_file, get_figure, dry_run: bool, source: str) -> Dict[str, Any]:
        """Merge an export payload into this machine. Shared by pull and import."""
        tables = _validate_tables(payload.get("tables"))
        files = [f for f in payload.get("files") or [] if isinstance(f, dict)] \
            if isinstance(payload.get("files"), list) else []

        db = SessionLocal()
        try:
            new = _plan(db, tables, user)
            new_material_ids = {m["id"] for m in new["materials"]}
            retry = _materials_missing_files(db, tables["materials"], new_material_ids, user)
            sections = [
                {"name": LABELS[name], "added": len(new[name]),
                 "skipped": len(tables[name]) - len(new[name])}
                for name, _m in TABLES
            ]
            if dry_run:
                wanted = new_material_ids | {m["id"] for m in retry}
                return {
                    "ok": True, "dry_run": True, "source": source,
                    "exported_at": payload.get("exported_at"),
                    "sections": sections,
                    "files": sum(1 for f in files
                                 if f.get("has_file") and f.get("material_id") in wanted),
                }
            # One transaction for every table: all of it lands or none of it.
            for name, model in TABLES:
                rows = [_dict_to_row(model, r, user) for r in new[name]]
                if rows:
                    db.execute(model.__table__.insert(), rows)
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.exception("Study transfer import failed")
            raise HTTPException(500, f"Study import failed and was rolled back ({type(e).__name__}). Nothing was saved.")
        finally:
            db.close()

        file_report = await _copy_files(get_file, get_figure, new["materials"] + retry, files, user)
        logger.info(
            "Study transfer from %s: %s; files %d, figures %d, %d failed",
            source, ", ".join(f"{s['name']} +{s['added']}" for s in sections),
            file_report["files_added"], file_report["figures_added"], len(file_report["failed"]),
        )
        return {
            "ok": True, "source": source,
            "exported_at": payload.get("exported_at"),
            "sections": sections,
            **file_report,
        }

    @router.post("/pull")
    async def pull_study(request: Request, body: dict = Body(default_factory=dict)):
        """Fetch another machine's Study export over the network and merge it."""
        require_admin(request)
        user = get_current_user(request)

        base = source_base_url(body.get("source_url"))
        token = body.get("token")
        if not isinstance(token, str) or not token.strip().startswith("ody_"):
            raise HTTPException(400, "An Odysseus API token (starts with ody_) is required")
        token = token.strip()

        raw = await fetch_from_source(
            f"{base}/api/study-transfer/export", token,
            max_bytes=MAX_EXPORT_BYTES, scope="study:read")
        payload = parse_transfer_payload(raw, TRANSFER_KIND)
        max_file = getattr(upload_handler, "max_upload_size", 512 * 1024 * 1024)

        async def get_file(mid):
            return await fetch_from_source(
                f"{base}/api/study-transfer/material-file/{mid}", token,
                max_bytes=max_file, scope="study:read")

        async def get_figure(mid, idx):
            return await fetch_from_source(
                f"{base}/api/study-transfer/figure/{mid}/{idx}", token,
                max_bytes=32 * 1024 * 1024, scope="study:read")

        return await _apply(payload, user, get_file, get_figure,
                            bool(body.get("dry_run", False)), base)

    @router.post("/import")
    async def import_bundle(request: Request, bundle: UploadFile = File(...),
                            dry_run: bool = Form(False)):
        """Merge a bundle made by Download Study Bundle on another machine."""
        require_admin(request)
        user = get_current_user(request)
        zf, payload = await asyncio.to_thread(open_bundle, bundle.file)
        try:
            async def get_file(mid):
                return await asyncio.to_thread(_read_member, zf, _bundle_file_name(mid))

            async def get_figure(mid, idx):
                return await asyncio.to_thread(_read_member, zf, _bundle_figure_name(mid, idx))

            return await _apply(payload, user, get_file, get_figure, dry_run,
                                bundle.filename or "bundle")
        finally:
            zf.close()

    return router
