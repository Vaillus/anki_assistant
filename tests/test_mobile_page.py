"""Tests for the phone page routes (/m, its manifest and service worker,
specs/mobile.md#phone-page), reached locally and through the tailnet gate."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from anki_assistant.web import main, routes_mobile

OWNER = "owner@example.com"
TS_HOST = "macbook-pro-de-hugo-2.chamois-velociraptor.ts.net"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MOBILE_OWNER_LOGIN", OWNER)
    return TestClient(main.create_app(), base_url="http://localhost")


def _remote(login: str | None = OWNER) -> dict[str, str]:
    headers = {"host": TS_HOST}
    if login is not None:
        headers["Tailscale-User-Login"] = login
    return headers


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


@pytest.mark.parametrize("path", ["/m", "/m/manifest.webmanifest", "/m/sw.js", "/static/mobile.js"])
def test_owner_reaches_the_page_through_the_gate(client: TestClient, path: str) -> None:
    assert client.get(path, headers=_remote()).status_code == 200


@pytest.mark.parametrize("login", [None, "colleague@example.com"])
def test_others_are_refused(client: TestClient, login: str | None) -> None:
    assert client.get("/m", headers=_remote(login)).status_code == 403
