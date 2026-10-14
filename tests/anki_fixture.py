"""A temporary collection for the tests: an empty Anki collection created in the test's temp
folder through the anki library, driven by the real `AnkiClient`.

`TempAnki` is an `AnkiClient` on that file with test helpers: building notes and cards with a
given scheduling state, reading the collection back as plain dicts, and failure injection keyed
on `AnkiClient` operations (`fail_on["update_note"] = 2`: the second call raises). Nothing here
touches the user's collection.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

from anki.collection import Collection

from anki_assistant.client import AnkiClient, AnkiError

#: The operations recorded in `calls` and open to `fail_on`.
OPERATIONS = (
    "deck_names",
    "create_deck",
    "delete_deck",
    "deck_config",
    "deck_limits",
    "find_card_ids",
    "find_note_ids",
    "cards_info",
    "notes_info",
    "update_note",
    "set_flag",
    "set_card_scheduling",
    "answer_card",
    "answer_outcomes",
    "set_due_date",
    "extend_limits",
    "suspend",
    "change_deck",
    "add_note",
    "delete_notes",
    "update_note_model",
    "model_field_names",
    "model_templates",
)
#: Operations that only read.
READS = {
    "deck_names",
    "deck_config",
    "deck_limits",
    "find_card_ids",
    "find_note_ids",
    "cards_info",
    "notes_info",
    "answer_outcomes",
    "model_field_names",
    "model_templates",
}


def new_collection(path: Path) -> Path:
    """Create an empty collection file at `path` and close it."""
    col = Collection(str(path))
    col.close()
    return path


class MediaFolder(MutableMapping[str, bytes]):
    """The collection's media folder as a mapping: file name -> content."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def __getitem__(self, name: str) -> bytes:
        path = self.folder / name
        if not path.is_file():
            raise KeyError(name)
        return path.read_bytes()

    def __setitem__(self, name: str, data: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / name).write_bytes(data)

    def __delitem__(self, name: str) -> None:
        (self.folder / name).unlink()

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(p.name for p in self.folder.iterdir()) if self.folder.exists() else [])

    def __len__(self) -> int:
        return len(list(iter(self)))


class TempAnki(AnkiClient):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(new_collection(tmp_path / "collection.anki2"))
        #: `AnkiClient` operations, in the order they were called (nested calls included).
        self.calls: list[str] = []
        #: operation -> n: the n-th call of that operation raises `AnkiError`.
        self.fail_on: dict[str, int] = {}
        self._seen: dict[str, int] = {}
        assert self.media_dir is not None
        self.media = MediaFolder(self.media_dir)
        for name in OPERATIONS:
            setattr(self, name, self._recorded(name, getattr(self, name)))

    def _recorded(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            self._seen[name] = self._seen.get(name, 0) + 1
            if self.fail_on.get(name) == self._seen[name]:
                raise AnkiError(f"{name}: boom")
            return method(*args, **kwargs)

        return wrapper

    def writes(self) -> list[str]:
        """The recorded operations that write."""
        return [name for name in self.calls if name not in READS]

    # ---------------------------------------------------------------- building

    def add(
        self,
        deck: str,
        *,
        model: str = "Cloze",
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
        flags: tuple[int, ...] = (0,),
        **scheduling: int,
    ) -> int:
        """Put a note in deck `deck` with one card per entry of `flags` (a cloze note gets as
        many clozes; the fields are then set to `fields`, cards kept). `scheduling` sets card
        columns on every card: `interval`, `due`, `queue`, `card_type`, `reps`, `lapses`,
        `factor`, `left`, or `rel_due` (a review card due that many days from today).
        Returns the note id."""
        with self.action() as col:
            notetype = col.models.by_name(model)
            assert notetype is not None, model
            note = col.new_note(notetype)
            names = note.keys()
            wanted = dict(fields or {names[0]: "x"})
            if notetype["type"] == 1:  # cloze
                note[names[0]] = " ".join(f"{{{{c{i + 1}::x}}}}" for i in range(len(flags)))
            else:
                assert len(flags) <= len(notetype["tmpls"]), "one card per template"
                note[names[0]] = "x"
                for extra in names[1 : len(flags)]:
                    note[extra] = "x"
            note.tags = list(tags or [])
            col.add_note(note, col.decks.id(deck, create=True))  # ty: ignore[invalid-argument-type]
            for name in names:
                note[name] = ""
            for name, value in wanted.items():
                note[name] = value
            col.update_note(note)
            cards = sorted((col.get_card(c) for c in note.card_ids()), key=lambda c: c.ord)
            for card, flag in zip(cards, flags, strict=True):
                card.flags = (card.flags & ~0b111) | flag
                self._schedule(col, card, scheduling)
            col.update_cards(cards)
            return int(note.id)

    def _schedule(self, col: Collection, card: Any, values: dict[str, int]) -> None:
        columns = {
            "interval": "ivl",
            "due": "due",
            "queue": "queue",
            "card_type": "type",
            "reps": "reps",
            "lapses": "lapses",
            "factor": "factor",
            "left": "left",
        }
        for key, value in values.items():
            if key == "rel_due":
                card.due = col.sched.today + value
            elif key == "last_review_days":
                card.last_review_time = col.sched.day_cutoff - (value + 1) * 86_400 + 3_600
            else:
                setattr(card, columns[key], value)

    def set_card(self, card_id: int, **scheduling: int) -> None:
        """Set columns of one card (same keys as `add`), or `deck` / `flag` through
        their own helpers."""
        with self.action() as col:
            card = col.get_card(card_id)  # ty: ignore[invalid-argument-type]
            self._schedule(col, card, scheduling)
            col.update_card(card)

    def set_fields(self, note_id: int, **fields: str) -> None:
        """Edit a note as if in Anki desktop."""
        with self.action() as col:
            note = col.get_note(note_id)  # ty: ignore[invalid-argument-type]
            for name, value in fields.items():
                note[name.replace("_", " ")] = value
            col.update_note(note)

    def add_field(self, model: str, name: str) -> None:
        """Add a field to a note type."""
        with self.action() as col:
            notetype = col.models.by_name(model)
            assert notetype is not None
            col.models.add_field(notetype, col.models.new_field(name))
            col.models.update_dict(notetype)

    def set_limits(
        self,
        deck: str,
        *,
        review: int | None = None,
        new: int | None = None,
        deck_review: int | None = None,
        today_review: int | None = None,
        today_new: int | None = None,
        **options: Any,
    ) -> None:
        """Set the daily limits of the deck's preset (`review`, `new`), its "This deck" review
        limit (`deck_review`), its "Today only" limits (`today_review`, `today_new`), and preset
        options (`reviewOrder=2`…)."""
        with self.action() as col:
            did = col.decks.id(deck, create=True)
            assert did is not None
            conf = col.decks.config_dict_for_deck_id(did)
            if review is not None:
                conf["rev"]["perDay"] = review
            if new is not None:
                conf["new"]["perDay"] = new
            conf.update(options)
            col.decks.update_config(conf)
            raw = col.decks.get(did)
            assert raw is not None
            if deck_review is not None:
                raw["reviewLimit"] = deck_review
            if today_review is not None:
                raw["reviewLimitToday"] = {"limit": today_review, "today": col.sched.today}
            if today_new is not None:
                raw["newLimitToday"] = {"limit": today_new, "today": col.sched.today}
            col.decks.save(raw)

    # ----------------------------------------------------------------- reading

    @property
    def notes(self) -> dict[int, dict[str, Any]]:
        with self.action() as col:
            out = {}
            for nid in col.find_notes(""):
                note = col.get_note(nid)
                cards = sorted((col.get_card(c) for c in note.card_ids()), key=lambda c: c.ord)
                out[int(nid)] = {
                    "modelName": note.note_type()["name"],  # ty: ignore[not-subscriptable]
                    "fields": dict(note.items()),
                    "tags": list(note.tags),
                    "cards": [int(c.id) for c in cards],
                }
            return out

    @property
    def cards(self) -> dict[int, dict[str, Any]]:
        with self.action() as col:
            out = {}
            for cid in col.find_cards(""):
                card = col.get_card(cid)
                out[int(cid)] = {
                    "note": int(card.nid),
                    "deckName": col.decks.name(card.did),
                    "flags": card.user_flag(),
                    "ord": card.ord,
                    "interval": card.ivl,
                    "due": card.due,
                    "queue": card.queue,
                    "type": card.type,
                    "reps": card.reps,
                    "lapses": card.lapses,
                    "factor": card.factor,
                    "left": card.left,
                }
            return out

    @property
    def decks(self) -> list[str]:
        with self.action() as col:
            return [d.name for d in col.decks.all_names_and_ids(skip_empty_default=True)]

    def flags_of(self, note_id: int) -> list[int]:
        cards = self.cards
        return [cards[c]["flags"] for c in self.notes[note_id]["cards"]]

    def decks_of(self, note_id: int) -> list[str]:
        cards = self.cards
        return [cards[c]["deckName"] for c in self.notes[note_id]["cards"]]

    def card_ids_of(self, note_id: int) -> list[int]:
        return list(self.notes[note_id]["cards"])

    def revlog(self) -> list[tuple[int, int, int, int, int]]:
        """Review history rows: (instant ms, card id, ease, interval, time ms)."""
        with self.action() as col:
            assert col.db is not None
            rows = col.db.all("select id, cid, ease, ivl, time from revlog order by id")
            return [tuple(int(v) for v in row) for row in rows]  # ty: ignore[invalid-return-type]

    def studied_today(self, deck: str) -> tuple[int, int]:
        """The deck's studied-today counter: (reviews, new cards)."""
        limits = self.deck_limits(deck)
        return limits.studied_review, limits.studied_new

    def anki_today(self) -> int:
        with self.action() as col:
            return int(col.sched.today)
