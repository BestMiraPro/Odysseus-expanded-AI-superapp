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

MAX_PAGES = 12          # cap per extraction run (cost/latency guard)
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


def text_layer_is_thin(char_count: int, page_count: int) -> bool:
    """Heuristic: a real text layer averages well over 400 chars/page.

    Below that, the PDF is most likely scanned or formula-image-heavy and
    text extraction will have missed the actual questions.
    """
    if page_count <= 0:
        return False
    return (char_count / page_count) < 400
