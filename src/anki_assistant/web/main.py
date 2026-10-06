"""App factories and entry point. Routers live in routes_*.py, logic in the package root.

One process serves two apps on two ports, both bound to 127.0.0.1: the main app on PORT, and
the phone app alone on the phone port, the only one exposed on the tailnet, behind the
tailnet gate (specs/mobile.md#tailnet-gate).
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from anki_assistant import mobile
from anki_assistant.client import AnkiClient
from anki_assistant.sources import SourceStore
from anki_assistant.web.gate import TailnetGate

HERE = Path(__file__).parent
HOST = "127.0.0.1"
PORT = 5070
DEFAULT_MOBILE_PORT = 5071


def create_app() -> FastAPI:
    load_dotenv()
    app = FastAPI(title="anki-assistant")
    app.state.anki = AnkiClient(url=os.environ.get("ANKI_CONNECT_URL") or "http://localhost:8765")
    app.state.store = SourceStore()
    app.state.study_deck = os.environ.get("STUDY_DECK") or None
    app.state.last_validation = None  # snapshot of the last workspace validation (undo)
    app.state.mobile_deck = os.environ.get("MOBILE_DECK") or mobile.DEFAULT_DECK
    app.state.rollover_hour = int(
        os.environ.get("ANKI_ROLLOVER_HOUR") or mobile.DEFAULT_ROLLOVER_HOUR
    )
    app.state.mobile_log = mobile.MobileLog()
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    from anki_assistant.web import (
        routes_chat,
        routes_mobile,
        routes_review,
        routes_sources,
        routes_workspace,
    )

    app.include_router(routes_review.router, prefix="/api")
    app.include_router(routes_sources.router, prefix="/api")
    app.include_router(routes_chat.router, prefix="/api")
    app.include_router(routes_workspace.router, prefix="/api")
    app.include_router(routes_mobile.router, prefix="/api")
    app.include_router(routes_mobile.page_router)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {})

    return app


def create_phone_app(main_app: FastAPI) -> FastAPI:
    """The phone app: only the page under /m, its shell files and /api/mobile, sharing the
    main app's state (AnkiClient, mobile deck, rollover hour, review log), every request
    behind the tailnet gate."""
    from anki_assistant.web import routes_mobile

    load_dotenv()
    phone = FastAPI(title="anki-assistant phone", docs_url=None, redoc_url=None, openapi_url=None)
    phone.state = main_app.state
    phone.add_middleware(TailnetGate, owner=os.environ.get("MOBILE_OWNER_LOGIN") or None)
    phone.include_router(routes_mobile.router, prefix="/api")
    phone.include_router(routes_mobile.page_router)
    phone.include_router(routes_mobile.shell_static_router)
    return phone


def mobile_port() -> int:
    return int(os.environ.get("MOBILE_PORT") or DEFAULT_MOBILE_PORT)


class PortDispatch:
    """One ASGI app over both listening sockets: a request is handed to the phone app when it
    arrived on the phone port, or when its local port is unknown (fail closed), and to the
    main app otherwise. The local port comes from the socket, not from the request."""

    def __init__(self, main_app: ASGIApp, phone_app: ASGIApp, phone_port: int) -> None:
        self.main_app = main_app
        self.phone_app = phone_app
        self.phone_port = phone_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.main_app(scope, receive, send)
            return
        server = scope.get("server")
        if server is None or server[1] == self.phone_port:
            await self.phone_app(scope, receive, send)
        else:
            await self.main_app(scope, receive, send)


app = create_app()
phone_app = create_phone_app(app)
#: What uvicorn serves: both apps, told apart by the port a request arrived on.
server_app = PortDispatch(app, phone_app, mobile_port())


def serve(port: int = PORT, phone_port: int | None = None, reload: bool = False) -> None:
    """Listen on 127.0.0.1 at `port` (main app) and `phone_port` (phone app) in one uvicorn
    server. With `reload`, a file change restarts the worker serving both sockets."""
    from uvicorn.supervisors import ChangeReload

    phone_port = mobile_port() if phone_port is None else phone_port
    if phone_port == port:
        raise SystemExit(f"MOBILE_PORT must differ from the main port ({port})")
    # The reloader's worker process rebuilds `server_app` from the environment.
    os.environ["MOBILE_PORT"] = str(phone_port)
    target = (
        "anki_assistant.web.main:server_app" if reload else PortDispatch(app, phone_app, phone_port)
    )
    config = uvicorn.Config(target, host=HOST, port=port, reload=reload)
    sockets = [
        config.bind_socket(),
        uvicorn.Config(target, host=HOST, port=phone_port).bind_socket(),
    ]
    server = uvicorn.Server(config)
    if config.should_reload:
        ChangeReload(config, target=server.run, sockets=sockets).run()
    else:
        server.run(sockets=sockets)


def run() -> None:
    """Serve both apps. Auto-reload unless ANKI_WEB_RELOAD=0 (the desktop launcher sets it)."""
    serve(reload=os.environ.get("ANKI_WEB_RELOAD", "1") != "0")


if __name__ == "__main__":
    run()
