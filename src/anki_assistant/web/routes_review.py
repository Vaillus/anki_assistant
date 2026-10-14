"""/api routes for review. Spec: specs/review.md#api.

A thin HTTP shell over `review.py`: parse the body, call the pure function, map errors.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from anki_assistant import deck_order, review
from anki_assistant.client import AnkiClient, SyncStatus
from anki_assistant.deck_order import DeckOrderStore, LabelInfo
from anki_assistant.review import (
    DeckNotes,
    DeckSummary,
    GroupEffects,
    NoteView,
    PriorityResult,
    SplitResult,
)
from anki_assistant.sources import SourceStore
from anki_assistant.web.errors import anki_errors
from anki_assistant.workspace import anchors_after_move

router = APIRouter()


def _anki(request: Request) -> AnkiClient:
    return request.app.state.anki


def _store(request: Request) -> SourceStore:
    return request.app.state.store


def _study_deck(request: Request) -> str | None:
    return request.app.state.study_deck


def _order(request: Request) -> DeckOrderStore:
    """The app deck order and labels (specs/review.md#where-the-app-stores-it)."""
    order = getattr(request.app.state, "deck_order", None)
    if order is None:
        order = request.app.state.deck_order = DeckOrderStore.in_memory()
    return order


def _protected(request: Request) -> list[str | None]:
    """The decks never turned into a label: the study deck and the mobile deck."""
    return [_study_deck(request), getattr(request.app.state, "mobile_deck", None)]


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


class CreateDeckBody(BaseModel):
    name: str


class MoveBody(BaseModel):
    deck: str


class DeckMoveBody(BaseModel):
    deck_id: int
    before: int | None = None
    after: int | None = None


class DeckLabelBody(BaseModel):
    deck_id: int
    #: An existing label; with neither this nor `name`, the label set on the deck comes off.
    label_id: str | None = None
    #: A new label's name.
    name: str | None = None


class GroupLabelBody(BaseModel):
    deck_id: int


class LookupBody(BaseModel):
    note_ids: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------- routes


class DecksResponse(BaseModel):
    decks: list[DeckSummary]
    labels: list[LabelInfo]
    priority_count: int


@router.get("/decks")
def get_decks(request: Request) -> DecksResponse:
    with _anki_errors(_anki(request)):
        listing = review.deck_listing(
            _anki(request), _store(request), _order(request), protected=_protected(request)
        )
        pcount = review.priority_count(_anki(request), study_deck=_study_deck(request))
    return DecksResponse(decks=listing.decks, labels=listing.labels, priority_count=pcount)


@router.post("/decks/move", status_code=204)
def move_deck(request: Request, body: DeckMoveBody) -> Response:
    """Move a deck among the decks of its tree parent (specs/review.md#moving-a-deck)."""
    with _anki_errors(_anki(request)):
        tree = review.app_tree(_anki(request), _order(request))
    try:
        deck_order.move(_order(request), tree, body.deck_id, before=body.before, after=body.after)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No deck with id {body.deck_id}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)


@router.put("/decks/label")
def set_deck_label(request: Request, body: DeckLabelBody) -> dict[str, str | None]:
    """Set a label on a deck, or take off the one set on it (specs/review.md#labels)."""
    with _anki_errors(_anki(request)):
        tree = review.app_tree(_anki(request), _order(request))
    try:
        if body.label_id is None and not body.name:
            deck_order.remove_label(_order(request), tree, body.deck_id)
            return {"label_id": None}
        lid = deck_order.set_label(
            _order(request), tree, body.deck_id, label_id=body.label_id, new_name=body.name
        )
    except deck_order.LabelRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown deck or label {exc}") from exc
    return {"label_id": lid}


@router.get("/decks/group-label")
def get_group_label_effects(request: Request, deck_id: int = Query(...)) -> GroupEffects:
    """What turning a group into a label would do, for the confirmation."""
    with _anki_errors(_anki(request)):
        try:
            return review.group_label_effects(
                _anki(request),
                _store(request),
                _order(request),
                deck_id,
                protected=_protected(request),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/decks/group-label")
def post_group_label(request: Request, body: GroupLabelBody) -> dict[str, str]:
    """Turn a group into a label (specs/review.md#labels)."""
    with _anki_errors(_anki(request)):
        try:
            lid = review.turn_group_into_label(
                _anki(request),
                _store(request),
                _order(request),
                body.deck_id,
                protected=_protected(request),
            )
        except deck_order.LabelRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"label_id": lid}


@router.post("/decks", status_code=201)
def create_deck(request: Request, body: CreateDeckBody) -> dict[str, str]:
    with _anki_errors(_anki(request)):
        try:
            name = review.create_deck(_anki(request), body.name)
        except review.DeckAlreadyExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"name": name}


@router.delete("/decks", status_code=204)
def delete_deck(request: Request, name: str = Query(...)) -> Response:
    with _anki_errors(_anki(request)):
        review.delete_deck(_anki(request), name)
    return Response(status_code=204)


@router.get("/notes/priority")
def get_priority_notes(request: Request) -> PriorityResult:
    with _anki_errors(_anki(request)):
        return review.priority_notes(
            _anki(request), study_deck=_study_deck(request), order=_order(request)
        )


@router.get("/notes")
def get_notes(request: Request, deck: str = Query(...)) -> DeckNotes:
    with _anki_errors(_anki(request)):
        return review.list_notes(_anki(request), deck, _order(request))


@router.post("/notes/lookup")
def lookup_notes(request: Request, body: LookupBody) -> list[NoteView]:
    """Several notes by id, unknown ids dropped, order kept — the workspace materialises its
    cards with it (specs/workspace.md#how-notes-enter)."""
    with _anki_errors(_anki(request)):
        return review.get_notes(_anki(request), body.note_ids)


@router.get("/notes/{note_id}")
def get_one_note(request: Request, note_id: int) -> NoteView:
    with _anki_errors(_anki(request)):
        return review.get_note(_anki(request), note_id)


@router.post("/notes/{note_id}/keep")
def keep_note(request: Request, note_id: int) -> NoteView:
    with _anki_errors(_anki(request)):
        return review.keep(_anki(request), note_id)


@router.patch("/notes/{note_id}")
def edit_note(request: Request, note_id: int, body: EditBody) -> NoteView:
    with _anki_errors(_anki(request)):
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
    with _anki_errors(_anki(request)):
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
    with _anki_errors(_anki(request)):
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
    with _anki_errors(_anki(request)):
        view = review.move(_anki(request), note_id, body.deck)
    # An anchor survives the move only if its source is in the destination's corpus.
    anchors_after_move(store, note_id, body.deck)
    return view


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(request: Request, note_id: int) -> Response:
    with _anki_errors(_anki(request)):
        review.delete(_anki(request), note_id)
    return Response(status_code=204)


@router.get("/models")
def get_models(request: Request) -> dict[str, list[str]]:
    """Every note type mapped to its field names, for the model pickers in the dialogs."""
    with _anki_errors(_anki(request)):
        return _anki(request).model_names_and_fields()


@router.get("/media/{filename}")
def get_media(request: Request, filename: str) -> Response:
    """One file from Anki's media folder, for the pictures pasted in notes
    (specs/review.md#rendering). The name must be a bare file name: no path separator, no
    `..`, so nothing outside the media folder can be asked for."""
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise HTTPException(status_code=404, detail=f"No media file {filename!r}")
    with _anki_errors():
        data = _anki(request).retrieve_media_file(filename)
    if data is None:
        raise HTTPException(status_code=404, detail=f"No media file {filename!r}")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # A media file is user data: never let the browser run it as a page (an SVG or HTML file
    # opened directly would otherwise execute on the app's origin).
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}
    return Response(content=data, media_type=media_type, headers=headers)


@router.get("/anki/sync")
def get_sync_status(request: Request) -> SyncStatus:
    """The sync status of the app's last AnkiWeb sync (specs/00-overview.md#ankiweb-sync),
    which the main page shows as a notice while it failed. Never opens the collection."""
    return _anki(request).last_sync or SyncStatus(ok=True, message=None)
