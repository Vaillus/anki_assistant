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
    "FlaggedCard",
    "NoteNotFound",
    "NoteView",
    "PriorityResult",
    "SplitResult",
    "create",
    "create_deck",
    "delete",
    "edit",
    "get_note",
    "get_notes",
    "keep",
    "list_decks",
    "list_notes",
    "move",
    "priority_notes",
    "search_notes",
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
class FlaggedCard:
    """One flagged card of a note. `ord` is Anki's card ordinal: cloze c{ord+1} on a Cloze note."""

    card_id: int
    ord: int
    flag_color: str


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
    flagged_cards: list[FlaggedCard]
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
class PriorityResult:
    """Cross-deck list of flagged notes matching urgency criteria."""

    count: int
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
        flagged_cards=[
            FlaggedCard(card_id=c.card_id, ord=c.ord, flag_color=c.flag_name)
            for c in sorted(flagged_cards, key=lambda c: c.ord)
        ],
        fields=fields,
        fields_html={name: render_field(value) for name, value in fields.items()},
        reason=strip_html(fields.get(REASON_FIELD, "")) if flagged else "",
    )


def _fetch_note(client: AnkiClient, note_id: int) -> Note:
    notes = client.notes_info([note_id])
    if not notes:
        raise NoteNotFound(f"No note with id {note_id}")
    return notes[0]


class DeckAlreadyExists(ValueError):
    """The requested deck name already exists in the collection."""


# ----------------------------------------------------------------------- reading


def create_deck(client: AnkiClient, name: str) -> str:
    """Create a deck (and intermediate parents). Raises if the name already exists."""
    existing = set(client.deck_names())
    if name in existing:
        raise DeckAlreadyExists(f"Deck already exists: {name}")
    client.create_deck(name)
    return name


def delete_deck(client: AnkiClient, name: str) -> None:
    """Delete a deck and its cards."""
    client.delete_deck(name, cards_too=True)


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


def get_notes(client: AnkiClient, note_ids: Sequence[int]) -> list[NoteView]:
    """Several notes in two round trips. Ids unknown to Anki are dropped; order is preserved."""
    notes = client.notes_info([int(note_id) for note_id in note_ids])
    cards_by_note: dict[int, list[Card]] = defaultdict(list)
    for card in client.cards_info([cid for note in notes for cid in note.card_ids]):
        cards_by_note[card.note_id].append(card)
    return [_build_view(note, cards_by_note.get(note.note_id, [])) for note in notes]


def search_notes(client: AnkiClient, query: str, limit: int = 50) -> list[NoteView]:
    """Notes matching an Anki search, capped at `limit`, flagged first then by note id."""
    ids = client.find_note_ids(query)[: max(0, int(limit))]
    views = get_notes(client, ids)
    views.sort(key=lambda v: (not v.flagged, v.note_id))
    return views


PRIORITY_QUERY = "-flag:0 (is:buried OR is:new OR is:due)"


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


def _due_within_budget(
    client: AnkiClient,
    flagged_due_cards: list[Card],
    study_deck: str,
) -> set[int]:
    """Return card ids of flagged due cards within the study deck's review budget.

    The user studies from one root deck (e.g. ``courant``); its ``review_count``
    from ``getDeckStats`` is the total remaining reviews for today across all
    sub-decks. Anki presents review cards grouped by sub-deck in tree order
    (alphabetical), so the sort key is ``(deck_name, due)`` — all of one sub-deck's
    cards before the next, most overdue first within each.
    """
    if not flagged_due_cards:
        return set()

    stats_raw = client.deck_stats(study_deck)
    budget = 0
    for stat in stats_raw.values():
        budget = int(stat.get("review_count", 0))

    if budget == 0:
        return set()

    all_due_ids = client.find_card_ids(f"deck:{_quote(study_deck)} is:due -is:new -is:buried")
    if len(all_due_ids) <= budget:
        return {c.card_id for c in flagged_due_cards}

    all_due = client.cards_info(all_due_ids)
    all_due.sort(key=lambda c: (c.deck_name, c.due))
    within = {c.card_id for c in all_due[:budget]}
    return {c.card_id for c in flagged_due_cards if c.card_id in within}


def priority_notes(client: AnkiClient, *, study_deck: str | None = None) -> PriorityResult:
    """Flagged notes matching urgency criteria: buried, new, or due within budget."""
    buried_ids = client.find_card_ids("-flag:0 is:buried")
    new_ids = client.find_card_ids("-flag:0 is:new")
    due_flagged_ids = client.find_card_ids("-flag:0 is:due -is:new -is:buried")

    always_priority = set(buried_ids) | set(new_ids)
    if not always_priority and not due_flagged_ids:
        return PriorityResult(count=0, notes=[])

    # Budget filtering: only when a study deck is configured.
    if study_deck and due_flagged_ids:
        due_cards = client.cards_info(due_flagged_ids)
        budgeted = _due_within_budget(client, due_cards, study_deck)
    elif due_flagged_ids:
        budgeted = set(due_flagged_ids)
    else:
        budgeted = set()

    priority_card_ids = list(always_priority | budgeted)
    if not priority_card_ids:
        return PriorityResult(count=0, notes=[])

    cards = client.cards_info(priority_card_ids)
    cards_by_note: dict[int, list[Card]] = defaultdict(list)
    for card in cards:
        cards_by_note[card.note_id].append(card)

    notes = client.notes_info(list(cards_by_note))

    # Fetch remaining cards of each note for complete flag state.
    all_card_ids = {cid for note in notes for cid in note.card_ids}
    fetched = {c.card_id for c in cards}
    missing = list(all_card_ids - fetched)
    if missing:
        for card in client.cards_info(missing):
            cards_by_note[card.note_id].append(card)

    views = [_build_view(note, cards_by_note[note.note_id]) for note in notes]
    views.sort(key=lambda v: v.note_id)
    return PriorityResult(count=len(views), notes=views)


def priority_count(client: AnkiClient, *, study_deck: str | None = None) -> int:
    """Exact count for the deck tree badge (same logic as priority_notes)."""
    return priority_notes(client, study_deck=study_deck).count


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
    reflag: Sequence[int] | None = None,
) -> NoteView:
    """Modifier: write fields and/or tags back to Anki, then resolve the note.

    `reflag` puts a (red) flag back on the given cards — the chat's undo uses it to return a
    reverted note to the queue. Colours carry no meaning here (specs/00-overview.md).
    """
    note = _fetch_note(client, note_id)
    if fields is not None or tags is not None:
        client.update_note(
            note.note_id,
            fields=dict(fields) if fields is not None else None,
            tags=list(tags) if tags is not None else None,
        )
    if unflag:
        client.unflag_note(note.note_id)
    if reflag:
        client.set_flag([int(card_id) for card_id in reflag], 1)
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
    """Create: add a sibling note. The note it was created from is left untouched, flag included."""
    new_id = client.add_note(deck=deck, model=model, fields=dict(fields), tags=list(tags or []))
    return get_note(client, new_id)


def move(client: AnkiClient, note_id: int, deck: str) -> NoteView:
    """Move: send every card of the note to another deck and resolve it."""
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
