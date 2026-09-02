# src/study_vision.py
"""PDF page rendering for vision-based question extraction.

Formula-heavy and scanned exam PDFs often have a near-empty text layer —
pypdf gets the cover page and little else. This module rasterizes pages to
images so a vision model can read the questions directly (the same approach
as the Study Bench prototype's Kimi pipeline).

Renderer: pypdfium2 (BSD-licensed, binary wheels, no system deps) + Pillow
(already an Odysseus dependency). Both imports are lazy so the rest of the
study module works without them; callers get an actionable error message.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import List

logger = logging.getLogger(__name__)

MAX_PAGES = 40          # cap per extraction run (cost/latency guard; pages beyond it are reported as truncated)
RENDER_SCALE = 2.0      # ~144 DPI — crisp enough for print text and formulas
JPEG_QUALITY = 85
MAX_SIDE = 2000         # downscale very large pages (token/cost guard)

_INSTALL_HINT = (
    "PDF page rendering requires the optional 'pypdfium2' package. "
    "Install it with: pip install pypdfium2"
)


def _load_pdfium():
    try:
        import pypdfium2 as pdfium  # noqa: F401
        return pdfium
    except ImportError:
        raise RuntimeError(_INSTALL_HINT)


def pdf_page_count(path: str) -> int:
    """Number of pages in the PDF (0 if unreadable)."""
    try:
        pdfium = _load_pdfium()
        pdf = pdfium.PdfDocument(path)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("study_vision: page count failed for %s: %s", path, e)
        return 0


def render_pdf_pages(path: str, max_pages: int = MAX_PAGES,
                     scale: float = RENDER_SCALE) -> List[bytes]:
    """Render up to `max_pages` PDF pages to JPEG bytes."""
    pdfium = _load_pdfium()
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise RuntimeError("PDF page rendering requires Pillow (pip install pillow)")

    pdf = pdfium.PdfDocument(path)
    out: List[bytes] = []
    try:
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            img = bitmap.to_pil()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max(img.size) > MAX_SIDE:
                ratio = MAX_SIDE / max(img.size)
                img = img.resize((int(img.width * ratio), int(img.height * ratio)))
            buf = io.BytesIO()
            try:
                img.save(buf, "JPEG", quality=JPEG_QUALITY)
            except Exception:
                # Pillow built without libjpeg — PNG is always available.
                buf = io.BytesIO()
                img.save(buf, "PNG")
            out.append(buf.getvalue())
            page.close()
    finally:
        pdf.close()
    if not out:
        raise RuntimeError("The PDF contains no renderable pages.")
    return out


def pages_to_data_urls(pages: List[bytes]) -> List[str]:
    out = []
    for p in pages:
        mime = "image/jpeg" if p[:3] == b"\xff\xd8\xff" else "image/png"
        out.append(f"data:{mime};base64," + base64.b64encode(p).decode("ascii"))
    return out


def batch_pages(items: List[str], per_batch: int = 3) -> List[List[str]]:
    """Group page data-URLs into per-call batches (context/cost guard)."""
    per_batch = max(1, per_batch)
    return [items[i:i + per_batch] for i in range(0, len(items), per_batch)]


def extract_pdf_figures(pdf_path: str, out_dir: str, *, min_side: int = 180,
                        max_figs: int = 12) -> List[dict]:
    """Best-effort: pull raster figures out of a PDF, largest first.

    Returns ``[{"idx", "page", "path", "width", "height"}]``. Vector-only PDFs
    (figures drawn as paths, not embedded bitmaps) yield ``[]`` — callers fall
    back to citing the source page instead. Never raises: figure extraction is
    a nice-to-have and must not break notes generation.
    """
    import os
    try:
        from pypdf import PdfReader
        from PIL import Image  # noqa: F401
    except Exception as e:
        logger.info("study_vision: figure extraction unavailable: %s", e)
        return []
    try:
        reader = PdfReader(pdf_path)
    except Exception as e:
        logger.warning("study_vision: could not open PDF for figures: %s", e)
        return []

    candidates = []  # (area, page_no, PIL image)
    for pno, page in enumerate(reader.pages, start=1):
        try:
            images = list(page.images)
        except Exception:
            images = []
        for im in images:
            try:
                pil = im.image  # pypdf -> PIL
                w, h = pil.size
                if min(w, h) < min_side:
                    continue  # skip icons, rules, tiny decorations
                candidates.append((w * h, pno, pil))
            except Exception:
                continue

    candidates.sort(key=lambda c: c[0], reverse=True)  # biggest figures first
    os.makedirs(out_dir, exist_ok=True)
    out: List[dict] = []
    for i, (_area, pno, pil) in enumerate(candidates[:max_figs]):
        try:
            if pil.mode not in ("RGB", "L"):
                pil = pil.convert("RGB")
            if max(pil.size) > MAX_SIDE:
                ratio = MAX_SIDE / max(pil.size)
                pil = pil.resize((int(pil.width * ratio), int(pil.height * ratio)))
            path = os.path.join(out_dir, f"{i}.jpg")
            pil.save(path, "JPEG", quality=85)
            out.append({"idx": i, "page": pno, "path": path,
                        "width": pil.width, "height": pil.height})
        except Exception as e:
            logger.debug("study_vision: figure %d save failed: %s", i, e)
            continue
    return out


def figure_data_url(path: str) -> str:
    """data: URL for one extracted figure image (for a vision captioning call)."""
    with open(path, "rb") as fh:
        data = fh.read()
    mime = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def text_layer_is_thin(char_count: int, page_count: int) -> bool:
    """Heuristic: a real text layer averages well over 400 chars/page.

    Below that, the PDF is most likely scanned or formula-image-heavy and
    text extraction will have missed the actual questions.
    """
    if page_count <= 0:
        return False
    return (char_count / page_count) < 400
