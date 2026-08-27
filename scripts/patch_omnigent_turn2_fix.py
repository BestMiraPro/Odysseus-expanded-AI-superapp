#!/usr/bin/env python3
"""Patch omnigent's open_responses_sdk to fix the turn-2 crash on chat-wire models.

Bug (omnigent 0.3.0, openai-agents 0.17.7): assistant history items are re-seeded
into the SDK session as ``{"type": "message", "role": "assistant", "content": "<str>"}``.
The openai-agents chatcmpl converter (``maybe_response_output_message``) treats any
``{type: message, role: assistant}`` item as a ResponseOutputMessage and iterates its
content parts — a bare string is iterated char-by-char and ``part["type"]`` raises
``TypeError: string indices must be integers, not 'str'``. First message always works
(no history to replay); every later message crashes.

Fix: in ``_normalize_message_content``, wrap assistant string content into
``[{"type": "output_text", "text": ...}]`` and coerce stray bare-string list elements
into proper content-part dicts.

Idempotent. Re-run after every ``uv tool upgrade omnigent`` (the upgrade replaces
site-packages and reverts this patch). Usage (WSL):

    python3 scripts/patch_omnigent_turn2_fix.py            # auto-locate install
    python3 scripts/patch_omnigent_turn2_fix.py /path/to/open_responses_sdk.py

Then restart the host: pkill runners / relaunch ``omni host`` so fresh runner
processes import the patched module.
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path

MARKER = "turn-2 re-seed crash"

# Support both omnigent 0.3 (Any) and 0.10 (object) signatures
OLD_SIG_V03 = (
    "def _normalize_message_content(\n"
    "    content: Any,  # type: ignore[explicit-any]\n"
    "    *,\n"
    "    empty_placeholder: str,\n"
    ") -> str | list[dict[str, Any]]:"
)
NEW_SIG_V03 = (
    "def _normalize_message_content(\n"
    "    content: Any,  # type: ignore[explicit-any]\n"
    "    *,\n"
    "    empty_placeholder: str,\n"
    '    role: str = "user",\n'
    ") -> str | list[dict[str, Any]]:"
)
OLD_SIG_V10 = (
    "def _normalize_message_content(\n"
    "    content: object,\n"
    "    *,\n"
    "    empty_placeholder: str,\n"
    ") -> str | list[dict[str, object]]:"
)
NEW_SIG_V10 = (
    "def _normalize_message_content(\n"
    "    content: object,\n"
    "    *,\n"
    "    empty_placeholder: str,\n"
    '    role: str = "user",\n'
    ") -> str | list[dict[str, object]]:"
)
# Keep OLD_SIG/NEW_SIG as aliases for backwards compat (tests may import them)
OLD_SIG = OLD_SIG_V03
NEW_SIG = NEW_SIG_V03

OLD_BODY = (
    "    if not content:\n"
    "        return empty_placeholder\n"
    "    if isinstance(content, str):\n"
    "        return content\n"
    "    if isinstance(content, list):\n"
    "        return content\n"
    "    return str(content)"
)
NEW_BODY = (
    '    is_assistant = role == "assistant"\n'
    '    block = "output_text" if is_assistant else "input_text"\n'
    "    # Assistant output messages MUST carry a *list* of content parts. A\n"
    "    # bare string reaches the openai-agents chatcmpl converter, whose\n"
    "    # maybe_response_output_message treats any {type:message, role:assistant}\n"
    "    # item as a ResponseOutputMessage and iterates content parts. A string\n"
    '    # is iterated char-by-char and part["type"] raises\n'
    '    # "string indices must be integers, not str" -- the turn-2 re-seed crash.\n'
    "    # User/system messages accept a plain string, so only wrap for assistant.\n"
    "    if not content:\n"
    "        if is_assistant:\n"
    '            return [{"type": block, "text": empty_placeholder}]\n'
    "        return empty_placeholder\n"
    "    if isinstance(content, str):\n"
    "        if is_assistant:\n"
    '            return [{"type": block, "text": content}]\n'
    "        return content\n"
    "    if isinstance(content, list):\n"
    "        # Coerce bare-string / typeless elements into proper content-part dicts.\n"
    "        fixed: list[dict[str, Any]] = []\n"
    "        for el in content:\n"
    "            if isinstance(el, str):\n"
    '                fixed.append({"type": block, "text": el})\n'
    '            elif isinstance(el, dict) and "type" not in el and "text" in el:\n'
    '                fixed.append({"type": block, "text": el.get("text") or ""})\n'
    "            else:\n"
    "                fixed.append(el)\n"
    "        return fixed\n"
    "    return str(content)"
)

OLD_CALL_USER = (
    'content": _normalize_message_content(content, empty_placeholder="(empty)"),\n'
    "                }\n"
    "            )\n"
    "\n"
    '        elif role == "assistant":'
)
NEW_CALL_USER = (
    'content": _normalize_message_content(content, empty_placeholder="(empty)", role="user"),\n'
    "                }\n"
    "            )\n"
    "\n"
    '        elif role == "assistant":'
)

OLD_CALL_ASST = (
    '"role": "assistant",\n'
    '                    "content": _normalize_message_content(content, empty_placeholder="(empty)"),'
)
NEW_CALL_ASST = (
    '"role": "assistant",\n'
    '                    "content": _normalize_message_content(content, empty_placeholder="(empty)", role="assistant"),'
)

DEFAULT_LOCATIONS = [
    "~/.local/share/uv/tools/omnigent/lib/python3.12/site-packages/omnigent/inner/open_responses_sdk.py",
]


def locate() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    for cand in DEFAULT_LOCATIONS:
        p = Path(cand).expanduser()
        if p.exists():
            return p
    sys.exit("could not locate open_responses_sdk.py — pass the path as an argument")


def main() -> None:
    path = locate()
    src = path.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"already patched: {path}")
        return

    # Determine which version is present (0.3 vs 0.10)
    if OLD_SIG_V10 in src:
        sig_old, sig_new = OLD_SIG_V10, NEW_SIG_V10
    elif OLD_SIG_V03 in src:
        sig_old, sig_new = OLD_SIG_V03, NEW_SIG_V03
    else:
        sys.exit(f"signature not found — upstream changed, patch needs updating: {path}")

    for name, old in (("body", OLD_BODY),
                      ("user call site", OLD_CALL_USER), ("assistant call site", OLD_CALL_ASST)):
        if old not in src:
            sys.exit(f"{name} not found — upstream changed, patch needs updating: {path}")

    backup = path.with_suffix(path.suffix + ".bak-turn2fix")
    shutil.copyfile(path, backup)

    src = src.replace(sig_old, sig_new, 1)
    src = src.replace(OLD_BODY, NEW_BODY, 1)
    src = src.replace(OLD_CALL_USER, NEW_CALL_USER, 1)
    src = src.replace(OLD_CALL_ASST, NEW_CALL_ASST, 1)

    ast.parse(src)  # refuse to write broken syntax
    path.write_text(src, encoding="utf-8")
    print(f"patched OK: {path}\nbackup: {backup}\nrestart the omni host so fresh runners load the fix")


if __name__ == "__main__":
    main()
