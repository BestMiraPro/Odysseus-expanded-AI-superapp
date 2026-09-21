"""S7 / #203–#210: the URL-substring findings in test files are assertions,
not dispatch conditions.

Each flagged line pins an allowlist membership or an expected error-message
output. None of them is a mock dispatch gate that decides where secrets or
requests go — the mock dispatch layers in these files are patched separately
and asserted ``assert_not_called`` for the dangerous branches. The disposition
(false positive, assertion kept) is falsifiable exactly against this: the
flagged line must remain an ``assert ... in ...`` statement, and the
deliberate negative examples must stay asserted.

Source-level checks are the narrow exception permitted by TESTING_STANDARD
when the invariant is about the test files' own form (a runtime driver would
re-execute the very tests being classified).
"""

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

# (file, line, expected left-hand literal, something in the assertion)
_CASES = [
    ("tests/test_admin_device_flow_static.py", 43, "generativelanguage.googleapis.com"),
    ("tests/test_caldav_url_nonstring.py", 31, "example.com"),
    ("tests/test_copilot.py", 182, "api.githubcopilot.com"),
    ("tests/test_email_test_connection_oauth.py", 196, "imap.gmail.com"),
    ("tests/test_email_test_connection_oauth.py", 197, "smtp.gmail.com"),
    ("tests/test_kimi_code_hosts.py", 13, "api.kimi.com"),
    ("tests/test_tool_support_heuristic.py", 157, "api.deepseek.com"),
    ("tests/test_tool_support_heuristic.py", 160, "deepseek.com"),
]


@pytest.mark.parametrize("rel_path,lineno,needle", _CASES)
def test_flagged_line_is_an_in_membership_assertion(rel_path, lineno, needle):
    lines = (_REPO / rel_path).read_text(encoding="utf-8").splitlines()
    node = ast.parse(lines[lineno - 1].strip(), mode="exec").body[0]

    assert isinstance(node, ast.Assert), (
        f"{rel_path}:{lineno} is no longer an assertion — a dispatch-like "
        f"rewrite would invalidate the S7 disposition"
    )
    assert isinstance(node.test, ast.Compare), (
        f"{rel_path}:{lineno} assertion is not a comparison"
    )
    assert any(isinstance(op, ast.In) for op in node.test.ops), (
        f"{rel_path}:{lineno} assertion is not a membership check"
    )
    assert needle in lines[lineno - 1]


def test_email_oauth_negative_example_still_asserts_tokens_unused():
    """#206/#207 keep their deliberate negative semantics: on the non-Google
    host, the OAuth token getter and both transports are asserted unused."""
    src = (_REPO / "tests" / "test_email_test_connection_oauth.py").read_text(
        encoding="utf-8"
    )
    assert "token_getter.assert_not_called()" in src
    assert "open_imap.assert_not_called()" in src
    assert "open_smtp.assert_not_called()" in src
    assert "open_smtp_ssl.assert_not_called()" in src


def test_no_path_exclusion_covers_the_tests_directory():
    """The S7 dispositions never exclude the tests directory from scanning —
    the evidence lives in the tests themselves, so an exclusion would be
    cheating. The CodeQL config must carry no path-based ignores at all."""
    import re

    config = _REPO / ".github" / "codeql" / "codeql-config.yml"
    assert config.exists(), "codeql config moved or removed"
    text = config.read_text(encoding="utf-8")
    assert not re.search(r"paths?-ignore", text), (
        "CodeQL config gained a path exclusion; tests must stay scanned"
    )