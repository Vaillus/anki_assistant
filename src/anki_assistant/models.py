"""Typed views over the raw dicts returned by AnkiConnect."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

# Anki flag index -> human name. 0 means "no flag".
FLAG_COLORS: dict[int, str] = {
    0: "none",
    1: "red",
    2: "orange",
    3: "green",
    4: "blue",
    5: "pink",
    6: "turquoise",
    7: "purple",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Return plain text from an Anki field: tags removed, entities decoded, whitespace folded."""
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _fields_to_dict(raw: dict[str, Any]) -> dict[str, str]:
    """AnkiConnect returns fields as {name: {"value": ..., "order": n}}; keep insertion order."""
    ordered = sorted(raw.items(), key=lambda kv: kv[1].get("order", 0))
    return {name: info.get("value", "") for name, info in ordered}


@dataclass
class Note:
    note_id: int
    model_name: str
    tags: list[str]
    fields: dict[str, str]
    card_ids: list[int] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Note:
        return cls(
            note_id=raw["noteId"],
            model_name=raw.get("modelName", ""),
            tags=list(raw.get("tags", [])),
            fields=_fields_to_dict(raw.get("fields", {})),
            card_ids=list(raw.get("cards", [])),
        )

    def plain_fields(self) -> dict[str, str]:
        return {k: strip_html(v) for k, v in self.fields.items()}


@dataclass
class Card:
    card_id: int
    note_id: int
    deck_name: str
    model_name: str
    fields: dict[str, str]
    question: str
    answer: str
    flag: int
    ord: int  # card ordinal within the note; for a Cloze note, card `ord` hides cloze c{ord+1}
    interval: int
    due: int
    reps: int
    lapses: int
    queue: int
    type: int

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Card:
        return cls(
            card_id=raw["cardId"],
            note_id=raw["note"],
            deck_name=raw.get("deckName", ""),
            model_name=raw.get("modelName", ""),
            fields=_fields_to_dict(raw.get("fields", {})),
            question=raw.get("question", ""),
            answer=raw.get("answer", ""),
            flag=int(raw.get("flags", 0)),
            ord=int(raw.get("ord", 0)),
            interval=int(raw.get("interval", 0)),
            due=int(raw.get("due", 0)),
            reps=int(raw.get("reps", 0)),
            lapses=int(raw.get("lapses", 0)),
            queue=int(raw.get("queue", 0)),
            type=int(raw.get("type", 0)),
        )

    @property
    def flag_name(self) -> str:
        return FLAG_COLORS.get(self.flag, f"unknown({self.flag})")

    @property
    def is_suspended(self) -> bool:
        return self.queue == -1

    def plain_fields(self) -> dict[str, str]:
        return {k: strip_html(v) for k, v in self.fields.items()}

    def plain_question(self) -> str:
        return strip_html(self.question)

    def plain_answer(self) -> str:
        return strip_html(self.answer)


@dataclass
class NoteType:
    """A note type (Anki "model"): its field names, card templates and stylesheet."""

    name: str
    fields: list[str]
    #: card name -> {"Front": html, "Back": html}
    templates: dict[str, dict[str, str]]
    css: str
