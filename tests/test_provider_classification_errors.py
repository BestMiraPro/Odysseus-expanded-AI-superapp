"""Upstream-error formatting for provider setup (REAL src.llm_core).

Split from `test_provider_classification.py` to keep error-message formatting
separate from provider identification.

  * `_format_upstream_error` — turns a raw upstream HTTP status into the
    one-line, provider-aware, fixed sentence the UI shows ("Provider probes"
    degraded reporting in the roadmap). Bodies are deliberately NOT echoed
    (S5a: upstream text is not controlled; the detail is a fixed per-status
    message so no provider body or request material can round-trip).

conftest.py stubs the heavy deps (sqlalchemy, src.database), so importing the
real module is side-effect free.
"""
from src.llm_core import _format_upstream_error


# ── _format_upstream_error ──
# Status → one-line provider-aware sentence; body must never be echoed.

class TestFormatUpstreamError:
    def test_401_rejects_key_with_provider(self):
        msg = _format_upstream_error(
            401, '{"error": {"message": "Invalid API key"}}', "https://api.x.ai/v1"
        )
        assert msg.startswith("xAI rejected the API key")
        assert "re-paste the key" in msg

    def test_403_denies_access(self):
        msg = _format_upstream_error(
            403, '{"error": {"message": "Forbidden"}}', "https://api.openai.com/v1"
        )
        assert "OpenAI denied access (403)" in msg

    def test_404_points_at_base_url(self):
        msg = _format_upstream_error(404, "", "https://api.groq.com/openai/v1")
        assert msg == "Groq returned 404 — check the base URL and model name."

    def test_429_rate_limited(self):
        msg = _format_upstream_error(
            429, '{"error": {"message": "slow down"}}', "https://api.anthropic.com"
        )
        assert msg == "Anthropic rate-limited the request (429)."

    def test_5xx_reported_as_outage(self):
        msg = _format_upstream_error(503, "", "https://api.deepseek.com")
        assert msg == "DeepSeek is having an outage (HTTP 503)."

    def test_other_status_passthrough(self):
        msg = _format_upstream_error(418, "", "https://api.openai.com/v1")
        assert msg == "OpenAI returned HTTP 418"

    def test_body_content_is_never_echoed(self):
        for status in (400, 401, 429, 500):
            msg = _format_upstream_error(
                status, '{"error": {"message": "private-marker /srv/private/auth.json"}}',
                "https://api.openai.com/v1",
            )
            assert "private-marker" not in msg
            assert "/srv/private/auth.json" not in msg

    def test_plain_text_body_is_never_echoed(self):
        msg = _format_upstream_error(500, "upstream exploded", "https://api.openai.com/v1")
        assert "OpenAI is having an outage (HTTP 500)." in msg
        assert "exploded" not in msg

    def test_unknown_url_falls_back_to_generic_label(self):
        msg = _format_upstream_error(401, "", "")
        assert msg.startswith("provider rejected the API key")