"""Unit tests for the pure-Python helpers in src/study_vision.py.

`study_vision.py` rasterizes PDF pages for the vision question-extraction
pipeline. The rendering helpers need pypdfium2 + Pillow (lazy-imported), but two
helpers are pure logic and were previously uncovered:

- `text_layer_is_thin(char_count, page_count)` — the heuristic that decides
  whether a PDF's extracted text layer is too sparse to trust, triggering the
  vision fallback. A regression here silently routes scanned PDFs through plain
  text extraction (missing questions) or wastes vision calls on clean PDFs.
- `batch_pages(items, per_batch)` — groups page data-URLs into per-call batches
  (context/cost guard). Off-by-one or empty-input bugs would drop or duplicate
  pages.

Both helpers are stdlib-only, so these tests have no optional-dependency or I/O
requirements.
"""
import pytest

from src.study_vision import batch_pages, text_layer_is_thin


# --- text_layer_is_thin -------------------------------------------------------
# Heuristic: average < 400 chars/page ⇒ "thin" (likely scanned/formula-image).

def test_thin_when_below_threshold():
    # 100 chars across 1 page → 100/page < 400 → thin.
    assert text_layer_is_thin(100, 1) is True


def test_not_thin_when_above_threshold():
    # 1000 chars on 1 page → well above 400/page.
    assert text_layer_is_thin(1000, 1) is False


def test_threshold_boundary_exactly_400_is_not_thin():
    # 400/page is the cutoff; the comparison is strict `< 400`, so exactly 400
    # is treated as a real text layer (not thin).
    assert text_layer_is_thin(400, 1) is False
    assert text_layer_is_thin(4000, 10) is False  # 400.0/page on a 10-pager


def test_threshold_boundary_just_below_400_is_thin():
    assert text_layer_is_thin(399, 1) is True
    assert text_layer_is_thin(3999, 10) is True   # 399.9/page


def test_multi_page_average_is_what_matters():
    # 4500 chars across 10 pages = 450/page → not thin, even though no single
    # page reaches 400 in isolation (the heuristic is an average).
    assert text_layer_is_thin(4500, 10) is False
    # 3000 chars across 10 pages = 300/page → thin.
    assert text_layer_is_thin(3000, 10) is True


def test_zero_pages_is_not_thin():
    # Guard against ZeroDivisionError: page_count <= 0 short-circuits to False.
    assert text_layer_is_thin(0, 0) is False
    assert text_layer_is_thin(5000, 0) is False


def test_negative_page_count_is_not_thin():
    # Defensive: a bogus negative page count must not divide or report thin.
    assert text_layer_is_thin(100, -3) is False


def test_zero_chars_with_pages_is_thin():
    # A totally empty text layer (0 chars over real pages) is the canonical
    # scanned-PDF case the heuristic exists to catch.
    assert text_layer_is_thin(0, 5) is True


# --- batch_pages --------------------------------------------------------------
# Groups data-URLs into per_batch-sized chunks, preserving order, no drops.

def test_batches_evenly_divisible():
    items = ["a", "b", "c", "d", "e", "f"]
    assert batch_pages(items, 3) == [["a", "b", "c"], ["d", "e", "f"]]


def test_batches_with_remainder():
    items = ["a", "b", "c", "d", "e"]
    assert batch_pages(items, 2) == [["a", "b"], ["c", "d"], ["e"]]


def test_default_batch_size_is_three():
    items = [str(i) for i in range(7)]
    out = batch_pages(items)
    assert out == [["0", "1", "2"], ["3", "4", "5"], ["6"]]


def test_empty_input_yields_no_batches():
    assert batch_pages([]) == []
    assert batch_pages([], 5) == []


def test_per_batch_larger_than_input_returns_single_batch():
    items = ["a", "b"]
    assert batch_pages(items, 10) == [["a", "b"]]


def test_per_batch_one_splits_every_item():
    items = ["a", "b", "c"]
    assert batch_pages(items, 1) == [["a"], ["b"], ["c"]]


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_non_positive_per_batch_is_clamped_to_one(bad):
    # max(1, per_batch) guards against a 0/negative step (range step 0 raises;
    # negative would silently drop items). Clamped to single-item batches.
    items = ["a", "b", "c"]
    assert batch_pages(items, bad) == [["a"], ["b"], ["c"]]


def test_batches_preserve_order_and_lose_nothing():
    items = [str(i) for i in range(23)]
    out = batch_pages(items, 4)
    # Flattening the batches reproduces the input exactly (order + count).
    assert [x for batch in out for x in batch] == items
    assert sum(len(b) for b in out) == len(items)
    assert all(len(b) <= 4 for b in out)
