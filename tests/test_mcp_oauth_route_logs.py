"""S7 / #152 / #153: MCP OAuth logging emits paths and presence flags,
never serialized credentials; written credential files are owner-only.

Source-to-sink: ``add_server`` receives the operator-submitted ``oauth_file``
JSON (client_id/client_secret), writes the external package's file format
under DATA_DIR/mcp_oauth, and logs the write. The legacy Google callback
(``_exchange_and_connect``) writes the token file and logs that too. The
flagged log sites must carry only file paths; the payload must never reach
the logger, and on POSIX the files must not stay group/other-readable.

The external file format (``{"installed": {...}}`` / raw token JSON) is
untouched: the MCP package reads these files directly.
"""

import json
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import routes.mcp.mcp_routes as mcp_routes

SENTINEL_ID = "client-id-sentinel-7c1"
SENTINEL_SECRET = "client-secret-S7-sentinel-7c1"
SENTINEL_ACCESS = "ya29.S7-ACCESS-TOKEN-SENTINEL"
SENTINEL_REFRESH = "1//S7-REFRESH-TOKEN-SENTINEL"


class _FakeDb:
    def __init__(self, row=None):
        self._row = row
        self.added = []

    def add(self, srv):
        self.added.append(srv)

    def query(self, model):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row

    def commit(self):
        pass

    def close(self):
        pass


class _FakeManager:
    async def connect_server(self, **kwargs):
        return True

    def get_server_status(self, server_id):
        return {"status": "connected", "tool_count": 0}


class _FakeResp:
    status_code = 200
    text = ""

    def json(self):
        return {
            "access_token": SENTINEL_ACCESS,
            "refresh_token": SENTINEL_REFRESH,
            "expires_in": 3600,
        }


class _FakeAsyncClient:
    def __init__(self):
        self.resp = _FakeResp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        return self.resp


def _route(manager, path, method):
    router = mcp_routes.setup_mcp_routes(manager)
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} not in MCP router")


def _install_common_patches(monkeypatch, mcp_dir, db):
    monkeypatch.setattr(mcp_routes, "MCP_OAUTH_DIR", str(mcp_dir))
    monkeypatch.setattr(mcp_routes, "SessionLocal", lambda: db)
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)


@pytest.mark.asyncio
async def test_add_server_never_logs_the_credential_payload(tmp_path, monkeypatch, caplog):
    """Sentinel client_id/client_secret must not appear in any log record;
    only the presence flag and the target path do."""
    mcp_dir = tmp_path / "mcp_oauth"
    _install_common_patches(monkeypatch, mcp_dir, _FakeDb())
    manager = _FakeManager()
    endpoint = _route(manager, "/api/mcp/servers", "POST")
    oauth_file = json.dumps({
        "dir": "gmail-oauth",
        "filename": "credentials.json",
        "client_id": SENTINEL_ID,
        "client_secret": SENTINEL_SECRET,
    })

    caplog.set_level(logging.INFO, logger="routes.mcp.mcp_routes")
    result = await endpoint(
        request=mock.Mock(),
        name="gmail-server",
        transport="sse",
        args="[]",
        env="{}",
        command="",
        url="http://127.0.0.1:9999",
        oauth_config="",
        oauth_file=oauth_file,
    )

    assert result["id"]
    text = caplog.text
    assert SENTINEL_SECRET not in text
    assert SENTINEL_ID not in text
    assert "oauth_file=provided" in text
    assert "Wrote OAuth credentials to" in text

    written = mcp_dir / "gmail-oauth" / "credentials.json"
    data = json.loads(written.read_text(encoding="utf-8"))
    # External package format is preserved verbatim.
    assert data["installed"]["client_id"] == SENTINEL_ID
    assert data["installed"]["client_secret"] == SENTINEL_SECRET
    if os.name != "nt":
        assert stat.S_IMODE(written.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_oauth_callback_logs_token_path_only_and_locks_the_file(
        tmp_path, monkeypatch, caplog):
    """The legacy Google exchange logs the token file path, never the token
    values, and the token file is owner-only on POSIX."""
    mcp_dir = tmp_path / "mcp_oauth"
    mcp_dir.mkdir(parents=True)
    keys_file = mcp_dir / "keys.json"
    token_file = mcp_dir / "tokens.json"
    keys_file.write_text(json.dumps({
        "installed": {
            "client_id": SENTINEL_ID,
            "client_secret": SENTINEL_SECRET,
            "redirect_uris": ["http://localhost"],
        }
    }), encoding="utf-8")

    srv = SimpleNamespace(
        id="srv-sentinel",
        name="Gmail",
        transport="sse",
        command=None,
        args="[]",
        env="{}",
        url="http://127.0.0.1:9999",
        oauth_config=json.dumps({
            "keys_file": str(keys_file),
            "token_file": str(token_file),
        }),
    )
    _install_common_patches(monkeypatch, mcp_dir, _FakeDb(row=srv))
    monkeypatch.setattr("src.mcp_oauth.resolve_pending", lambda state, code: False)
    monkeypatch.setattr(mcp_routes.httpx, "AsyncClient", _FakeAsyncClient)
    manager = _FakeManager()
    endpoint = _route(manager, "/api/mcp/oauth/callback", "GET")

    caplog.set_level(logging.INFO, logger="routes.mcp.mcp_routes")
    resp = await endpoint(code="s7-code", state="srv-sentinel", request=mock.Mock())

    assert resp.status_code == 200
    text = caplog.text
    assert SENTINEL_ACCESS not in text
    assert SENTINEL_REFRESH not in text
    assert SENTINEL_SECRET not in text
    assert "Saved OAuth tokens to" in text

    tokens = json.loads(token_file.read_text(encoding="utf-8"))
    assert tokens["access_token"] == SENTINEL_ACCESS
    if os.name != "nt":
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_oauth_file_log_line_is_metadata_only(monkeypatch):
    """Guard the fix itself: the add_server presence log must not interpolate
    the oauth_file payload. Source assertion (narrow exception) — the log is
    one line inside a transport-heavy handler already driven above; this pins
    the exact replacement."""
    src = (
        Path(__file__).resolve().parents[1]
        / "routes" / "mcp" / "mcp_routes.py"
    ).read_text(encoding="utf-8")
    assert 'oauth_file={oauth_file!r}' not in src
    assert 'logger.info("MCP add_server: oauth_file=%s", "provided" if oauth_file else "not provided")' in src