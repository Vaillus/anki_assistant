"""/api routes for review. Spec: specs/review.md#api.

A thin HTTP shell over `review.py`: parse the body, call the pure function, map errors.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from anki_assistant import review
from anki_assistant.client import AnkiClient
from anki_assistant.review import DeckNotes, DeckSummary, NoteView, SplitResult
from anki_assistant.sources import SourceStore
from anki_assistant.web.errors import anki_errors
from anki_assistant.workspace import anchors_after_move

router = APIRouter()


def _anki(request: Request) -> AnkiClient:
    return request.app.state.anki


def _store(request: Request) -> SourceStore:
    return request.app.state.store


_anki_errors = anki_errors


# ---------------------------------------------------------------------- bodies


class EditBody(BaseModel):
    fields: dict[str, str] | None = None
    tags: list[str] | None = None
    unflag: bool = True
    #: Card ids to flag again (chat undo, specs/chat.md#undo).
    reflag: list[int] | None = None


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
    #: Anchors of the new note (specs/sources.md#anchors); the chat defaults them client-side.
    source_ids: list[str] | None = None


class MoveBody(BaseModel):
    deck: str


class LookupBody(BaseModel):
    note_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------- routes


@router.get("/decks")
def get_decks(request: Request) -> list[DeckSummary]:
    with _anki_errors():
        return review.list_decks(_anki(request), _store(request))


@router.get("/notes")
def get_notes(request: Request, deck: str = Query(...)) -> DeckNotes:
    with _anki_errors():
        return review.list_notes(_anki(request), deck)


@router.post("/notes/lookup")
def lookup_notes(request: Request, body: LookupBody) -> list[NoteView]:
    """Several notes by id, unknown ids dropped, order kept — the workspace materialises its
    cards with it (specs/workspace.md#how-notes-enter)."""
    with _anki_errors():
        return review.get_notes(_anki(request), body.note_ids)


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
            reflag=body.reflag,
        )


@router.post("/notes/{note_id}/split")
def split_note(request: Request, note_id: int, body: SplitBody) -> SplitResult:
    store = _store(request)
    anchors = store.anchors(note_id)
    with _anki_errors():
        result = review.split(
            _anki(request),
            note_id,
            original=body.original.model_dump() if body.original is not None else None,
            new_notes=[n.model_dump(exclude_none=True) for n in body.new_notes],
        )
    # The fragments inherit the original's anchors (specs/sources.md#anchors).
    if anchors:
        for created in result.created:
            store.set_anchors(created.note_id, anchors)
        if result.original is None:
            store.remove_anchors([note_id])
    return result


@router.post("/notes")
def create_note(request: Request, body: CreateBody) -> NoteView:
    with _anki_errors():
        view = review.create(
            _anki(request), deck=body.deck, model=body.model, fields=body.fields, tags=body.tags
        )
    if body.source_ids:
        try:
            _store(request).set_anchors(view.note_id, body.source_ids)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return view


@router.post("/notes/{note_id}/move")
def move_note(request: Request, note_id: int, body: MoveBody) -> NoteView:
    store = _store(request)
    with _anki_errors():
        view = review.move(_anki(request), note_id, body.deck)
    # An anchor survives the move only if its source is in the destination's corpus.
    anchors_after_move(store, note_id, body.deck)
    return view


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
