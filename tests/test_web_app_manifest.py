"""How the server hands a phone the web app manifest and its icons."""

import json

import pytest

from openflight import server as server_module

UI_PUBLIC_DIR = server_module.FRONTEND_SOURCE_DIR / "public"


@pytest.fixture
def client(monkeypatch):
    """Serve ui/public, where the build copies these files from, as the built app."""
    monkeypatch.setattr(server_module.app, "static_folder", str(UI_PUBLIC_DIR))
    return server_module.app.test_client()


def test_the_manifest_is_served_as_a_web_app_manifest(client):
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    manifest = json.loads(response.data)
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"


def test_every_manifest_icon_is_served_as_a_png(client):
    manifest = json.loads((UI_PUBLIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    sources = [icon["src"] for icon in manifest["icons"]] + ["/icons/apple-touch-icon.png"]

    for source in sources:
        response = client.get(source)

        assert response.status_code == 200, source
        assert response.mimetype == "image/png", source
        assert response.data.startswith(b"\x89PNG"), source
