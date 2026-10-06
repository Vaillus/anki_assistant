"""/api/mobile routes: the phone app's batch, sync and media. Spec: specs/mobile.md#api.
Also the phone page itself under /m: the page, its manifest and its service worker
(specs/mobile.md#phone-page).

A thin HTTP shell over `mobile.py`. The deck, rollover hour and log files are read from
`app.state` (set by `create_app` from the environment).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator

from anki_assistant import mobile
from anki_assistant.client import AnkiClient
from anki_assistant.web import routes_review
from anki_assistant.web.errors import anki_errors

router = APIRouter(prefix="/mobile")
#: The phone page, mounted at the root (not under /api).
page_router = APIRouter()

WEB = Path(__file__).parent
STATIC = WEB / "static"
TEMPLATES = WEB / "templates"
templates = Jinja2Templates(directory=TEMPLATES)

#: Static files the phone page loads, precached by the service worker.
SHELL_STATIC = (
    "themes.css",
    "app.css",
    "mobile.css",
    "themes.js",
    "display.js",
    "mobile-queue.js",
    "mobile.js",
    "star.svg",
    "star-180.png",
)
#: The default dark theme's background (tokyo-night in themes.css), for the manifest.
THEME_COLOR = "#1a1b26"


def _anki(request: Request) -> AnkiClient:
    return request.app.state.anki


def _deck(request: Request) -> str:
    return getattr(request.app.state, "mobile_deck", None) or mobile.DEFAULT_DECK


def _rollover(request: Request) -> int:
    hour = getattr(request.app.state, "rollover_hour", None)
    return mobile.DEFAULT_ROLLOVER_HOUR if hour is None else int(hour)


def _log(request: Request) -> mobile.MobileLog:
    log = getattr(request.app.state, "mobile_log", None)
    return log if log is not None else mobile.MobileLog()


class ActionBody(BaseModel):
    id: str = Field(min_length=1)
    kind: Literal["answer", "flag", "unflag", "suspend"]
    card_id: int
    at: int  # ms since epoch
    ease: int | None = Field(default=None, ge=1, le=4)
    time_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _answer_needs_ease(self) -> ActionBody:
        if self.kind == "answer" and self.ease is None:
            raise ValueError("an answer needs an ease (1-4)")
        return self


class SyncBody(BaseModel):
    actions: list[ActionBody] = Field(default_factory=list)


@router.get("/batch")
def get_batch(request: Request) -> mobile.Batch:
    """A fresh batch, nothing applied."""
    with anki_errors():
        anki, deck, hour = _anki(request), _deck(request), _rollover(request)
        return mobile.build_batch(anki, deck, hour, datetime.now(), _log(request))


@router.post("/sync")
def post_sync(request: Request, body: SyncBody) -> mobile.SyncResult:
    """Apply the phone's pending actions oldest first, then return a fresh batch."""
    actions = [mobile.Action(**a.model_dump()) for a in body.actions]
    with anki_errors():
        return mobile.sync(
            _anki(request),
            actions,
            _log(request),
            deck=_deck(request),
            rollover_hour=_rollover(request),
        )


@router.get("/media/{filename}")
def get_media(request: Request, filename: str) -> Response:
    """The main app's media route, under the prefix the tailnet gate lets through."""
    return routes_review.get_media(request, filename)


# ---------- the phone page (specs/mobile.md#phone-page) ----------


def shell_version() -> str:
    """A hash of every file the page shell is made of: a change to any of them gives new
    static URLs and a new service worker, so the phone picks up the update."""
    digest = hashlib.sha256()
    for name in SHELL_STATIC:
        digest.update((STATIC / name).read_bytes())
    for name in ("mobile.html", "mobile-sw.js"):
        digest.update((TEMPLATES / name).read_bytes())
    return digest.hexdigest()[:12]


def _shell_urls(version: str) -> list[str]:
    return ["/m", f"/m/manifest.webmanifest?v={version}"] + [
        f"/static/{name}?v={version}" for name in SHELL_STATIC
    ]


@page_router.get("/m", response_class=HTMLResponse)
def mobile_page(request: Request) -> HTMLResponse:
    """The phone app."""
    response = templates.TemplateResponse(request, "mobile.html", {"v": shell_version()})
    response.headers["Cache-Control"] = "no-cache"
    return response


@page_router.get("/m/manifest.webmanifest")
def mobile_manifest() -> Response:
    """The web app manifest, for « Add to Home Screen »."""
    manifest = {
        "name": "Anki mobile reviewer",
        "short_name": "Anki",
        "start_url": "/m",
        "scope": "/m",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": "/static/star.svg", "sizes": "any", "type": "image/svg+xml"},
            {"src": "/static/star-180.png", "sizes": "180x180", "type": "image/png"},
        ],
    }
    return Response(content=json.dumps(manifest), media_type="application/manifest+json")


@page_router.get("/m/sw.js")
def mobile_service_worker() -> Response:
    """The service worker, with this shell version and its file list prepended. Its scope
    is `/m`, above its own directory, hence `Service-Worker-Allowed`."""
    version = shell_version()
    shell = json.dumps(_shell_urls(version))
    head = f"const VERSION = {json.dumps(version)};\nconst SHELL = {shell};\n"
    body = head + (TEMPLATES / "mobile-sw.js").read_text(encoding="utf-8")
    headers = {"Service-Worker-Allowed": "/m", "Cache-Control": "no-cache"}
    return Response(content=body, media_type="text/javascript", headers=headers)
