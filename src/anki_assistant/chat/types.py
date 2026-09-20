"""Data types shared across the chat package.

Protocols, dataclasses, type aliases and config constants.  No business logic —
every other chat module imports from here, nothing here imports from them.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# --------------------------------------------------------------------------- config

DEFAULT_MODEL = "claude-opus-4-6"
MAX_TOKENS = 8192
#: Total budget for the attached sources block (block 3 of the system prompt).
MAX_ATTACHED_CHARS = 150_000
#: A `search_notes` result in `full` detail longer than this is refused with the count instead.
MAX_FULL_RESULT_CHARS = 100_000
#: Model calls per turn. Reads count: search -> get_notes -> propose -> comment is four calls.
MAX_TOOL_LOOPS = 8
#: Characters kept per field in a `brief` line.
BRIEF_FIELD_CHARS = 120
#: Cards a workspace holds at most (specs/workspace.md#how-notes-enter).
MAX_CARDS = 50
#: Web tool versions. These filter results server-side before they enter context, which needs
#: Opus 4.6 / Sonnet 4.6 or later — the floor `DEFAULT_MODEL` already sits on.
WEB_SEARCH_TYPE = "web_search_20260209"
WEB_FETCH_TYPE = "web_fetch_20260209"
#: Web tool calls per turn. Billed per search on top of tokens, hence a cap rather than none.
MAX_WEB_SEARCHES = 8
MAX_WEB_FETCHES = 5
#: URLs spelled out in a `web_search` reading line; the rest are counted.
WEB_URLS_SHOWN = 5
#: Characters of a citation's `cited_text` kept for the marker's tooltip. Search citations are
#: at most 150 by the API; fetch citations quote whole passages, hundreds of characters long.
CITED_TEXT_CHARS = 200


def default_model() -> str:
    """Model id for the chat: `ANKI_CHAT_MODEL` if set and non-empty, else `DEFAULT_MODEL`."""
    return (os.environ.get("ANKI_CHAT_MODEL") or "").strip() or DEFAULT_MODEL


# ----------------------------------------------------------------- injected interfaces


class NoteLike(Protocol):
    """The shape `review.NoteView` exposes; only what the read tools' output needs."""

    note_id: int
    deck: str
    model: str
    tags: list[str]
    fields: dict[str, str]
    reason: str

    @property
    def flagged_cards(self) -> Sequence[FlaggedCardLike]: ...


class FlaggedCardLike(Protocol):
    """One flagged card of a note; `ord` + 1 is the cloze number on a Cloze note."""

    ord: int


@dataclass
class WorkspaceCard:
    """One card of the workspace as the client sent it (specs/chat.md#api), for block 4.

    `fields` are the raw values of the shown version; `original_fields` are v0's when the shown
    version is not v0 (None otherwise). `note_id` is None for a draft note.
    """

    wid: str
    note_id: int | None
    fields: dict[str, str]
    deck: str = ""
    model: str = ""
    tags: list[str] = field(default_factory=list)
    active: bool = True
    original_fields: dict[str, str] | None = None
    #: Cloze numbers (c{ord+1}) of the flagged cards.
    flagged_clozes: list[int] = field(default_factory=list)
    reason: str = ""
    anchor_ids: list[str] = field(default_factory=list)
    deleted: bool = False
    keep: bool = False
    #: Deferred (« à revoir »): flag kept, `comment` written to `Back Extra` at validation.
    defer: bool = False
    comment: str = ""
    move_to: str | None = None
    parent_wid: str | None = None


class SourceLike(Protocol):
    """The shape `sources.Source` exposes; only what a source header needs.

    Read-only properties, not attributes: `Source.kind` is a `Literal`, which only satisfies a
    `str` member when that member cannot be written to.
    """

    @property
    def id(self) -> str: ...
    @property
    def deck(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def target(self) -> str: ...
    @property
    def pages(self) -> str: ...
    @property
    def note(self) -> str: ...


class SourceTextLike(Protocol):
    """The shape `sources.SourceText` exposes."""

    text: str
    truncated: bool
    n_pages: int | None
    warning: str
    extraction: str


@dataclass
class CorpusEntry:
    """One line of the corpus index (block 2): a source's header, never its text."""

    id: str
    kind: str
    target: str
    pages: str = ""
    note: str = ""
    deck: str = ""
    missing: bool = False
    #: Notes anchored to this source; the index marks those that are in context.
    anchored_note_ids: list[int] = field(default_factory=list)
    n_pages: int | None = None
    toc: list[tuple[int, str]] = field(default_factory=list)


class DeckLike(Protocol):
    """The shape `review.DeckSummary` exposes; only what `format_decks` needs."""

    name: str
    depth: int
    flagged_own: int
    flagged_total: int


class NoteTypeLike(Protocol):
    """The shape `models.NoteType` exposes."""

    name: str
    fields: list[str]
    templates: dict[str, dict[str, str]]
    css: str


CorpusLoader = Callable[[str], list[CorpusEntry]]
#: An attached source, loaded: the source and its extracted text.
Attached = tuple[SourceLike, SourceTextLike]
SourceLoader = Callable[[str], Attached]
#: A read tool: takes the tool input as given by the model, returns the text Claude will read.
ReadTool = Callable[[Mapping[str, Any]], str]


@dataclass
class ChatEvent:
    """One SSE event. `type` is one of "text", "citation", "reading", "added", "proposal",
    "done", "error"."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
