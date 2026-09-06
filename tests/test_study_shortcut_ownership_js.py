"""B03 — a hidden or unfocused Study must not act on keyboard shortcuts.

The document-level keydown listener is installed in openPanel and only removed
by _forceClose. Minimizing goes through the modal manager, which merely adds
`hidden`/`modal-minimized` to the pane — so a minimized Study kept rating cards
and answering Escape from anywhere on the page.

The old guard was `e.target.closest('input, textarea, select')`, which misses
contenteditable editors, IME composition, and Ctrl/Cmd chords entirely.

Acceptance from the handoff: a visible active Study keeps its shortcuts; a
minimized or unfocused one cannot mutate a session; typing in a contenteditable
editor or pressing a command chord does not rate cards.
"""

from __future__ import annotations

import pytest

from tests._study_js_harness import needs_node, run_js

pytestmark = needs_node


def _prelude(
    *,
    open_="true",
    classes="[]",
    minimized="false",
    target="body",
):
    """target: body | inside | outside | editable | input"""
    return f"""
const PANE = {{
  classList: {{ contains: (c) => {classes}.includes(c) }},
  contains: (node) => node && node.__inPane === true,
}};
const _open = {open_};
const _pane = PANE;
const Modals = {{ isMinimized: () => {minimized} }};

const BODY = {{ __neutral: true }};
globalThis.document = {{ body: BODY, documentElement: {{ __neutral: true }} }};

function node(kind) {{
  const base = {{
    __inPane: kind === 'inside' || kind === 'input' || kind === 'editable',
    isContentEditable: kind === 'editable',
    closest(sel) {{
      if (kind === 'input' && /input|textarea|select/.test(sel)) return {{}};
      if (kind === 'editable' && sel.includes('contenteditable')) return {{}};
      return null;
    }},
  }};
  return base;
}}
const TARGET = {target!r} === 'body' ? BODY : node({target!r});

function makeKey(key, mods = {{}}) {{
  return {{
    key,
    target: TARGET,
    ctrlKey: !!mods.ctrl,
    metaKey: !!mods.meta,
    altKey: !!mods.alt,
    isComposing: !!mods.composing,
    keyCode: mods.composing ? 229 : 0,
  }};
}}
""".replace("{target!r}", f"'{target}'")


def _run(prelude, key="3", mods="{}"):
    epilogue = f"""
let error = null, accepted = null;
try {{ accepted = _studyAcceptsShortcut(makeKey({key!r}, {mods})); }}
catch (e) {{ error = String(e && e.name ? e.name + ': ' + e.message : e); }}
console.log(JSON.stringify({{ accepted, error }}));
"""
    return run_js(prelude, "_studyAcceptsShortcut", epilogue=epilogue)


# --------------------------------------------------------------------------
# The case that must keep working
# --------------------------------------------------------------------------

def test_visible_study_with_neutral_focus_accepts_shortcuts():
    out = _run(_prelude())
    assert out["error"] is None, out["error"]
    assert out["accepted"] is True


def test_visible_study_with_focus_inside_the_pane_accepts_shortcuts():
    out = _run(_prelude(target="inside"))
    assert out["accepted"] is True


# --------------------------------------------------------------------------
# Hidden / minimized must not act
# --------------------------------------------------------------------------

@pytest.mark.parametrize("classes", ['["hidden"]', '["modal-minimized"]'])
def test_a_minimized_pane_refuses_shortcuts(classes):
    out = _run(_prelude(classes=classes))
    assert out["accepted"] is False, "a hidden Study pane still rated cards"


def test_the_modal_managers_minimized_flag_is_honoured():
    out = _run(_prelude(minimized="true"))
    assert out["accepted"] is False


def test_a_closed_panel_refuses_shortcuts():
    out = _run(_prelude(open_="false"))
    assert out["accepted"] is False


def test_a_missing_pane_does_not_throw():
    prelude = _prelude().replace("const _pane = PANE;", "const _pane = null;")
    out = _run(prelude)
    assert out["error"] is None, out["error"]
    assert out["accepted"] is False


# --------------------------------------------------------------------------
# Ownership: another surface has focus
# --------------------------------------------------------------------------

def test_focus_in_another_surface_refuses_shortcuts():
    """The handoff's trigger: minimize, focus another non-input surface, press 1-4."""
    out = _run(_prelude(target="outside"))
    assert out["accepted"] is False, "Study acted on a key aimed at another surface"


def test_escape_is_refused_when_study_is_hidden():
    out = _run(_prelude(classes='["hidden"]'), key="Escape")
    assert out["accepted"] is False, "hidden Study closed itself from a global Escape"


# --------------------------------------------------------------------------
# Editing and chords
# --------------------------------------------------------------------------

def test_typing_in_a_contenteditable_editor_refuses_shortcuts():
    out = _run(_prelude(target="editable"))
    assert out["accepted"] is False, "a contenteditable editor rated a card"


def test_typing_in_an_input_refuses_shortcuts():
    out = _run(_prelude(target="input"))
    assert out["accepted"] is False


@pytest.mark.parametrize("mods", ["{ctrl: true}", "{meta: true}", "{alt: true}"])
def test_command_chords_refuse_shortcuts(mods):
    out = _run(_prelude(), mods=mods)
    assert out["accepted"] is False, f"a chord {mods} was treated as a rating key"


def test_ime_composition_refuses_shortcuts():
    out = _run(_prelude(), mods="{composing: true}")
    assert out["accepted"] is False
