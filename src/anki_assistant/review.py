"""Note-level view of a deck and the decisions that write to Anki. Spec: specs/review.md.

Pure functions over an `AnkiClient` (plus a `SourceStore` for deck source kinds), no FastAPI:
`web/routes_review.py` is a thin HTTP shell over this module, and the tests drive it with a
client on a temporary collection.

The review unit is the **note**, not the card: a note is flagged when any of its cards carries a
flag, and resolving a note clears the flag on all of them. Flag colours carry no meaning.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from anki_assistant import deck_order
from anki_assistant.client import AnkiClient
from anki_assistant.deck_order import DeckOrderStore, DeckTree, LabelInfo, LabelMark
from anki_assistant.models import Card, Note, strip_html
from anki_assistant.sources import SourceStore
from anki_assistant.web.render import render_field

REASON_FIELD = "Back Extra"

__all__ = [
    "DeckListing",
    "DeckNotes",
    "DeckSummary",
    "GroupEffects",
    "FlaggedCard",
    "NoteNotFound",
    "NoteView",
    "PriorityResult",
    "SplitResult",
    "create",
    "create_deck",
    "delete",
    "deck_listing",
    "edit",
    "group_label_effects",
    "get_note",
    "get_notes",
    "keep",
    "list_decks",
    "list_notes",
    "move",
    "priority_notes",
    "search_notes",
    "split",
    "turn_group_into_label",
]


class NoteNotFound(LookupError):
    """No note with that id in the collection (the HTTP layer turns this into a 404)."""


# --------------------------------------------------------------------------- data


@dataclass
class DeckSummary:
    """One row of column 1, in the app's tree: a deck with its flagged-note counts, its corpus
    kinds and its label. `leaf` is its own name, number prefix hidden; `depth` its depth in the
    app's tree; `parent_id` its tree parent's deck id (0 at the top level)."""

    name: str
    leaf: str
    depth: int
    flagged_own: int
    flagged_total: int
    source_kinds: list[str] = field(default_factory=list)
    id: int = 0
    parent_id: int = 0
    label: LabelMark | None = None
    #: « Transformer en étiquette » is offered on this deck.
    can_group: bool = False
    #: Why turning it into a label is refused (a deck under it carries a label), or None.
    group_refused: str | None = None


@dataclass
class DeckListing:
    """Column 1: every row of the app's tree, in order, and the labels."""

    decks: list[DeckSummary]
    labels: list[LabelInfo]


@dataclass
class GroupEffects:
    """What turning a group into a label would do (specs/review.md#labels)."""

    refused: str | None
    own_cards: int
    misc: str
    misc_exists: bool
    sources: int


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


def app_tree(client: AnkiClient, order: DeckOrderStore | None) -> DeckTree:
    """The app's tree of the collection's decks (specs/review.md#deck-tree-column-1); with no
    store, Anki's deck order and no label."""
    return deck_order.build_tree(client.all_decks(), order or DeckOrderStore.in_memory())


def _protected(protected: Iterable[str | None]) -> set[str]:
    return {name for name in protected if name}


def deck_listing(
    client: AnkiClient,
    store: SourceStore,
    order: DeckOrderStore | None = None,
    *,
    protected: Iterable[str | None] = (),
) -> DeckListing:
    """Every deck of the app's tree, in order, with its flagged-**note** counts (own and rolled
    up over its sub-decks), its label and whether it can turn into a label. `protected`: the
    decks never turned into a label (the study deck, the mobile deck)."""
    order = order or DeckOrderStore.in_memory()
    tree = app_tree(client, order)
    never = _protected(protected)
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

    decks = [
        DeckSummary(
            name=row.name,
            leaf=row.leaf,
            depth=row.depth,
            flagged_own=len(own.get(row.name, ())),
            flagged_total=len(total.get(row.name, ())),
            source_kinds=_source_kinds(store, row.name),
            id=row.id,
            parent_id=row.parent_id,
            label=row.label,
            can_group=row.has_subdecks and row.label is None and row.name not in never,
            group_refused=(
                deck_order.group_refusal(tree, order, row.id, never)
                if row.has_subdecks and row.label is None and row.name not in never
                else None
            ),
        )
        for row in tree.rows
    ]
    return DeckListing(decks=decks, labels=tree.labels)


def list_decks(
    client: AnkiClient,
    store: SourceStore,
    order: DeckOrderStore | None = None,
    *,
    protected: Iterable[str | None] = (),
) -> list[DeckSummary]:
    """The rows of `deck_listing`."""
    return deck_listing(client, store, order, protected=protected).decks


def group_label_effects(
    client: AnkiClient,
    store: SourceStore,
    order: DeckOrderStore,
    deck_id: int,
    *,
    protected: Iterable[str | None] = (),
) -> GroupEffects:
    """What turning the group `deck_id` into a label would do: the cards it holds itself, the
    `misc` sub-deck they would go to, the sources of its own corpus."""
    tree = app_tree(client, order)
    name = _tree_name(tree, deck_id)
    misc = f"{name}::{deck_order.MISC}"
    return GroupEffects(
        refused=deck_order.group_refusal(tree, order, deck_id, _protected(protected)),
        own_cards=len(client.own_card_ids(name)),
        misc=misc,
        misc_exists=misc in tree.by_name,
        sources=len(store.corpora.get(name, [])),
    )


def _tree_name(tree: DeckTree, deck_id: int) -> str:
    for name, did in tree.by_name.items():
        if did == deck_id:
            return name
    raise KeyError(f"No deck with id {deck_id}")


def turn_group_into_label(
    client: AnkiClient,
    store: SourceStore,
    order: DeckOrderStore,
    deck_id: int,
    *,
    protected: Iterable[str | None] = (),
) -> str:
    """Turn a group into a label (specs/review.md#labels): its own cards move to its `misc`
    sub-deck (created with the group's preset when missing), its corpus is copied onto each of
    its sub-decks and removed from it, then the label is recorded. Returns the label id;
    raises `LabelRefused` when the deck cannot turn."""
    tree = app_tree(client, order)
    name = _tree_name(tree, deck_id)
    refused = deck_order.group_refusal(tree, order, deck_id, _protected(protected))
    if refused:
        raise deck_order.LabelRefused(refused)
    sub_decks = [row.id for row in tree.rows if row.parent_id == deck_id]
    children = [n for n in tree.by_name if n.rsplit("::", 1)[0] == name and "::" in n]
    misc = f"{name}::{deck_order.MISC}"
    misc_created: int | None = None
    own_cards = client.own_card_ids(name)
    if own_cards:
        if misc not in tree.by_name:
            misc_created = client.create_deck(misc, preset_of=name)
            children.append(misc)
        client.change_deck(own_cards, misc)
    group_sources = store.corpora.get(name)
    if group_sources:
        for child in children:
            own = list(store.corpora.get(child, []))
            ids = {src.id for src in own}
            copies = [replace(src, deck=child) for src in group_sources if src.id not in ids]
            store.corpora[child] = own + copies
        del store.corpora[name]
        store.save()
    return deck_order.turn_into_label(
        order, tree, deck_id, sub_decks=sub_decks, misc_created=misc_created
    )


def list_notes(client: AnkiClient, deck: str, order: DeckOrderStore | None = None) -> DeckNotes:
    """Notes of `deck` and its sub-decks: flagged first, then by their deck's place in the app's
    tree read top to bottom, then by creation order (note id)."""
    cards_by_note: dict[int, list[Card]] = defaultdict(list)
    for card in client.cards_in_deck(deck):
        cards_by_note[card.note_id].append(card)

    notes = client.notes_info(list(cards_by_note))
    views = [_build_view(note, cards_by_note.get(note.note_id, [])) for note in notes]
    tree = app_tree(client, order)
    views.sort(key=lambda v: (not v.flagged, tree.position(v.deck), v.note_id))
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


def _due_within_budget(
    client: AnkiClient,
    flagged_due_cards: list[Card],
    study_deck: str,
) -> set[int]:
    """Return card ids of flagged due cards within the study deck's review budget: the cards
    of Anki's study queue for that deck, which is what Anki will show today, limits applied.
    """
    if not flagged_due_cards:
        return set()
    within = {queued.card_id for queued in client.study_queue(study_deck)}
    return {c.card_id for c in flagged_due_cards if c.card_id in within}


def priority_notes(
    client: AnkiClient, *, study_deck: str | None = None, order: DeckOrderStore | None = None
) -> PriorityResult:
    """Flagged notes matching urgency criteria: buried, new, or due within budget; by their
    deck's place in the app's tree, then by creation order."""
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
    tree = app_tree(client, order)
    views.sort(key=lambda v: (tree.position(v.deck), v.note_id))
    return PriorityResult(count=len(views), notes=views)


def priority_count(client: AnkiClient, *, study_deck: str | None = None) -> int:
    """Exact count for the deck tree badge (same criteria as priority_notes)."""
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
