"""/api routes for sources and anchors. Spec: specs/sources.md#api.

Mounted by main.py under the `/api` prefix, so paths declared here are without it.

NOTE for the frontend: deck names contain `::` and spaces, which do not survive a path segment
cleanly, so a deck always travels as a query parameter (`?deck=<name>`). Source ids are
path-safe and go in the path (`/sources/{source_id}/text`).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from anki_assistant import review
from anki_assistant.client import AnkiClient
from anki_assistant.sources import (
    Kind,
    Source,
    SourceStore,
    detect_kind,
    is_url,
    is_valid_pages,
    vault_notes,
)
from anki_assistant.web.routes_review import _anki_errors

router = APIRouter()


# ---------------------------------------------------------------------- bodies


class SourceEntryIn(BaseModel):
    #: Empty for a new entry: the store assigns one. Sent back as-is for an existing entry so
    #: that rewriting a corpus (the form) keeps the ids the anchors point to.
    id: str = ""
    #: Omitted = auto-detected from the target (specs/sources.md#source-entry).
    kind: Kind | None = None
    target: str
    pages: str = ""
    note: str = ""

    @field_validator("pages")
    @classmethod
    def _pages_valid(cls, v: str) -> str:
        if not is_valid_pages(v):
            raise ValueError("pages must look like '12-19', '7' or '3-5,9'")
        return v

    @field_validator("target")
    @classmethod
    def _target_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("target must not be blank")
        return v.strip()

    @model_validator(mode="after")
    def _kind_fits_target(self) -> SourceEntryIn:
        if self.kind is None:
            self.kind = detect_kind(self.target)
        if self.kind == "web" and not is_url(self.target):
            raise ValueError("a web target must be an http(s) URL")
        if self.pages and self.kind != "pdf":
            raise ValueError("pages only apply to a pdf source")
        return self

    def to_source(self, deck: str) -> Source:
        return Source(
            deck=deck,
            kind=self.kind or detect_kind(self.target),
            target=self.target,
            pages=self.pages,
            note=self.note,
            id=self.id,
        )


class AddSourceIn(SourceEntryIn):
    """`POST /api/sources`: one entry appended to the deck's own corpus."""

    #: Notes to anchor to the new source in the same call.
    anchor_note_ids: list[int] = Field(default_factory=list)


class SourceView(BaseModel):
    id: str
    kind: str
    target: str
    pages: str = ""
    note: str = ""
    on_deck: str
    exists: bool
    uri: str
    text: str
    truncated: bool
    n_pages: int | None = None
    warning: str = ""
    anchored_count: int = 0


class CorpusResponse(BaseModel):
    deck: str
    inherited_from: str | None = None
    #: The `note_id` query parameter's anchors, in order; [] without one.
    anchored: list[str] = Field(default_factory=list)
    sources: list[SourceView]


class DeckSourcesResponse(BaseModel):
    deck: str
    sources: list[SourceView]
    removed_anchors: int = 0


class AnchorView(BaseModel):
    source_id: str
    #: valid = in the effective corpus of the note's deck; dangling = known source, other corpus.
    status: Literal["valid", "dangling"]


class AnchorsResponse(BaseModel):
    note_id: int
    anchors: list[AnchorView]


class AnchorsIn(BaseModel):
    source_ids: list[str]


class VaultNoteIn(BaseModel):
    deck: str
    name: str
    content: str
    anchor_note_ids: list[int] = Field(default_factory=list)
    #: Optional id announced beforehand (the chat's create-source proposal), see specs/chat.md.
    id: str | None = None


class ReplaceIn(BaseModel):
    old: str
    new: str


class SourceResponse(BaseModel):
    deck: str | None = None
    source: SourceView


# ------------------------------------------------------------------- plumbing


def _store(request: Request) -> SourceStore:
    return request.app.state.store


def _anki(request: Request) -> AnkiClient:
    return request.app.state.anki


def _to_view(store: SourceStore, source: Source) -> SourceView:
    text = source.text(store.vault)
    return SourceView(
        id=source.id,
        kind=source.kind,
        target=source.target,
        pages=source.pages,
        note=source.note,
        on_deck=source.deck,
        exists=source.exists(store.vault),
        uri=source.uri(store.vault),
        text=text.text,
        truncated=text.truncated,
        n_pages=text.n_pages,
        warning=text.warning,
        anchored_count=len(store.anchors_to(source.id)),
    )


def _anchors_response(store: SourceStore, anki: AnkiClient, note_id: int) -> AnchorsResponse:
    with _anki_errors():
        deck = review.get_note(anki, note_id).deck
    in_corpus = {source.id for source in store.corpus(deck)}
    return AnchorsResponse(
        note_id=note_id,
        anchors=[
            AnchorView(source_id=sid, status="valid" if sid in in_corpus else "dangling")
            for sid in store.anchors(note_id)
        ],
    )


def _orphans(store: SourceStore, anki: AnkiClient) -> list[int]:
    """Anchored note ids that Anki no longer knows."""
    ids = store.anchored_note_ids()
    if not ids:
        return []
    with _anki_errors():
        known = {note.note_id for note in anki.notes_info(ids)}
    return [note_id for note_id in ids if note_id not in known]


# ---------------------------------------------------------------------- corpus


@router.get("/sources")
def list_sources(request: Request) -> dict[str, list[dict]]:
    """The whole `decks` mapping, list-valued (never the legacy single-object form)."""
    store = _store(request)
    return {
        deck: [source.to_dict() for source in sources]
        for deck, sources in sorted(store.corpora.items())
    }


@router.get("/sources/corpus", response_model=CorpusResponse)
def get_corpus(deck: str, request: Request, note_id: int | None = None) -> CorpusResponse:
    store = _store(request)
    corpus = store.corpus(deck)
    inherited_from = None
    if corpus and corpus[0].deck != deck:
        inherited_from = corpus[0].deck
    return CorpusResponse(
        deck=deck,
        inherited_from=inherited_from,
        anchored=store.anchors(note_id) if note_id is not None else [],
        sources=[_to_view(store, source) for source in corpus],
    )


@router.put("/sources", response_model=DeckSourcesResponse)
def put_sources(deck: str, entries: list[SourceEntryIn], request: Request) -> DeckSourcesResponse:
    """Replace the corpus written on `deck` (query param). An empty list body deletes the entry."""
    store = _store(request)
    _, removed = store.set_corpus(deck, [e.to_source(deck) for e in entries])
    return DeckSourcesResponse(
        deck=deck,
        sources=[_to_view(store, source) for source in store.corpus(deck)],
        removed_anchors=removed,
    )


@router.post("/sources", response_model=SourceResponse)
def add_source(deck: str, body: AddSourceIn, request: Request) -> SourceResponse:
    """Append one entry to the corpus written on `deck` (query param).

    An inherited corpus is materialised on the deck first (specs/sources.md#api). `id` lets the
    chat keep the id it announced to Claude; `anchor_note_ids` anchors those notes to it.
    """
    store = _store(request)
    try:
        source = store.add_source(deck, body.to_source(deck))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for note_id in body.anchor_note_ids:
        store.add_anchor(note_id, source.id)
    return SourceResponse(deck=deck, source=_to_view(store, source))


@router.get("/vault/notes")
def get_vault_notes(request: Request, q: str = "") -> list[str]:
    return vault_notes(_store(request).vault, q, limit=50)


# --------------------------------------------------------------------- anchors


@router.get("/sources/anchors", response_model=AnchorsResponse)
def get_anchors(note_id: int, request: Request) -> AnchorsResponse:
    return _anchors_response(_store(request), _anki(request), note_id)


@router.put("/sources/anchors", response_model=AnchorsResponse)
def put_anchors(note_id: int, body: AnchorsIn, request: Request) -> AnchorsResponse:
    store = _store(request)
    try:
        store.set_anchors(note_id, body.source_ids)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _anchors_response(store, _anki(request), note_id)


@router.get("/sources/anchors/orphans")
def get_orphan_anchors(request: Request) -> dict[str, list[int]]:
    return {"note_ids": _orphans(_store(request), _anki(request))}


@router.post("/sources/anchors/prune")
def prune_anchors(request: Request) -> dict[str, int]:
    store = _store(request)
    return {"removed": store.remove_anchors(_orphans(store, _anki(request)))}


# ---------------------------------------------------------------- vault writes


@router.post("/sources/notes", response_model=SourceResponse)
def create_vault_note(body: VaultNoteIn, request: Request) -> SourceResponse:
    """Create a note in the vault and add it to the deck's corpus. 409 if the file exists."""
    store = _store(request)
    try:
        source = store.create_note(body.deck, body.name, body.content, source_id=body.id)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for note_id in body.anchor_note_ids:
        store.add_anchor(note_id, source.id)
    return SourceResponse(deck=body.deck, source=_to_view(store, source))


@router.patch("/sources/{source_id}/text", response_model=SourceResponse)
def replace_source_text(source_id: str, body: ReplaceIn, request: Request) -> SourceResponse:
    """Replace one passage of an obsidian source. 409 when `old` occurs 0 or 2+ times; 400 on
    a pdf or web source, which are read-only."""
    store = _store(request)
    source = store.by_id(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"source inconnue : {source_id}")
    if source.kind != "obsidian":
        raise HTTPException(status_code=400, detail="seule une note Obsidian peut être modifiée")
    try:
        store.replace_in_note(source_id, body.old, body.new)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SourceResponse(deck=source.deck, source=_to_view(store, source))
