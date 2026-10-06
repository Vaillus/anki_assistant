"""Tests for the tailnet gate and the phone port (web/gate.py, web/main.py,
specs/mobile.md#tailnet-gate)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from anki_assistant.web import gate, main, routes_mobile
from anki_assistant.web.gate import TailnetGate

OWNER = "owner@example.com"
TS_HOST = "macbook-pro-de-hugo-2.chamois-velociraptor.ts.net"


def _gated(owner: str | None) -> TestClient:
    app = FastAPI()
    app.add_middleware(TailnetGate, owner=owner)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def anything(path: str) -> dict[str, str]:
        return {"path": path}

    return TestClient(app)


def _get(client: TestClient, path: str, login: str | None = None, host: str = TS_HOST) -> int:
    headers = {"host": host}
    if login is not None:
        headers["Tailscale-User-Login"] = login
    return client.get(path, headers=headers).status_code


# ---------- the gate ----------


@pytest.mark.parametrize("host", [TS_HOST, "localhost", "127.0.0.1:5071"])
def test_owner_passes_whatever_the_host(host: str) -> None:
    assert _get(_gated(OWNER), "/anything", OWNER, host) == 200
    assert _get(_gated(OWNER), "/anything", f"  {OWNER} ", host) == 200


@pytest.mark.parametrize("host", [TS_HOST, "localhost", "127.0.0.1:5071"])
@pytest.mark.parametrize("login", [None, "", "colleague@example.com", "OWNER@example.com"])
def test_other_logins_are_refused_whatever_the_host(login: str | None, host: str) -> None:
    assert _get(_gated(OWNER), "/m", login, host) == 403


@pytest.mark.parametrize("owner", [None, "", "  "])
def test_unset_owner_refuses_everything(owner: str | None) -> None:
    client = _gated(owner)
    assert _get(client, "/m", OWNER) == 403
    assert _get(client, "/m", "", "localhost") == 403


def test_a_repeated_login_header_is_refused() -> None:
    assert not gate.allows([OWNER, OWNER], OWNER)
    assert not gate.allows([OWNER, "colleague@example.com"], OWNER)
    assert gate.allows([OWNER], OWNER)


def test_refusal_body() -> None:
    res = _gated(OWNER).post("/api/mobile/sync")
    assert res.status_code == 403
    assert res.json() == {"detail": "Forbidden"}


# ---------- the phone app ----------


@pytest.fixture
def phone(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MOBILE_OWNER_LOGIN", OWNER)
    return TestClient(main.create_phone_app(main.create_app()))


PHONE_PATHS = ["/m", "/m/manifest.webmanifest", "/m/sw.js"] + [
    f"/static/{name}" for name in routes_mobile.SHELL_STATIC
]


@pytest.mark.parametrize("path", PHONE_PATHS)
def test_owner_reaches_the_phone_app(phone: TestClient, path: str) -> None:
    assert _get(phone, path, OWNER) == 200


@pytest.mark.parametrize("path", [*PHONE_PATHS, "/api/mobile/batch", "/api/notes", "/"])
@pytest.mark.parametrize("login", [None, "colleague@example.com"])
def test_others_are_refused_on_every_path(phone: TestClient, path: str, login: str | None) -> None:
    assert _get(phone, path, login) == 403
    assert _get(phone, path, login, "localhost") == 403


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api/notes",
        "/api/decks",
        "/api/media/a.png",
        "/api/chat",
        "/static/app.js",
        "/static/workspace.js",
        "/static/../pyproject.toml",
        "/docs",
        "/openapi.json",
        "/mfoo",
    ],
)
def test_main_app_paths_are_404_on_the_phone_app(phone: TestClient, path: str) -> None:
    assert _get(phone, path, OWNER) == 404


def test_phone_app_shares_the_main_app_state() -> None:
    main_app = main.create_app()
    phone_app = main.create_phone_app(main_app)
    assert phone_app.state.anki is main_app.state.anki
    assert phone_app.state.mobile_log is main_app.state.mobile_log


def test_phone_api_reaches_anki_through_the_shared_client(phone: TestClient) -> None:
    class Anki:
        def retrieve_media_file(self, name: str) -> bytes:
            return b"img"

    phone.app.state.anki = Anki()  # ty: ignore[unresolved-attribute]
    res = phone.get("/api/mobile/media/a.png", headers={"Tailscale-User-Login": OWNER})
    assert res.status_code == 200
    assert res.content == b"img"


# ---------- the main app ----------


def test_main_app_has_no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOBILE_OWNER_LOGIN", OWNER)
    client = TestClient(main.create_app())
    for path in ["/", "/m", "/static/app.js", "/static/themes.css"]:
        assert _get(client, path, None, TS_HOST) == 200, path
        assert _get(client, path, "colleague@example.com", TS_HOST) == 200, path


# ---------- one server, two ports ----------


def _dispatch_to(port: int | None) -> str:
    seen: list[str] = []

    def app(name: str) -> ASGIApp:
        async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(name)

        return asgi

    async def receive() -> Message:
        return {}

    async def send(message: Message) -> None:
        pass

    dispatch = main.PortDispatch(app("main"), app("phone"), 5071)
    scope: Scope = {"type": "http", "server": None if port is None else ("127.0.0.1", port)}
    asyncio.run(dispatch(scope, receive, send))
    return seen[0]


def test_port_dispatch() -> None:
    assert _dispatch_to(5071) == "phone"
    assert _dispatch_to(5070) == "main"
    assert _dispatch_to(None) == "phone"  # unknown local port: fail closed
