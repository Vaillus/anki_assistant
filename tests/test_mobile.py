"""Tests for mobile.py (specs/mobile.md), on a temporary collection (tests/anki_fixture.py):
Anki's own search, scheduler and writes answer, on an empty collection created in the test's
temp folder.

Anki's scheduler reads the real clock, so the sync and batch tests run at the real instant
`NOW`; the pure functions use fixed dates.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from anki.collection import Collection
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant import mobile
from anki_assistant.client import AnkiError
from anki_assistant.mobile import Action, MobileLog
from anki_assistant.models import Card
from anki_assistant.web import routes_mobile
from tests.anki_fixture import TempAnki

DECK = "courant"
NOW = datetime.now().replace(microsecond=0)
TODAY = mobile.anki_day(NOW, 4)

TEMPLATES: dict[str, dict[str, dict[str, str]]] = {
    "Basic": {
        "Card 1": {
            "Front": "{{Front}}",
            "Back": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}\n"
            '{{#Back Extra}}<div class="extra">{{Back Extra}}</div>{{/Back Extra}}',
        }
    },
    "Cloze": {"Cloze": {"Front": "{{cloze:Text}}", "Back": "{{cloze:Text}}<br>\n{{Back Extra}}"}},
}

#: Display-order options the batch does not honour: every option keeps its default.
PLAIN_ORDER = {
    "newGatherPriority": 1,
    "newSortOrder": 0,
    "reviewOrder": 0,
    "newMix": 1,
    "interdayLearningMix": 1,
}


def ms(instant: datetime) -> int:
    return round(instant.timestamp() * 1000)


class PhoneAnki(TempAnki):
    """A temporary collection holding the mobile deck, one card per note."""

    def new_card(
        self,
        *,
        deck: str = DECK,
        model: str = "Basic",
        fields: dict[str, str] | None = None,
        queue: int = 2,
        rel_due: int | None = 0,
        due: int | None = None,
        interval: int = 10,
        flags: int = 0,
    ) -> int:
        """Add a note with one card and return the card id. `queue` 2 is a review card due
        `rel_due` days from today, 3 an interday learning card, 1 an intraday learning card due
        a minute ago, 0 a new card at position `due`."""
        scheduling: dict[str, int] = {}
        if queue == 2:
            scheduling = {"queue": 2, "card_type": 2, "interval": interval, "factor": 2500}
            scheduling |= {"reps": 3, "last_review_days": interval}
        elif queue == 3:
            scheduling = {"queue": 3, "card_type": 3, "interval": 1, "left": 1, "reps": 2}
        elif queue == 1:
            scheduling = {"queue": 1, "card_type": 1, "left": 1, "reps": 1}
            scheduling["due"] = int(time.time()) - 60 if due is None else due
        elif queue == 0 and due is not None:
            scheduling = {"due": due}
        if queue in (2, 3) and rel_due is not None:
            scheduling["rel_due"] = rel_due
        if queue in (-1, -2):
            scheduling = {"queue": queue, "card_type": 2, "interval": interval, "rel_due": -90}
        if fields is None:
            fields = {"Text": "{{c1::a}}", "Back Extra": ""} if model == "Cloze" else {}
        nid = self.add(deck, model=model, fields=fields or None, flags=(flags,), **scheduling)  # ty: ignore[invalid-argument-type]
        cid = self.card_ids_of(nid)[0]
        if not fields:
            self.set_fields(nid, Front=f"q{cid}", Back=f"a{cid}")
        return cid


@pytest.fixture
def anki(tmp_path: Path) -> PhoneAnki:
    client = PhoneAnki(tmp_path)
    client.add_field("Basic", "Back Extra")
    client.set_limits(DECK, review=20, new=5, **PLAIN_ORDER)
    client.calls.clear()
    return client


@pytest.fixture
def log(tmp_path: Path) -> MobileLog:
    return MobileLog(tmp_path / "review_log.jsonl", tmp_path / "mobile_actions.jsonl")


def make_card(model: str, ord_: int, fields: dict[str, str]) -> Card:
    return Card(
        card_id=1,
        note_id=2,
        deck_name=DECK,
        model_name=model,
        fields=fields,
        flag=0,
        ord=ord_,
        interval=0,
        due=0,
        reps=0,
        lapses=0,
        queue=0,
        type=0,
        factor=0,
        left=0,
    )


BASIC_FIELDS = {"Front": "f", "Back": "b", "Back Extra": ""}


# ------------------------------------------------------------------- Anki day


def test_anki_day_starts_at_rollover_hour() -> None:
    assert mobile.anki_day(datetime(2026, 10, 7, 2, 0), 4) == date(2026, 10, 6)
    assert mobile.anki_day(datetime(2026, 10, 7, 3, 59), 4) == date(2026, 10, 6)
    assert mobile.anki_day(datetime(2026, 10, 7, 4, 0), 4) == date(2026, 10, 7)
    assert mobile.anki_day(datetime(2026, 10, 7, 0, 30), 0) == date(2026, 10, 7)


def test_window_is_three_days_from_today() -> None:
    assert mobile.window(date(2026, 12, 31)) == [
        date(2026, 12, 31),
        date(2027, 1, 1),
        date(2027, 1, 2),
    ]


# ---------------------------------------------------------- precomputed outcomes


def test_clean_label_removes_isolation_marks() -> None:
    assert mobile.clean_label("⁨3,9⁩mo") == "3,9mo"
    assert mobile.clean_label("<⁨10⁩m") == "<10m"


# ----------------------------------------------------------- question / answer


def test_template_fields_skips_non_fields_and_filters() -> None:
    names = ["Front", "Back", "Back Extra"]
    tmpl = (
        "<p>{{info::New?}}</p> Tags - {{Tags}} {{FrontSide}} {{text:Front}} "
        "{{#Back Extra}}{{Back Extra}}{{/Back Extra}} {{ Back }}"
    )
    assert mobile.template_fields(tmpl, names) == ["Front", "Back Extra", "Back"]


def test_card_sides_basic_shows_back_extra_on_answer() -> None:
    card = make_card("Basic", 0, BASIC_FIELDS)
    assert mobile.card_sides(card, TEMPLATES["Basic"]) == (["Front"], ["Back", "Back Extra"], None)


def test_card_sides_reversed_card_uses_template_at_ord() -> None:
    templates = {
        "Card 1": {"Front": "{{Front}}", "Back": "{{FrontSide}}<hr id=answer>{{Back}}"},
        "Card 2": {"Front": "{{Back}}", "Back": "{{FrontSide}}<hr id=answer>{{Front}}"},
    }
    card = make_card("Basic", 1, {"Front": "f", "Back": "b"})
    assert mobile.card_sides(card, templates) == (["Back"], ["Front"], None)


def test_card_sides_answer_drops_fields_already_asked() -> None:
    # AnkiBrain-Basic repeats {{Front}} on the back instead of {{FrontSide}}.
    templates = {"T": {"Front": "{{Front}}", "Back": '{{Front}} <hr id="answer">{{Back}}'}}
    card = make_card("Basic", 0, {"Front": "f", "Back": "b"})
    assert mobile.card_sides(card, templates) == (["Front"], ["Back"], None)


def test_card_sides_cloze_number_is_ord_plus_one() -> None:
    card = make_card("Cloze", 2, {"Text": "{{c3::a}}", "Back Extra": ""})
    assert mobile.card_sides(card, TEMPLATES["Cloze"]) == (["Text"], ["Back Extra"], 3)


def test_card_sides_without_template_falls_back_to_field_order() -> None:
    card = make_card("Basic", 5, BASIC_FIELDS)  # an orphan card: no template at this ordinal
    assert mobile.card_sides(card, TEMPLATES["Basic"]) == (["Front"], ["Back", "Back Extra"], None)


def test_mobile_html_points_pictures_at_mobile_media() -> None:
    raw = 'Paris <img src="paste-a b.jpg"> <img src="latex.png" alt="\\(x\\)">'
    rendered, names = mobile.mobile_html(raw)
    assert names == ["paste-a b.jpg"]
    assert 'src="/api/mobile/media/paste-a%20b.jpg"' in rendered
    assert "/api/media/" not in rendered
    assert "\\(x\\)" in rendered


# ---------------------------------------------------------------------- batch


def build(anki: PhoneAnki) -> mobile.Batch:
    return mobile.build_batch(anki, DECK, 4, NOW)


def test_batch_quotas_use_the_effective_deck_limit(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=4, new=2)
    batch = build(anki)
    assert [(q.review, q.new) for q in batch.quota] == [(4, 2), (4, 2), (4, 2)]
    assert batch.days == [d.isoformat() for d in mobile.window(TODAY)]
    assert batch.rollover_hour == 4
    assert batch.generated_at == ms(NOW)


def test_batch_quotas_this_deck_and_today_only(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=4, new=2, deck_review=6)
    assert [q.review for q in build(anki).quota] == [6, 6, 6], "This deck overrides the preset"
    anki.set_limits(DECK, today_review=9)
    assert [q.review for q in build(anki).quota] == [9, 6, 6], "Today only: day 0 alone"


def test_batch_day0_quota_counts_what_was_studied_today(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=4, new=2)
    cid = anki.new_card()
    anki.answer_card(cid, 3, answered_at=ms(NOW), time_ms=1000)
    assert [(q.review, q.new) for q in build(anki).quota] == [(3, 2), (4, 2), (4, 2)]


def test_batch_day0_is_ankis_study_queue_later_days_are_rebuilt(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=2, new=1)
    r_recent = anki.new_card(rel_due=-1)
    r_overdue = anki.new_card(rel_due=-40)
    r_mid = anki.new_card(rel_due=-20)
    r_backlog = anki.new_card(rel_due=-5)
    r_tomorrow = anki.new_card(rel_due=1)
    r_day3 = anki.new_card(rel_due=2)
    r_later = anki.new_card(rel_due=3)  # past the window
    learn = anki.new_card(queue=1)
    n_second = anki.new_card(queue=0, due=8)
    n_first = anki.new_card(queue=0, due=7)
    anki.new_card(queue=0, due=9)
    anki.new_card(queue=-1)  # suspended
    anki.new_card(queue=-2)  # buried
    anki.new_card(deck="other", rel_due=-90)  # another deck

    got = [(c.card_id, c.day, c.kind) for c in build(anki).cards]
    day0 = [(q.card_id, 0, q.kind) for q in anki.study_queue(DECK)]
    # Anki's review limit also caps new cards: two reviews leave no room for a new one today.
    assert day0 == [(learn, 0, "learn"), (r_overdue, 0, "review"), (r_mid, 0, "review")]
    assert got == day0 + [
        (r_backlog, 1, "review"),
        (r_recent, 1, "review"),
        (n_first, 1, "new"),
        (r_tomorrow, 2, "review"),
        (r_day3, 2, "review"),
        (n_second, 2, "new"),
    ]
    assert r_later not in {c for c, _, _ in got}


def test_batch_day0_follows_ankis_subdeck_limits(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=10, new=0)
    anki.set_limits("courant::maths", deck_review=1)
    maths = [anki.new_card(deck="courant::maths", rel_due=-5) for _ in range(3)]
    other = anki.new_card(rel_due=-1)
    day0 = [c.card_id for c in build(anki).cards if c.day == 0]
    assert len(set(day0) & set(maths)) == 1, "the sub-deck's own limit"
    assert other in day0


DECK_ORDER = {
    "newGatherPriority": 0,
    "newSortOrder": 1,
    "reviewOrder": 2,
    "newMix": 1,
    "interdayLearningMix": 1,
}


def rebuilt_days_only(anki: PhoneAnki, review: int, new: int, **order: int) -> None:
    """Preset limits for days 1 and 2, nothing left today: day 0 holds learning cards only and
    the rebuild of days 1 and 2 decides the rest."""
    anki.set_limits(DECK, review=review, new=new, today_review=0, today_new=0, **order)


def test_batch_deck_order_decides_which_cards_are_taken(anki: PhoneAnki) -> None:
    rebuilt_days_only(anki, 2, 1, **DECK_ORDER)
    r_b = anki.new_card(deck="courant::b", rel_due=-40)  # most overdue, last deck
    r_a_late = anki.new_card(deck="courant::A", rel_due=-20)
    r_a_early = anki.new_card(deck="courant::A", rel_due=-30)
    r_root = anki.new_card(deck="courant", rel_due=-1)
    anki.new_card(deck="courant::b", queue=0, due=1)  # lowest position, last deck
    n_a = anki.new_card(deck="courant::A", queue=0, due=5)
    n_ax = anki.new_card(deck="courant::A::x", queue=0, due=3)

    got = [(c.card_id, c.day, c.kind) for c in build(anki).cards]
    assert got == [
        (r_root, 1, "review"),
        (r_a_early, 1, "review"),
        (n_a, 1, "new"),
        (r_a_late, 2, "review"),
        (r_b, 2, "review"),
        (n_ax, 2, "new"),
    ]


def test_batch_interday_learning_by_deck_order(anki: PhoneAnki) -> None:
    rebuilt_days_only(anki, 0, 0, **DECK_ORDER)
    i_b = anki.new_card(deck="courant::b", queue=3, rel_due=1)
    i_a = anki.new_card(deck="courant::a", queue=3, rel_due=1)
    assert [(c.card_id, c.day) for c in build(anki).cards] == [(i_a, 1), (i_b, 1)]


def test_batch_mixes_interday_learning_and_new_cards_into_reviews(anki: PhoneAnki) -> None:
    rebuilt_days_only(anki, 4, 2, **(DECK_ORDER | {"newMix": 0, "interdayLearningMix": 0}))
    r1, r2, r3, r4 = (anki.new_card(rel_due=d) for d in (-9, -8, -7, -6))
    n1, n2 = (anki.new_card(queue=0, due=d) for d in (1, 2))
    interday = anki.new_card(queue=3, rel_due=1)

    got = [(c.card_id, c.kind) for c in build(anki).cards if c.day == 1]
    assert got == [
        (r1, "review"),
        (n1, "new"),
        (r2, "review"),
        (interday, "learn"),
        (n2, "new"),
        (r3, "review"),
        (r4, "review"),
    ]


@pytest.mark.parametrize(
    "order",
    [
        PLAIN_ORDER,
        {"newGatherPriority": 0, "newSortOrder": 0, "reviewOrder": 1},
    ],
)
def test_batch_order_defaults_when_options_other(anki: PhoneAnki, order: dict[str, int]) -> None:
    rebuilt_days_only(anki, 2, 1, **order)
    r_b = anki.new_card(deck="courant::b", rel_due=-40)
    r_a = anki.new_card(deck="courant::a", rel_due=-30)
    anki.new_card(deck="courant", rel_due=-1)
    n_b = anki.new_card(deck="courant::b", queue=0, due=1)
    anki.new_card(deck="courant::a", queue=0, due=5)
    i_b = anki.new_card(deck="courant::b", queue=3, rel_due=0)
    i_a = anki.new_card(deck="courant::a", queue=3, rel_due=1)

    got = [c.card_id for c in build(anki).cards if c.day == 1]
    assert got == [i_b, i_a, r_b, r_a, n_b]


@pytest.mark.parametrize(
    ("one", "two", "mixed"),
    [
        ("abcd", "x", "abxcd"),
        ("abcde", "xy", "axbcyde"),
        ("ab", "xyz", "xaybz"),
        ("", "xy", "xy"),
        ("ab", "", "ab"),
    ],
)
def test_intersperse_spreads_evenly(one: str, two: str, mixed: str) -> None:
    assert "".join(mobile.intersperse(one, two)) == mixed


def test_batch_card_content(anki: PhoneAnki) -> None:
    nid = anki.add(
        DECK,
        model="Cloze",
        flags=(0, 2),
        fields={"Text": '{{c1::A}} {{c2::B}} <img src="paste-1.png">', "Back Extra": "why"},
        queue=2,
        card_type=2,
        interval=10,
        factor=2500,
        reps=3,
        rel_due=0,
        last_review_days=10,
    )
    cid = anki.card_ids_of(nid)[1]
    batch = build(anki)
    card = next(c for c in batch.cards if c.card_id == cid)
    assert (card.model, card.ord, card.cloze, card.flag) == ("Cloze", 1, 2, 2)
    assert [f.name for f in card.question] == ["Text"]
    assert 'data-n="2"' in card.question[0].html
    assert [(f.name, f.html) for f in card.answer] == [("Back Extra", "why")]
    assert card.media == ["paste-1.png"]
    assert batch.media == ["paste-1.png"]


def test_batch_outcomes_are_the_schedulers_raw_seconds(anki: PhoneAnki) -> None:
    cid = anki.new_card(interval=10)
    card = build(anki).cards[0]
    assert card.card_id == cid
    again, hard, good, easy = card.outcomes
    assert again == 600, "relearning step: 10 minutes"
    assert hard is not None and good is not None and easy is not None
    assert all(o % 86_400 == 0 for o in (hard, good, easy)), "review outcomes: whole days"
    assert hard < good < easy
    assert len(card.outcome_labels) == 4 and all(card.outcome_labels)
    assert "⁨" not in "".join(card.outcome_labels), "isolation marks removed"


def test_batch_gives_the_review_order_of_each_card(anki: PhoneAnki) -> None:
    anki.set_limits(DECK, review=10, new=0, **DECK_ORDER)
    overdue = anki.new_card(deck="courant::b", rel_due=-3)
    tomorrow = anki.new_card(deck="courant::A", rel_due=1)
    learn = anki.new_card(queue=1)
    batch = build(anki)
    by_id = {c.card_id: c for c in batch.cards}
    assert batch.review_order == "deck"
    assert by_id[overdue].due_day == (TODAY - timedelta(days=3)).isoformat()
    assert by_id[tomorrow].due_day == (TODAY + timedelta(days=1)).isoformat()
    assert by_id[learn].due_day is None, "intraday learning: due in seconds, no day"
    assert by_id[learn].deck_rank < by_id[tomorrow].deck_rank < by_id[overdue].deck_rank
    anki.set_limits(DECK, reviewOrder=0)
    assert build(anki).review_order == "due"


def test_batch_is_read_only(anki: PhoneAnki) -> None:
    anki.new_card()
    build(anki)
    assert anki.writes() == []


# ----------------------------------------------------------------------- sync


def answer(action_id: str, cid: int, at: datetime, ease: int = 3, time_ms: int = 5000) -> Action:
    return Action(id=action_id, kind="answer", card_id=cid, at=ms(at), ease=ease, time_ms=time_ms)


def act(action_id: str, kind: Any, cid: int, at: datetime = NOW) -> Action:
    return Action(id=action_id, kind=kind, card_id=cid, at=ms(at))


def run(anki: PhoneAnki, actions: list[Action], log: MobileLog) -> mobile.SyncResult:
    return mobile.sync(anki, actions, log, deck=DECK, rollover_hour=4, now=lambda: NOW)


def answers_in_history(anki: PhoneAnki) -> list[tuple[int, int]]:
    """(card id, ease) of the answers in Anki's review history, oldest first; set-due-date
    rows (ease 0) left out."""
    return [(cid, ease) for _, cid, ease, _, _ in anki.revlog() if ease]


def test_sync_replays_oldest_first(anki: PhoneAnki, log: MobileLog) -> None:
    a, b, c = anki.new_card(), anki.new_card(), anki.new_card()
    t = NOW - timedelta(minutes=10)
    actions = [
        answer("x3", c, t + timedelta(minutes=3), ease=4),
        answer("x1", a, t + timedelta(minutes=1), ease=1),
        answer("x2", b, t + timedelta(minutes=2), ease=2),
    ]
    result = run(anki, actions, log)
    assert answers_in_history(anki) == [(a, 1), (b, 2), (c, 4)]
    assert result.applied == ["x1", "x2", "x3"]
    assert result.dropped == []


def test_sync_replay_keeps_the_answers_instant_and_time(anki: PhoneAnki, log: MobileLog) -> None:
    cid = anki.new_card()
    at = NOW - timedelta(minutes=7)
    run(anki, [answer("a", cid, at, time_ms=6500)], log)
    [(instant, card_id, ease, _, taken)] = anki.revlog()
    assert (instant, card_id, ease, taken) == (ms(at), cid, 3, 6500)


def test_sync_same_instant_keeps_sent_order(anki: PhoneAnki, log: MobileLog) -> None:
    a = anki.new_card()
    run(anki, [act("f", "flag", a), act("u", "unflag", a)], log)
    assert anki.cards[a]["flags"] == 0
    run(anki, [act("u2", "unflag", a), act("f2", "flag", a)], log)
    assert anki.cards[a]["flags"] == 2


@pytest.mark.parametrize(("days_ago", "interval"), [(1, 10), (2, 10), (5, 30), (4, 1)])
def test_sync_redates_late_review(
    anki: PhoneAnki, log: MobileLog, days_ago: int, interval: int
) -> None:
    cid = anki.new_card(interval=interval, rel_due=-days_ago)
    run(anki, [answer("a", cid, NOW - timedelta(days=days_ago))], log)
    card = anki.cards[cid]
    expected = max(0, card["interval"] - days_ago)  # answer day + interval, clamped to today
    assert card["due"] - anki.anki_today() == expected
    assert "set_due_date" in anki.calls


def good_days(anki: PhoneAnki, cid: int) -> int:
    seconds = anki.answer_outcomes([cid])[cid].seconds[2]
    assert seconds is not None
    return seconds // 86_400


def test_late_replay_counts_on_its_own_day(anki: PhoneAnki, log: MobileLog) -> None:
    """An answer given two days before the sync: Anki's history holds its instant and time,
    the card gets the interval its own day would have given, re-dated to that day, and today's
    counters are untouched."""
    lag = 2
    cid = anki.new_card(interval=10, rel_due=0)
    anki.set_card(cid, last_review_days=10)
    on_its_day = good_days(anki, cid)  # the card as it stood on the answer's day
    anki.set_card(cid, rel_due=-lag, last_review_days=10 + lag)
    counted_to_today = good_days(anki, cid)  # elapsed days counted up to the sync

    at = NOW - timedelta(days=lag)
    run(anki, [answer("a", cid, at, time_ms=6500)], log)

    card = anki.cards[cid]
    assert card["interval"] == on_its_day
    assert on_its_day < counted_to_today
    assert card["due"] - anki.anki_today() == max(0, on_its_day - lag)
    [(instant, card_id, ease, _, taken)] = [row for row in anki.revlog() if row[2]]
    assert (instant, card_id, ease, taken) == (ms(at), cid, 3, 6500)
    assert anki.studied_today(DECK) == (0, 0)


def test_sync_redating_uses_anki_day_not_calendar_day() -> None:
    # 02:00 today is still yesterday's Anki day (rollover 4): one day late.
    answered = mobile.anki_day(datetime(2026, 10, 6, 2, 0), 4)
    assert mobile.redate_days(10, answered, date(2026, 10, 6)) == 9
    assert mobile.redate_days(10, date(2026, 10, 6), date(2026, 10, 6)) is None


def test_sync_no_redating_for_today_or_learning(anki: PhoneAnki, log: MobileLog) -> None:
    today = anki.new_card()
    lapsed = anki.new_card()
    run(
        anki,
        [
            answer("t", today, NOW - timedelta(minutes=2)),
            answer("l", lapsed, NOW - timedelta(days=2), ease=1),
        ],
        log,
    )
    assert "set_due_date" not in anki.calls
    assert anki.calls.count("answer_card") == 2
    assert anki.cards[lapsed]["queue"] == 1, "« again »: relearning, Anki's delay kept"
    assert anki.cards[today]["due"] - anki.anki_today() == anki.cards[today]["interval"]


def test_sync_flag_unflag_suspend(anki: PhoneAnki, log: MobileLog) -> None:
    a, b, c = anki.new_card(), anki.new_card(flags=2), anki.new_card()
    result = run(anki, [act("1", "flag", a), act("2", "unflag", b), act("3", "suspend", c)], log)
    assert anki.cards[a]["flags"] == 2
    assert anki.cards[b]["flags"] == 0
    assert anki.cards[c]["queue"] == -1
    assert result.applied == ["1", "2", "3"]
    assert c not in {card.card_id for card in result.batch.cards}


def test_sync_dropped_actions(anki: PhoneAnki, log: MobileLog) -> None:
    moved = anki.new_card()
    suspended = anki.new_card()
    edited = anki.new_card()
    anki.change_deck([moved], "elsewhere")
    anki.suspend([suspended])
    anki.set_fields(anki.cards[edited]["note"], Front="rewritten on the Mac")
    gone = 999_999
    result = run(
        anki,
        [
            answer("g", gone, NOW),
            answer("m", moved, NOW),
            act("s", "flag", suspended),
            answer("e", edited, NOW),
        ],
        log,
    )
    assert [(d.id, d.reason) for d in result.dropped] == [
        ("g", "card_missing"),
        ("m", "left_deck"),
        ("s", "suspended"),
    ]
    assert result.applied == ["e"]
    assert answers_in_history(anki) == [(edited, 3)]
    assert anki.cards[suspended]["flags"] == 0


def test_sync_subdeck_is_in_deck(anki: PhoneAnki, log: MobileLog) -> None:
    cid = anki.new_card(deck="courant::04-maths")
    assert run(anki, [act("f", "flag", cid)], log).applied == ["f"]
    assert mobile.drop_reason(None, DECK) == "card_missing"
    assert mobile.in_deck("courantX", DECK) is False


def test_sync_suspend_then_answer_same_card_drops_answer(anki: PhoneAnki, log: MobileLog) -> None:
    cid = anki.new_card()
    result = run(
        anki,
        [act("s", "suspend", cid, NOW - timedelta(minutes=2)), answer("a", cid, NOW)],
        log,
    )
    assert result.applied == ["s"]
    assert [(d.id, d.reason) for d in result.dropped] == [("a", "suspended")]


def test_sync_retry_is_idempotent(anki: PhoneAnki, log: MobileLog) -> None:
    a = anki.new_card()
    gone = 424242
    actions = [answer("1", a, NOW), answer("2", gone, NOW)]
    first = run(anki, actions, log)
    anki.calls.clear()
    second = run(anki, actions, log)
    assert second.applied == first.applied == ["1"]
    assert [(d.id, d.reason) for d in second.dropped] == [("2", "card_missing")]
    assert anki.writes() == []
    assert len(log.reviews()) == 2  # logged once each


def test_sync_duplicate_id_in_one_request_applies_once(anki: PhoneAnki, log: MobileLog) -> None:
    a = anki.new_card()
    result = run(anki, [answer("1", a, NOW), answer("1", a, NOW)], log)
    assert result.applied == ["1"]
    assert anki.calls.count("answer_card") == 1


def test_sync_failure_midway_keeps_what_was_done(anki: PhoneAnki, log: MobileLog) -> None:
    a, b = anki.new_card(), anki.new_card()
    actions = [act("f", "flag", a, NOW - timedelta(minutes=1)), answer("x", b, NOW)]
    anki.fail_on["answer_card"] = 1
    with pytest.raises(AnkiError):
        run(anki, actions, log)
    assert set(log.processed()) == {"f"}
    anki.calls.clear()
    result = run(anki, actions, log)
    assert result.applied == ["f", "x"]
    assert anki.writes() == ["answer_card"]


def test_sync_failure_midway_reports_the_first_error(anki: PhoneAnki, log: MobileLog) -> None:
    """A late replay was counted, then the sync stops: the give-back still runs, and when it
    fails too the error raised is the one that stopped the sync."""
    a, b = anki.new_card(rel_due=-3), anki.new_card()
    late = NOW - timedelta(days=2)
    anki.fail_on["answer_card"] = 2
    gave_back: list[str] = []

    def broken(deck: str, *, new: int = 0, review: int = 0) -> None:
        gave_back.append(deck)
        raise AnkiError("give-back: boom")

    anki.extend_limits = broken  # ty: ignore[invalid-assignment]
    with pytest.raises(AnkiError, match="answer_card: boom"):
        run(anki, [answer("a", a, late), answer("b", b, NOW)], log)
    assert gave_back == [DECK]


def test_failed_give_back_is_done_by_the_retry(anki: PhoneAnki, log: MobileLog) -> None:
    """The late replays are recorded, then the give-back fails: the retry skips the replays
    but still gives back what they counted, and a third sync gives nothing twice."""
    anki.set_limits(DECK, review=5, new=2)
    cards = [anki.new_card(rel_due=-3) for _ in range(2)]
    actions = [answer(str(i), c, NOW - timedelta(days=2)) for i, c in enumerate(cards)]
    anki.fail_on["extend_limits"] = 1
    with pytest.raises(AnkiError):
        run(anki, actions, log)
    assert set(log.processed()) == {"0", "1"}
    assert anki.studied_today(DECK) == (2, 0)

    anki.calls.clear()
    assert run(anki, actions, log).applied == ["0", "1"]
    assert anki.calls.count("answer_card") == 0
    assert anki.studied_today(DECK) == (0, 0)

    anki.calls.clear()
    run(anki, actions, log)
    assert anki.calls.count("extend_limits") == 0
    assert anki.studied_today(DECK) == (0, 0)


def test_sync_rejects_answer_without_ease(anki: PhoneAnki, log: MobileLog) -> None:
    a = anki.new_card()
    bad = Action(id="1", kind="answer", card_id=a, at=ms(NOW))
    with pytest.raises(ValueError):
        run(anki, [bad], log)
    assert anki.writes() == []


def test_review_log_entries(anki: PhoneAnki, log: MobileLog) -> None:
    a = anki.new_card()
    at = NOW - timedelta(minutes=5)
    run(
        anki,
        [answer("1", a, at, ease=2, time_ms=8400), act("f", "flag", a), answer("2", 7, at)],
        log,
    )
    entries = [json.loads(line) for line in log.review_log.read_text().splitlines()]
    assert entries == [
        {
            "action_id": "1",
            "card_id": a,
            "ease": 2,
            "answered_at": ms(at),
            "time_ms": 8400,
            "synced_at": ms(NOW),
            "status": "applied",
            "reason": None,
            "was_new": False,
        },
        {
            "action_id": "2",
            "card_id": 7,
            "ease": 3,
            "answered_at": ms(at),
            "time_ms": 5000,
            "synced_at": ms(NOW),
            "status": "dropped",
            "reason": "card_missing",
            "was_new": None,
        },
    ]


def test_review_log_records_was_new_before_replay(anki: PhoneAnki, log: MobileLog) -> None:
    new = anki.new_card(queue=0, due=3)
    run(anki, [answer("n", new, NOW - timedelta(days=1))], log)
    assert anki.cards[new]["type"] != 0  # the replay graduated it
    assert log.reviews()[0]["was_new"] is True


# ------------------------------------------------------------- late replays


def test_sync_gives_back_the_limits_late_replays_used_up(anki: PhoneAnki, log: MobileLog) -> None:
    anki.set_limits(DECK, review=5, new=2)
    reviewed = [anki.new_card(rel_due=-3) for _ in range(3)]
    fresh = anki.new_card(queue=0, due=1)
    today = anki.new_card(rel_due=-1)
    actions = [answer(f"r{i}", c, NOW - timedelta(days=2)) for i, c in enumerate(reviewed)]
    actions.append(answer("n", fresh, NOW - timedelta(days=1)))
    actions.append(answer("t", today, NOW - timedelta(minutes=1)))
    batch = run(anki, actions, log).batch
    assert anki.studied_today(DECK) == (1, 0), "only today's answer counts today"
    assert (batch.quota[0].review, batch.quota[0].new) == (4, 2)


def test_late_replay_into_a_deck_last_studied_on_an_earlier_day(
    anki: PhoneAnki, log: MobileLog
) -> None:
    """The deck's counter still holds an earlier day's count: Anki resets it on today's first
    answer, and the give-back must lower it by what the replay counted, not by the old count."""
    with anki.action() as col:
        col.crt -= 10 * 86_400  # the collection is ten days old, so Anki's today is day 10
    cid = anki.new_card(rel_due=-3)
    with anki.action() as col:
        raw = col.decks.by_name(DECK)
        assert raw is not None
        raw["revToday"] = [col.sched.today - 4, 15]
        col.decks.save(raw)
    run(anki, [answer("a", cid, NOW - timedelta(days=2))], log)
    assert anki.studied_today(DECK) == (0, 0)


@pytest.mark.parametrize("all_parent_limits", [False, True])
def test_late_replays_in_subdecks_give_back_every_parent(
    anki: PhoneAnki, log: MobileLog, all_parent_limits: bool
) -> None:
    with anki.action() as col:
        col.set_config("applyAllParentLimits", all_parent_limits)
    maths = [anki.new_card(deck="courant::maths", rel_due=-3) for _ in range(2)]
    root = anki.new_card(rel_due=-3)
    late = NOW - timedelta(days=2)
    run(anki, [answer(str(i), c, late) for i, c in enumerate([*maths, root])], log)
    assert anki.studied_today("courant::maths") == (0, 0)
    assert anki.studied_today(DECK) == (0, 0)


def test_limits_to_give_back_per_deck_or_summed_over_parents() -> None:
    counted = {
        "courant::maths": mobile.DayQuota(review=24, new=0),
        "courant": mobile.DayQuota(review=46, new=2),
    }
    on = mobile.limits_to_give_back(counted, all_parent_limits=True)
    assert {d: (q.review, q.new) for d, q in on.items()} == {
        "courant::maths": (24, 0),
        "courant": (46, 2),
    }
    off = mobile.limits_to_give_back(counted, all_parent_limits=False)
    assert {d: (q.review, q.new) for d, q in off.items()} == {
        "courant::maths": (24, 0),
        "courant": (70, 2),
    }


def test_log_survives_a_truncated_line(log: MobileLog) -> None:
    log.actions_log.write_text('{"id": "a", "status": "applied"}\n{"id": "b", "sta\n')
    assert log.processed() == {"a": None}


# ---------------------------------------------------------------------- routes


@pytest.fixture
def http(anki: PhoneAnki, log: MobileLog) -> TestClient:
    app = FastAPI()
    app.state.anki = anki
    app.state.mobile_deck = DECK
    app.state.rollover_hour = 4
    app.state.mobile_log = log
    app.include_router(routes_mobile.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def test_route_batch(http: TestClient, anki: PhoneAnki) -> None:
    cid = anki.new_card(
        fields={"Front": 'q <img src="paste-1.png">', "Back": "a", "Back Extra": ""}
    )
    res = http.get("/api/mobile/batch")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {
        "deck",
        "generated_at",
        "rollover_hour",
        "days",
        "quota",
        "cards",
        "media",
        "review_order",
    }
    assert body["quota"][0] == {"review": 20, "new": 5}
    card = body["cards"][0]
    assert card["card_id"] == cid
    assert card["question"][0]["name"] == "Front"
    assert 'src="/api/mobile/media/paste-1.png"' in card["question"][0]["html"]
    assert card["outcomes"][0] == 600
    assert body["media"] == ["paste-1.png"]
    assert anki.writes() == []


def test_route_sync(http: TestClient, anki: PhoneAnki, log: MobileLog) -> None:
    cid = anki.new_card()
    actions = [
        {"id": "a1", "kind": "answer", "card_id": cid, "at": ms(NOW), "ease": 3, "time_ms": 900},
        {"id": "f1", "kind": "flag", "card_id": 31337, "at": ms(NOW)},
    ]
    res = http.post("/api/mobile/sync", json={"actions": actions})
    assert res.status_code == 200
    body = res.json()
    assert body["applied"] == ["a1"]
    assert body["dropped"] == [{"id": "f1", "reason": "card_missing"}]
    assert body["batch"]["deck"] == DECK
    assert log.reviews()[0]["time_ms"] == 900


def test_route_batch_later_same_day_keeps_the_given_back_limits(
    http: TestClient, anki: PhoneAnki
) -> None:
    anki.set_limits(DECK, review=1)
    cid = anki.new_card(rel_due=-2)
    late = {"id": "a", "kind": "answer", "card_id": cid, "at": ms(NOW - timedelta(days=2))}
    assert http.post("/api/mobile/sync", json={"actions": [late | {"ease": 3}]}).status_code == 200
    assert http.get("/api/mobile/batch").json()["quota"][0] == {"review": 1, "new": 5}


@pytest.mark.parametrize(
    "action",
    [
        {"id": "a", "kind": "answer", "card_id": 1, "at": 0},  # no ease
        {"id": "a", "kind": "answer", "card_id": 1, "at": 0, "ease": 5},
        {"id": "a", "kind": "bury", "card_id": 1, "at": 0},
        {"id": "", "kind": "flag", "card_id": 1, "at": 0},
    ],
)
def test_route_sync_rejects_malformed_actions(
    http: TestClient, anki: PhoneAnki, action: dict[str, Any]
) -> None:
    assert http.post("/api/mobile/sync", json={"actions": [action]}).status_code == 422
    assert anki.writes() == []


def test_route_sync_while_anki_desktop_is_open(http: TestClient, anki: PhoneAnki) -> None:
    assert anki.path is not None
    desktop = Collection(str(anki.path))
    try:
        res = http.post("/api/mobile/sync", json={"actions": []})
    finally:
        desktop.close()
    assert res.status_code == 503
    assert res.json()["detail"] == "Close Anki desktop first"


def test_route_media(http: TestClient, anki: PhoneAnki) -> None:
    anki.media["paste-1.png"] = b"\x89PNG"
    res = http.get("/api/mobile/media/paste-1.png")
    assert res.status_code == 200
    assert res.content == b"\x89PNG"
    assert res.headers["content-type"] == "image/png"
    assert http.get("/api/mobile/media/missing.png").status_code == 404
