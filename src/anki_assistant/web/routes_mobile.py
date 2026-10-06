"""/api/mobile routes: the phone app's batch, sync and media. Spec: specs/mobile.md#api.

A thin HTTP shell over `mobile.py`. The deck, rollover hour and log files are read from
`app.state` (set by `create_app` from the environment).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, model_validator

from anki_assistant import mobile
from anki_assistant.client import AnkiClient
from anki_assistant.web import routes_review
from anki_assistant.web.errors import anki_errors

router = APIRouter(prefix="/mobile")


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
        return mobile.build_batch(anki, deck, hour, datetime.now())


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
