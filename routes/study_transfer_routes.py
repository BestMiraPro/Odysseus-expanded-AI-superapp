"""Study transfer — copy a user's whole Study app from another machine.

Same shape as the memory transfer (routes/memory_transfer_routes.py):

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

Merging is by row id: a row whose id already exists here is left alone, so a
second pull only adds what is new and never overwrites progress made on this
machine. Rows are re-owned to the pulling user (the source's usernames mean
nothing here). The database part is one transaction, so a failure leaves no
half-imported subject behind. Files follow, each saved through the normal
upload handler; a file that fails is reported and its material still has the
extracted text it was studied from.
"""

import asyncio
import io
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
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


def setup_study_transfer_routes(upload_handler=None) -> APIRouter:
    router = APIRouter(prefix="/api/study-transfer", tags=["study-transfer"])

    # ── source side ────────────────────────────────────────────────────

    @router.get("/export")
    def export_study(request: Request):
        owner = _export_owner(request)
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
            fig_dir = _figures_dir(m["id"])
            try:
                entry["figures"] = sorted(
                    int(match.group(1)) for f in os.listdir(fig_dir)
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

    async def _pull_files(base, token, materials, files, user) -> Dict[str, Any]:
        """Bring over each new material's PDF and figures. Best-effort per file."""
        report = {"files_added": 0, "figures_added": 0, "failed": []}
        by_id = {m["id"]: m for m in materials}
        max_file = getattr(upload_handler, "max_upload_size", 512 * 1024 * 1024)
        for entry in files:
            mid = entry.get("material_id")
            material = by_id.get(mid)
            if material is None:
                continue  # already here with its file; keep the local copy
            if entry.get("has_file") and upload_handler is not None:
                try:
                    data = await fetch_from_source(
                        f"{base}/api/study-transfer/material-file/{mid}", token,
                        max_bytes=max_file, scope="study:read")
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
                    data = await fetch_from_source(
                        f"{base}/api/study-transfer/figure/{mid}/{idx}", token,
                        max_bytes=32 * 1024 * 1024, scope="study:read")
                    os.makedirs(out_dir, exist_ok=True)
                    with open(target, "wb") as fh:
                        fh.write(data)
                    report["figures_added"] += 1
                except Exception:
                    report["failed"].append({"material": material.get("name"), "reason": f"figure {idx}"})
        return report

    @router.post("/pull")
    async def pull_study(request: Request, body: dict = Body(default_factory=dict)):
        require_admin(request)
        user = get_current_user(request)

        base = source_base_url(body.get("source_url"))
        token = body.get("token")
        if not isinstance(token, str) or not token.strip().startswith("ody_"):
            raise HTTPException(400, "An Odysseus API token (starts with ody_) is required")
        token = token.strip()
        dry_run = bool(body.get("dry_run", False))

        raw = await fetch_from_source(
            f"{base}/api/study-transfer/export", token,
            max_bytes=MAX_EXPORT_BYTES, scope="study:read")
        payload = parse_transfer_payload(raw, TRANSFER_KIND)
        tables = _validate_tables(payload.get("tables"))
        files = payload.get("files") if isinstance(payload.get("files"), list) else []

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
                return {
                    "ok": True, "dry_run": True, "source": base,
                    "exported_at": payload.get("exported_at"),
                    "sections": sections,
                    "files": sum(
                        1 for f in files
                        if isinstance(f, dict) and f.get("has_file")
                        and f.get("material_id") in new_material_ids | {m["id"] for m in retry}
                    ),
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

        file_report = await _pull_files(
            base, token, new["materials"] + retry,
            [f for f in files if isinstance(f, dict)], user)
        logger.info(
            "Study transfer from %s: %s; files %d, figures %d, %d failed",
            base, ", ".join(f"{s['name']} +{s['added']}" for s in sections),
            file_report["files_added"], file_report["figures_added"], len(file_report["failed"]),
        )
        return {
            "ok": True, "source": base,
            "exported_at": payload.get("exported_at"),
            "sections": sections,
            **file_report,
        }

    return router
