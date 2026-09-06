# tests/test_security_headers_middleware.py
"""
Focused regression coverage for `SecurityHeadersMiddleware`
(core/middleware.py), added alongside the HSTS + Permissions-Policy
hardening:

  1. HSTS is emitted only for HTTPS requests, including those reaching
     the app over a reverse proxy (`X-Forwarded-Proto: https`).
  2. HSTS is absent on plain HTTP so local/dev deployments are unaffected.
  3. `Permissions-Policy` locks down camera/geolocation but preserves
     same-origin microphone access (`microphone=(self)`), so the app's
     own voice/STT flow (`getUserMedia({ audio: true })`) keeps working.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core.middleware import SecurityHeadersMiddleware


def _build_app():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return app


def _client(base_url="http://testserver"):
    return TestClient(_build_app(), base_url=base_url)


def test_hsts_absent_on_plain_http():
    response = _client().get("/")

    assert "strict-transport-security" not in response.headers


def test_hsts_present_for_direct_https_requests():
    response = _client(base_url="https://testserver").get("/")

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_hsts_present_via_x_forwarded_proto_https():
    response = _client().get("/", headers={"X-Forwarded-Proto": "https"})

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_permissions_policy_locks_camera_and_geolocation_but_allows_self_microphone():
    response = _client().get("/")

    policy = response.headers["permissions-policy"]
    assert policy == "camera=(), microphone=(self), geolocation=()"

    # Explicitly pin the contract the reviewer flagged: an empty allowlist
    # would also block the app's own same-origin voice/STT button.
    assert "microphone=()" not in policy
    assert "microphone=(self)" in policy


@pytest.mark.parametrize("root_path", ["", "/odysseus", "/api/research/report/mount"])
@pytest.mark.parametrize(
    ("route_path", "frame_options", "policy_fragment"),
    [
        ("/api/document/doc-1/render-pdf", "SAMEORIGIN", "frame-ancestors 'self'"),
        ("/api/study/materials/m-1/file", "SAMEORIGIN", "frame-ancestors 'self'"),
        ("/api/research/report/r-1", None, "script-src 'self' 'unsafe-inline'"),
        ("/api/tools/example/render", None, None),
        ("/api/study/materials/m-1/notes", "DENY", "frame-ancestors 'none'"),
    ],
)
def test_mounted_routes_keep_their_security_policy(root_path, route_path, frame_options, policy_fragment):
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    async def endpoint():
        return {"reached": True}

    app.add_api_route(route_path, endpoint)
    with TestClient(app, root_path=root_path) as client:
        response = client.get(root_path + route_path)

    assert response.status_code == 200
    assert response.json() == {"reached": True}
    assert response.headers.get("X-Frame-Options") == frame_options
    if policy_fragment is None:
        assert "Content-Security-Policy" not in response.headers
    else:
        assert policy_fragment in response.headers["Content-Security-Policy"]
