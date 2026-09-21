import json

import pytest
from fastapi import HTTPException

import routes.embedding_routes as embedding_routes


def test_load_custom_endpoint_ignores_non_object_json(tmp_path, monkeypatch):
    endpoint_file = tmp_path / "embedding_endpoint.json"
    endpoint_file.write_text(json.dumps(["not", "an", "endpoint", "object"]), encoding="utf-8")
    monkeypatch.setattr(embedding_routes, "_ENDPOINT_FILE", str(endpoint_file))

    assert embedding_routes._load_custom_endpoint() == {}


def test_load_custom_endpoint_keeps_object_json(tmp_path, monkeypatch):
    endpoint_file = tmp_path / "embedding_endpoint.json"
    endpoint_file.write_text(
        json.dumps({"url": "http://127.0.0.1:11434", "model": "nomic-embed-text"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(embedding_routes, "_ENDPOINT_FILE", str(endpoint_file))

    assert embedding_routes._load_custom_endpoint() == {
        "url": "http://127.0.0.1:11434",
        "model": "nomic-embed-text",
    }


# ── S7 / #148: api_key is encrypted at rest; embedded URL creds are refused ──

_SENTINEL_API_KEY = "sk-embed-s7-sentinel-key"
_URL_CRED_SENTINEL = "s7-url-credential-sentinel"


def _endpoint_route(path, method):
    router = embedding_routes.setup_embedding_routes()
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} not found in embedding router")


def test_url_credentials_are_detected():
    assert embedding_routes._url_has_credentials(
        f"https://user:{_URL_CRED_SENTINEL}@embed.example/v1"
    )
    assert embedding_routes._url_has_credentials(
        f"https://embed.example/v1?api_key={_URL_CRED_SENTINEL}"
    )
    assert embedding_routes._url_has_credentials(
        "https://embed.example/v1?token=" + _URL_CRED_SENTINEL
    )
    assert not embedding_routes._url_has_credentials("https://embed.example/v1")
    assert not embedding_routes._url_has_credentials(
        "https://embed.example/v1?model_version=2"
    )


@pytest.mark.parametrize("url", [
    f"https://user:{_URL_CRED_SENTINEL}@embed.example/v1",
    f"https://embed.example/v1?api_key={_URL_CRED_SENTINEL}",
    f"https://embed.example/v1?token={_URL_CRED_SENTINEL}",
])
def test_set_endpoint_rejects_credentials_embedded_in_the_url(url):
    endpoint = _endpoint_route("/api/embeddings/endpoint", "POST")

    with pytest.raises(HTTPException) as excinfo:
        endpoint(url=url, model="m", api_key="")

    assert excinfo.value.status_code == 400
    assert "API key field" in excinfo.value.detail
    assert _URL_CRED_SENTINEL not in excinfo.value.detail


def test_set_endpoint_encrypts_key_and_round_trips_across_reload(tmp_path, monkeypatch):
    import src.secret_storage as _ss
    from cryptography.fernet import Fernet

    app_key = tmp_path / ".app_key"
    app_key.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(_ss, "_KEY_PATH", app_key)
    monkeypatch.setattr(_ss, "_fernet", None)

    monkeypatch.setattr(
        embedding_routes, "_ENDPOINT_FILE",
        str(tmp_path / "embedding_endpoint.json"),
    )
    # set_endpoint assigns these env vars for immediate use; pre-seeding via
    # monkeypatch makes the simulated run restore them afterwards.
    monkeypatch.setenv("EMBEDDING_URL", "")
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    monkeypatch.setenv("EMBEDDING_API_KEY", "")
    monkeypatch.setattr(
        "src.url_safety.check_outbound_url",
        lambda url, block_private=False, resolver=None: (True, "ok"),
    )

    class _OkResp:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        "httpx.post",
        lambda url, json=None, headers=None, timeout=None: _OkResp(),
    )

    endpoint = _endpoint_route("/api/embeddings/endpoint", "POST")
    result = endpoint(
        url="https://embed.example/v1",
        model="embed-model",
        api_key=_SENTINEL_API_KEY,
    )
    assert result == {
        "success": True,
        "url": "https://embed.example/v1",
        "model": "embed-model",
    }

    saved_raw = (tmp_path / "embedding_endpoint.json").read_text(encoding="utf-8")
    assert _SENTINEL_API_KEY not in saved_raw
    saved = json.loads(saved_raw)
    assert saved["url"] == "https://embed.example/v1"
    assert saved["api_key"].startswith("enc:")
    assert _ss.decrypt(saved["api_key"]) == _SENTINEL_API_KEY

    # Process-reload shape: a fresh load returns the same config, still keyless.
    reloaded = embedding_routes._load_custom_endpoint()
    assert reloaded["url"] == "https://embed.example/v1"
    assert _SENTINEL_API_KEY not in json.dumps(reloaded)


def test_decrypt_returns_empty_for_wrong_key_or_corrupt_token(tmp_path, monkeypatch):
    import src.secret_storage as _ss
    from cryptography.fernet import Fernet

    app_key = tmp_path / ".app_key2"
    app_key.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(_ss, "_KEY_PATH", app_key)
    monkeypatch.setattr(_ss, "_fernet", None)

    assert _ss.decrypt("enc:not-a-fernet-token") == ""


def test_get_endpoint_response_excludes_the_saved_api_key(tmp_path, monkeypatch):
    endpoint_file = tmp_path / "embedding_endpoint.json"
    endpoint_file.write_text(
        json.dumps({
            "url": "https://embed.example/v1",
            "model": "m",
            "api_key": "enc:" + "A" * 100,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(embedding_routes, "_ENDPOINT_FILE", str(endpoint_file))

    endpoint = _endpoint_route("/api/embeddings/endpoint", "GET")
    payload = endpoint()

    assert "api_key" not in payload
    assert payload["url"] == "https://embed.example/v1"