"""Source-file links for extracted study questions."""

import re
from typing import Dict, Optional
from urllib.parse import quote


_PAGE_MARKER_RE = re.compile(
    r"(?:^|\n)\s*\[?Page\s+(\d+)\s+text\]?:",
    re.IGNORECASE,
)


def _positive_int(value) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 else None


def _text_key(value: str) -> str:
    s = str(value or "").lower()
    s = re.sub(r"\\[a-z]+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _qnum_key(value) -> str:
    s = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    s = re.sub(r"^(question|exercise|problem|ex|q|p)(?=\d)", "", s)
    return re.sub(r"^0+(?=\d)", "", s)


def build_original_question_link(
    file_id: Optional[str],
    *,
    page=None,
    name: Optional[str] = None,
) -> Optional[Dict]:
    """Return the browser URL for the original uploaded file/page."""
    fid = str(file_id or "").strip()
    if not fid:
        return None

    page_num = _positive_int(page)
    label = (str(name or "").strip() or "Original question")
    if page_num:
        label = f"{label}, p.{page_num}"

    url = f"/api/upload/{quote(fid, safe='')}?inline=1"
    if page_num:
        url += f"#page={page_num}"

    return {
        "file_id": fid,
        "name": str(name or "").strip() or None,
        "page": page_num,
        "label": label,
        "url": url,
    }


def _page_blocks(content: str):
    matches = list(_PAGE_MARKER_RE.finditer(content or ""))
    for i, match in enumerate(matches):
        page = _positive_int(match.group(1))
        if not page:
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        yield page, content[start:end]


def _line_starts_with_number(line: str, wanted_key: str) -> bool:
    if not wanted_key:
        return False
    m = re.match(
        r"\s*(?:question|exercise|problem|ex|q|p)?\.?\s*"
        r"([0-9]+(?:\s*[a-z])?)\s*(?:[\).:\-]|$)",
        line,
        re.IGNORECASE,
    )
    return bool(m and _qnum_key(m.group(1)) == wanted_key)


def infer_source_page(content: str, *, number=None, question: Optional[str] = None) -> Optional[int]:
    """Best-effort page inference from stored PDF text-layer page markers."""
    blocks = list(_page_blocks(content or ""))
    if not blocks:
        return None

    qkey = _text_key(question or "")
    fragments = []
    if len(qkey) >= 30:
        fragments.extend([qkey[:160], qkey[:100], qkey[:60]])
    fragments = [f for f in fragments if len(f) >= 30]
    for page, block in blocks:
        bkey = _text_key(block)
        if any(f in bkey for f in fragments):
            return page

    wanted = _qnum_key(number)
    if wanted:
        for page, block in blocks:
            if any(_line_starts_with_number(line, wanted) for line in block.splitlines()):
                return page

    return None
