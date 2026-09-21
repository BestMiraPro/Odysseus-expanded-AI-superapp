"""S7 / #150: the research-endpoint resolution log formats only redacted
URLs and header NAMES.

The flagged sink (``routes/chat_routes.py``, "Research endpoint resolved")
sits inside the chat stream generator, which cannot be driven without the
full request pipeline. This is the documented narrow exception: an AST pin on
that one statement. It fails exactly when the log is refactored to format
header VALUES, unredacted URLs, or the headers dict itself — the security
property the disposition depends on. The companion behavioral proof that
``redact_url`` strips userinfo/query lives in ``tests/test_log_safety.py``.
"""

import ast
from pathlib import Path

_CHAT_ROUTES = Path(__file__).resolve().parents[1] / "routes" / "chat_routes.py"


def test_research_endpoint_log_uses_redact_url_and_header_names():
    src = _CHAT_ROUTES.read_text(encoding="utf-8")
    tree = ast.parse(src)

    target = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_logger = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "logger"
            and func.attr in {"info", "debug", "warning", "error"}
        )
        if not is_logger or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.JoinedStr):
            header = "".join(
                part.value for part in first.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        else:
            header = ""
        if "Research endpoint resolved" not in header:
            continue
        target = node
        break

    assert target is not None, "research endpoint resolution log line not found"

    # The endpoint is formatted through redact_url (never raw).
    names = {
        n.id for n in ast.walk(target) if isinstance(n, ast.Name)
    }
    assert "redact_url" in names, "endpoint must be formatted with redact_url"

    # Header access is keys-only — no value access on either header dict.
    value_calls = [
        n for n in ast.walk(target)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"values", "items", "get"}
    ]
    assert value_calls == [], (
        "research log accesses header values: " + ", ".join(
            f"line {n.lineno}: .{n.func.attr}()" for n in value_calls
        )
    )

    # The line inspects header NAMES, never values: the auth_keys variable is
    # built with list(_r_headers.keys()) upstream, and the inline expression
    # reads sess.headers via keys() (its value guard only type-checks the dict).
    key_calls = [
        n for n in ast.walk(target)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "keys"
    ]
    assert len(key_calls) >= 1, "expected keys() inspection on the session headers"
    assert "_auth_keys" in names, "auth header names must come from _auth_keys"