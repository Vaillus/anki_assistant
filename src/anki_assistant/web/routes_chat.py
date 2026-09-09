"""/api routes for chat: `POST /chat` (SSE) and `GET /chat/status`. Spec: specs/chat.md.

Mounted with prefix `/api` by `web/main.py`, so paths are declared without it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from anki_assistant import review
from anki_assistant.chat import (
    MAX_FULL_RESULT_CHARS,
    Attached,
    ChatEvent,
    CorpusEntry,
    ReadTool,
    SourceLoader,
    WorkspaceCard,
    default_model,
    format_decks,
    format_note_type,
    format_notes,
    format_notes_brief,
    format_source,
    stream_chat,
)
from anki_assistant.client import AnkiClient
from anki_assistant.sources import SourceStore

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
NO_KEY_DETAIL = "ANTHROPIC_API_KEY absente : ajoute-la dans .env puis relance anki-web."
#: Upper bound on the notes a `search_notes` call fetches from Anki (brief / full).
SEARCH_LIMIT = 5000

_NAMES_A_DECK = re.compile(r'(^|\s)-?"?deck:')


# ------------------------------------------------------------------------------- body


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class CardIn(BaseModel):
    """One card of the workspace as the client holds it (specs/chat.md#api)."""

    wid: str
    note_id: int | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    deck: str = ""
    model: str = ""
    tags: list[str] = Field(default_factory=list)
    active: bool = True
    original_fields: dict[str, str] | None = None
    flagged_clozes: list[int] = Field(default_factory=list)
    reason: str = ""
    anchor_ids: list[str] = Field(default_factory=list)
    deleted: bool = False
    keep: bool = False
    move_to: str | None = None
    parent_wid: str | None = None


class ChatRequest(BaseModel):
    deck: str
    #: The workspace, root first (block 4 of the system prompt).
    cards: list[CardIn] = Field(default_factory=list)
    #: Attached sources (block 3 of the system prompt), by source id.
    source_ids: list[str] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    #: Optional: number of flagged notes in the deck, shown to Claude as deck context.
    flagged_count: int | None = None


# ------------------------------------------------------------------------- api client

_clients: dict[str, Any] = {}


def _api_key() -> str:
    """`.env` is loaded by `web/main.py` (python-dotenv), so the env var is enough here."""
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def get_client() -> Any | None:
    """Lazily build the AsyncAnthropic client; None when no API key is configured."""
    key = _api_key()
    if not key:
        return None
    client = _clients.get(key)
    if client is None:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=key)
        _clients[key] = client
    return client


# ------------------------------------------------------------------------ read tools


def scope_query(query: str, deck: str) -> str:
    """Restrict an Anki search to `deck` and its sub-decks unless it names a deck itself."""
    if _NAMES_A_DECK.search(query):
        return query
    return f'"deck:{deck}" ({query})'


def read_tools_for(
    anki: AnkiClient, store: SourceStore, deck: str, load_source: SourceLoader
) -> dict[str, ReadTool]:
    """The read tools of one turn (specs/chat.md#read-tools), closed over the current deck.

    Each takes the tool input as the model sent it and returns the text Claude reads. Input
    problems raise ValueError; `stream_chat` turns any exception into an error tool result.
    """

    def list_decks(_inp: Mapping[str, Any]) -> str:
        return format_decks(review.list_decks(anki, store))

    def search_notes(inp: Mapping[str, Any]) -> str:
        query = str(inp.get("query") or "").strip()
        if not query:
            raise ValueError("query manquante")
        detail = str(inp.get("detail") or "brief")
        if detail not in ("count", "brief", "full"):
            raise ValueError(f"detail inconnu : {detail} (count, brief ou full)")
        scoped = scope_query(query, deck)
        if detail == "count":
            return f"# {len(anki.find_note_ids(scoped))} note(s) correspondent à {query}"
        views = review.search_notes(anki, scoped, limit=SEARCH_LIMIT)
        if detail == "brief":
            return format_notes_brief(views, deck)
        wanted = [str(name) for name in inp.get("fields") or []]
        if wanted:
            views = [
                replace(v, fields={k: val for k, val in v.fields.items() if k in wanted})
                for v in views
            ]
        text = format_notes(views)
        if len(text) > MAX_FULL_RESULT_CHARS:
            return (
                f"# {len(views)} note(s) correspondent — résultat non renvoyé\n\n"
                f"En detail=full il ferait {len(text)} caractères, plus que le plafond de "
                f"{MAX_FULL_RESULT_CHARS}. Affine la requête ou limite `fields` aux champs "
                "utiles ; detail=brief donne l'aperçu."
            )
        return text

    def get_notes(inp: Mapping[str, Any]) -> str:
        ids = [int(i) for i in inp.get("note_ids") or []]
        if not ids:
            raise ValueError("note_ids vide")
        return format_notes(review.get_notes(anki, ids))

    def get_note_type(inp: Mapping[str, Any]) -> str:
        model = str(inp.get("model") or "").strip()
        if not model:
            raise ValueError("model manquant")
        return format_note_type(anki.note_type(model))

    def read_source(inp: Mapping[str, Any]) -> str:
        source_id = str(inp.get("source_id") or "").strip()
        if not source_id:
            raise ValueError("source_id manquant")
        source, text = load_source(source_id)
        return format_source(source, text)

    return {
        "list_decks": list_decks,
        "search_notes": search_notes,
        "get_notes": get_notes,
        "get_note_type": get_note_type,
        "read_source": read_source,
    }


# ------------------------------------------------------------------------------- sse


def _sse(event: ChatEvent) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


async def _one_error(detail: str) -> AsyncIterator[str]:
    yield _sse(ChatEvent("error", {"detail": detail}))


# ---------------------------------------------------------------------------- routes


@router.get("/chat/status")
def chat_status() -> dict[str, Any]:
    return {"configured": bool(_api_key()), "model": default_model()}


@router.post("/chat")
def post_chat(request: Request, body: ChatRequest) -> StreamingResponse:
    client = get_client()
    if client is None:
        return StreamingResponse(
            _one_error(NO_KEY_DETAIL), media_type="text/event-stream", headers=SSE_HEADERS
        )

    anki = request.app.state.anki
    store: SourceStore = request.app.state.store

    def load_corpus(deck: str) -> list[CorpusEntry]:
        return [
            CorpusEntry(
                id=source.id,
                kind=source.kind,
                target=source.target,
                pages=source.pages,
                note=source.note,
                deck=source.deck,
                missing=not source.exists(store.vault),
                anchored_note_ids=store.anchors_to(source.id),
            )
            for source in store.corpus(deck)
        ]

    def load_source(source_id: str) -> Attached:
        source = store.by_id(source_id)
        if source is None:
            raise KeyError(f"source inconnue : {source_id}")
        return source, source.text(store.vault)

    async def body_stream() -> AsyncIterator[str]:
        events = stream_chat(
            client,
            body.deck,
            [WorkspaceCard(**card.model_dump()) for card in body.cards],
            body.source_ids,
            [message.model_dump() for message in body.messages],
            load_corpus,
            load_source,
            read_tools=read_tools_for(anki, store, body.deck, load_source),
            flagged_count=body.flagged_count,
        )
        async for event in events:
            yield _sse(event)

    return StreamingResponse(body_stream(), media_type="text/event-stream", headers=SSE_HEADERS)
