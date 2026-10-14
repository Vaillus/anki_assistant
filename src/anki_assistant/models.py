"""Typed views of Anki's notes and cards, as `AnkiClient` returns them."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

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


@dataclass
class Note:
    note_id: int
    model_name: str
    tags: list[str]
    fields: dict[str, str]
    card_ids: list[int] = field(default_factory=list)

    def plain_fields(self) -> dict[str, str]:
        return {k: strip_html(v) for k, v in self.fields.items()}


@dataclass
class Card:
    card_id: int
    note_id: int
    deck_name: str
    model_name: str
    fields: dict[str, str]
    flag: int
    ord: int  # card ordinal within the note; for a Cloze note, card `ord` hides cloze c{ord+1}
    interval: int
    due: int
    reps: int
    lapses: int
    queue: int
    type: int
    factor: int
    left: int

    @property
    def flag_name(self) -> str:
        return FLAG_COLORS.get(self.flag, f"unknown({self.flag})")

    @property
    def is_suspended(self) -> bool:
        return self.queue == -1

    def plain_fields(self) -> dict[str, str]:
        return {k: strip_html(v) for k, v in self.fields.items()}


@dataclass
class NoteType:
    """A note type (Anki "model"): its field names, card templates and stylesheet."""

    name: str
    fields: list[str]
    #: card name -> {"Front": html, "Back": html}
    templates: dict[str, dict[str, str]]
    css: str
