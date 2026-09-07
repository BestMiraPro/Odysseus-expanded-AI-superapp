"""Backup routes — export/import user data (memories, presets, settings, skills, preferences)."""

import json
import logging
import math
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from core.middleware import require_admin
from services.memory import MemoryStoreUnreadable
from src.auth_helpers import get_current_user
from src.settings import load_settings, save_settings, load_features, save_features

logger = logging.getLogger(__name__)

# Sections import_data knows how to apply, in the order it applies them.
# Anything else in the payload is ignored, so a newer export can be restored
# by an older build without being rejected outright.
_IMPORT_SECTIONS = (
    "memories", "skills", "presets", "settings", "features", "preferences",
)
# Row fields the importer calls string methods on. A non-string here used to
# raise AttributeError partway through the import.
_MEMORY_STR_FIELDS = ("text", "owner")

# The complete accepted shape for a skill row, grouped by what the store can
# actually consume (services/memory/skills.py, add_skill).
#
# Scalar strings. 'id' is included because the importer tests it for set
# membership (`sid in existing_ids`), which raises on an unhashable value.
_SKILL_STR_FIELDS = (
    "id", "title", "name", "description", "problem", "solution", "owner",
    "category", "when_to_use", "teacher_model", "status", "version", "source",
)
# add_skill wraps each of these in list(). list(42) raises; list("biology")
# does something worse — it silently yields seven single-character tags — so a
# bare string is rejected rather than accepted as a one-element list.
_SKILL_LIST_FIELDS = (
    "tags", "steps", "procedure", "pitfalls", "verification",
    "platforms", "requires_toolsets", "fallback_for_toolsets",
)
# add_skill calls float(confidence). Skills treat confidence as a 0..1 score
# (the store's own defaults are 0.8 and 0.5), so anything outside that is a
# corrupt export rather than a usable value.
_CONFIDENCE_MIN, _CONFIDENCE_MAX = 0.0, 1.0


def _reject(section: str, detail: str, index=None):
    where = f"{section}[{index}]" if index is not None else section
    raise HTTPException(400, f"Invalid backup: {where} — {detail}")


def _check_str_fields(row, fields, section, index):
    for field in fields:
        value = row.get(field)
        # Absent and null are fine; the importer already handles both.
        # bool is an int subclass, so it is rejected here rather than being
        # coerced into a surprising string later.
        if value is None or isinstance(value, str):
            continue
        _reject(section, f"{field!r} must be a string, got {type(value).__name__}", index)


def _check_list_of_str(row, fields, section, index):
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            _reject(section, f"{field!r} must be a list of strings, not a string "
                             f"(it would be split into characters)", index)
        if not isinstance(value, list):
            _reject(section, f"{field!r} must be a list of strings, "
                             f"got {type(value).__name__}", index)
        for position, item in enumerate(value):
            if not isinstance(item, str):
                _reject(section, f"{field!r}[{position}] must be a string, "
                                 f"got {type(item).__name__}", index)


def _check_confidence(row, section, index):
    value = row.get("confidence")
    if value is None:
        return
    # bool is an int subclass; True would silently become 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(section, f"'confidence' must be a number, "
                         f"got {type(value).__name__}", index)
    if not math.isfinite(value):
        _reject(section, "'confidence' must be a finite number", index)
    if not (_CONFIDENCE_MIN <= value <= _CONFIDENCE_MAX):
        _reject(section, f"'confidence' must be between {_CONFIDENCE_MIN} and "
                         f"{_CONFIDENCE_MAX}, got {value}", index)


def _check_memory_row(row, section, index):
    _check_str_fields(row, _MEMORY_STR_FIELDS, section, index)


def _check_skill_row(row, section, index):
    _check_str_fields(row, _SKILL_STR_FIELDS, section, index)
    _check_list_of_str(row, _SKILL_LIST_FIELDS, section, index)
    _check_confidence(row, section, index)


def _check_rows(body, section, check_row):
    rows = body[section]
    if not isinstance(rows, list):
        _reject(section, f"expected a list, got {type(rows).__name__}")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            _reject(section, f"expected an object, got {type(row).__name__}", index)
        check_row(row, section, index)


def _check_str_keyed_object(body, section):
    obj = body[section]
    if not isinstance(obj, dict):
        _reject(section, f"expected an object, got {type(obj).__name__}")
    for key in obj:
        if not isinstance(key, str):
            _reject(section, f"keys must be strings, got {type(key).__name__}")


def _validate_import_payload(body: dict) -> None:
    """Validate every recognised section before any of them is written.

    Import applies sections sequentially to different stores, so there is no
    transaction to roll back. Validating the whole payload first is what makes
    a malformed file a no-op instead of a half-applied restore.

    Partial-import policy: once validation passes, sections are written in
    ``_IMPORT_SECTIONS`` order. A failure after that point is *usually*
    infrastructure (unwritable store, full disk), but it can also mean this
    validator missed a shape the store rejects — so the handler reports what
    it observed rather than asserting a cause, and names the sections already
    applied so the operator knows the restore is incomplete.
    """
    if "memories" in body:
        _check_rows(body, "memories", _check_memory_row)
    if "skills" in body:
        _check_rows(body, "skills", _check_skill_row)
    if "presets" in body:
        _check_str_keyed_object(body, "presets")
        for key, value in body["presets"].items():
            if not isinstance(value, (dict, list)):
                _reject("presets", f"{key!r} must be an object or a list, "
                                   f"got {type(value).__name__}")
    for section in ("settings", "features", "preferences"):
        if section in body:
            _check_str_keyed_object(body, section)



# How each recognised section is applied, and whether it affects data shared
# with other users. Surfaced by the preview so an operator sees what a restore
# will actually change before it changes it.
_SECTION_BEHAVIOUR = {
    "memories":    {"mode": "merge",            "shared": False},
    "skills":      {"mode": "merge",            "shared": False},
    "presets":     {"mode": "replace-matching", "shared": True},
    "settings":    {"mode": "merge",            "shared": True},
    "features":    {"mode": "merge",            "shared": True},
    "preferences": {"mode": "merge",            "shared": False},
}


def _section_count(body, section):
    value = body.get(section)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0


def summarise_import_payload(body: dict) -> dict:
    """Describe what an import of this payload would do. Writes nothing."""
    sections = []
    for name in _IMPORT_SECTIONS:
        if name not in body:
            continue
        behaviour = _SECTION_BEHAVIOUR[name]
        sections.append({
            "name": name,
            "count": _section_count(body, name),
            "mode": behaviour["mode"],
            "shared": behaviour["shared"],
        })
    ignored = sorted(
        key for key in body
        if key not in _IMPORT_SECTIONS
        and key not in ("version", "exported_at", "exported_by")
    )
    return {
        "version": body.get("version"),
        "exported_at": body.get("exported_at"),
        "exported_by": body.get("exported_by"),
        "sections": sections,
        "ignored": ignored,
    }


def setup_backup_routes(memory_manager, preset_manager, skills_manager) -> APIRouter:
    router = APIRouter(tags=["backup"])

    @router.get("/api/export")
    async def export_data(request: Request):
        """Export all user data as a downloadable JSON file."""
        require_admin(request)
        user = get_current_user(request)

        # Memories (filtered by owner when auth is enabled)
        memories = memory_manager.load(owner=user)

        # Presets (shared across users — export all)
        presets = preset_manager.get_all()

        # Skills (filtered by owner when auth is enabled)
        skills = skills_manager.load(owner=user)

        # Settings
        settings = load_settings()

        # Feature flags
        features = load_features()

        # User preferences
        from routes.prefs_routes import _load_for_user
        preferences = _load_for_user(user)

        export_data = {
            "version": 1,
            "exported_at": datetime.now().isoformat(),
            "exported_by": user,
            "memories": memories,
            "presets": presets,
            "skills": skills,
            "settings": settings,
            "features": features,
            "preferences": preferences,
        }

        filename = f"odysseus_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            content=json.dumps(export_data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @router.post("/api/import/preview")
    async def preview_import(request: Request):
        """Describe what importing this file would change, without writing.

        Uses the same validator as the import, so a file this accepts is a file
        the import will accept — a preview that disagreed with the real thing
        would be worse than none.
        """
        require_admin(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")

        _validate_import_payload(body)
        summary = summarise_import_payload(body)
        summary["ok"] = bool(summary["sections"])
        if not summary["ok"]:
            summary["message"] = "No recognized data found in the file"
        return summary

    @router.post("/api/import")
    async def import_data(request: Request):
        """Import user data from a previously exported JSON file. Merges with existing data."""
        require_admin(request)
        user = get_current_user(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        if not isinstance(body, dict):
            raise HTTPException(400, "Expected a JSON object")

        # Validate the whole payload before touching any store. Sections are
        # applied sequentially to independent stores with no shared
        # transaction, so this pass is what keeps a malformed file from
        # leaving a half-applied restore behind.
        _validate_import_payload(body)

        imported = []
        # Updated as rows land, not after a section completes, so a failure
        # partway through can report what was actually written.
        progress = {}

        def _progress(name, **counts):
            progress.setdefault(name, {"name": name, "added": 0, "skipped": 0})
            progress[name].update(counts)

        try:

            # ── Memories ──
            if "memories" in body and isinstance(body["memories"], list):
                # Strict load: importing on top of an unreadable store would write
                # only the incoming rows back and drop everything already saved.
                try:
                    existing = memory_manager.load_all_for_update()
                except MemoryStoreUnreadable as e:
                    logger.error("Refusing to import memories: %s", e)
                    raise HTTPException(
                        503, "Memory store is temporarily unreadable — nothing was imported."
                    )
                # Dedup against THIS user's own memories only. Using every tenant's
                # rows (load_all) meant a memory whose text matched any other
                # user's was silently skipped, so the importing user lost their own
                # data. The full store is still saved back below.
                existing_texts = {e.get("text", "").strip().lower()
                                  for e in existing if e.get("owner") == user}
                added = 0
                for mem in body["memories"]:
                    if not isinstance(mem, dict) or not mem.get("text"):
                        continue
                    if mem["text"].strip().lower() in existing_texts:
                        continue  # skip duplicates
                    # Assign owner when auth is enabled
                    if user and not mem.get("owner"):
                        mem["owner"] = user
                    existing.append(mem)
                    existing_texts.add(mem["text"].strip().lower())
                    added += 1
                memory_manager.save(existing)
                _progress("memories", added=added,
                          skipped=len(body["memories"]) - added)
                imported.append(f"{added} memories")

            # ── Skills ──
            if "skills" in body and isinstance(body["skills"], list):
                existing = skills_manager.load_all()
                # Dedup against THIS user's own skills only. Using every tenant's
                # rows (load_all) meant a skill whose id/name/title matched any
                # other user's was silently skipped, so the importing user lost
                # their own data — same cross-tenant bug fixed for memories above.
                # The full store is still saved back below.
                own = [s for s in existing if s.get("owner") == user]
                existing_names = {s.get("name") for s in own if s.get("name")}
                existing_ids = {s.get("id") for s in own if s.get("id")}
                existing_titles = {
                    (s.get("title") or s.get("description") or "").strip().lower()
                    for s in own
                }
                added = 0
                for skill in body["skills"]:
                    if not isinstance(skill, dict):
                        continue
                    title = (
                        skill.get("title") or skill.get("description")
                        or skill.get("name") or ""
                    ).strip()
                    if not title:
                        continue
                    sid = skill.get("id") or skill.get("name")
                    if sid and sid in existing_ids:
                        continue
                    nm = skill.get("name")
                    if nm and nm in existing_names:
                        continue
                    if title.lower() in existing_titles:
                        continue
                    owner = skill.get("owner")
                    if user and not owner:
                        owner = user
                    # Skills live on disk as SKILL.md files; the old JSON-era
                    # skills_manager.save() no longer exists. Write each new skill
                    # via add_skill (source="user" skips auto-dedup — this is an
                    # explicit backup restore).
                    result = skills_manager.add_skill(
                        title=title,
                        name=skill.get("name"),
                        description=skill.get("description"),
                        problem=skill.get("problem", ""),
                        solution=skill.get("solution", ""),
                        steps=skill.get("steps"),
                        tags=skill.get("tags"),
                        source="user",
                        teacher_model=skill.get("teacher_model"),
                        confidence=skill.get("confidence", 0.8),
                        owner=owner,
                        category=skill.get("category", "general"),
                        when_to_use=skill.get("when_to_use"),
                        procedure=skill.get("procedure"),
                        pitfalls=skill.get("pitfalls"),
                        verification=skill.get("verification"),
                        platforms=skill.get("platforms"),
                        requires_toolsets=skill.get("requires_toolsets"),
                        fallback_for_toolsets=skill.get("fallback_for_toolsets"),
                        status=skill.get("status", "draft"),
                        version=skill.get("version", "1.0.0"),
                    )
                    if result.get("_deduped"):
                        continue
                    if result.get("name"):
                        existing_names.add(result["name"])
                    if result.get("id"):
                        existing_ids.add(result["id"])
                    existing_titles.add(title.lower())
                    added += 1
                    _progress("skills", added=added)
                _progress("skills", added=added,
                          skipped=len(body["skills"]) - added)
                imported.append(f"{added} skills")

            # ── Presets ──
            if "presets" in body and isinstance(body["presets"], dict):
                current = preset_manager.get_all()
                for key, value in body["presets"].items():
                    if isinstance(value, dict):
                        current[key] = value
                    elif isinstance(value, list):
                        current[key] = value
                preset_manager.save(current)
                _progress("presets", added=1)
                imported.append("presets")

            # ── Settings ──
            if "settings" in body and isinstance(body["settings"], dict):
                current = load_settings()
                current.update(body["settings"])
                save_settings(current)
                _progress("settings", added=1)
                imported.append("settings")

            # ── Features ──
            if "features" in body and isinstance(body["features"], dict):
                current = load_features()
                current.update(body["features"])
                save_features(current)
                _progress("features", added=1)
                imported.append("features")

            # ── Preferences ──
            if "preferences" in body and isinstance(body["preferences"], dict):
                from routes.prefs_routes import _load_for_user, _save_for_user
                current = _load_for_user(user)
                current.update(body["preferences"])
                _save_for_user(user, current)
                _progress("preferences", added=1)
                imported.append("preferences")

        except HTTPException:
            raise
        except Exception as e:
            # Validation passed, so this is most likely infrastructure (an
            # unwritable store, a full disk) — but it can equally mean the
            # validator missed a shape the store rejects, which is how the
            # skill-field gaps went unnoticed. Don't assert a cause. Sections
            # apply to independent stores with no shared transaction, so name
            # what landed: the restore is incomplete and the operator needs to
            # know where it stopped.
            logger.exception("Import failed after applying: %s", progress)
            landed = ", ".join(
                f"{p['name']}: {p['added']} saved" for p in progress.values()
            ) or "nothing"
            raise HTTPException(
                500,
                f"Import failed partway through: {e}. Already applied: "
                f"{landed}. Re-run the import once the underlying problem is "
                f"fixed; records already saved are skipped as duplicates.",
            )

        if not imported:
            return {"ok": False, "message": "No recognized data found in the file"}

        return {
            "ok": True,
            # Legacy fields: older clients read these two.
            "imported": imported,
            "message": f"Imported: {', '.join(imported)}",
            "sections": list(progress.values()),
        }

    return router
