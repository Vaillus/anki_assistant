"""/api routes for review. Spec: specs/review.md#api.

A thin HTTP shell over `review.py`: parse the body, call the pure function, map errors.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from anki_assistant import review
from anki_assistant.client import AnkiClient, AnkiConnectError
from anki_assistant.review import DeckNotes, DeckSummary, NoteNotFound, NoteView, SplitResult
from anki_assistant.sources import SourceStore

router = APIRouter()

# `AnkiClient.invoke` wraps a URLError with this prefix; it is the one AnkiConnect failure that
# means "Anki is not running" rather than "Anki refused the request".
UNREACHABLE_PREFIX = "Cannot reach"


# ------------------------------------------------------------------- plumbing


def _anki(request: Request) -> AnkiClient:
    return request.app.state.anki


def _store(request: Request) -> SourceStore:
    return request.app.state.store


@contextmanager
def _anki_errors() -> Iterator[None]:
    """Unknown note -> 404, Anki unreachable -> 503, any other AnkiConnect failure -> 502."""
    try:
        yield
    except NoteNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AnkiConnectError as exc:
        message = str(exc)
        status = 503 if message.startswith(UNREACHABLE_PREFIX) else 502
        raise HTTPException(status_code=status, detail=message) from exc


# ---------------------------------------------------------------------- bodies


class EditBody(BaseModel):
    fields: dict[str, str] | None = None
    tags: list[str] | None = None
    unflag: bool = True


class OriginalBody(BaseModel):
    fields: dict[str, str]
    tags: list[str] | None = None


class NewNoteBody(BaseModel):
    model: str | None = None
    fields: dict[str, str]
    tags: list[str] | None = None


class SplitBody(BaseModel):
    original: OriginalBody | None = None
    new_notes: list[NewNoteBody] = Field(default_factory=list)


class CreateBody(BaseModel):
    deck: str
    model: str
    fields: dict[str, str]
    tags: list[str] | None = None


class MoveBody(BaseModel):
    deck: str


# ---------------------------------------------------------------------- routes


@router.get("/decks")
def get_decks(request: Request) -> list[DeckSummary]:
    with _anki_errors():
        return review.list_decks(_anki(request), _store(request))


@router.get("/notes")
def get_notes(request: Request, deck: str = Query(...)) -> DeckNotes:
    with _anki_errors():
        return review.list_notes(_anki(request), deck)


@router.get("/notes/{note_id}")
def get_one_note(request: Request, note_id: int) -> NoteView:
    with _anki_errors():
        return review.get_note(_anki(request), note_id)


@router.post("/notes/{note_id}/keep")
def keep_note(request: Request, note_id: int) -> NoteView:
    with _anki_errors():
        return review.keep(_anki(request), note_id)


@router.patch("/notes/{note_id}")
def edit_note(request: Request, note_id: int, body: EditBody) -> NoteView:
    with _anki_errors():
        return review.edit(
            _anki(request),
            note_id,
            fields=body.fields,
            tags=body.tags,
            unflag=body.unflag,
        )


@router.post("/notes/{note_id}/split")
def split_note(request: Request, note_id: int, body: SplitBody) -> SplitResult:
    with _anki_errors():
        return review.split(
            _anki(request),
            note_id,
            original=body.original.model_dump() if body.original is not None else None,
            new_notes=[n.model_dump(exclude_none=True) for n in body.new_notes],
        )


@router.post("/notes")
def create_note(request: Request, body: CreateBody) -> NoteView:
    with _anki_errors():
        return review.create(
            _anki(request), deck=body.deck, model=body.model, fields=body.fields, tags=body.tags
        )


@router.post("/notes/{note_id}/move")
def move_note(request: Request, note_id: int, body: MoveBody) -> NoteView:
    with _anki_errors():
        return review.move(_anki(request), note_id, body.deck)


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(request: Request, note_id: int) -> Response:
    with _anki_errors():
        review.delete(_anki(request), note_id)
    return Response(status_code=204)


@router.get("/models")
def get_models(request: Request) -> dict[str, list[str]]:
    """Every note type mapped to its field names, for the model pickers in the dialogs."""
    with _anki_errors():
        return _anki(request).model_names_and_fields()
