from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware


def _client():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    @app.get("/api/document/{doc_id}/render-pdf")
    async def render_pdf(doc_id: str):
        return Response(b"%PDF-1.4\n", media_type="application/pdf")

    @app.get("/api/study/materials/{material_id}/file")
    async def study_material_file(material_id: str):
        return Response(b"%PDF", media_type="application/pdf")

    @app.get("/api/study/materials/{material_id}/notes")
    async def study_material_notes(material_id: str):
        return {"ok": True}

    return TestClient(app)


def test_default_routes_remain_unframeable():
    response = _client().get("/plain")

    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_document_pdf_preview_can_be_framed_by_same_origin():
    response = _client().get("/api/document/doc-123/render-pdf")

    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; frame-ancestors 'self'"
    )


def test_study_material_file_can_be_framed_by_same_origin():
    """The in-app material viewer frames this route; without the exception the
    blanket X-Frame-Options: DENY blocks it."""
    response = _client().get("/api/study/materials/m-1/file")

    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; frame-ancestors 'self'"
    )


def test_other_study_routes_remain_unframeable():
    """The exception is scoped to the file route alone."""
    response = _client().get("/api/study/materials/m-1/notes")

    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
