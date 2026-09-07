"""/api routes for chat: `POST /chat` (SSE) and `GET /chat/status`. Spec: specs/chat.md.

Mounted with prefix `/api` by `web/main.py`, so paths are declared without it.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from anki_assistant import review
from anki_assistant.chat import (
    ChatEvent,
    CorpusText,
    ReadTool,
    default_model,
    format_deck_index,
    format_decks,
    format_note_type,
    format_notes,
    stream_chat,
)
from anki_assistant.client import AnkiClient
from anki_assistant.sources import SourceStore

router = APIRouter()

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
NO_KEY_DETAIL = "ANTHROPIC_API_KEY absente : ajoute-la dans .env puis relance anki-web."


# ------------------------------------------------------------------------------- body


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    deck: str
    note_ids: list[int] = Field(default_factory=list)
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


def read_tools_for(anki: AnkiClient, store: SourceStore, deck: str) -> dict[str, ReadTool]:
    """The read tools of one turn (specs/chat.md#reading-tools), closed over the current deck.

    Each takes the tool input as the model sent it and returns the text Claude reads. Input
    problems raise ValueError; `stream_chat` turns any exception into an error tool result.
    """

    def list_decks(_inp: Mapping[str, Any]) -> str:
        return format_decks(review.list_decks(anki, store))

    def list_deck_notes(inp: Mapping[str, Any]) -> str:
        target = str(inp.get("deck") or deck)
        return format_deck_index(review.list_notes(anki, target).notes, target)

    def search_notes(inp: Mapping[str, Any]) -> str:
        query = str(inp.get("query") or "").strip()
        if not query:
            raise ValueError("query manquante")
        limit = int(inp.get("limit") or 50)
        return format_deck_index(review.search_notes(anki, query, limit))

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

    return {
        "list_decks": list_decks,
        "list_deck_notes": list_deck_notes,
        "search_notes": search_notes,
        "get_notes": get_notes,
        "get_note_type": get_note_type,
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
    store = request.app.state.store

    def load_note(note_id: int) -> Any:
        return review.get_note(anki, note_id)

    def load_corpus(deck: str) -> list[CorpusText]:
        out: list[CorpusText] = []
        for source in store.corpus(deck):
            extracted = source.text(store.vault)
            out.append(
                CorpusText(
                    kind=source.kind,
                    target=source.target,
                    text=extracted.text,
                    pages=getattr(source, "pages", "") or "",
                    deck=getattr(source, "deck", "") or "",
                    truncated=extracted.truncated,
                    warning=extracted.warning,
                    n_pages=extracted.n_pages,
                )
            )
        return out

    async def body_stream() -> AsyncIterator[str]:
        events = stream_chat(
            client,
            body.deck,
            body.note_ids,
            [message.model_dump() for message in body.messages],
            load_note,
            load_corpus,
            read_tools=read_tools_for(anki, store, body.deck),
            flagged_count=body.flagged_count,
        )
        async for event in events:
            yield _sse(event)

    return StreamingResponse(body_stream(), media_type="text/event-stream", headers=SSE_HEADERS)
