"""Structural guard for the Settings modal panel layout.

A conflict resolution during the upstream port concatenated two sides of a
merge and dropped three closing ``</div>`` tags. The markup still parsed and
every one of the ~5,900 other tests passed, but the browser nested the
remaining Settings panels inside an earlier one: ``.settings-panels`` went from
13 direct children to 2, and no panel could be shown. Symptom was "Add Models
and Added Models show nothing".

Nothing in the suite catches that, because it is a nesting bug rather than a
syntax error. These tests pin the structure directly.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_INDEX_PATH = _REPO / "static" / "index.html"
_INDEX = _INDEX_PATH.read_text(encoding="utf-8")


class _PanelCounter(HTMLParser):
    """Count direct ``<div>`` children of the ``.settings-panels`` container."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.panels_depth: int | None = None
        self.direct_children = 0

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        self.depth += 1
        if self.panels_depth is not None and self.depth == self.panels_depth + 1:
            self.direct_children += 1
        if "settings-panels" in (dict(attrs).get("class") or ""):
            self.panels_depth = self.depth

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self.panels_depth is not None and self.depth == self.panels_depth:
            self.panels_depth = None
        self.depth -= 1


def _tab_names() -> set[str]:
    return {m.group(1) for m in re.finditer(r'data-settings-tab="([^"]+)"', _INDEX)}


def test_index_html_div_tags_are_balanced():
    # `<div\b` rather than a literal "<div " — a couple of tags in this file put
    # their attributes on the following line, so a plain string count misses them.
    opens = len(re.findall(r"<div\b", _INDEX))
    closes = _INDEX.count("</div>")
    assert opens == closes, (
        f"static/index.html has {opens - closes} unclosed <div> tag(s). "
        "Unbalanced divs silently re-nest the Settings panels."
    )


def test_every_settings_tab_has_its_own_panel():
    counter = _PanelCounter()
    counter.feed(_INDEX)
    tabs = _tab_names()
    assert tabs, "no data-settings-tab entries found — markup changed shape"
    assert counter.direct_children == len(tabs), (
        f".settings-panels has {counter.direct_children} direct children but "
        f"there are {len(tabs)} settings tabs {sorted(tabs)}. Every tab needs "
        "its own top-level panel or some tabs render nothing."
    )


@pytest.mark.parametrize(
    "element_id",
    [
        "adm-epList-api",     # Added Models -> API endpoints list
        "adm-epList-local",   # Added Models -> local endpoints list
        "set-userCountry",    # Study's region setting (Search tab)
    ],
)
def test_settings_controls_are_present(element_id):
    assert f'id="{element_id}"' in _INDEX, (
        f"{element_id} missing from static/index.html"
    )
