"""FastAPI app factory and entry point. Routers live in routes_*.py, logic in the package root."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from anki_assistant import mobile
from anki_assistant.client import AnkiClient
from anki_assistant.sources import SourceStore
from anki_assistant.web.gate import TailnetGate

HERE = Path(__file__).parent
HOST = "127.0.0.1"
PORT = 5070


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
    # Tailnet requests reach only the phone app, and only from the owner's login.
    app.add_middleware(TailnetGate, owner=os.environ.get("MOBILE_OWNER_LOGIN") or None)
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html", {})

    return app


app = create_app()


def run() -> None:
    """Serve the app. Auto-reload unless ANKI_WEB_RELOAD=0 (the desktop launcher sets it)."""
    reload = os.environ.get("ANKI_WEB_RELOAD", "1") != "0"
    uvicorn.run("anki_assistant.web.main:app", host=HOST, port=PORT, reload=reload)


if __name__ == "__main__":
    run()
