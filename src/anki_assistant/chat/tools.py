"""Tool definitions for the chat: proposal, read, and web tools.

Schema helpers and the three `*_defs()` functions that return the tool
definitions Claude receives.  Also the constants that `stream` uses to
dispatch tool calls (`TOOL_KINDS`, `TARGETED`, `READ_TOOLS`, `WEB_TOOLS`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .types import (
    MAX_CARDS,
    MAX_WEB_FETCHES,
    MAX_WEB_SEARCHES,
    WEB_FETCH_TYPE,
    WEB_SEARCH_TYPE,
)

# -------------------------------------------------------------- dispatch constants

#: tool name -> the `kind` carried by the `proposal` SSE event.
TOOL_KINDS: dict[str, str] = {
    "propose_edit": "edit",
    "propose_split": "split",
    "propose_create": "create",
    "propose_move": "move",
    "propose_add_source": "add_source",
    "propose_create_source": "create_source",
    "propose_edit_source": "edit_source",
}

#: Proposals that bring a new source into the corpus: answered with the id it will carry.
NEW_SOURCE_KINDS: frozenset[str] = frozenset({"add_source", "create_source"})

#: Proposal tools whose `target` names a card (or a note to add as a card).
TARGETED: frozenset[str] = frozenset({"propose_edit", "propose_split", "propose_move"})

#: Read tools, executed server-side through the `read_tools` map given to `stream_chat`.
#: `add_notes` runs `get_notes` and additionally streams an `added` event.
READ_TOOLS: tuple[str, ...] = (
    "list_decks",
    "search_notes",
    "get_notes",
    "add_notes",
    "get_note_type",
    "read_source",
)

#: Tools Anthropic executes. Nothing here dispatches them; they are reported, not run.
WEB_TOOLS: tuple[str, ...] = ("web_search", "web_fetch")

#: Result block type -> the web tool that produced it.
WEB_RESULTS: dict[str, str] = {
    "web_search_tool_result": "web_search",
    "web_fetch_tool_result": "web_fetch",
}


# ------------------------------------------------------------------ schema helpers

_FIELDS_DESC = "Raw Anki field values, by field name. HTML allowed, cloze markers preserved."


def _fields_schema(description: str = _FIELDS_DESC) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "additionalProperties": {"type": "string"},
    }


_RATIONALE = {
    "type": "string",
    "description": "One sentence, in the user's language: why this change.",
}
_TAGS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Complete tag list after the change. Omit to leave tags untouched.",
}
_TARGET = {
    "type": "string",
    "description": (
        "Target card: its workspace identifier (« 3 »), or the Anki id (a number) of a note "
        "not yet in the workspace (it will be added)."
    ),
}
_MODEL = {
    "type": "string",
    "description": "Anki note type name. Omit to keep the starting note's type.",
}


def _obj(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


# ------------------------------------------------------------------ public builders


def tools() -> list[dict[str, Any]]:
    """Every tool definition: the proposal tools, then the read tools, then the web tools."""
    return proposal_tools() + read_tool_defs() + web_tool_defs()


def web_tool_defs() -> list[dict[str, Any]]:
    """The two Anthropic server tools. No `input_schema`: the API owns their shape.

    `allowed_callers` is pinned to direct calls. At its default these versions run the tools
    inside code execution and filter results before they reach context, and the reply then
    carries no citations at all (observed: a dozen code-execution round trips and not one
    `citations_delta`). Search results are always cited; fetched pages only when asked, hence
    `citations` on `web_fetch` (specs/chat.md#web-tools).
    """
    return [
        {
            "type": WEB_SEARCH_TYPE,
            "name": "web_search",
            "max_uses": MAX_WEB_SEARCHES,
            "allowed_callers": ["direct"],
        },
        {
            "type": WEB_FETCH_TYPE,
            "name": "web_fetch",
            "max_uses": MAX_WEB_FETCHES,
            "allowed_callers": ["direct"],
            "citations": {"enabled": True},
        },
    ]


def proposal_tools() -> list[dict[str, Any]]:
    """The seven proposal tools. Each call becomes one `proposal` event for the client."""
    return [
        {
            "name": "propose_edit",
            "description": (
                "Propose a rewrite of a card: a new version of the target card. Return only "
                "the fields that change, as complete raw values; the other fields of the "
                "shown version are kept as is. "
                "To change the note type (e.g. Cloze → Basic): pass `model` with the new "
                "type's name and give **all** fields of the target type (nothing carries over "
                "from the old version, the schemas differ). c1's scheduling history is kept; "
                "c2+ become orphaned until the next « Check Database »."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
                    "model": {
                        "type": "string",
                        "description": (
                            "New note type (e.g. « Basic »). Omit to keep the current type. "
                            "When it changes, `fields` must give all fields of the target type."
                        ),
                    },
                    "fields": _fields_schema(
                        "Changed fields only (or all fields of the target type if `model` "
                        "changes). Complete raw values. " + _FIELDS_DESC
                    ),
                    "tags": _TAGS,
                    "rationale": _RATIONALE,
                },
                required=["target", "fields", "rationale"],
            ),
        },
        {
            "name": "propose_split",
            "description": (
                "Propose cutting a card into several. By default the original note is kept "
                "and becomes the first fragment (give all its fields in `original`); set "
                "`original` to null to mark it deleted, each fragment then being a new note. "
                "Fragments appear as draft cards under the target card."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
                    "original": {
                        "description": (
                            "Fields of the original note after the split, or null to delete "
                            "it. Always give `model` (the fragment's note type). When the type "
                            "changes (e.g. Cloze → Basic), give **all** fields of the target "
                            "type (nothing carries over from the old version)."
                        ),
                        "anyOf": [
                            _obj(
                                {"model": _MODEL, "fields": _fields_schema()},
                                required=["model", "fields"],
                            ),
                            {"type": "null"},
                        ],
                    },
                    "new_notes": {
                        "type": "array",
                        "description": (
                            "Notes to create (all their fields), in the same deck and with the "
                            "same tags as the target card. Always give `model`."
                        ),
                        "minItems": 1,
                        "items": _obj(
                            {"model": _MODEL, "fields": _fields_schema()},
                            required=["model", "fields"],
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["target", "original", "new_notes", "rationale"],
            ),
        },
        {
            "name": "propose_create",
            "description": (
                "Propose an additional note (all its fields), in the current deck, without "
                "touching existing cards: a new draft card. The root's tags are carried over "
                "automatically."
            ),
            "input_schema": _obj(
                {
                    "fields": _fields_schema(),
                    "model": _MODEL,
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Sources (ids from the corpus index) the note is drawn from: its "
                            "anchors. Omit to carry over the root's."
                        ),
                    },
                    "rationale": _RATIONALE,
                },
                required=["fields", "rationale"],
            ),
        },
        {
            "name": "propose_move",
            "description": (
                "Propose moving a card to another deck (a badge on the card, applied on "
                "validation)."
            ),
            "input_schema": _obj(
                {
                    "target": _TARGET,
                    "deck": {
                        "type": "string",
                        "description": "Destination deck, full name with `::`.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["target", "deck", "rationale"],
            ),
        },
        {
            "name": "propose_add_source",
            "description": (
                "Propose adding to the current deck's corpus a source that already exists: a "
                "web page (its URL — the server re-reads it when needed, nothing is copied), a "
                "vault note, or a PDF on disk. Typically after a web_fetch on a page worth "
                "keeping. The tool result gives the id the source will carry once added: reuse "
                "it in propose_create's `source_ids`."
            ),
            "input_schema": _obj(
                {
                    "target": {
                        "type": "string",
                        "description": (
                            "http(s) URL of the page, name of a vault note (relative to its "
                            "root, without `.md`), or path of a PDF. The user can correct it "
                            "before applying."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["web", "obsidian", "pdf"],
                        "description": (
                            "Kind of the source. Omit to infer it from the target (URL → web, "
                            "`.pdf` → pdf, else obsidian)."
                        ),
                    },
                    "pages": {
                        "type": "string",
                        "description": "PDF only: page range, e.g. « 12-19 », « 3-5,9 ».",
                    },
                    "note": {
                        "type": "string",
                        "description": "Short comment displayed next to the source.",
                    },
                    "anchor_note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Anki notes to anchor to this source once added.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["target", "rationale"],
            ),
        },
        {
            "name": "propose_create_source",
            "description": (
                "Propose creating an Obsidian note in the vault and adding it to the current "
                "deck's corpus — to record what a conversation established (for a web page "
                "that already exists, prefer propose_add_source). The tool result gives the id "
                "the source will carry once created: reuse it in propose_create's "
                "`source_ids`."
            ),
            "input_schema": _obj(
                {
                    "name": {
                        "type": "string",
                        "description": (
                            "Note name, relative to the vault root, `/` allowed for a folder, "
                            "without `.md`. The user can edit it before applying."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete Markdown content of the note.",
                    },
                    "anchor_note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Anki notes to anchor to this source once created.",
                    },
                    "rationale": _RATIONALE,
                },
                required=["name", "content", "rationale"],
            ),
        },
        {
            "name": "propose_edit_source",
            "description": (
                "Propose replacing one passage of an Obsidian source with another. `old` must "
                "appear exactly once in the source (copy it verbatim); the replacement is "
                "refused otherwise. A pdf or web source cannot be edited."
            ),
            "input_schema": _obj(
                {
                    "source_id": {
                        "type": "string",
                        "description": "Source identifier (see the corpus index).",
                    },
                    "old": {"type": "string", "description": "Current passage, verbatim."},
                    "new": {"type": "string", "description": "Replacement passage."},
                    "rationale": _RATIONALE,
                },
                required=["source_id", "old", "new", "rationale"],
            ),
        },
    ]


def read_tool_defs() -> list[dict[str, Any]]:
    """The read tools. Executed by the server; their text output is the tool result."""
    return [
        {
            "name": "list_decks",
            "description": (
                "Read the deck tree with each deck's flagged note count. Use to locate the "
                "current deck or pick a move destination."
            ),
            "input_schema": _obj({}),
        },
        {
            "name": "search_notes",
            "description": (
                "Search notes with Anki's search syntax (e.g. `re:lagrang`, `Text:*KKT*`, "
                "`tag:convexity`, `flag:1`). The search is limited to the current deck and its "
                "subdecks unless the query names a deck (`deck:…`). `detail` picks the result's "
                "shape: `count` (the number alone), `brief` (one line per note, fields as "
                "truncated plain text — for spotting notes), `full` (complete raw fields, tags, "
                "flags, reason — for a format or structure audit, which truncated text does not "
                "allow)."
            ),
            "input_schema": _obj(
                {
                    "query": {"type": "string", "description": "Anki query."},
                    "detail": {
                        "type": "string",
                        "enum": ["count", "brief", "full"],
                        "description": "Result shape. Default: brief.",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            'With `full`: only return these fields (e.g. ["Text"]), when the '
                            "others are noise."
                        ),
                    },
                },
                required=["query"],
            ),
        },
        {
            "name": "get_notes",
            "description": (
                "Read notes in full (raw fields, tags, flags, reason), in the same format as "
                "the cards in context. Nothing changes in the workspace."
            ),
            "input_schema": _obj(
                {
                    "note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Note identifiers.",
                    }
                },
                required=["note_ids"],
            ),
        },
        {
            "name": "add_notes",
            "description": (
                "Add notes to the workspace so the user sees them and you can target them — "
                "typically after a search_notes. Returns the notes in full like get_notes. The "
                f"workspace holds {MAX_CARDS} cards at most: beyond that, the tool refuses and "
                "the selection must be narrowed."
            ),
            "input_schema": _obj(
                {
                    "note_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 1,
                        "description": "Identifiers of the notes to add.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "One sentence, in the user's language: why these notes.",
                    },
                },
                required=["note_ids", "rationale"],
            ),
        },
        {
            "name": "get_note_type",
            "description": (
                "Read the card templates (front / back) and CSS of a note type. Field names "
                "are already in context (« Note types » section); this tool is for inspecting "
                "the rendering."
            ),
            "input_schema": _obj(
                {"model": {"type": "string", "description": "Note type name."}},
                required=["model"],
            ),
        },
        {
            "name": "read_source",
            "description": (
                "Read the text of a corpus source (see the index) for this turn. The text is "
                "not kept for the next turn: if you still need it, read it again, or ask the "
                "user to attach the source. For a PDF, you can request specific pages with the "
                "pages parameter."
            ),
            "input_schema": _obj(
                {
                    "source_id": {
                        "type": "string",
                        "description": "Source identifier, as given in the index.",
                    },
                    "pages": {
                        "type": "string",
                        "description": (
                            "PDF only: page range to read, e.g. « 12-19 » or « 3-5,9 ». "
                            "Without this parameter, the source's associated pages are read."
                        ),
                    },
                },
                required=["source_id"],
            ),
        },
    ]
