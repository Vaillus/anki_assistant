"""Note-level view of a deck and the decisions that write to Anki. Spec: specs/review.md.

Pure functions over an `AnkiClient` (plus a `SourceStore` for deck source kinds), no FastAPI:
`web/routes_review.py` is a thin HTTP shell over this module, and the tests drive it with a fake
client holding an in-memory collection.

The review unit is the **note**, not the card: a note is flagged when any of its cards carries a
flag, and resolving a note clears the flag on all of them. Flag colours carry no meaning.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from anki_assistant.client import AnkiClient
from anki_assistant.models import Card, Note, strip_html
from anki_assistant.sources import SourceStore
from anki_assistant.web.render import render_field

REASON_FIELD = "Back Extra"

__all__ = [
    "DeckNotes",
    "DeckSummary",
    "NoteNotFound",
    "NoteView",
    "SplitResult",
    "create",
    "delete",
    "edit",
    "get_note",
    "keep",
    "list_decks",
    "list_notes",
    "move",
    "split",
]


class NoteNotFound(LookupError):
    """No note with that id in the collection (the HTTP layer turns this into a 404)."""


# --------------------------------------------------------------------------- data


@dataclass
class DeckSummary:
    """One row of column 1: a deck with its flagged-note counts and its corpus kinds."""

    name: str
    leaf: str
    depth: int
    flagged_own: int
    flagged_total: int
    source_kinds: list[str] = field(default_factory=list)


@dataclass
class NoteView:
    """One note as the UI needs it: raw fields to edit, rendered fields to display."""

    note_id: int
    deck: str
    model: str
    tags: list[str]
    card_ids: list[int]
    flagged: bool
    flag_colors: list[str]
    fields: dict[str, str]
    fields_html: dict[str, str]
    reason: str


@dataclass
class DeckNotes:
    """The queue of column 2: every note of a deck (and its sub-decks), flagged first."""

    deck: str
    total: int
    flagged: int
    notes: list[NoteView]


@dataclass
class SplitResult:
    """Outcome of a split: the original (updated, or None when it was deleted) and the fragments."""

    original: NoteView | None
    created: list[NoteView]


# ---------------------------------------------------------------------- internals


def _source_kinds(store: SourceStore, deck: str) -> list[str]:
    """Kinds of the deck's effective corpus, deduplicated, in order."""
    kinds: list[str] = []
    for source in store.corpus(deck):
        if source.kind not in kinds:
            kinds.append(source.kind)
    return kinds


def _build_view(note: Note, cards: Sequence[Card]) -> NoteView:
    """Assemble a NoteView. `cards` is the flag/deck evidence for this note."""
    flagged_cards = [c for c in cards if c.flag]
    flagged = bool(flagged_cards)
    fields = dict(note.fields)
    return NoteView(
        note_id=note.note_id,
        deck=cards[0].deck_name if cards else "",
        model=note.model_name,
        tags=list(note.tags),
        card_ids=list(note.card_ids) or [c.card_id for c in cards],
        flagged=flagged,
        flag_colors=sorted({c.flag_name for c in flagged_cards}),
        fields=fields,
        fields_html={name: render_field(value) for name, value in fields.items()},
        reason=strip_html(fields.get(REASON_FIELD, "")) if flagged else "",
    )


def _fetch_note(client: AnkiClient, note_id: int) -> Note:
    notes = client.notes_info([note_id])
    if not notes:
        raise NoteNotFound(f"No note with id {note_id}")
    return notes[0]


# ----------------------------------------------------------------------- reading


def list_decks(client: AnkiClient, store: SourceStore) -> list[DeckSummary]:
    """Every deck with its flagged-**note** counts, own and rolled up over its sub-decks."""
    names = client.deck_names()
    own: dict[str, set[int]] = defaultdict(set)
    for card in client.flagged_cards():
        own[card.deck_name].add(card.note_id)

    # Union of note ids rather than a sum: a note whose flagged cards straddle two sub-decks
    # must count once in the ancestor.
    total: dict[str, set[int]] = defaultdict(set)
    for deck, note_ids in own.items():
        parts = deck.split("::")
        for cut in range(1, len(parts) + 1):
            total["::".join(parts[:cut])] |= note_ids

    return [
        DeckSummary(
            name=name,
            leaf=name.split("::")[-1],
            depth=name.count("::"),
            flagged_own=len(own.get(name, ())),
            flagged_total=len(total.get(name, ())),
            source_kinds=_source_kinds(store, name),
        )
        for name in names
    ]


def list_notes(client: AnkiClient, deck: str) -> DeckNotes:
    """Notes of `deck` and its sub-decks, flagged first then by creation order (note id)."""
    cards_by_note: dict[int, list[Card]] = defaultdict(list)
    for card in client.cards_in_deck(deck):
        cards_by_note[card.note_id].append(card)

    notes = client.notes_info(list(cards_by_note))
    views = [_build_view(note, cards_by_note.get(note.note_id, [])) for note in notes]
    views.sort(key=lambda v: (not v.flagged, v.note_id))
    return DeckNotes(
        deck=deck,
        total=len(views),
        flagged=sum(1 for v in views if v.flagged),
        notes=views,
    )


def get_note(client: AnkiClient, note_id: int) -> NoteView:
    """One note, with the flag state of all its cards."""
    note = _fetch_note(client, note_id)
    return _build_view(note, client.cards_info(list(note.card_ids)))


# --------------------------------------------------------------------- decisions


def keep(client: AnkiClient, note_id: int) -> NoteView:
    """Garder: the flag was a false alarm. Clear it on every card, change nothing else."""
    note = _fetch_note(client, note_id)
    client.unflag_note(note.note_id)
    return get_note(client, note_id)


def edit(
    client: AnkiClient,
    note_id: int,
    fields: Mapping[str, str] | None = None,
    tags: Sequence[str] | None = None,
    unflag: bool = True,
) -> NoteView:
    """Modifier: write fields and/or tags back to Anki, then resolve the note."""
    note = _fetch_note(client, note_id)
    if fields is not None or tags is not None:
        client.update_note(
            note.note_id,
            fields=dict(fields) if fields is not None else None,
            tags=list(tags) if tags is not None else None,
        )
    if unflag:
        client.unflag_note(note.note_id)
    return get_note(client, note_id)


def split(
    client: AnkiClient,
    note_id: int,
    original: Mapping[str, Any] | None,
    new_notes: Sequence[Mapping[str, Any]],
) -> SplitResult:
    """Splitter: turn one note into several.

    `original` is `{"fields": {...}, "tags": [...]?}` to keep the note as the first fragment
    (preserving its scheduling history), or None to delete it. Each entry of `new_notes` is
    `{"fields": {...}, "model"?: str, "tags"?: [...]}`; model, tags and deck default to the
    original's. The original is deleted **after** the fragments were created, so a failure
    halfway leaves the note in place.
    """
    before = get_note(client, note_id)
    created_ids = [
        client.add_note(
            deck=before.deck,
            model=str(spec.get("model") or before.model),
            fields=dict(spec.get("fields") or {}),
            tags=list(spec["tags"]) if spec.get("tags") is not None else list(before.tags),
        )
        for spec in new_notes
    ]

    if original is None:
        client.delete_notes([note_id])
        kept = None
    else:
        kept = edit(
            client,
            note_id,
            fields=original.get("fields"),
            tags=original.get("tags"),
            unflag=True,
        )
    return SplitResult(original=kept, created=[get_note(client, nid) for nid in created_ids])


def create(
    client: AnkiClient,
    deck: str,
    model: str,
    fields: Mapping[str, str],
    tags: Sequence[str] | None = None,
) -> NoteView:
    """Créer: add a sibling note. The note it was created from is left untouched, flag included."""
    new_id = client.add_note(deck=deck, model=model, fields=dict(fields), tags=list(tags or []))
    return get_note(client, new_id)


def move(client: AnkiClient, note_id: int, deck: str) -> NoteView:
    """Déplacer: send every card of the note to another deck and resolve it."""
    note = _fetch_note(client, note_id)
    card_ids = list(note.card_ids) or client.note_card_ids(note_id)
    if card_ids:
        client.change_deck(card_ids, deck)
        client.clear_flag(card_ids)
    return get_note(client, note_id)


def delete(client: AnkiClient, note_id: int) -> None:
    """Supprimer: remove the note and all its cards. No undo (see specs/00-overview.md)."""
    _fetch_note(client, note_id)
    client.delete_notes([note_id])
