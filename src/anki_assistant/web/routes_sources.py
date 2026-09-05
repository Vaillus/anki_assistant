"""/api routes for sources. Spec: specs/sources.md.

Mounted by main.py under the `/api` prefix, so paths declared here are without it:
`GET /sources`, `GET /sources/corpus`, `PUT /sources`, `GET /vault/notes`.

NOTE for the frontend: deck names contain `::` and spaces, which do not survive a path segment
cleanly, so the single-source-of-truth write endpoint is `PUT /api/sources?deck=<name>` (a query
parameter), not `PUT /api/sources/{deck}` as the spec table's shorthand suggests.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, field_validator

from anki_assistant.sources import Kind, Source, SourceStore, is_valid_pages, vault_notes

router = APIRouter()


class SourceEntryIn(BaseModel):
    kind: Kind
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
        return v


class SourceView(BaseModel):
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


class CorpusResponse(BaseModel):
    deck: str
    inherited_from: str | None = None
    sources: list[SourceView]


class DeckSourcesResponse(BaseModel):
    deck: str
    sources: list[SourceView]


def _to_view(store: SourceStore, source: Source) -> SourceView:
    text = source.text(store.vault)
    return SourceView(
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
    )


@router.get("/sources")
def list_sources(request: Request) -> dict[str, list[dict]]:
    """The whole `decks` mapping, list-valued (never the legacy single-object form)."""
    store: SourceStore = request.app.state.store
    return {
        deck: [source.to_dict() for source in sources]
        for deck, sources in sorted(store.corpora.items())
    }


@router.get("/sources/corpus", response_model=CorpusResponse)
def get_corpus(deck: str, request: Request) -> CorpusResponse:
    store: SourceStore = request.app.state.store
    corpus = store.corpus(deck)
    inherited_from = None
    if corpus and corpus[0].deck != deck:
        inherited_from = corpus[0].deck
    return CorpusResponse(
        deck=deck,
        inherited_from=inherited_from,
        sources=[_to_view(store, source) for source in corpus],
    )


@router.put("/sources", response_model=DeckSourcesResponse)
def put_sources(deck: str, entries: list[SourceEntryIn], request: Request) -> DeckSourcesResponse:
    """Replace the corpus written on `deck` (query param). An empty list body deletes the entry."""
    store: SourceStore = request.app.state.store
    sources = [
        Source(deck=deck, kind=e.kind, target=e.target, pages=e.pages, note=e.note) for e in entries
    ]
    store.set_corpus(deck, sources)
    return DeckSourcesResponse(
        deck=deck, sources=[_to_view(store, source) for source in store.corpus(deck)]
    )


@router.get("/vault/notes")
def get_vault_notes(request: Request, q: str = "") -> list[str]:
    store: SourceStore = request.app.state.store
    return vault_notes(store.vault, q, limit=50)
