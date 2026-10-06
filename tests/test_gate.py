"""Tests for the tailnet gate (web/gate.py, specs/mobile.md#tailnet-gate)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant.web import gate
from anki_assistant.web.gate import TailnetGate

OWNER = "owner@example.com"
TS_HOST = "macbook-pro-de-hugo-2.chamois-velociraptor.ts.net"
ALLOWED = ["/m", "/m/sw.js", "/static/themes.css", "/api/mobile/batch", "/api/mobile/media/a.png"]
REFUSED = [
    "/",
    "/api/decks",
    "/api/media/a.png",
    "/api/chat",
    "/mfoo",
    "/api/mobilex",
    "/m/../api/decks",
    "/static/../api/decks",
]


def _client(owner: str | None) -> TestClient:
    app = FastAPI()
    app.add_middleware(TailnetGate, owner=owner)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def anything(path: str) -> dict[str, str]:
        return {"path": path}

    return TestClient(app)


def _get(client: TestClient, path: str, host: str, login: str | None = None) -> int:
    headers = {"host": host}
    if login is not None:
        headers["Tailscale-User-Login"] = login
    return client.get(path, headers=headers).status_code


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "localhost:5070", "127.0.0.1:5070"])
@pytest.mark.parametrize("owner", [OWNER, None])
def test_local_requests_reach_everything(host: str, owner: str | None) -> None:
    client = _client(owner)
    for path in ALLOWED + REFUSED[:5]:
        assert _get(client, path, host) == 200, path


@pytest.mark.parametrize("path", ALLOWED)
def test_owner_reaches_phone_routes(path: str) -> None:
    assert _get(_client(OWNER), path, TS_HOST, OWNER) == 200


@pytest.mark.parametrize("path", REFUSED)
def test_owner_cannot_reach_the_rest(path: str) -> None:
    assert _get(_client(OWNER), path, TS_HOST, OWNER) == 403


@pytest.mark.parametrize("login", [None, "", "colleague@example.com", "OWNER@example.com"])
def test_other_logins_are_refused(login: str | None) -> None:
    assert _get(_client(OWNER), "/m", TS_HOST, login) == 403


@pytest.mark.parametrize("owner", [None, "", "  "])
def test_unset_owner_refuses_every_remote_request(owner: str | None) -> None:
    client = _client(owner)
    assert _get(client, "/m", TS_HOST, OWNER) == 403
    assert _get(client, "/api/mobile/batch", f"{TS_HOST}:443", "") == 403


def test_refusal_body() -> None:
    res = _client(OWNER).post("/api/decks", headers={"host": TS_HOST})
    assert res.status_code == 403
    assert res.json() == {"detail": "Forbidden"}


@pytest.mark.parametrize(
    ("host", "remote"),
    [
        ("localhost", False),
        ("LOCALHOST:5070", False),
        ("127.0.0.1:5070", False),
        (TS_HOST, True),
        ("localhost.example.ts.net", True),
        ("100.64.0.1:5070", True),
        ("[::1]:5070", True),
        ("", True),
        (None, True),
    ],
)
def test_is_remote(host: str | None, remote: bool) -> None:
    assert gate.is_remote(host) is remote


def test_app_installs_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from anki_assistant.web import main

    monkeypatch.setenv("MOBILE_OWNER_LOGIN", OWNER)
    client = TestClient(main.create_app())
    assert _get(client, "/api/decks", TS_HOST, OWNER) == 403
    assert _get(client, "/static/themes.css", TS_HOST, OWNER) == 200
