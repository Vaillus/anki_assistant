"""Validation of a workspace: plan, snapshot, ordered writes, rollback, undo.

Spec: specs/workspace.md.

Pure functions over an `AnkiClient` and a `SourceStore`, no FastAPI: `web/routes_workspace.py`
is the HTTP shell, the tests drive this module with the fake client of `tests/test_review.py`.

AnkiConnect has no transactions, so "all or nothing" is emulated:

1. the plan is validated before anything is written;
2. every existing note of the plan is read once into a `Snapshot`;
3. writes happen in an order where nothing is lost until the very last step
   (creates -> edits -> moves -> unflag / flag -> deletes);
4. on the first failure, whatever was written is put back from the snapshot (best effort), and
   the report says what failed and whether the rollback completed.

The snapshot of a successful validation is what `undo` replays later — the same restore path as
the rollback — unless the validation deleted notes, which cannot be recreated.
"""

from __future__ import annotations

import html
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from anki_assistant import review
from anki_assistant.client import AnkiClient, AnkiConnectError
from anki_assistant.models import strip_html
from anki_assistant.review import REASON_FIELD, NoteNotFound
from anki_assistant.sources import SourceStore

__all__ = [
    "ACTIONS",
    "ApplyPlan",
    "ApplyReport",
    "CardPlan",
    "CardSched",
    "ModifiedSince",
    "NoteSnap",
    "NothingToUndo",
    "PlanError",
    "Snapshot",
    "anchors_after_move",
    "apply",
    "check_fields",
    "comment_html",
    "take_snapshot",
    "target_model",
    "undo",
    "validate",
]

Action = Literal["edit", "create", "delete", "keep", "defer"]
ACTIONS: tuple[str, ...] = ("edit", "create", "delete", "keep", "defer")


class PlanError(ValueError):
    """The plan is malformed; nothing was written."""


class NothingToUndo(LookupError):
    """No validation to undo, or one that deleted notes."""


class ModifiedSince(RuntimeError):
    """A note no longer holds the values the validation wrote; nothing was written."""


# --------------------------------------------------------------------------- data


@dataclass
class CardPlan:
    """One card of the apply request (specs/workspace.md#api)."""

    wid: str
    action: str
    note_id: int | None = None
    #: Complete raw values of the shown version (edit) or of the new note (create).
    fields: dict[str, str] | None = None
    tags: list[str] | None = None
    model: str | None = None
    deck: str | None = None
    source_ids: list[str] | None = None
    move_to: str | None = None
    parent_wid: str | None = None
    #: Deferred (« to review »): the flag stays (or is set), `Back Extra` becomes `comment`.
    #: Implied by the action `defer`; a modifier on `edit` and `create`.
    defer: bool = False
    #: Plain text for `Back Extra`, only with a deferral. None leaves the field alone.
    comment: str | None = None

    @property
    def deferred(self) -> bool:
        return self.action == "defer" or (self.action in ("edit", "create") and self.defer)


@dataclass
class ApplyPlan:
    deck: str
    cards: list[CardPlan] = field(default_factory=list)
    #: Empty `Back Extra` on every edited note that had a reason.
    clear_reason: bool = True


@dataclass
class CardSched:
    """Scheduling state of one card, enough to stamp onto a newly-created card."""

    interval: int
    due: int
    queue: int
    card_type: int
    factor: int
    reps: int
    lapses: int
    left: int


@dataclass
class NoteSnap:
    """What it takes to put an existing note back: fields, tags, deck, model and flags."""

    note_id: int
    fields: dict[str, str]
    tags: list[str]
    deck: str
    model: str
    card_ids: list[int]
    #: card id -> flag (0 = none), for every card of the note.
    flags: dict[int, int]
    #: card id -> scheduling state, for fragment inheritance on split.
    scheduling: dict[int, CardSched] = field(default_factory=dict)

    @property
    def flagged_card_ids(self) -> list[int]:
        return [cid for cid, flag in self.flags.items() if flag]

    @property
    def best_scheduling(self) -> CardSched | None:
        """The scheduling of the most-reviewed card (longest interval), for fragment inheritance."""
        if not self.scheduling:
            return None
        return max(self.scheduling.values(), key=lambda s: (s.interval, s.reps))


@dataclass
class Snapshot:
    """The state before a validation plus what the validation did, enough to undo it."""

    notes: dict[int, NoteSnap] = field(default_factory=dict)
    #: Note ids created, in creation order.
    created: list[int] = field(default_factory=list)
    #: Note ids deleted (an undo is refused when this is non-empty).
    deleted: list[int] = field(default_factory=list)
    #: Note ids whose cards were moved.
    moved: list[int] = field(default_factory=list)
    #: note id -> anchors before the move (a move can drop anchors; moving back restores them).
    anchors_before: dict[int, list[str]] = field(default_factory=dict)
    #: note id -> fields written, for the "modified since" check of undo.
    written: dict[int, dict[str, str]] = field(default_factory=dict)
    #: Note ids whose note type was changed (old model is in NoteSnap.model).
    model_changed: list[int] = field(default_factory=list)
    #: Note ids whose flags were cleared.
    unflagged: list[int] = field(default_factory=list)
    #: Note ids that carried no flag and were flagged by a deferral (every card, red).
    flagged: list[int] = field(default_factory=list)


@dataclass
class ApplyReport:
    """What a validation (or an undo) did. `ok` means every Anki write went through."""

    ok: bool = True
    #: wid -> note id, for every draft note that exists in Anki when the report is sent.
    created: dict[str, int] = field(default_factory=dict)
    resolved: list[int] = field(default_factory=list)
    #: Note ids left (or put) in the queue with their comment written.
    deferred: list[int] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    moved: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rolled_back: bool = False
    undo_available: bool = False


# ---------------------------------------------------------------------- validation


def validate(plan: ApplyPlan) -> list[str]:
    """Shape errors of a plan, shown as-is. Empty when the plan is fine."""
    errors: list[str] = []
    if not plan.cards:
        errors.append("empty plan")
    seen_wids: set[str] = set()
    seen_notes: set[int] = set()
    for card in plan.cards:
        who = card.wid or "?"
        if not card.wid:
            errors.append("card without an identifier")
        elif card.wid in seen_wids:
            errors.append(f"duplicate identifier {card.wid}")
        seen_wids.add(card.wid)
        if card.action not in ACTIONS:
            errors.append(f"{who}: unknown action « {card.action} »")
            continue
        if card.action == "create":
            if card.note_id is not None:
                errors.append(f"{who}: a note to create has a note_id")
            if not card.model:
                errors.append(f"{who}: missing note type")
            if not card.fields:
                errors.append(f"{who}: missing fields")
            if card.move_to:
                errors.append(f"{who}: a note to create cannot be moved")
            if card.comment is not None and not card.defer:
                errors.append(f"{who}: a comment accompanies a non-deferred note")
            continue
        if card.note_id is None:
            errors.append(f"{who}: missing note_id")
        elif card.note_id in seen_notes:
            errors.append(f"{who}: note #{card.note_id} appears twice")
        else:
            seen_notes.add(card.note_id)
        if card.action == "edit" and not card.fields:
            errors.append(f"{who}: missing fields")
        if card.action == "delete" and card.move_to:
            errors.append(f"{who}: a deleted note cannot be moved")
        if card.defer and card.action not in ("edit", "create"):
            errors.append(f"{who}: only an edit or a create can be deferred")
        if card.comment is not None and not card.deferred:
            errors.append(f"{who}: a comment accompanies a non-deferred note")
    return errors


# ------------------------------------------------------------------------ snapshot


def take_snapshot(client: AnkiClient, plan: ApplyPlan) -> Snapshot:
    """Read every existing note of the plan in two round trips. Unknown note -> NoteNotFound."""
    ids = [card.note_id for card in plan.cards if card.action != "create" and card.note_id]
    snap = Snapshot()
    if not ids:
        return snap
    notes = {note.note_id: note for note in client.notes_info(ids)}
    missing = [nid for nid in ids if nid not in notes]
    if missing:
        raise NoteNotFound("note(s) not found: " + ", ".join(f"#{n}" for n in missing))
    cards_by_note: dict[int, list] = defaultdict(list)
    for card in client.cards_info([cid for note in notes.values() for cid in note.card_ids]):
        cards_by_note[card.note_id].append(card)
    for nid in ids:
        note = notes[nid]
        cards = sorted(cards_by_note.get(nid, []), key=lambda c: c.ord)
        snap.notes[nid] = NoteSnap(
            note_id=nid,
            fields=dict(note.fields),
            tags=list(note.tags),
            deck=cards[0].deck_name if cards else plan.deck,
            model=note.model_name,
            card_ids=[c.card_id for c in cards] or list(note.card_ids),
            flags={c.card_id: c.flag for c in cards},
            scheduling={
                c.card_id: CardSched(
                    interval=c.interval,
                    due=c.due,
                    queue=c.queue,
                    card_type=c.type,
                    factor=c.factor,
                    reps=c.reps,
                    lapses=c.lapses,
                    left=c.left,
                )
                for c in cards
            },
        )
    return snap


def target_model(card: CardPlan, snap: Snapshot) -> str:
    """The note type a created or edited card writes: `model` when given, else the note's."""
    if card.model:
        return card.model
    return snap.notes[int(card.note_id or 0)].model


def check_fields(client: AnkiClient, plan: ApplyPlan, snap: Snapshot) -> dict[str, list[str]]:
    """Fit the field names of every created or edited note to its note type, before any write.

    The note type is the one the card writes ([target_model]): an edit that changes the type is
    checked against the new type, not the note's current one. A name that differs only by case
    is corrected in place to the type's spelling. An unknown name, or an empty first field on a
    note to create (Anki refuses it as an empty note), raises `PlanError` naming the card, the
    field and the type's fields. Proposals are the usual source: Claude may spell a field from
    memory instead of reading the type.

    Returns the field names of every note type met, so that `apply` decides about `Back Extra`
    from the type it writes without asking Anki again.
    """
    targets: dict[str, str] = {}
    for card in plan.cards:
        if card.action in ("create", "edit"):
            targets[card.wid] = target_model(card, snap)
    fields_of: dict[str, list[str]] = {}
    if not targets:
        return fields_of
    errors: list[str] = []
    for model in sorted(set(targets.values())):
        try:
            fields_of[model] = list(client.model_field_names(model))
        except AnkiConnectError as exc:
            errors.append(f"note type « {model} »: {exc}")
    for card in plan.cards:
        model = targets.get(card.wid)
        if model is None or model not in fields_of:
            continue
        names = fields_of[model]
        by_lower = {name.lower(): name for name in names}
        fixed: dict[str, str] = {}
        for name, value in (card.fields or {}).items():
            real = name if name in names else by_lower.get(name.lower())
            if real is None:
                errors.append(
                    f"{card.wid}: field « {name} » unknown for type {model}"
                    f" (fields: {', '.join(names)})"
                )
                continue
            fixed[real] = value
        if card.action == "create" and names and not strip_html(fixed.get(names[0], "")).strip():
            errors.append(
                f"{card.wid}: the first field ({names[0]}) is empty, Anki refuses the note"
            )
        card.fields = fixed
    if errors:
        raise PlanError(" ; ".join(errors))
    return fields_of


# --------------------------------------------------------------------------- apply


def anchors_after_move(store: SourceStore, note_id: int, deck: str) -> None:
    """Keep each anchor of the note only if its source is in the destination's effective corpus."""
    anchors = store.anchors(note_id)
    if not anchors:
        return
    in_destination = {source.id for source in store.corpus(deck)}
    store.set_anchors(note_id, [sid for sid in anchors if sid in in_destination])


def _inherit_scheduling(
    client: AnkiClient,
    card: CardPlan,
    view: review.NoteView,
    wid_to_note: Mapping[str, int | None],
    snap: Snapshot,
) -> None:
    """Copy the parent note's best scheduling state onto a fragment's cards."""
    parent_nid = wid_to_note.get(str(card.parent_wid))
    if not parent_nid or parent_nid not in snap.notes:
        return
    sched = snap.notes[parent_nid].best_scheduling
    if sched is None or (sched.interval == 0 and sched.reps == 0):
        return
    card_ids = list(view.card_ids)
    if card_ids:
        client.set_card_scheduling(
            card_ids,
            interval=sched.interval,
            due=sched.due,
            queue=sched.queue,
            card_type=sched.card_type,
            factor=sched.factor,
            reps=sched.reps,
            lapses=sched.lapses,
            left=sched.left,
        )


def _reason_to_clear(snap: NoteSnap) -> bool:
    return bool(strip_html(snap.fields.get(REASON_FIELD, "")))


def comment_html(comment: str) -> str:
    """The `Back Extra` value for a plain-text comment: escaped, one `<br>` per line break."""
    return "<br>".join(
        html.escape(line.strip(), quote=False) for line in comment.strip().splitlines()
    )


def _comment_for(card: CardPlan, field_names: list[str], report: ApplyReport) -> str | None:
    """What a deferred card writes to `Back Extra`: the comment as HTML, or None when there is
    no comment or the note type has no such field (reported, not fatal: the flag still lands).

    `field_names` are those of the type the card writes — after a change of note type, the new
    type's — not the note's current fields."""
    if not card.deferred or card.comment is None:
        return None
    if REASON_FIELD not in field_names:
        if card.comment.strip():
            report.errors.append(f"{card.wid}: no {REASON_FIELD} field, comment not written")
        return None
    return comment_html(card.comment)


def apply(client: AnkiClient, store: SourceStore, plan: ApplyPlan) -> tuple[ApplyReport, Snapshot]:
    """Write the plan in the safe order; on failure roll back and report. Never raises past
    validation, the snapshot and the field check (a malformed plan or an unknown field ->
    PlanError, an unknown note -> NoteNotFound)."""
    errors = validate(plan)
    if errors:
        raise PlanError(" ; ".join(errors))
    snap = take_snapshot(client, plan)
    fields_of = check_fields(client, plan, snap)
    report = ApplyReport()

    creates = [c for c in plan.cards if c.action == "create"]
    edits = [c for c in plan.cards if c.action == "edit"]
    keeps = [c for c in plan.cards if c.action == "keep"]
    defers = [c for c in plan.cards if c.action == "defer"]
    deletes = [c for c in plan.cards if c.action == "delete"]
    moves = [c for c in edits + keeps + defers if c.move_to]
    deferred = [c for c in edits + defers if c.deferred]

    wid_to_note = {c.wid: c.note_id for c in plan.cards if c.note_id}

    step = ""
    try:
        for card in creates:
            step = f"creating {card.wid}"
            fields = dict(card.fields or {})
            # A proposal may have left the field out: the note type says, not the proposal.
            comment = _comment_for(card, fields_of.get(str(card.model), []), report)
            if comment is not None:
                fields[REASON_FIELD] = comment
            view = review.create(
                client,
                deck=card.deck or plan.deck,
                model=str(card.model),
                fields=fields,
                tags=list(card.tags or []),
            )
            snap.created.append(view.note_id)
            report.created[card.wid] = view.note_id
            if card.source_ids:
                store.set_anchors(view.note_id, card.source_ids)
            if card.parent_wid:
                _inherit_scheduling(client, card, view, wid_to_note, snap)
            if card.deferred:
                step = f"flag de {card.wid}"
                client.set_flag(list(view.card_ids), 1)
                report.deferred.append(view.note_id)

        for card in edits:
            nid = int(card.note_id or 0)
            step = f"editing #{nid}"
            fields = dict(card.fields or {})
            new_model = target_model(card, snap)
            old_model = snap.notes[nid].model
            # `Back Extra` follows the type being written: a Cloze turned Basic has none.
            names = fields_of.get(new_model, [])
            comment = _comment_for(card, names, report)
            if comment is not None:
                fields[REASON_FIELD] = comment
            elif (
                not card.deferred
                and plan.clear_reason
                and REASON_FIELD in names
                and _reason_to_clear(snap.notes[nid])
            ):
                fields[REASON_FIELD] = ""
            if new_model != old_model:
                tags = list(card.tags) if card.tags is not None else list(snap.notes[nid].tags)
                client.update_note_model(nid, model=new_model, fields=fields, tags=tags)
                snap.model_changed.append(nid)
            else:
                review.edit(client, nid, fields=fields, tags=card.tags, unflag=False)
            snap.written[nid] = fields

        for card in defers:
            nid = int(card.note_id or 0)
            step = f"commentaire de #{nid}"
            comment = _comment_for(card, list(snap.notes[nid].fields), report)
            if comment is not None:
                fields = {REASON_FIELD: comment}
                client.update_note(nid, fields=fields)
                snap.written[nid] = fields

        for card in moves:
            nid = int(card.note_id or 0)
            step = f"moving #{nid}"
            snap.anchors_before[nid] = store.anchors(nid)
            client.change_deck(snap.notes[nid].card_ids, str(card.move_to))
            snap.moved.append(nid)
            report.moved.append(nid)
            anchors_after_move(store, nid, str(card.move_to))

        for card in edits + keeps:
            if card.deferred:
                continue
            nid = int(card.note_id or 0)
            step = f"clearing flag of #{nid}"
            flagged = snap.notes[nid].flagged_card_ids
            if flagged:
                client.clear_flag(flagged)
            snap.unflagged.append(nid)
            report.resolved.append(nid)

        for card in deferred:
            nid = int(card.note_id or 0)
            step = f"flag de #{nid}"
            note = snap.notes[nid]
            if not note.flagged_card_ids:
                client.set_flag(note.card_ids, 1)
                snap.flagged.append(nid)
            report.deferred.append(nid)

        if deletes:
            ids = [int(c.note_id or 0) for c in deletes]
            step = "deleting"
            client.delete_notes(ids)
            snap.deleted = ids
            report.deleted = ids
            try:
                store.remove_anchors(ids)
            except Exception as exc:  # noqa: BLE001 — sources.json is not Anki; report, keep ok
                report.errors.append(f"anchors of deleted notes: {exc}")
    except Exception as exc:  # noqa: BLE001 — every failure is reported, never raised
        report.ok = False
        report.errors.append(f"{step}: {exc}")
        rollback_errors = _restore(client, store, snap, report)
        report.errors.extend(rollback_errors)
        report.rolled_back = not rollback_errors
        report.resolved = []
        report.deferred = []
        report.moved = []

    report.undo_available = report.ok and not snap.deleted
    return report, snap


# -------------------------------------------------------------------- restore / undo


def _restore(
    client: AnkiClient, store: SourceStore, snap: Snapshot, report: ApplyReport
) -> list[str]:
    """Put back what the snapshot recorded; returns the errors met (empty = complete).

    Runs the steps of `apply` in reverse. Each one is attempted even if a previous one failed.
    Created notes that could be deleted disappear from `report.created` (they no longer exist);
    the others stay, so that the client can keep their id.
    """
    errors: list[str] = []
    for nid in reversed(snap.created):
        try:
            client.delete_notes([nid])
            store.remove_anchors([nid])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reverting creation of #{nid}: {exc}")
        else:
            for wid, created_id in list(report.created.items()):
                if created_id == nid:
                    del report.created[wid]
    snap.created = [nid for nid in snap.created if nid in report.created.values()]

    for nid in snap.unflagged:
        note = snap.notes[nid]
        by_flag: dict[int, list[int]] = defaultdict(list)
        for cid, flag in note.flags.items():
            if flag:
                by_flag[flag].append(cid)
        try:
            for flag, cids in by_flag.items():
                client.set_flag(cids, flag)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reverting flag clear of #{nid}: {exc}")
    snap.unflagged = []

    for nid in snap.flagged:
        try:
            client.clear_flag(snap.notes[nid].card_ids)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reverting flag of #{nid}: {exc}")
    snap.flagged = []

    for nid in snap.moved:
        note = snap.notes[nid]
        try:
            client.change_deck(note.card_ids, note.deck)
            store.set_anchors(nid, snap.anchors_before.get(nid, []))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reverting move of #{nid}: {exc}")
    snap.moved = []
    snap.anchors_before = {}

    for nid in list(snap.written):
        note = snap.notes[nid]
        try:
            if nid in snap.model_changed:
                client.update_note_model(
                    nid, model=note.model, fields=dict(note.fields), tags=list(note.tags)
                )
            else:
                client.update_note(nid, fields=dict(note.fields), tags=list(note.tags))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"reverting edit of #{nid}: {exc}")
    snap.written = {}
    snap.model_changed = []
    return errors


def undo(client: AnkiClient, store: SourceStore, snap: Snapshot | None) -> ApplyReport:
    """Revert the last validation from its snapshot (specs/workspace.md#undo).

    Refused (`NothingToUndo`) without a snapshot or when the validation deleted notes; refused
    (`ModifiedSince`) when an edited note no longer holds the values that were written — then
    nothing is written at all.
    """
    if snap is None:
        raise NothingToUndo("no validation to undo")
    if snap.deleted:
        raise NothingToUndo("the last validation deleted notes: cannot undo")
    if snap.written:
        current = {note.note_id: note for note in client.notes_info(list(snap.written))}
        changed = [
            nid
            for nid, fields in snap.written.items()
            if nid not in current
            or any(str(current[nid].fields.get(k, "")) != str(v) for k, v in fields.items())
        ]
        if changed:
            raise ModifiedSince(
                ", ".join(f"#{n}" for n in changed) + " modified since, cannot undo"
            )
    report = ApplyReport(created={f"#{nid}": nid for nid in snap.created})
    errors = _restore(client, store, snap, report)
    report.created = {}
    report.errors = errors
    report.ok = not errors
    report.rolled_back = not errors
    return report
