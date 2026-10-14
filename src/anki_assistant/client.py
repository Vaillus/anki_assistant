"""The app's typed client for Anki: every read and write goes through `AnkiClient`, which opens
Anki's collection file itself through the anki library (Anki's own Python package, the code
Anki desktop runs). Spec: specs/00-overview.md#anki.

**Collection lock.** Anki opens a collection file with an exclusive lock: while Anki desktop
has the profile loaded, opening it fails, and while the app holds it, Anki desktop cannot load
the profile. The app and Anki desktop take turns.

**Holding policy.** The client opens the file for one action (`action()`, one HTTP request or
one CLI command) and closes it when the action is done. An action that wrote keeps the file open
for one AnkiWeb sync, run in the background after the action returned, then closes it. Another
action arriving meanwhile waits for the file. An operation called outside an action is its own
action.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anki.collection  # first: anki.cards imports it back, circularly
import anki.errors
from anki.cards import Card as AnkiCard
from anki.collection import Collection
from anki.decks import DeckId
from anki.notes import Note as AnkiNote
from anki.scheduler.v3 import Scheduler
from anki.scheduler_pb2 import CardAnswer, QueuedCards, SchedulingState, SchedulingStates
from anki.sync import SyncAuth
from anki.sync_pb2 import SyncCollectionResponse

from anki_assistant.models import Card, Note, NoteType

log = logging.getLogger(__name__)

#: Where Anki desktop keeps its profiles on macOS.
ANKI_BASE = Path.home() / "Library" / "Application Support" / "Anki2"

LOCKED_MESSAGE = "Close Anki desktop first"
FULL_SYNC_MESSAGE = "AnkiWeb needs a full sync: open Anki desktop to choose"

_DAY_SECONDS = 86_400
_RATINGS = {
    1: CardAnswer.AGAIN,
    2: CardAnswer.HARD,
    3: CardAnswer.GOOD,
    4: CardAnswer.EASY,
}


class AnkiError(RuntimeError):
    """Anki refused an operation, or the collection could not be opened."""


class CollectionLocked(AnkiError):
    """The collection file is held by another process (Anki desktop): nothing was done."""


def default_collection_path() -> Path | None:
    """The collection of the only Anki desktop profile, or None when there is not exactly one."""
    found = sorted(ANKI_BASE.glob("*/collection.anki2"))
    return found[0] if len(found) == 1 else None


def _quote(value: str) -> str:
    """Quote a value for use inside an Anki search query."""
    return '"' + value.replace('"', '\\"') + '"'


@dataclass
class DeckLimits:
    """A deck's daily limits as Anki applies them, and what Anki counts as studied today.

    `review` / `new`: the effective deck limit today — the deck's "Today only" value when set
    for today, else its "This deck" value, else its preset's. `review_default` / `new_default`:
    the same without "Today only", the limit of a later day. `studied_review` / `studied_new`:
    the deck's studied-today counter, which Anki checks against the limit.
    """

    review: int
    new: int
    review_default: int
    new_default: int
    studied_review: int = 0
    studied_new: int = 0


@dataclass
class QueuedCard:
    """One card of Anki's study queue: its id and what Anki shows it as."""

    card_id: int
    kind: str  # "new", "learn", "review"


@dataclass
class Replay:
    """What a replayed answer did: the card after it, and what Anki added to the
    studied-today counter of the card's deck."""

    card: Card
    counted_new: int
    counted_review: int


@dataclass
class Outcomes:
    """The raw outcome of each answer button (again, hard, good, easy): the delay in seconds
    until the card is due again, from the scheduler; with Anki's display strings."""

    seconds: list[int | None]
    labels: list[str]


@dataclass
class SyncStatus:
    """The outcome of the last AnkiWeb sync the app ran (None message: no sync yet)."""

    ok: bool
    message: str | None


def full_sync_choice(required: int, schema_changed_at_open: bool, schema_changed_now: bool) -> str:
    """What the app does with AnkiWeb's answer to a sync (specs/00-overview.md#ankiweb-sync):
    `done`, `upload` (the app's file replaces AnkiWeb's), or `leave` (left to Anki desktop).
    The app uploads a full sync only when its own write changed the schema."""
    req = SyncCollectionResponse.ChangesRequired
    if required in (req.NO_CHANGES, req.NORMAL_SYNC):
        return "done"
    if required == req.FULL_UPLOAD:
        return "upload"
    if required == req.FULL_SYNC and not schema_changed_at_open and schema_changed_now:
        return "upload"
    return "leave"


def state_seconds(state: SchedulingState) -> int | None:
    """Seconds until a card in this scheduling state is due again."""
    if state.WhichOneof("kind") == "filtered":
        filtered = state.filtered
        if filtered.WhichOneof("kind") == "preview":
            return int(filtered.preview.scheduled_secs)
        if filtered.WhichOneof("kind") != "rescheduling":
            return None
        normal = filtered.rescheduling.original_state
    else:
        normal = state.normal
    kind = normal.WhichOneof("kind")
    if kind == "review":
        return int(normal.review.scheduled_days) * _DAY_SECONDS
    if kind == "learning":
        return int(normal.learning.scheduled_secs)
    if kind == "relearning":
        return int(normal.relearning.learning.scheduled_secs)
    return None


class AnkiClient:
    def __init__(self, path: str | Path | None = None, *, auth: SyncAuth | None = None) -> None:
        """`path`: the collection file (`collection.anki2`); None picks the only profile's.
        `auth`: the AnkiWeb login; None runs no AnkiWeb sync."""
        self.path: Path | None = Path(path) if path else default_collection_path()
        self.auth = auth
        self.last_sync: SyncStatus | None = None
        self._lock = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self._col: Collection | None = None
        self._wrote = False
        self._schema_changed_at_open = False

    # ------------------------------------------------------------- holding policy

    @property
    def media_dir(self) -> Path | None:
        """The collection's media folder (`collection.media` beside the file)."""
        return self.path.with_suffix(".media") if self.path else None

    def _open(self) -> Collection:
        if self.path is None:
            raise AnkiError("No Anki collection: set ANKI_COLLECTION to its collection.anki2")
        if not self.path.exists():
            raise AnkiError(f"No Anki collection at {self.path}")
        try:
            return Collection(str(self.path))
        except anki.errors.DBError as exc:
            if "already open" in str(exc):
                raise CollectionLocked(LOCKED_MESSAGE) from exc
            raise AnkiError(f"Cannot open {self.path}: {exc}") from exc

    @contextmanager
    def action(self) -> Iterator[Collection]:
        """Hold the collection for one action: open it now (waiting for another action or its
        AnkiWeb sync), close it at the end, after one AnkiWeb sync in the background when the
        action wrote. Nested calls in the same thread share the open collection."""
        me = threading.get_ident()
        if self._owner == me and self._col is not None:
            self._depth += 1
            try:
                yield self._col
            finally:
                self._depth -= 1
            return
        self._lock.acquire()
        try:
            col = self._open()
        except BaseException:
            self._lock.release()
            raise
        self._col, self._owner, self._depth, self._wrote = col, me, 1, False
        try:
            if self.auth is not None:
                self._schema_changed_at_open = bool(col.schema_changed())
            yield col
        finally:
            wrote = self._wrote
            self._col, self._owner, self._depth, self._wrote = None, None, 0, False
            if wrote and self.auth is not None:
                at_open = self._schema_changed_at_open
                threading.Thread(
                    target=self._sync_and_close, args=(col, at_open), daemon=True
                ).start()
            else:
                self._close(col)

    def _close(self, col: Collection) -> None:
        try:
            col.close()
        except Exception as exc:  # noqa: BLE001 — closing must release the lock
            log.warning("closing the collection failed: %s", exc)
        finally:
            self._lock.release()

    def _sync_and_close(self, col: Collection, schema_changed_at_open: bool) -> None:
        """The background half of a write action: one AnkiWeb sync, then close the file. A
        failed sync loses nothing: the changes stay marked for the next sync."""
        try:
            self.last_sync = self._sync(col, schema_changed_at_open)
        except Exception as exc:  # noqa: BLE001 — reported, never raised: the reply is sent
            self.last_sync = SyncStatus(ok=False, message=f"AnkiWeb sync failed: {exc}")
        if not self.last_sync.ok:
            log.warning("%s", self.last_sync.message)
        self._close(col)

    def _sync(self, col: Collection, schema_changed_at_open: bool) -> SyncStatus:
        """One AnkiWeb sync of the open collection, media included."""
        auth = self.auth
        if auth is None:
            return SyncStatus(ok=True, message="AnkiWeb sync off")
        out = col.sync_collection(auth, sync_media=False)
        if out.new_endpoint:
            auth.endpoint = out.new_endpoint
        choice = full_sync_choice(out.required, schema_changed_at_open, bool(col.schema_changed()))
        if choice == "leave":
            return SyncStatus(ok=False, message=FULL_SYNC_MESSAGE)
        if choice == "upload":
            col.close_for_full_sync()
            col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
            col.reopen(after_full_sync=True)
        col.sync_media(auth)
        while col.media_sync_status().active:
            threading.Event().wait(0.2)
        return SyncStatus(ok=True, message="synced with AnkiWeb")

    @contextmanager
    def _use(self, *, write: bool = False) -> Iterator[Collection]:
        """The open collection for one operation; Anki's errors become `AnkiError`."""
        with self.action() as col:
            try:
                yield col
            except anki.errors.AnkiException as exc:
                raise AnkiError(str(exc) or type(exc).__name__) from exc
            finally:
                if write:
                    self._wrote = True

    def login(self, username: str, password: str) -> SyncAuth:
        """Log in to AnkiWeb; the returned key (`hkey`) is what to keep, never the password."""
        with self._use() as col:
            try:
                return col.sync_login(username, password, endpoint=None)
            except anki.errors.SyncError as exc:
                raise AnkiError(f"AnkiWeb login failed: {exc}") from exc

    # ----------------------------------------------------------------- decks

    def deck_names(self) -> list[str]:
        with self._use() as col:
            return sorted(d.name for d in col.decks.all_names_and_ids(skip_empty_default=True))

    def deck_names_and_ids(self) -> dict[str, int]:
        with self._use() as col:
            return {d.name: int(d.id) for d in col.decks.all_names_and_ids()}

    def create_deck(self, name: str) -> int:
        """Create a deck (and any intermediate parents). Returns the deck id."""
        with self._use(write=True) as col:
            return int(col.decks.id(name, create=True) or 0)

    def delete_deck(self, name: str, *, cards_too: bool = False) -> None:
        """Delete a deck and its sub-decks. When *cards_too* is False, cards move to Default."""
        with self._use(write=True) as col:
            did = col.decks.id_for_name(name)
            if not did:
                return
            if not cards_too:
                cids = col.find_cards(f"deck:{_quote(name)}")
                if cids:
                    col.set_deck(cids, 1)
            col.decks.remove([did])

    def _deck_id(self, col: Collection, deck: str) -> DeckId:
        did = col.decks.id_for_name(deck)
        if not did:
            raise AnkiError(f"deck was not found: {deck}")
        return did

    def deck_config(self, deck: str) -> dict[str, Any]:
        """The options group (preset) of a deck (`new.perDay`, `rev.perDay`, `reviewOrder`…)."""
        with self._use() as col:
            return dict(col.decks.config_dict_for_deck_id(self._deck_id(col, deck)))

    def deck_limits(self, deck: str) -> DeckLimits:
        """The effective deck limit of `deck` and its studied-today counter."""
        with self._use() as col:
            did = self._deck_id(col, deck)
            raw = col.decks.get(did) or {}
            conf = col.decks.config_dict_for_deck_id(did)
            today = col.sched.today

            def limit(kind: str, preset: int) -> tuple[int, int]:
                own = raw.get(f"{kind}Limit")
                default = preset if own is None else int(own)
                today_only = raw.get(f"{kind}LimitToday") or {}
                if today_only and int(today_only.get("today", -1)) == today:
                    return int(today_only.get("limit", default)), default
                return default, default

            def studied(key: str) -> int:
                day, count = (raw.get(key) or [0, 0])[:2]
                return int(count) if int(day) == today else 0

            review, review_default = limit("review", int(conf["rev"]["perDay"]))
            new, new_default = limit("new", int(conf["new"]["perDay"]))
            return DeckLimits(
                review=review,
                new=new,
                review_default=review_default,
                new_default=new_default,
                studied_review=studied("revToday"),
                studied_new=studied("newToday"),
            )

    def apply_all_parent_limits(self) -> bool:
        """Anki's option to apply every parent deck's limit to a sub-deck studied alone."""
        with self._use() as col:
            return bool(col.get_config("applyAllParentLimits", False))

    def extend_limits(self, deck: str, *, new: int = 0, review: int = 0) -> None:
        """Lower the deck's studied-today counter by `new` and `review` (Custom Study
        « increase today's limit »); its parents too when `apply_all_parent_limits` is on.
        Touches neither the cards nor the review history."""
        if not new and not review:
            return
        with self._use(write=True) as col:
            did = self._deck_id(col, deck)
            col._backend.extend_limits(deck_id=did, new_delta=new, review_delta=review)

    def study_queue(self, deck: str, limit: int = 10_000) -> list[QueuedCard]:
        """The study queue of `deck`: the cards Anki's scheduler would show today, in Anki's
        order, its limits, sub-deck limits, sibling burying and display order applied."""
        with self._use() as col:
            did = self._deck_id(col, deck)
            previous = col.decks.get_current_id()
            col.decks.select(did)
            try:
                queued: QueuedCards = _sched(col).get_queued_cards(fetch_limit=limit)
            finally:
                col.decks.select(previous)
            kinds = {QueuedCards.NEW: "new", QueuedCards.LEARNING: "learn"}
            return [QueuedCard(int(q.card.id), kinds.get(q.queue, "review")) for q in queued.cards]

    def today(self) -> int:
        """Anki's day number for today (what a review card's `due` counts in)."""
        with self._use() as col:
            return int(col.sched.today)

    # ---------------------------------------------------------------- search

    def find_card_ids(self, query: str) -> list[int]:
        with self._use() as col:
            return [int(c) for c in col.find_cards(query)]

    def find_note_ids(self, query: str) -> list[int]:
        with self._use() as col:
            return [int(n) for n in col.find_notes(query)]

    def cards_info(self, card_ids: list[int]) -> list[Card]:
        """Cards by id, in the order given. Unknown ids are dropped."""
        if not card_ids:
            return []
        with self._use() as col:
            notes: dict[int, AnkiNote] = {}
            decks: dict[int, str] = {}
            out = []
            for cid in card_ids:
                try:
                    card = col.get_card(cid)  # ty: ignore[invalid-argument-type]
                except anki.errors.NotFoundError:
                    continue
                note = notes.get(card.nid)
                if note is None:
                    note = notes[card.nid] = card.note()
                if card.did not in decks:
                    decks[card.did] = col.decks.name(card.did)
                out.append(_card(card, note, decks[card.did]))
            return out

    def notes_info(self, note_ids: list[int]) -> list[Note]:
        """Notes by id, in the order given. Unknown ids are dropped."""
        if not note_ids:
            return []
        with self._use() as col:
            out = []
            for nid in note_ids:
                try:
                    note = col.get_note(nid)  # ty: ignore[invalid-argument-type]
                except anki.errors.NotFoundError:
                    continue
                out.append(_note(note))
            return out

    def find_cards(self, query: str) -> list[Card]:
        with self._use():
            return self.cards_info(self.find_card_ids(query))

    def find_notes(self, query: str) -> list[Note]:
        with self._use():
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
            raise AnkiError(f"No card with id {card_id}")
        return cards[0]

    def note(self, note_id: int) -> Note:
        notes = self.notes_info([note_id])
        if not notes:
            raise AnkiError(f"No note with id {note_id}")
        return notes[0]

    def note_card_ids(self, note_id: int) -> list[int]:
        """Ids of every card of a note, wherever those cards live."""
        return self.find_card_ids(f"nid:{note_id}")

    # ------------------------------------------------------------------ edit

    def _get_note(self, col: Collection, note_id: int) -> AnkiNote:
        try:
            return col.get_note(note_id)  # ty: ignore[invalid-argument-type]
        except anki.errors.NotFoundError as exc:
            raise AnkiError(f"note was not found: {note_id}") from exc

    def _set_fields(self, note: AnkiNote, fields: Mapping[str, str]) -> None:
        names = note.keys()
        for name, value in fields.items():
            if name not in names:
                raise AnkiError(f"field « {name} » not in note type {_type_name(note)}")
            note[name] = value

    def update_note_fields(self, note_id: int, fields: dict[str, str]) -> None:
        """Overwrite the given fields of a note (other fields untouched). Values are HTML."""
        self.update_note(note_id, fields=fields)

    def update_note(
        self,
        note_id: int,
        fields: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        with self._use(write=True) as col:
            note = self._get_note(col, note_id)
            if fields is not None:
                self._set_fields(note, fields)
            if tags is not None:
                note.tags = list(tags)
            col.update_note(note)

    def add_tags(self, note_ids: list[int], tags: str) -> None:
        """`tags` is a space-separated string, as Anki expects."""
        with self._use(write=True) as col:
            col.tags.bulk_add(note_ids, tags)  # ty: ignore[invalid-argument-type]

    def remove_tags(self, note_ids: list[int], tags: str) -> None:
        with self._use(write=True) as col:
            col.tags.bulk_remove(note_ids, tags)  # ty: ignore[invalid-argument-type]

    def set_flag(self, card_ids: list[int], flag: int) -> None:
        """Set the flag colour (0 clears) on the given cards."""
        if not 0 <= flag <= 7:
            raise ValueError("flag must be between 0 (none) and 7")
        if not card_ids:
            return
        with self._use(write=True) as col:
            col.set_user_flag_for_cards(flag, card_ids)  # ty: ignore[invalid-argument-type]

    def clear_flag(self, card_ids: list[int]) -> None:
        self.set_flag(card_ids, 0)

    def set_card_scheduling(
        self,
        card_ids: list[int],
        *,
        interval: int,
        due: int,
        queue: int,
        card_type: int,
        factor: int,
        reps: int,
        lapses: int,
        left: int,
    ) -> None:
        """Copy a scheduling state onto the given cards (used for split-fragment inheritance)."""
        with self._use(write=True) as col:
            cards = []
            for cid in card_ids:
                card = col.get_card(cid)  # ty: ignore[invalid-argument-type]
                card.ivl, card.due, card.factor = interval, due, factor
                card.queue, card.type = queue, card_type  # ty: ignore[invalid-assignment]
                card.reps, card.lapses, card.left = reps, lapses, left
                cards.append(card)
            col.update_cards(cards)

    def unflag_note(self, note_id: int) -> None:
        """Clear the flag on every card of a note (the review unit is the note)."""
        with self._use(write=True):
            self.clear_flag(self.note_card_ids(note_id))

    def answer_outcomes(self, card_ids: list[int]) -> dict[int, Outcomes]:
        """The raw outcome of each answer button for each card, as the scheduler computes it
        now. Unknown ids are left out."""
        out: dict[int, Outcomes] = {}
        if not card_ids:
            return out
        with self._use() as col:
            for cid in card_ids:
                try:
                    states: SchedulingStates = col._backend.get_scheduling_states(cid)
                except anki.errors.NotFoundError:
                    continue
                buttons = [states.again, states.hard, states.good, states.easy]
                labels = [str(s) for s in _sched(col).describe_next_states(states)]
                out[cid] = Outcomes(seconds=[state_seconds(s) for s in buttons], labels=labels)
        return out

    def answer_card(
        self, card_id: int, ease: int, *, answered_at: int, time_ms: int, days_late: int = 0
    ) -> Replay | None:
        """Replay one answer (ease 1 again .. 4 easy) given at `answered_at` (ms since epoch)
        after `time_ms` on the card: a timestamped replay. Anki's review history and the card's
        last-review time get that instant and duration.

        `days_late` > 0 says the answer's Anki day was that many days before today. The
        scheduler counts elapsed days up to today, so the card then gets the state of the
        answer's day instead: the scheduler is asked with the card's due date and last-review
        time moved forward by the lag, the card is put back as it was, and it is answered with
        that state (Anki accepts a new state from the caller when the current one matches the
        card). Two extra card writes, outside Anki's undo list. Returns None when the card does
        not exist."""
        rating = _RATINGS.get(ease)
        if rating is None:
            raise ValueError("ease must be between 1 and 4")
        with self._use(write=True) as col:
            try:
                card = col.get_card(card_id)  # ty: ignore[invalid-argument-type]
            except anki.errors.NotFoundError:
                return None
            states: SchedulingStates = col._backend.get_scheduling_states(card_id)
            new_states = states
            if days_late > 0 and card.queue in (2, 3):  # due counted in days
                due, last_review = card.due, card.last_review_time
                card.due = due + days_late
                if last_review:
                    card.last_review_time = last_review + days_late * _DAY_SECONDS
                col.update_card(card, skip_undo_entry=True)
                try:
                    new_states = col._backend.get_scheduling_states(card_id)
                finally:
                    card.due, card.last_review_time = due, last_review
                    col.update_card(card, skip_undo_entry=True)
            deck_before = col.decks.get(card.did) or {}
            _sched(col).answer_card(
                CardAnswer(
                    card_id=card_id,
                    current_state=states.current,
                    new_state=_button_state(new_states, rating),
                    rating=rating,
                    answered_at_millis=answered_at,
                    milliseconds_taken=max(0, time_ms),
                )
            )
            deck_after = col.decks.get(card.did) or {}
            answered = col.get_card(card_id)  # ty: ignore[invalid-argument-type]
            today = col.sched.today

            def counted(key: str) -> int:
                return _counter(deck_after, key, today) - _counter(deck_before, key, today)

            return Replay(
                card=_card(answered, answered.note(), col.decks.name(answered.did)),
                counted_new=counted("newToday"),
                counted_review=counted("revToday"),
            )

    def set_due_date(self, card_ids: list[int], days: str) -> None:
        """Anki's "Set due date": `days` is relative to today ("0" today, "3" in three days,
        "1-7" a random day in range). A trailing `!` also resets the interval to that delay;
        without it Anki keeps the interval."""
        with self._use(write=True) as col:
            col.sched.set_due_date(card_ids, days)  # ty: ignore[invalid-argument-type]

    def suspend(self, card_ids: list[int]) -> None:
        with self._use(write=True) as col:
            col.sched.suspend_cards(card_ids)  # ty: ignore[invalid-argument-type]

    def unsuspend(self, card_ids: list[int]) -> None:
        with self._use(write=True) as col:
            col.sched.unsuspend_cards(card_ids)  # ty: ignore[invalid-argument-type]

    def change_deck(self, card_ids: list[int], deck: str) -> None:
        with self._use(write=True) as col:
            did = col.decks.id(deck, create=True)
            col.set_deck(card_ids, int(did or 0))  # ty: ignore[invalid-argument-type]

    def add_note(
        self,
        deck: str,
        model: str,
        fields: dict[str, str],
        tags: list[str] | None = None,
        allow_duplicate: bool = False,
    ) -> int:
        with self._use(write=True) as col:
            notetype = col.models.by_name(model)
            if notetype is None:
                raise AnkiError(f"model was not found: {model}")
            note = col.new_note(notetype)
            self._set_fields(note, fields)
            note.tags = list(tags or [])
            check = note.fields_check()
            if check == 1:  # EMPTY
                raise AnkiError("cannot create note because it is empty")
            if check == 2 and not allow_duplicate:  # DUPLICATE
                raise AnkiError("cannot create note because it is a duplicate")
            col.add_note(note, col.decks.id(deck, create=True))  # ty: ignore[invalid-argument-type]
            return int(note.id)

    def delete_notes(self, note_ids: list[int]) -> None:
        with self._use(write=True) as col:
            for nid in note_ids:
                self._get_note(col, nid)
            col.remove_notes(note_ids)  # ty: ignore[invalid-argument-type]

    def update_note_model(
        self,
        note_id: int,
        model: str,
        fields: dict[str, str],
        tags: list[str],
    ) -> None:
        """Change the note's type, fields and tags. Fields are matched to the new type by name,
        case ignored; a field not given is left empty. Cards whose template the new type lacks
        are removed. A change of type is a schema change: AnkiWeb then needs a full sync."""
        with self._use(write=True) as col:
            note = self._get_note(col, note_id)
            new_type = col.models.by_name(model)
            if new_type is None:
                raise AnkiError(f"model was not found: {model}")
            if note.mid != new_type["id"]:
                info = col.models.change_notetype_info(
                    old_notetype_id=note.mid, new_notetype_id=new_type["id"]
                )
                request = info.input
                request.note_ids.extend([note_id])
                col.models.change_notetype_of_notes(request)
                note = self._get_note(col, note_id)
            names = note.keys()
            by_lower = {name.lower(): name for name in names}
            for name in names:
                note[name] = ""
            for name, value in fields.items():
                real = by_lower.get(name.lower())
                if real is not None:
                    note[real] = value
            note.tags = list(tags)
            col.update_note(note)

    # ----------------------------------------------------------------- media

    def retrieve_media_file(self, name: str) -> bytes | None:
        """The bytes of one file in the collection's media folder, or None when it does not
        exist. Read straight from the folder: no need to open the collection."""
        folder = self.media_dir
        if folder is None or not name or os.sep in name or name in {".", ".."}:
            return None
        path = folder / name
        return path.read_bytes() if path.is_file() else None

    # ---------------------------------------------------------------- models

    def _notetype(self, col: Collection, model: str) -> dict[str, Any]:
        notetype = col.models.by_name(model)
        if notetype is None:
            raise AnkiError(f"model was not found: {model}")
        return dict(notetype)

    def model_names(self) -> list[str]:
        with self._use() as col:
            return sorted(col.models.all_names())

    def model_field_names(self, model: str) -> list[str]:
        with self._use() as col:
            return [f["name"] for f in self._notetype(col, model)["flds"]]

    def model_names_and_fields(self) -> dict[str, list[str]]:
        """Every note type mapped to its field names."""
        with self._use() as col:
            return {
                nt["name"]: [f["name"] for f in nt["flds"]]
                for nt in sorted(col.models.all(), key=lambda nt: nt["name"])
            }

    def model_styling(self, model: str) -> str:
        """The note type's stylesheet."""
        with self._use() as col:
            return str(self._notetype(col, model).get("css", ""))

    def model_templates(self, model: str) -> dict[str, dict[str, str]]:
        """Card name -> {"Front": html, "Back": html}."""
        with self._use() as col:
            return _templates(self._notetype(col, model))

    def note_type(self, model: str) -> NoteType:
        """Fields, card templates and CSS of a note type."""
        with self._use() as col:
            nt = self._notetype(col, model)
            return NoteType(
                name=model,
                fields=[f["name"] for f in nt["flds"]],
                templates=_templates(nt),
                css=str(nt.get("css", "")),
            )


def _templates(notetype: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {t["name"]: {"Front": t["qfmt"], "Back": t["afmt"]} for t in notetype["tmpls"]}


def _button_state(states: SchedulingStates, rating: int) -> SchedulingState:
    return {
        CardAnswer.AGAIN: states.again,
        CardAnswer.HARD: states.hard,
        CardAnswer.GOOD: states.good,
        CardAnswer.EASY: states.easy,
    }[rating]


def _counter(deck: Mapping[str, Any], key: str, today: int) -> int:
    """A deck's studied-today counter (`newToday`, `revToday`: [day, count]); a count stored
    for an earlier day is 0, since Anki resets it on the deck's first answer of `today`."""
    value = deck.get(key) or [0, 0]
    if len(value) < 2 or int(value[0]) != today:
        return 0
    return int(value[1])


def _sched(col: Collection) -> Scheduler:
    """The v3 scheduler, the only one Anki 23.10 and later run."""
    return cast(Scheduler, col.sched)


def _type_name(note: AnkiNote) -> str:
    notetype = note.note_type()
    return str(notetype["name"]) if notetype else ""


def _note(note: AnkiNote) -> Note:
    return Note(
        note_id=int(note.id),
        model_name=_type_name(note),
        tags=list(note.tags),
        fields=dict(note.items()),
        card_ids=[int(c) for c in note.card_ids()],
    )


def _card(card: AnkiCard, note: AnkiNote, deck_name: str) -> Card:
    return Card(
        card_id=int(card.id),
        note_id=int(card.nid),
        deck_name=deck_name,
        model_name=_type_name(note),
        fields=dict(note.items()),
        flag=int(card.user_flag()),
        ord=int(card.ord),
        interval=int(card.ivl),
        due=int(card.due),
        reps=int(card.reps),
        lapses=int(card.lapses),
        queue=int(card.queue),
        type=int(card.type),
        factor=int(card.factor),
        left=int(card.left),
    )


def anki_client_from_env(
    env: Mapping[str, str] | None = None, *, collection: str | Path | None = None
) -> AnkiClient:
    """The client the app and the CLI use: collection from `collection`, else `ANKI_COLLECTION`,
    else the only profile's; AnkiWeb login from `ANKIWEB_HKEY` (and `ANKIWEB_ENDPOINT`), when
    set, whichever the collection."""
    env = os.environ if env is None else env
    hkey = (env.get("ANKIWEB_HKEY") or "").strip()
    auth = SyncAuth(hkey=hkey, endpoint=env.get("ANKIWEB_ENDPOINT") or None) if hkey else None
    return AnkiClient(collection or env.get("ANKI_COLLECTION") or None, auth=auth)


__all__ = [
    "LOCKED_MESSAGE",
    "AnkiClient",
    "AnkiError",
    "CollectionLocked",
    "DeckLimits",
    "Outcomes",
    "QueuedCard",
    "Replay",
    "SyncStatus",
    "anki_client_from_env",
    "default_collection_path",
    "full_sync_choice",
    "state_seconds",
]
