"""Tests for the phone page routes (/m, its manifest and service worker,
specs/mobile.md#phone-page), on the main app. The phone port is tested in test_gate.py."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from anki_assistant.web import main, routes_mobile


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.create_app(), base_url="http://localhost")


def test_page_is_html_with_versioned_assets(client: TestClient) -> None:
    r = client.get("/m")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    version = routes_mobile.shell_version()
    for asset in ("mobile.js", "mobile-queue.js", "display.js", "mobile.css", "themes.css"):
        assert f"/static/{asset}?v={version}" in r.text
    assert '<link rel="manifest" href="/m/manifest.webmanifest' in r.text
    assert 'name="apple-mobile-web-app-capable"' in r.text


def test_manifest(client: TestClient) -> None:
    r = client.get("/m/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(r.text)
    assert manifest["start_url"] == "/m"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == routes_mobile.THEME_COLOR


def test_service_worker_scope_and_shell(client: TestClient) -> None:
    r = client.get("/m/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["service-worker-allowed"] == "/m"
    version = routes_mobile.shell_version()
    assert r.text.startswith(f'const VERSION = "{version}";')
    assert f"/static/mobile.js?v={version}" in r.text


def test_shell_files_exist(client: TestClient) -> None:
    version = routes_mobile.shell_version()
    for name in routes_mobile.SHELL_STATIC:
        assert client.get(f"/static/{name}?v={version}").status_code == 200, name
