"""Thin client over the AnkiConnect HTTP API (https://foosoft.net/projects/anki-connect/).

Anki must be running with the AnkiConnect add-on installed (default port 8765).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from anki_assistant.models import Card, Note, NoteType

DEFAULT_URL = "http://localhost:8765"
API_VERSION = 6


class AnkiConnectError(RuntimeError):
    """Raised when AnkiConnect is unreachable or returns an error."""


def _quote(value: str) -> str:
    """Quote a value for use inside an Anki search query."""
    return '"' + value.replace('"', '\\"') + '"'


class AnkiClient:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout

    # ------------------------------------------------------------------ core

    def invoke(self, action: str, **params: Any) -> Any:
        """Call any AnkiConnect action and return its `result`."""
        payload = json.dumps({"action": action, "version": API_VERSION, "params": params})
        req = urllib.request.Request(
            self.url, data=payload.encode(), headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.load(resp)
        except urllib.error.URLError as exc:
            raise AnkiConnectError(
                f"Cannot reach AnkiConnect at {self.url}. Is Anki running with the add-on? ({exc})"
            ) from exc
        if not isinstance(body, dict) or "error" not in body or "result" not in body:
            raise AnkiConnectError(f"Unexpected response for {action!r}: {body!r}")
        if body["error"] is not None:
            raise AnkiConnectError(f"{action}: {body['error']}")
        return body["result"]

    def invoke_multi(self, calls: Sequence[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Run several actions in one HTTP round trip and return their results, in order.

        Uses AnkiConnect's `multi`. Each sub-action carries its own version so that failures
        come back as `{"result": ..., "error": ...}` instead of a silent `None`.
        """
        if not calls:
            return []
        actions = [
            {"action": action, "version": API_VERSION, "params": params} for action, params in calls
        ]
        raw = self.invoke("multi", actions=actions)
        if not isinstance(raw, list) or len(raw) != len(actions):
            raise AnkiConnectError(f"Unexpected 'multi' response: {raw!r}")
        results: list[Any] = []
        for (action, _), item in zip(calls, raw, strict=True):
            if isinstance(item, dict) and "error" in item and "result" in item:
                if item["error"] is not None:
                    raise AnkiConnectError(f"{action}: {item['error']}")
                results.append(item["result"])
            else:  # older AnkiConnect: bare result, no envelope
                results.append(item)
        return results

    def version(self) -> int:
        return int(self.invoke("version"))

    def sync(self) -> None:
        self.invoke("sync")

    # ----------------------------------------------------------------- decks

    def deck_names(self) -> list[str]:
        return sorted(self.invoke("deckNames"))

    def deck_names_and_ids(self) -> dict[str, int]:
        return self.invoke("deckNamesAndIds")

    def deck_stats(self, *deck_names: str) -> dict[str, Any]:
        """Per-deck counts (new/learn/review/total). Keyed by deck id as a string."""
        return self.invoke("getDeckStats", decks=list(deck_names))

    # ---------------------------------------------------------------- search

    def find_card_ids(self, query: str) -> list[int]:
        return self.invoke("findCards", query=query)

    def find_note_ids(self, query: str) -> list[int]:
        return self.invoke("findNotes", query=query)

    def cards_info(self, card_ids: list[int]) -> list[Card]:
        """Cards by id. AnkiConnect returns `{}` for an unknown id; those are dropped."""
        if not card_ids:
            return []
        return [Card.from_api(c) for c in self.invoke("cardsInfo", cards=card_ids) if c]

    def notes_info(self, note_ids: list[int]) -> list[Note]:
        """Notes by id. AnkiConnect returns `{}` for an unknown id; those are dropped."""
        if not note_ids:
            return []
        return [Note.from_api(n) for n in self.invoke("notesInfo", notes=note_ids) if n]

    def find_cards(self, query: str) -> list[Card]:
        return self.cards_info(self.find_card_ids(query))

    def find_notes(self, query: str) -> list[Note]:
        return self.notes_info(self.find_note_ids(query))

    def cards_in_deck(self, deck: str, extra_query: str = "") -> list[Card]:
        """All cards of a deck (including sub-decks). `extra_query` is appended verbatim."""
        query = f"deck:{_quote(deck)} {extra_query}".strip()
        return self.find_cards(query)

    def flagged_cards(self, deck: str | None = None, flag: int | None = None) -> list[Card]:
        """Cards carrying a flag. `flag` restricts to one colour (1-7); None means any flag."""
        parts = []
        if deck:
            parts.append(f"deck:{_quote(deck)}")
        parts.append(f"flag:{flag}" if flag else "-flag:0")
        return self.find_cards(" ".join(parts))

    def card(self, card_id: int) -> Card:
        cards = self.cards_info([card_id])
        if not cards:
            raise AnkiConnectError(f"No card with id {card_id}")
        return cards[0]

    def note(self, note_id: int) -> Note:
        notes = self.notes_info([note_id])
        if not notes:
            raise AnkiConnectError(f"No note with id {note_id}")
        return notes[0]

    def note_card_ids(self, note_id: int) -> list[int]:
        """Ids of every card of a note, wherever those cards live."""
        return self.find_card_ids(f"nid:{note_id}")

    # ------------------------------------------------------------------ edit

    def update_note_fields(self, note_id: int, fields: dict[str, str]) -> None:
        """Overwrite the given fields of a note (other fields untouched). Values are HTML."""
        self.invoke("updateNoteFields", note={"id": note_id, "fields": fields})

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        note: dict[str, Any] = {"id": note_id}
        if fields is not None:
            note["fields"] = fields
        if tags is not None:
            note["tags"] = tags
        self.invoke("updateNote", note=note)

    def add_tags(self, note_ids: list[int], tags: str) -> None:
        """`tags` is a space-separated string, as Anki expects."""
        self.invoke("addTags", notes=note_ids, tags=tags)

    def remove_tags(self, note_ids: list[int], tags: str) -> None:
        self.invoke("removeTags", notes=note_ids, tags=tags)

    def set_flag(self, card_ids: list[int], flag: int) -> None:
        """Set the flag colour (0 clears) on the given cards, in a single round trip."""
        if not 0 <= flag <= 7:
            raise ValueError("flag must be between 0 (none) and 7")
        self.invoke_multi(
            [
                ("setSpecificValueOfCard", {"card": cid, "keys": ["flags"], "newValues": [flag]})
                for cid in card_ids
            ]
        )

    def clear_flag(self, card_ids: list[int]) -> None:
        self.set_flag(card_ids, 0)

    def unflag_note(self, note_id: int) -> None:
        """Clear the flag on every card of a note (the review unit is the note)."""
        self.clear_flag(self.note_card_ids(note_id))

    def suspend(self, card_ids: list[int]) -> None:
        self.invoke("suspend", cards=card_ids)

    def unsuspend(self, card_ids: list[int]) -> None:
        self.invoke("unsuspend", cards=card_ids)

    def change_deck(self, card_ids: list[int], deck: str) -> None:
        self.invoke("changeDeck", cards=card_ids, deck=deck)

    def add_note(
        self,
        deck: str,
        model: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
    ) -> int:
        note = {
            "deckName": deck,
            "modelName": model,
            "fields": fields,
            "tags": tags or [],
            "options": {"allowDuplicate": allow_duplicate},
        }
        return int(self.invoke("addNote", note=note))

    def delete_notes(self, note_ids: list[int]) -> None:
        self.invoke("deleteNotes", notes=note_ids)

    def update_note_model(
        self,
        note_id: int,
        model: str,
        fields: dict[str, str],
        tags: list[str],
    ) -> None:
        """Change the note's type, fields and tags in one call (AnkiConnect `updateNoteModel`).

        This swaps the note type id, rebuilds the field list by case-insensitive name match,
        and replaces tags. Cards are untouched: orphan cards (ordinals without a template in the
        new type) remain until Anki's Check Database removes them.
        """
        self.invoke(
            "updateNoteModel",
            note={"id": note_id, "modelName": model, "fields": fields, "tags": tags},
        )

    # ---------------------------------------------------------------- models

    def model_names(self) -> list[str]:
        return sorted(self.invoke("modelNames"))

    def model_field_names(self, model: str) -> list[str]:
        return self.invoke("modelFieldNames", modelName=model)

    def model_names_and_fields(self) -> dict[str, list[str]]:
        """Every note type mapped to its field names, in one round trip after `modelNames`."""
        names = self.model_names()
        fields = self.invoke_multi([("modelFieldNames", {"modelName": n}) for n in names])
        return {name: list(f or []) for name, f in zip(names, fields, strict=True)}

    def model_styling(self, model: str) -> str:
        """The note type's stylesheet."""
        raw = self.invoke("modelStyling", modelName=model)
        return str((raw or {}).get("css", ""))

    def model_templates(self, model: str) -> dict[str, dict[str, str]]:
        """Card name -> {"Front": html, "Back": html}."""
        raw = self.invoke("modelTemplates", modelName=model)
        return {name: dict(sides or {}) for name, sides in (raw or {}).items()}

    def note_type(self, model: str) -> NoteType:
        """Fields, card templates and CSS of a note type, in one round trip."""
        fields, templates, styling = self.invoke_multi(
            [
                ("modelFieldNames", {"modelName": model}),
                ("modelTemplates", {"modelName": model}),
                ("modelStyling", {"modelName": model}),
            ]
        )
        return NoteType(
            name=model,
            fields=list(fields or []),
            templates={name: dict(sides or {}) for name, sides in (templates or {}).items()},
            css=str((styling or {}).get("css", "")),
        )
