"""Study route sub-module: materials handlers (Phase 4.2 split)."""
from routes.study._common import *  # noqa: F401,F403
import routes.study._common as _common  # noqa: F401
from routes.study._common import _question_row_kwargs

from fastapi import APIRouter  # noqa: F401  (re-exported via _common but explicit)


def material_rows_with_counts(db, deck_id: str, user) -> List[Dict]:
    """A subject's materials (newest first) with *live* question counts.

    The stored ``question_count`` column never decremented on delete, so it is
    only a fallback; this counts from the questions table instead."""
    q = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
    if user is not None:
        q = q.filter(StudyMaterial.owner == user)
    mats = q.order_by(StudyMaterial.created_at.desc()).all()
    cq = db.query(StudyQuestion).filter(StudyQuestion.deck_id == deck_id)
    if user is not None:
        cq = cq.filter(StudyQuestion.owner == user)
    counts: Dict[str, int] = {}
    for row in cq.all():
        counts[row.material_id] = counts.get(row.material_id, 0) + 1
    return [_material_to_dict(m, counts.get(m.id, 0)) for m in mats]


def create_material_record(user, deck_id: str, *, name=None, text=None,
                           file_id=None, category=None) -> Dict:
    """Attach source material to a subject: pasted text or a previously
    uploaded file (text is extracted in full; scanned PDFs are kept with an
    empty text layer so vision extraction / transcription can read them).
    Shared by the route and the Study agent."""
    db = _common.SessionLocal()
    try:
        deck = study_service.get_deck(db, deck_id, user)
        page_count = None
        if file_id:
            kind = "pdf" if file_id.lower().endswith(".pdf") else "file"
            name = (name or file_id).strip()
            try:
                text = _common._extract_file_text(file_id, user)
            except HTTPException as e:
                # Scanned/image-only PDFs have no text layer - keep the
                # material anyway; vision extraction reads the pages.
                if kind == "pdf" and e.status_code == 422:
                    text = ""
                else:
                    raise
            if kind == "pdf":
                try:
                    from src.study_vision import pdf_page_count
                    page_count = pdf_page_count(_common._resolve_uploaded_file(file_id)) or None
                except Exception:
                    page_count = None
        else:
            text = (text or "").strip()
            kind = "text"
            name = (name or "").strip() or (
                text[:48] + "\u2026" if len(text) > 48 else text[:48])
        if len(text) < 30 and kind != "pdf":
            raise HTTPException(400, "Provide more material (at least a paragraph).")
        cat = category if category in MATERIAL_CATEGORIES else classify_material(name)
        m = StudyMaterial(
            id=str(uuid.uuid4()), owner=user, deck_id=deck.id,
            name=name[:200], kind=kind, file_id=file_id,
            content=text, char_count=len(text), page_count=page_count,
            category=cat,   # auto-tag on upload; the user can change it
        )
        db.add(m)
        db.commit()
        return _material_to_dict(m, 0)
    finally:
        db.close()


async def run_extraction(user, material_id: str, *, mode: str = "extract",
                         types=None, count: int = 15, vision=None) -> Dict:
    """AI question extraction: material -> saved question bank items.

    mode "extract" pulls the actual questions out of past papers / problem
    sets, faithfully; mode "author" writes new exam-style questions from
    notes. Long materials are chunked and partial results are kept (JSON
    repair recovers complete objects from malformed replies).

    Shared by the /materials/{id}/extract route and the Study agent, so
    both run one pipeline."""
    mode = mode if mode in ("extract", "author") else "extract"
    types = [t for t in (types or ["mcq", "open"]) if t in ("mcq", "open")] or ["mcq", "open"]
    db = _common.SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        deck_id = m.deck_id
        content = m.content or ""
        file_id = m.file_id
        kind = m.kind
    finally:
        db.close()

    # Locate the original PDF (vision mode and the auto-fallback need it).
    pdf_path = None
    if file_id and (kind == "pdf" or str(file_id).lower().endswith(".pdf")):
        try:
            pdf_path = _common._resolve_uploaded_file(file_id)
        except HTTPException:
            pdf_path = None

    # Vision by default whenever the original PDF is on disk and a vision-
    # capable model is configured; an explicit ``vision`` flag forces a path.
    use_vision = should_use_vision(
        has_pdf=bool(pdf_path),
        vision_available=bool(pdf_path) and bool(_common._vision_candidates(user)),
        explicit=vision)
    # Thin text layer (formula images / scans): go vision-first instead of
    # wasting a text pass on cover-page scraps.
    page_info = None
    auto_vision = False
    if pdf_path and not use_vision:
        try:
            from src.study_vision import pdf_page_count, text_layer_is_thin
            auto_vision = text_layer_is_thin(len(content), pdf_page_count(pdf_path))
        except Exception:
            auto_vision = False

    system = EXTRACT_QUESTIONS_SYSTEM if mode == "extract" else AUTHOR_QUESTIONS_SYSTEM
    type_note = ("Only produce questions of type: " + ", ".join(types) + ".") \
        if len(types) == 1 else ""

    async def _run_text_pass(text_chunks: List[str]) -> tuple:
        """Per-chunk text extraction. Returns (collected, raw_count, errors)."""
        per_chunk = (max(3, min(40, count) // len(text_chunks) + 1)
                     if mode == "author" else None)
        t_collected: List[Dict] = []
        t_raw = 0
        t_errors = 0
        for i, chunk in enumerate(text_chunks):
            if mode == "author":
                instruction = (f"Write about {per_chunk} questions from this material "
                               f"(part {i + 1}/{len(text_chunks)}). {type_note}")
            else:
                instruction = (f"Extract every practice question from this material "
                               f"(part {i + 1}/{len(text_chunks)}). {type_note}")
            value = None
            for attempt in range(2):
                strict = "" if attempt == 0 else (
                    "\n\nIMPORTANT: your previous reply was not valid JSON. Reply with "
                    "ONLY the JSON array - it must start with [ and end with ]. "
                    "No prose, no markdown, no explanations.")
                try:
                    value = await _common._llm_json(user, system,
                                            f"{instruction}{strict}\n\n--- MATERIAL ---\n{chunk}",
                                            temperature=0.2 if mode == "extract" else 0.5,
                                            max_tokens=EXTRACTION_MAX_TOKENS,
                                            timeout=300, thinking_off=True)
                    break
                except HTTPException as e:
                    if e.status_code == 503:
                        raise  # no model configured — fail loudly, not partially
                    logger.warning("study extract: chunk %d attempt %d failed: %s",
                                   i, attempt + 1, e.detail)
                    if attempt == 1:
                        t_errors += 1
            if value is not None:
                if isinstance(value, list):
                    t_raw += len(value)
                elif isinstance(value, dict):
                    t_raw += len(value.get("questions") or [value])
                t_collected.extend(normalize_questions(value))
        return t_collected, t_raw, t_errors

    def _finalize(items: List[Dict]) -> List[Dict]:
        """Type-filter, de-duplicate, and (in extract mode) drop conclusion-
        style 'questions' that leak their own answer."""
        qs = dedupe_questions([q for q in items if q["qtype"] in types])
        if mode == "extract":
            qs = [q for q in qs if not question_is_conclusion(q)]
        return qs

    used_vision = False
    coverage = None
    if use_vision or auto_vision:
        if not pdf_path:
            raise HTTPException(400, "Vision extraction needs the original PDF "
                                     "file. Re-upload the PDF to this subject.")
        collected, raw_count, n_batches, errors, coverage, page_info = \
            await _common._extract_questions_vision(user, mode, types, pdf_path)
        used_vision = True
        chunks = [None] * n_batches  # for the response chunk count
    else:
        chunks = chunk_material(content)
        if not chunks and pdf_path:
            # No text layer at all - skip straight to vision.
            collected, raw_count, n_batches, errors, coverage, page_info = \
                await _common._extract_questions_vision(user, mode, types, pdf_path)
            used_vision = True
            chunks = [None] * n_batches
        elif not chunks:
            raise HTTPException(400, "Material has no text to extract from.")
        else:
            collected, raw_count, errors = await _run_text_pass(chunks)
            # Coverage pass (text): compare against a discovery manifest
            # and re-request anything missed in one targeted call.
            if mode == "extract" and collected:
                manifest = await _discover_questions_text(user, content)
                page_by_number = _manifest_page_map(manifest)
                _attach_source_pages(collected, page_by_number)
                coverage = _coverage_report(manifest, collected)
                if coverage and coverage["missing"]:
                    nums = ", ".join(coverage["missing"])
                    logger.info("study coverage: re-requesting question(s) %s "
                                "(text)", nums)
                    try:
                        value = await _common._llm_json(
                            user, system,
                            f"A previous pass missed some questions. Extract "
                            f"ONLY question(s) {nums} from this material, "
                            f"faithfully and completely. {type_note}\n\n"
                            f"--- MATERIAL ---\n{content[:40000]}",
                            temperature=0.2, max_tokens=EXTRACTION_MAX_TOKENS,
                            timeout=300, thinking_off=True)
                        fresh = normalize_questions(value)
                        _attach_source_pages(fresh, page_by_number)
                        raw_count += len(fresh)
                        collected.extend(fresh)
                        coverage = _coverage_report(manifest, collected)
                    except HTTPException as e:
                        if e.status_code == 503:
                            raise
                        logger.warning("study coverage: targeted text pass "
                                       "failed: %s", e.detail)

    questions = _finalize(collected)

    # Auto-fallback: text extraction found nothing usable but we have the
    # original PDF — its text layer is probably thin (formula images,
    # scans). Try vision before giving up.
    if not questions and not used_vision and pdf_path:
        logger.info("study extract: text pass empty for material %s — "
                    "falling back to vision extraction", material_id)
        try:
            v_collected, v_raw, v_batches, v_errors, v_coverage, page_info = \
                await _common._extract_questions_vision(user, mode, types, pdf_path)
            if v_collected:
                collected, raw_count, errors = v_collected, v_raw, v_errors
                chunks = [None] * v_batches
                used_vision = True
                coverage = v_coverage
                questions = _finalize(collected)
        except HTTPException as e:
            logger.warning("study extract: vision fallback unavailable: %s", e.detail)

    # Mirror fallback: vision found nothing usable but the material HAS a
    # text layer (e.g. the vision model can't read images, or the user hit
    # "Extract (vision)" on a text-rich PDF). Try text before giving up.
    if not questions and used_vision:
        text_chunks = chunk_material(content)
        if text_chunks:
            logger.info("study extract: vision pass empty for material %s — "
                        "falling back to text extraction", material_id)
            try:
                t_collected, t_raw, t_errors = await _run_text_pass(text_chunks)
                if t_collected:
                    collected, raw_count, errors = t_collected, t_raw, t_errors
                    chunks = text_chunks
                    used_vision = False
                    coverage = None  # manifest came from the failed pass
                    questions = _finalize(collected)
            except HTTPException as e:
                logger.warning("study extract: text fallback failed: %s", e.detail)

    if mode == "extract" and questions:
        for q in questions:
            if not q.get("source_page"):
                page = infer_source_page(
                    content,
                    number=q.get("number"),
                    question=q.get("question"),
                )
                if page:
                    q["source_page"] = page

    if not questions:
        if errors >= len(chunks):
            detail = ("Every chunk failed: the model's replies were empty or "
                      "not parseable as JSON (the server log has the raw "
                      "replies). Empty replies usually mean the reply was "
                      "truncated mid-reasoning; retry, or switch the Study "
                      "model (the model selector in the top bar).")
        elif raw_count == 0:
            detail = ("The model returned valid JSON but found no questions in "
                      "this material. If it is notes rather than an exam, use "
                      "'Author questions' instead of 'Extract questions'.")
        elif types != ["mcq", "open"]:
            detail = (f"{raw_count} question(s) were found but none matched the "
                      f"requested type filter ({', '.join(types)}).")
        else:
            detail = (f"The model found {raw_count} question(s) but none were "
                      "usable (e.g. MCQs whose correct answer could not be "
                      "identified). Try again or switch the Study model.")
        raise HTTPException(502, detail)

    db = _common.SessionLocal()
    try:
        now = _common._utcnow_naive()
        # Cross-run dedupe: never re-add a question already in this deck, so
        # re-running extraction (or extracting overlapping materials) tops up
        # the bank instead of duplicating it.
        existing_keys = {
            question_key(text) for (text,) in
            db.query(StudyQuestion.question)
              .filter(StudyQuestion.deck_id == deck_id).all()
        }
        saved = []
        duplicates = 0
        for q in questions:
            key = question_key(q["question"])
            if key in existing_keys:
                duplicates += 1
                continue
            existing_keys.add(key)
            row = StudyQuestion(
                id=str(uuid.uuid4()), owner=user, deck_id=deck_id,
                material_id=material_id, qtype=q["qtype"],
                question=q["question"],
                context=q.get("context"),
                options=json.dumps(q["options"]) if q["options"] else None,
                correct_index=q["correct_index"], reference=q["reference"],
                topic=q["topic"], difficulty=q["difficulty"],
                number=q.get("number"),
                source_page=q.get("source_page"),
                origin="extracted" if mode == "extract" else "authored",
                state="new", due=now,
                **_question_row_kwargs(q),
            )
            db.add(row)
            saved.append(row)
        m = db.query(StudyMaterial).filter(StudyMaterial.id == material_id).first()
        if m:
            m.question_count = (m.question_count or 0) + len(saved)
        db.commit()
        original_links = _question_original_links(db, saved)
        resp = {
            "created": len(saved),
            "duplicates": duplicates,
            "chunks": len(chunks),
            "chunk_errors": errors,
            "vision": used_vision,
            # How much of a long PDF was actually read (MAX_PAGES cap).
            "pages": page_info,
            "coverage": coverage,
            "questions": [_question_to_dict(r, original=original_links.get(r.id))
                          for r in saved],
        }
        created_n = len(saved)
    finally:
        db.close()

    # Auto-link multi-part problems for this material so practice immediately
    # shows earlier parts + answers as context. Best-effort — an extraction
    # must never fail because grouping did.
    if created_n:
        try:
            await _common._link_deck_parts(user, deck_id, only_material=material_id)
        except Exception as e:
            logger.warning("study: auto link-parts after extraction failed: %s", e)
    return resp


async def run_transcribe_material(user, material_id: str) -> Dict:
    """Vision OCR for a scanned / formula-image PDF: transcribe every page to
    Markdown (LaTeX math), TRANSCRIBE_PAGES_PER_CALL pages per call, and store
    it as the material's text with ``[Page N text]:`` markers — the same shape
    the PDF text extractor produces — so notes, consult, explain-further and
    text extraction work on it. Returns {char_count, pages, pages_failed}."""
    from src.study_vision import pages_to_data_urls, render_pdf_pages

    db = _common.SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        if not m.file_id:
            raise HTTPException(400, "Only file-backed PDF materials can be transcribed.")
        file_id = m.file_id
    finally:
        db.close()
    pdf_path = _common._resolve_uploaded_file(file_id)
    try:
        urls = pages_to_data_urls(render_pdf_pages(pdf_path))
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    parts: List[str] = []
    failed = 0
    for start in range(0, len(urls), TRANSCRIBE_PAGES_PER_CALL):
        batch = urls[start:start + TRANSCRIBE_PAGES_PER_CALL]
        instruction = (f"Transcribe these {len(batch)} pages (pages {start + 1}-"
                       f"{start + len(batch)} of the document). Start each page with "
                       f"a line '[Page N text]:' using the document page number.")
        try:
            text = await _llm_text_vision(user, TRANSCRIBE_SYSTEM, instruction, batch)
        except HTTPException as e:
            if e.status_code == 503:
                raise
            failed += len(batch)
            logger.warning("study transcribe: pages %d+ failed: %s", start + 1, e.detail)
            continue
        # Guarantee the page markers even if the model dropped them.
        if "[Page" not in text:
            text = f"[Page {start + 1} text]:\n{text}"
        parts.append(text.strip())
    full = "\n\n".join(parts).strip()
    if len(full) < 30:
        raise HTTPException(502, "The vision model returned no usable transcription.")
    db = _common.SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        m.content = full
        m.char_count = len(full)
        m.page_count = m.page_count or len(urls)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "char_count": len(full), "pages": len(urls), "pages_failed": failed}


async def run_generate_notes(user, material_id: str) -> Dict:
    """Generate (or regenerate) consultable study notes for one material.
    Shared by the route and the Study agent."""
    if not _ai_limiter.check(request.client.host):
        raise HTTPException(429, "Too many requests — try again later")
    db = _common.SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        content = (m.content or "").strip()
        name, file_id, kind = m.name, m.file_id, m.kind
    finally:
        db.close()
    if len(content) < 200:
        raise HTTPException(400, "Not enough text in this material to write "
                                 "notes. If it is a scanned PDF, run vision "
                                 "extraction or re-extract its text first.")
    notes = await _common._llm_text(
        user, STUDY_NOTES_SYSTEM,
        f"Material name: {name}\n\n--- MATERIAL ---\n{content[:120000]}",
        temperature=0.3, max_tokens=8000, timeout=240)

    figures_md = ""
    pdf_path = None
    if file_id and (kind == "pdf" or str(file_id).lower().endswith(".pdf")):
        try:
            pdf_path = _common._resolve_uploaded_file(file_id)
        except HTTPException:
            pdf_path = None
    if pdf_path:
        figures_md = await _build_figures_section(user, material_id, file_id, pdf_path)

    full = notes.strip() + figures_md
    db = _common.SessionLocal()
    try:
        m = study_service.get_material(db, material_id, user)
        m.summary = full
        db.commit()
    finally:
        db.close()
    return {"summary": full, "has_figures": bool(figures_md)}


async def run_generate_overview(user, deck_id: str) -> Dict:
    """Generate a short subject overview from the chapter notes (preferred) or
    the raw materials. Shared by the route and the Study agent."""
    if not _ai_limiter.check(request.client.host):
        raise HTTPException(429, "Too many requests — try again later")
    db = _common.SessionLocal()
    try:
        deck = study_service.get_deck(db, deck_id, user)
        deck_name = deck.name
        q = db.query(StudyMaterial).filter(StudyMaterial.deck_id == deck_id)
        if user is not None:
            q = q.filter(StudyMaterial.owner == user)
        parts = []
        for m in q.order_by(StudyMaterial.created_at.asc()).all():
            src = (m.summary or m.content or "")[:4000].strip()
            if src:
                parts.append(f"### {m.name}\n{src}")
    finally:
        db.close()
    if not parts:
        raise HTTPException(400, "Add materials (and ideally generate chapter "
                                 "notes) before generating a subject overview.")
    prompt = (f"Subject: {deck_name}\n\n" + "\n\n".join(parts))[:60000]
    overview = await _common._llm_text(user, SUBJECT_OVERVIEW_SYSTEM, prompt,
                               temperature=0.3, max_tokens=4000, timeout=180)
    db = _common.SessionLocal()
    try:
        deck = study_service.get_deck(db, deck_id, user)
        deck.overview = overview
        db.commit()
    finally:
        db.close()
    return {"overview": overview}


def register(router: APIRouter) -> None:
    # ------------------------------------------------------------------ materials (v2)

    @router.get("/decks/{deck_id}/materials")
    def list_materials(request: Request, deck_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            return {"materials": material_rows_with_counts(db, deck.id, user)}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/materials")
    def create_material(request: Request, deck_id: str, body: MaterialCreate):
        """Attach source material: pasted text or a previously uploaded file."""
        return create_material_record(
            _owner(request), deck_id, name=body.name, text=body.text,
            file_id=body.file_id)

    @router.delete("/materials/{material_id}")
    def delete_material(request: Request, material_id: str, with_questions: bool = False):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            if with_questions:
                db.query(StudyQuestion).filter(StudyQuestion.material_id == m.id).delete()
            db.delete(m)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.put("/materials/{material_id}/category")
    def set_material_category(request: Request, material_id: str, body: MaterialCategoryIn):
        """Change a material's category (theory vs exam/answer-key). This drives
        whether Consult / Explain-further search it as a theory source."""
        if body.category not in MATERIAL_CATEGORIES:
            raise HTTPException(400, f"category must be one of {MATERIAL_CATEGORIES}")
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            m.category = body.category
            db.commit()
            return {"ok": True, "category": m.category}
        finally:
            db.close()

    @router.post("/materials/{material_id}/reextract-text")
    def reextract_material_text(request: Request, material_id: str):
        """Re-read a file-backed material's text in full.

        Materials uploaded before the 15k cap was lifted only stored the first
        ~15k chars. This re-extracts the whole file so notes/extraction see all
        of it. No-op for pasted-text materials (they were never truncated)."""
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            if not m.file_id:
                raise HTTPException(400, "This material is pasted text, not a file.")
            file_id = m.file_id
        finally:
            db.close()
        text = _common._extract_file_text(file_id, user)  # max_chars=None -> full text
        db = _common.SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            before = m.char_count or 0
            m.content = text
            m.char_count = len(text)
            db.commit()
            return {"ok": True, "char_count": len(text), "previous": before}
        finally:
            db.close()

    # ------------------------------------------------------------ study notes

    @router.get("/figures/{material_id}/{idx}")
    def get_study_figure(request: Request, material_id: str, idx: int):
        """Serve one extracted figure image inline (embedded in study notes)."""
        import os
        from fastapi.responses import FileResponse
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            study_service.get_material(db, material_id, user)  # ownership check
        finally:
            db.close()
        path = os.path.join(_study_figures_dir(material_id), f"{int(idx)}.jpg")
        if not os.path.isfile(path):
            raise HTTPException(404, "Figure not found")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"X-Content-Type-Options": "nosniff"},
                            content_disposition_type="inline")

    @router.get("/materials/{material_id}/file")
    def material_file(request: Request, material_id: str):
        """Serve a material's source file inline so the in-pane viewer can frame
        it (see SecurityHeadersMiddleware). Deliberately narrow: the caller must
        own the material, and only PDFs and images are served - anything else
        would be an HTML/script payload rendered on our own origin."""
        import mimetypes as _mt

        from fastapi.responses import FileResponse

        user = _owner(request)
        db = _common.SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            file_id, name = m.file_id, m.name
        finally:
            db.close()
        if not file_id:
            raise HTTPException(404, "This material has no source file")
        path = _common._resolve_uploaded_file(file_id)
        mime = _mt.guess_type(path)[0] or "application/octet-stream"
        if mime != "application/pdf" and not mime.startswith("image/"):
            raise HTTPException(415, "Only PDFs and images can be previewed")
        return FileResponse(
            path, media_type=mime, filename=name,
            headers={"X-Content-Type-Options": "nosniff"},
            content_disposition_type="inline",
        )

    @router.post("/materials/{material_id}/transcribe")
    async def transcribe_material(request: Request, material_id: str):
        """Vision OCR for scanned / formula PDFs (see run_transcribe_material)."""
        return await run_transcribe_material(_owner(request), material_id)

    @router.get("/materials/{material_id}/notes")
    def get_material_notes(request: Request, material_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            m = study_service.get_material(db, material_id, user)
            return {"summary": m.summary or "", "name": m.name, "file_id": m.file_id}
        finally:
            db.close()

    @router.post("/materials/{material_id}/notes")
    async def generate_material_notes(request: Request, material_id: str):
        """Generate (or regenerate) consultable study notes for one material."""
        return await run_generate_notes(_owner(request), material_id)

    @router.get("/decks/{deck_id}/overview")
    def get_deck_overview(request: Request, deck_id: str):
        user = _owner(request)
        db = _common.SessionLocal()
        try:
            deck = study_service.get_deck(db, deck_id, user)
            return {"overview": deck.overview or ""}
        finally:
            db.close()

    @router.post("/decks/{deck_id}/overview")
    async def generate_deck_overview(request: Request, deck_id: str):
        """Generate a short subject overview from the chapter notes (preferred) or"""
        return await run_generate_overview(_owner(request), deck_id)

    @router.post("/materials/{material_id}/extract")
    async def extract_questions(request: Request, material_id: str,
                                body: ExtractIn):
        """AI question extraction (see run_extraction). Rate-limited per
        client because each call can fan out into many model requests."""
        if not _ai_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        return await run_extraction(
            _owner(request), material_id, mode=body.mode, types=body.types,
            count=body.count, vision=body.vision)
