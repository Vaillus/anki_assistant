"""The phone app's server side: Anki day, batch, precomputed outcomes, sync. Spec: specs/mobile.md.

The phone reviews offline from a **batch** (3 Anki days of the mobile deck, each card with the
intervals Anki would give it) and later sends its **pending actions**; `sync` replays them into
Anki, oldest first and at most once each, then builds the next batch.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from urllib.parse import unquote

from anki_assistant.client import AnkiClient, Outcomes, _quote
from anki_assistant.models import Card
from anki_assistant.web.render import render_field

_log = logging.getLogger(__name__)

WINDOW_DAYS = 3
DEFAULT_DECK = "courant"
DEFAULT_ROLLOVER_HOUR = 4
ORANGE = 2

_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REVIEW_LOG = _ROOT / "review_log.jsonl"
DEFAULT_ACTIONS_LOG = _ROOT / "mobile_actions.jsonl"

ActionKind = Literal["answer", "flag", "unflag", "suspend"]
DropReason = Literal["card_missing", "left_deck", "suspended"]

# ------------------------------------------------------------------- Anki day


def anki_day(instant: datetime, rollover_hour: int) -> date:
    """The Anki day of a local instant: the date of *instant − rollover hour*."""
    return (instant - timedelta(hours=rollover_hour)).date()


def from_epoch_ms(ms: int) -> datetime:
    """A phone timestamp (ms since epoch) as a naive local datetime, like `datetime.now()`."""
    return datetime.fromtimestamp(ms / 1000)


def window(today: date) -> list[date]:
    """The Anki days one batch covers, starting today."""
    return [today + timedelta(days=k) for k in range(WINDOW_DAYS)]


# ---------------------------------------------------------- precomputed outcomes

# Unicode isolation / direction marks Anki wraps numbers in.
_MARKS = re.compile("[⁦-⁩‎‏]")


def clean_label(label: str) -> str:
    """An interval display string with Anki's isolation marks removed: `<⁨10⁩m` → `<10m`."""
    return _MARKS.sub("", label).strip()


# ----------------------------------------------------------- question / answer

_REF = re.compile(r"\{\{\s*[#^/]?\s*([^{}]+?)\s*\}\}")
_CLOZE_REF = re.compile(r"\{\{\s*cloze\s*:", re.I)
_MEDIA_SRC = re.compile(r'src="/api/media/([^"]+)"')


def template_fields(template: str, field_names: Iterable[str]) -> list[str]:
    """The note fields a card template references, in order of first reference.

    Filters (`cloze:`, `text:`…) are dropped; names that are not fields (`FrontSide`, `Tags`,
    add-on tags) are skipped.
    """
    names = set(field_names)
    out: list[str] = []
    for m in _REF.finditer(template):
        name = m.group(1).split(":")[-1].strip()
        if name in names and name not in out:
            out.append(name)
    return out


def card_sides(
    card: Card, templates: dict[str, dict[str, str]]
) -> tuple[list[str], list[str], int | None]:
    """(question fields, answer fields, cloze number) of a card, from its note type's templates.

    Question: fields on the front template. Answer: fields on the back template not already in
    the question. The cloze number is `ord + 1` for a cloze note type, else None.
    """
    names = list(card.fields)
    tmpls = list(templates.values())
    cloze: int | None = None
    template: dict[str, str] | None = None
    if tmpls and _CLOZE_REF.search(tmpls[0].get("Front", "")):
        template, cloze = tmpls[0], card.ord + 1
    elif 0 <= card.ord < len(tmpls):
        template = tmpls[card.ord]
    question = template_fields(template.get("Front", ""), names) if template else []
    if not question:  # no usable template: first field asks, the rest answers
        question = names[:1]
        answer = names[1:]
    else:
        back = template_fields(template.get("Back", ""), names) if template else []
        answer = [n for n in back if n not in question]
    return question, answer, cloze


def mobile_html(raw: str) -> tuple[str, list[str]]:
    """A field rendered by the display transform, pictures pointed at `/api/mobile/media/`,
    and the media file names it references."""
    rendered = render_field(raw)
    names = [unquote(html.unescape(m.group(1))) for m in _MEDIA_SRC.finditer(rendered)]
    return rendered.replace('src="/api/media/', 'src="/api/mobile/media/'), names


# ---------------------------------------------------------------------- batch


@dataclass
class FieldView:
    name: str
    html: str


@dataclass
class DayQuota:
    review: int
    new: int


@dataclass
class BatchCard:
    card_id: int
    note_id: int
    deck: str
    model: str
    ord: int
    day: int
    kind: Literal["learn", "review", "new"]
    cloze: int | None
    question: list[FieldView]
    answer: list[FieldView]
    flag: int
    media: list[str]
    #: Seconds Anki would give for again, hard, good, easy (None when not understood).
    outcomes: list[int | None]
    outcome_labels: list[str]
    #: The Anki day the card is due (review and interday learning cards), else None.
    due_day: str | None = None
    #: The card's deck's place in Anki's deck list, among the batch's decks.
    deck_rank: int = 0


@dataclass
class Batch:
    deck: str
    generated_at: int
    rollover_hour: int
    days: list[str]
    quota: list[DayQuota]
    cards: list[BatchCard] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    #: The preset's review order: `deck` (deck first, then due day) or `due` (due day).
    review_order: Literal["due", "deck"] = "due"


def daily_quotas(anki: AnkiClient, deck: str) -> list[DayQuota]:
    """Day 0: the effective deck limit minus what Anki counts as studied today. Later days:
    the effective deck limit without its "Today only" value."""
    limits = anki.deck_limits(deck)
    first = DayQuota(
        review=max(0, limits.review - limits.studied_review),
        new=max(0, limits.new - limits.studied_new),
    )
    later = [DayQuota(review=limits.review_default, new=limits.new_default)]
    return [first] + [DayQuota(review=q.review, new=q.new) for q in later * (WINDOW_DAYS - 1)]


@dataclass
class OrderSettings:
    """The display-order options of the mobile deck's options group that the batch honours
    (specs/mobile.md#batch). False keeps the default order for that option."""

    new_by_deck: bool = False
    review_by_deck: bool = False
    mix_interday: bool = False
    mix_new: bool = False


def order_settings(config: dict[str, Any]) -> OrderSettings:
    """The honoured order options of a deck's options group (`getDeckConfig`)."""
    return OrderSettings(
        new_by_deck=config.get("newGatherPriority") == 0 and config.get("newSortOrder") == 1,
        review_by_deck=config.get("reviewOrder") == 2,
        mix_interday=config.get("interdayLearningMix") == 0,
        mix_new=config.get("newMix") == 0,
    )


def _deck_order(card: Card) -> tuple[str, ...]:
    """Anki's deck-list order: parent before children, siblings by name, case ignored."""
    return deck_list_key(card.deck_name)


def deck_list_key(deck: str) -> tuple[str, ...]:
    """Sort key of a deck name in Anki's deck list."""
    return tuple(part.casefold() for part in deck.split("::"))


_T = TypeVar("_T")


def intersperse(one: Sequence[_T], two: Sequence[_T]) -> list[_T]:
    """Spread `two` evenly through `one`, each keeping its own order (Anki's intersperser,
    rslib scheduler/queue/builder/intersperser.rs)."""
    ratio = (len(one) + 1) / (len(two) + 1)
    out: list[_T] = []
    i = j = 0
    while i < len(one) and j < len(two):
        if (j + 1) * ratio <= i + 1:
            out.append(two[j])
            j += 1
        else:
            out.append(one[i])
            i += 1
    return out + list(one[i:]) + list(two[j:])


CardKind = Literal["learn", "review", "new"]


def _select(
    anki: AnkiClient, deck: str, quotas: Sequence[DayQuota], order: OrderSettings | None = None
) -> list[tuple[Card, int, CardKind]]:
    """The batch's cards, each with its day and kind, in batch order (specs/mobile.md#batch).

    Day 0 is Anki's study queue of the deck. Days 1 and 2 are rebuilt: each day's cards not
    taken by an earlier day are gathered in the order settings' order, then cut at the quota.
    Without order settings, every option keeps its default.
    """
    order = order or OrderSettings()
    queue = anki.study_queue(deck)
    scope = f"deck:{_quote(deck)} -is:suspended -is:buried"
    due_by = {k: set(anki.find_card_ids(f"{scope} -is:new prop:due<={k}")) for k in (1, 2)}
    new_ids = anki.find_card_ids(f"{scope} is:new")
    wanted = {q.card_id for q in queue} | due_by[WINDOW_DAYS - 1] | set(new_ids)
    infos = {c.card_id: c for c in anki.cards_info(sorted(wanted))}

    def new_key(c: Card) -> tuple[Any, ...]:
        return (_deck_order(c), c.due, c.ord) if order.new_by_deck else (c.due, c.ord)

    def review_key(c: Card) -> tuple[Any, ...]:
        return (_deck_order(c), c.due, c.card_id) if order.review_by_deck else (c.due, c.card_id)

    def interday_key(c: Card) -> tuple[Any, ...]:
        return (_deck_order(c), c.due) if order.review_by_deck else (c.due,)

    new_cards = sorted((infos[c] for c in new_ids if c in infos), key=new_key)

    chosen: list[tuple[Card, int, CardKind]] = [
        (infos[q.card_id], 0, cast(CardKind, q.kind)) for q in queue if q.card_id in infos
    ]
    taken = {card.card_id for card, _, _ in chosen}
    for day in range(1, len(quotas)):
        quota = quotas[day]
        pool = [infos[c] for c in due_by[day] if c in infos and c not in taken]
        intraday = sorted((c for c in pool if c.queue == 1), key=lambda c: c.due)
        interday = sorted((c for c in pool if c.queue == 3), key=interday_key)
        reviews = sorted((c for c in pool if c.queue == 2), key=review_key)
        news = [c for c in new_cards if c.card_id not in taken]
        intra: list[tuple[Card, CardKind]] = [(c, "learn") for c in intraday]
        learning: list[tuple[Card, CardKind]] = [(c, "learn") for c in interday]
        main: list[tuple[Card, CardKind]] = [(c, "review") for c in reviews[: max(quota.review, 0)]]
        fresh: list[tuple[Card, CardKind]] = [(c, "new") for c in news[: max(quota.new, 0)]]
        main = intersperse(main, learning) if order.mix_interday else learning + main
        main = intersperse(main, fresh) if order.mix_new else main + fresh
        for card, kind in intra + main:
            taken.add(card.card_id)
            chosen.append((card, day, kind))
    return chosen


def _templates(anki: AnkiClient, models: Iterable[str]) -> dict[str, dict[str, dict[str, str]]]:
    return {name: anki.model_templates(name) for name in sorted(set(models))}


def batch_card(
    card: Card,
    day: int,
    kind: CardKind,
    templates: dict[str, dict[str, str]],
    outcomes: Outcomes | None = None,
) -> BatchCard:
    question, answer, cloze = card_sides(card, templates)
    media: list[str] = []

    def views(names: list[str]) -> list[FieldView]:
        out = []
        for name in names:
            rendered, pictures = mobile_html(card.fields.get(name, ""))
            media.extend(p for p in pictures if p not in media)
            out.append(FieldView(name=name, html=rendered))
        return out

    seconds = list(outcomes.seconds[:4]) if outcomes else []
    seconds += [None] * (4 - len(seconds))
    labels = [clean_label(s) for s in outcomes.labels[:4]] if outcomes else []
    labels += [""] * (4 - len(labels))
    return BatchCard(
        card_id=card.card_id,
        note_id=card.note_id,
        deck=card.deck_name,
        model=card.model_name,
        ord=card.ord,
        day=day,
        kind=kind,
        cloze=cloze,
        question=views(question),
        answer=views(answer),
        flag=card.flag,
        media=media,
        outcomes=seconds,
        outcome_labels=labels,
    )


def build_batch(anki: AnkiClient, deck: str, rollover_hour: int, now: datetime) -> Batch:
    """The cards the phone downloads: Anki's study queue for day 0, one daily quota for each
    later Anki day of the window."""
    quotas = daily_quotas(anki, deck)
    order = order_settings(anki.deck_config(deck) or {})
    chosen = _select(anki, deck, quotas, order)
    templates = _templates(anki, (c.model_name for c, _, _ in chosen))
    outcomes = anki.answer_outcomes([c.card_id for c, _, _ in chosen])
    today = anki_day(now, rollover_hour)
    anki_today = anki.today() if chosen else 0
    ranks = {
        name: rank
        for rank, name in enumerate(sorted({c.deck_name for c, _, _ in chosen}, key=deck_list_key))
    }
    cards = []
    for c, day, kind in chosen:
        bc = batch_card(c, day, kind, templates.get(c.model_name, {}), outcomes.get(c.card_id))
        if c.queue in (2, 3):  # due counted in days
            bc.due_day = (today + timedelta(days=c.due - anki_today)).isoformat()
        bc.deck_rank = ranks[c.deck_name]
        cards.append(bc)
    media: list[str] = []
    for bc in cards:
        media.extend(n for n in bc.media if n not in media)
    return Batch(
        deck=deck,
        generated_at=round(now.timestamp() * 1000),
        rollover_hour=rollover_hour,
        days=[d.isoformat() for d in window(today)],
        quota=list(quotas),
        cards=cards,
        media=media,
        review_order="deck" if order.review_by_deck else "due",
    )


# ----------------------------------------------------------------------- sync


@dataclass
class Action:
    """One pending action sent by the phone."""

    id: str
    kind: ActionKind
    card_id: int
    at: int  # ms since epoch
    ease: int | None = None  # answer only: 1 again, 2 hard, 3 good, 4 easy
    time_ms: int = 0  # answer only


@dataclass
class Dropped:
    id: str
    reason: str


@dataclass
class SyncResult:
    applied: list[str]
    dropped: list[Dropped]
    batch: Batch


class MobileLog:
    """The two append-only files a sync writes: the review log (every answer given on the
    phone) and the processed-actions log (every action id already handled, for idempotency,
    with what each late replay counted, and each give-back done: specs/mobile.md#sync)."""

    def __init__(
        self, review_log: Path = DEFAULT_REVIEW_LOG, actions_log: Path = DEFAULT_ACTIONS_LOG
    ) -> None:
        self.review_log = review_log
        self.actions_log = actions_log

    def processed(self) -> dict[str, Dropped | None]:
        """Action id -> None when it was applied, its `Dropped` record when it was dropped."""
        out: dict[str, Dropped | None] = {}
        for entry in _read_lines(self.actions_log):
            if "id" not in entry:
                continue  # a give-back line
            action_id = str(entry["id"])
            if entry.get("status") == "dropped":
                out[action_id] = Dropped(id=action_id, reason=str(entry.get("reason")))
            else:
                out[action_id] = None
        return out

    def record(
        self,
        action: Action,
        dropped: Dropped | None,
        synced_at: int,
        late: LateCount | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "id": action.id,
            "kind": action.kind,
            "card_id": action.card_id,
            "at": action.at,
            "status": "dropped" if dropped else "applied",
            "reason": dropped.reason if dropped else None,
            "synced_at": synced_at,
        }
        if late is not None:
            entry["late"] = late.as_json()
        _append(self.actions_log, entry)

    def record_give_back(self, given: LateCount, synced_at: int) -> None:
        """One deck's studied-today counter lowered by `extend_limits`."""
        _append(self.actions_log, {"give_back": given.as_json(), "synced_at": synced_at})

    def owed(self, day: date) -> dict[str, DayQuota]:
        """What the late replays recorded on Anki day `day` counted, per deck of their cards."""
        return _sum_by_deck(_late_counts(self.actions_log, "late", day))

    def given_back(self, day: date) -> dict[str, DayQuota]:
        """What was already given back on Anki day `day`, per deck lowered."""
        return _sum_by_deck(_late_counts(self.actions_log, "give_back", day))

    def log_answer(
        self, action: Action, dropped: Dropped | None, synced_at: int, was_new: bool | None
    ) -> None:
        _append(
            self.review_log,
            {
                "action_id": action.id,
                "card_id": action.card_id,
                "ease": action.ease,
                "answered_at": action.at,
                "time_ms": action.time_ms,
                "synced_at": synced_at,
                "status": "dropped" if dropped else "applied",
                "reason": dropped.reason if dropped else None,
                "was_new": was_new,
            },
        )

    def reviews(self) -> list[dict[str, Any]]:
        return list(_read_lines(self.review_log))


@dataclass
class LateCount:
    """Reviews and new cards counted as studied on Anki day `day` in `deck`'s counter."""

    day: date
    deck: str
    review: int
    new: int

    def as_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "deck": self.deck,
            "review": self.review,
            "new": self.new,
        }


def _late_counts(path: Path, key: str, day: date) -> Iterable[LateCount]:
    for entry in _read_lines(path):
        raw = entry.get(key)
        if not isinstance(raw, dict) or raw.get("day") != day.isoformat():
            continue
        yield LateCount(
            day=day,
            deck=str(raw.get("deck")),
            review=int(raw.get("review") or 0),
            new=int(raw.get("new") or 0),
        )


def _sum_by_deck(counts: Iterable[LateCount]) -> dict[str, DayQuota]:
    out: dict[str, DayQuota] = {}
    for count in counts:
        total = out.setdefault(count.deck, DayQuota(review=0, new=0))
        total.review += count.review
        total.new += count.new
    return out


def _read_lines(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut short by a crash: skip it, keep the rest
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _append(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def in_deck(card_deck: str, deck: str) -> bool:
    return card_deck == deck or card_deck.startswith(deck + "::")


def drop_reason(card: Card | None, deck: str) -> DropReason | None:
    """Why an action on this card is not applied, or None to apply it. An edit never drops."""
    if card is None:
        return "card_missing"
    if not in_deck(card.deck_name, deck):
        return "left_deck"
    if card.is_suspended:
        return "suspended"
    return None


def redate_days(interval: int, answered: date, today: date) -> int | None:
    """Days from today for a card answered on `answered` that Anki just gave `interval`, so it
    falls due on *answered + interval*, clamped to today; None when nothing should move (an
    answer given today)."""
    lag = (today - answered).days
    if lag <= 0:
        return None
    return max(0, interval - lag)


def _apply(anki: AnkiClient, action: Action, rollover_hour: int, today: date) -> DayQuota | None:
    """Apply one action. For a late replay (an answer from an earlier Anki day), returns what
    Anki counted as studied today for it."""
    cid = action.card_id
    if action.kind == "answer":
        answered = anki_day(from_epoch_ms(action.at), rollover_hour)
        replayed = anki.answer_card(
            cid,
            action.ease or 0,
            answered_at=action.at,
            time_ms=action.time_ms,
            days_late=max(0, (today - answered).days),
        )
        if answered >= today or replayed is None:
            return None  # answered today: Anki's due date already counts from today
        if replayed.card.queue == 2:  # learning cards keep Anki's delays
            days = redate_days(replayed.card.interval, answered, today)
            if days is not None:
                anki.set_due_date([cid], str(days))
        return DayQuota(review=replayed.counted_review, new=replayed.counted_new)
    if action.kind == "flag":
        anki.set_flag([cid], ORANGE)
    elif action.kind == "unflag":
        anki.set_flag([cid], 0)
    elif action.kind == "suspend":
        anki.suspend([cid])
    else:
        raise ValueError(f"unknown action kind {action.kind!r}")
    return None


def _parents(deck: str) -> list[str]:
    parts = deck.split("::")
    return ["::".join(parts[:cut]) for cut in range(1, len(parts))]


def limits_to_give_back(
    counted: Mapping[str, DayQuota], all_parent_limits: bool
) -> dict[str, DayQuota]:
    """Per deck, how much to lower its studied-today counter after late replays (`counted`:
    what Anki counted, per deck of the replayed cards). A replay raises the counter of its
    deck and of every parent deck; `extend_limits` lowers the parents along with the deck only
    when Anki applies all parent limits, so otherwise each parent gets its own sum."""
    if all_parent_limits:
        return {deck: q for deck, q in counted.items() if q.review or q.new}
    out: dict[str, DayQuota] = {}
    for deck, quota in counted.items():
        for name in [deck, *_parents(deck)]:
            total = out.setdefault(name, DayQuota(review=0, new=0))
            total.review += quota.review
            total.new += quota.new
    return {deck: q for deck, q in out.items() if q.review or q.new}


def give_back_limits(
    anki: AnkiClient,
    owed: Mapping[str, DayQuota],
    given: Mapping[str, DayQuota],
    on_given: Callable[[str, DayQuota], None],
) -> None:
    """Lower the studied-today counters the late replays raised, so that a late replay does
    not count against today's limits (specs/mobile.md#sync). `owed`: what today's late
    replays counted, per deck of their cards; `given`: what earlier give-backs already
    lowered, per deck; `on_given` records each deck lowered, right after it is."""
    if not any(q.review or q.new for q in owed.values()):
        return
    for deck, quota in limits_to_give_back(owed, anki.apply_all_parent_limits()).items():
        done = given.get(deck, DayQuota(review=0, new=0))
        rest = DayQuota(review=max(0, quota.review - done.review), new=max(0, quota.new - done.new))
        if not rest.review and not rest.new:
            continue
        anki.extend_limits(deck, new=rest.new, review=rest.review)
        on_given(deck, rest)


def apply_actions(
    anki: AnkiClient,
    actions: Sequence[Action],
    log: MobileLog,
    *,
    deck: str,
    rollover_hour: int,
    now: datetime,
) -> tuple[list[str], list[Dropped]]:
    """Apply pending actions oldest first, each at most once across syncs.

    Returns the applied ids and the dropped ones; every distinct id sent is in exactly one.
    An Anki failure propagates: what was processed before it is recorded, so a retry
    skips it, and gives back what today's recorded late replays still owe.
    """
    for action in actions:
        if action.kind == "answer" and action.ease not in (1, 2, 3, 4):
            raise ValueError(f"answer {action.id!r} needs an ease between 1 and 4")
    done = log.processed()
    today = anki_day(now, rollover_hour)
    synced_at = round(now.timestamp() * 1000)
    applied: list[str] = []
    dropped: list[Dropped] = []
    seen: set[str] = set()
    ordered = sorted(enumerate(actions), key=lambda pair: (pair[1].at, pair[0]))

    def give_back() -> None:
        def record(deck: str, quota: DayQuota) -> None:
            log.record_give_back(LateCount(today, deck, quota.review, quota.new), synced_at)

        give_back_limits(anki, log.owed(today), log.given_back(today), record)

    try:
        for _, action in ordered:
            if action.id in seen:
                continue
            seen.add(action.id)
            if action.id in done:
                previous = done[action.id]
                if previous is None:
                    applied.append(action.id)
                else:
                    dropped.append(previous)
                continue
            cards = anki.cards_info([action.card_id])
            card = cards[0] if cards else None
            reason = drop_reason(card, deck)
            record = Dropped(id=action.id, reason=reason) if reason else None
            counted: LateCount | None = None
            if record is None and card is not None:
                late = _apply(anki, action, rollover_hour, today)
                if late is not None:  # never negative: a give-back must not raise a counter
                    review, new = max(0, late.review), max(0, late.new)
                    if review or new:
                        counted = LateCount(today, card.deck_name, review, new)
                applied.append(action.id)
            elif record is not None:
                dropped.append(record)
            log.record(action, record, synced_at, counted)
            if action.kind == "answer":
                log.log_answer(action, record, synced_at, card.type == 0 if card else None)
    except BaseException:
        try:  # the room is given back even when the sync stops midway
            give_back()
        except Exception as exc:  # noqa: BLE001 — the first failure is the one to report
            _log.warning("giving back today's limits failed: %s", exc)
        raise
    give_back()
    return applied, dropped


def sync(
    anki: AnkiClient,
    actions: Sequence[Action],
    log: MobileLog,
    *,
    deck: str,
    rollover_hour: int,
    now: Callable[[], datetime] = datetime.now,
) -> SyncResult:
    """One sync (specs/mobile.md#sync): apply the pending actions, then build the next batch."""
    applied, dropped = apply_actions(
        anki, actions, log, deck=deck, rollover_hour=rollover_hour, now=now()
    )
    batch = build_batch(anki, deck, rollover_hour, now())
    return SyncResult(applied=applied, dropped=dropped, batch=batch)
