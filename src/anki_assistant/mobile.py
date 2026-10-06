"""The phone app's server side: Anki day, batch, precomputed outcomes, sync. Spec: specs/mobile.md.

The phone reviews offline from a **batch** (3 Anki days of the mobile deck, each card with the
intervals Anki would give it) and later sends its **pending actions**; `sync` replays them into
Anki, oldest first and at most once each, then builds the next batch.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import unquote

from anki_assistant.client import AnkiClient, _quote
from anki_assistant.models import Card
from anki_assistant.web.render import render_field

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

_DAY = 86_400.0
_YEAR = 365 * _DAY
# Anki's own time-span constants (rslib scheduler/timespan.rs): a month is a year / 12.
_UNIT_SECONDS: dict[str, float] = {
    "s": 1.0,
    "m": 60.0,
    "min": 60.0,
    "h": 3_600.0,
    "d": _DAY,
    "j": _DAY,
    "mo": _YEAR / 12,
    "y": _YEAR,
    "a": _YEAR,
}
# Unicode isolation / direction marks Anki wraps numbers in, and spaces.
_MARKS = re.compile("[⁦-⁩‎‏]")
_SPACES = re.compile(r"[\s  ]+")
_SPAN = re.compile(r"^[<>]?(\d+(?:[.,]\d+)?)([^\d.,]+)$")


def clean_label(label: str) -> str:
    """An interval display string with Anki's isolation marks removed: `<⁨10⁩m` → `<10m`."""
    return _MARKS.sub("", label).strip()


def parse_interval(label: str) -> int | None:
    """Seconds for one of Anki's interval display strings (`<⁨10⁩m`, `⁨3,9⁩mo`, `1.5y`…),
    or None when the string is not understood."""
    text = _SPACES.sub("", clean_label(label)).lower()
    m = _SPAN.match(text)
    if not m:
        return None
    unit = _UNIT_SECONDS.get(m.group(2))
    if unit is None:
        return None
    return round(float(m.group(1).replace(",", ".")) * unit)


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


@dataclass
class Batch:
    deck: str
    generated_at: int
    rollover_hour: int
    days: list[str]
    quota: list[DayQuota]
    cards: list[BatchCard] = field(default_factory=list)
    media: list[str] = field(default_factory=list)


def late_replays(reviews: Iterable[dict[str, Any]], rollover_hour: int, today: date) -> DayQuota:
    """Cards whose answer, given on an earlier Anki day, a sync replayed today: Anki counts
    them as done today. Each card once; new when one of its entries says it was new."""
    was_new: dict[int, bool] = {}
    for entry in reviews:
        if entry.get("status") != "applied":
            continue
        try:
            card_id = int(entry["card_id"])
            synced = anki_day(from_epoch_ms(int(entry["synced_at"])), rollover_hour)
            answered = anki_day(from_epoch_ms(int(entry["answered_at"])), rollover_hour)
        except (KeyError, TypeError, ValueError):
            continue
        if synced != today or answered >= today:
            continue
        was_new[card_id] = was_new.get(card_id, False) or entry.get("was_new") is True
    new = sum(was_new.values())
    return DayQuota(review=len(was_new) - new, new=new)


def daily_quotas(anki: AnkiClient, deck: str, late: DayQuota | None = None) -> list[DayQuota]:
    """Day 0: what `getDeckStats` says is left today, plus the late replays (capped by the deck
    options' limits, never below the stats). Later days: the deck options' limits."""
    stats = anki.deck_stats(deck)
    today = next((s for s in stats.values() if s.get("name") == deck), None)
    if today is None:
        today = next(iter(stats.values()), {})
    config = anki.deck_config(deck) or {}
    per_review = int((config.get("rev") or {}).get("perDay", 0))
    per_new = int((config.get("new") or {}).get("perDay", 0))
    left_review, left_new = int(today.get("review_count", 0)), int(today.get("new_count", 0))
    late = late or DayQuota(review=0, new=0)
    first = DayQuota(
        review=max(left_review, min(left_review + late.review, per_review)),
        new=max(left_new, min(left_new + late.new, per_new)),
    )
    return [first] + [DayQuota(review=per_review, new=per_new) for _ in range(WINDOW_DAYS - 1)]


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
    return tuple(part.casefold() for part in card.deck_name.split("::"))


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

    Each day's cards are gathered in the order settings' order, then cut at the quota.
    Without order settings, every option keeps its default.
    """
    order = order or OrderSettings()
    scope = f"deck:{_quote(deck)} -is:suspended -is:buried"
    due_by = [set(anki.find_card_ids(f"{scope} -is:new prop:due<={k}")) for k in range(WINDOW_DAYS)]
    new_ids = anki.find_card_ids(f"{scope} is:new")
    infos = {c.card_id: c for c in anki.cards_info(sorted(due_by[-1] | set(new_ids)))}

    def new_key(c: Card) -> tuple[Any, ...]:
        return (_deck_order(c), c.due, c.ord) if order.new_by_deck else (c.due, c.ord)

    def review_key(c: Card) -> tuple[Any, ...]:
        return (_deck_order(c), c.due, c.card_id) if order.review_by_deck else (c.due, c.card_id)

    def interday_key(c: Card) -> tuple[Any, ...]:
        return (_deck_order(c), c.due) if order.review_by_deck else (c.due,)

    new_cards = sorted((infos[c] for c in new_ids if c in infos), key=new_key)

    taken: set[int] = set()
    chosen: list[tuple[Card, int, CardKind]] = []
    for day, quota in enumerate(quotas):
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
    names = sorted(set(models))
    raw = anki.invoke_multi([("modelTemplates", {"modelName": n}) for n in names])
    return {
        name: {card: dict(sides or {}) for card, sides in (tm or {}).items()}
        for name, tm in zip(names, raw, strict=True)
    }


def batch_card(
    card: Card,
    day: int,
    kind: CardKind,
    templates: dict[str, dict[str, str]],
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

    labels = [clean_label(s) for s in card.next_reviews[:4]]
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
        outcomes=[parse_interval(s) if s else None for s in labels],
        outcome_labels=labels,
    )


def build_batch(
    anki: AnkiClient,
    deck: str,
    rollover_hour: int,
    now: datetime,
    log: MobileLog | None = None,
) -> Batch:
    """The cards the phone downloads: one daily quota per Anki day of the window. The review
    log, when given, adds today's late replays back to day 0's quota."""
    late = late_replays(log.reviews(), rollover_hour, anki_day(now, rollover_hour)) if log else None
    quotas = daily_quotas(anki, deck, late)
    chosen = _select(anki, deck, quotas, order_settings(anki.deck_config(deck) or {}))
    templates = _templates(anki, (c.model_name for c, _, _ in chosen))
    cards = [batch_card(c, day, kind, templates.get(c.model_name, {})) for c, day, kind in chosen]
    media: list[str] = []
    for bc in cards:
        media.extend(n for n in bc.media if n not in media)
    return Batch(
        deck=deck,
        generated_at=round(now.timestamp() * 1000),
        rollover_hour=rollover_hour,
        days=[d.isoformat() for d in window(anki_day(now, rollover_hour))],
        quota=list(quotas),
        cards=cards,
        media=media,
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
    phone) and the processed-actions log (every action id already handled, for idempotency)."""

    def __init__(
        self, review_log: Path = DEFAULT_REVIEW_LOG, actions_log: Path = DEFAULT_ACTIONS_LOG
    ) -> None:
        self.review_log = review_log
        self.actions_log = actions_log

    def processed(self) -> dict[str, Dropped | None]:
        """Action id -> None when it was applied, its `Dropped` record when it was dropped."""
        out: dict[str, Dropped | None] = {}
        for entry in _read_lines(self.actions_log):
            action_id = str(entry.get("id", ""))
            if entry.get("status") == "dropped":
                out[action_id] = Dropped(id=action_id, reason=str(entry.get("reason")))
            else:
                out[action_id] = None
        return out

    def record(self, action: Action, dropped: Dropped | None, synced_at: int) -> None:
        _append(
            self.actions_log,
            {
                "id": action.id,
                "kind": action.kind,
                "card_id": action.card_id,
                "at": action.at,
                "status": "dropped" if dropped else "applied",
                "reason": dropped.reason if dropped else None,
                "synced_at": synced_at,
            },
        )

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


def _apply(anki: AnkiClient, action: Action, rollover_hour: int, today: date) -> None:
    cid = action.card_id
    if action.kind == "answer":
        anki.answer_cards([(cid, action.ease or 0)])
        answered = anki_day(from_epoch_ms(action.at), rollover_hour)
        if answered >= today:
            return  # answered today: Anki's due date already counts from today
        replayed = anki.cards_info([cid])
        if replayed and replayed[0].queue == 2:  # learning cards keep Anki's delays
            days = redate_days(replayed[0].interval, answered, today)
            if days is not None:
                anki.set_due_date([cid], str(days))
    elif action.kind == "flag":
        anki.set_flag([cid], ORANGE)
    elif action.kind == "unflag":
        anki.set_flag([cid], 0)
    elif action.kind == "suspend":
        anki.suspend([cid])
    else:
        raise ValueError(f"unknown action kind {action.kind!r}")


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
    An AnkiConnect failure propagates: what was processed before it is recorded, so a retry
    skips it.
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
        if record is None:
            _apply(anki, action, rollover_hour, today)
            applied.append(action.id)
        else:
            dropped.append(record)
        log.record(action, record, synced_at)
        if action.kind == "answer":
            log.log_answer(action, record, synced_at, card.type == 0 if card else None)
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
    batch = build_batch(anki, deck, rollover_hour, now(), log)
    return SyncResult(applied=applied, dropped=dropped, batch=batch)
