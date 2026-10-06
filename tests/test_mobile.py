"""Tests for mobile.py (specs/mobile.md), driven by an in-memory fake AnkiConnect.

`FakeAnki` subclasses `AnkiClient` and overrides only `invoke()`. Each card carries a test-only
`rel_due` (days from today, the value Anki's `prop:due` compares) next to its real `due`.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anki_assistant import mobile
from anki_assistant.client import AnkiClient, AnkiConnectError
from anki_assistant.mobile import Action, MobileLog
from anki_assistant.web import routes_mobile

DECK = "courant"
NOW = datetime(2026, 10, 6, 9, 0)  # Anki day 2026-10-06 with the default rollover

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

# Real `nextReviews` strings read from AnkiConnect (French locale, isolation marks included).
LABELS = ["<⁨10⁩m", "⁨24⁩j", "⁨3⁩mo", "⁨3,9⁩mo"]


def ms(instant: datetime) -> int:
    return round(instant.timestamp() * 1000)


class FakeAnki(AnkiClient):
    def __init__(self) -> None:
        super().__init__(url="http://fake.invalid")
        self.cards: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.stats = {"review_count": 20, "new_count": 5, "learn_count": 0}
        self.config = {"new": {"perDay": 5}, "rev": {"perDay": 20}}
        self.media: dict[str, bytes] = {}
        #: card id -> interval Anki gives on replay (and the queue the card lands in).
        self.replay: dict[int, tuple[int, int]] = {}
        self.fail_on: str | None = None  # action name that raises, to simulate Anki dying
        self._ids = count(1000)

    def add(
        self,
        *,
        deck: str = DECK,
        model: str = "Basic",
        fields: dict[str, str] | None = None,
        queue: int = 2,
        card_type: int | None = None,
        due: int = 100,
        rel_due: int = 0,
        ord_: int = 0,
        flags: int = 0,
        interval: int = 10,
        labels: list[str] | None = None,
    ) -> int:
        cid = next(self._ids)
        if fields is None:
            fields = (
                {"Text": "{{c1::a}} {{c2::b}}", "Back Extra": ""}
                if model == "Cloze"
                else {"Front": f"q{cid}", "Back": f"a{cid}", "Back Extra": ""}
            )
        if card_type is None:
            card_type = {0: 0, 1: 1, 3: 1, 2: 2}.get(queue, 2)
        self.cards[cid] = {
            "cardId": cid,
            "note": cid + 100_000,
            "deckName": deck,
            "modelName": model,
            "fields": {n: {"value": v, "order": i} for i, (n, v) in enumerate(fields.items())},
            "flags": flags,
            "ord": ord_,
            "interval": interval,
            "due": due,
            "queue": queue,
            "type": card_type,
            "reps": 3,
            "lapses": 0,
            "factor": 0,
            "left": 0,
            "nextReviews": list(LABELS if labels is None else labels),
            "rel_due": rel_due,
        }
        return cid

    def invoke(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if action == self.fail_on:
            raise AnkiConnectError(f"Cannot reach AnkiConnect ({action})")
        return getattr(self, f"_do_{action}")(**params)

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        reads = {"cardsInfo", "findCards", "getDeckStats", "getDeckConfig", "multi"}
        return [(a, p) for a, p in self.calls if a not in reads and a != "modelTemplates"]

    # -- actions

    def _do_multi(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"result": self.invoke(a["action"], **a.get("params", {})), "error": None}
            for a in actions
        ]

    def _do_getDeckStats(self, decks: list[str]) -> dict[str, Any]:
        return {"1721677596007": {"deck_id": 1721677596007, "name": decks[0], **self.stats}}

    def _do_getDeckConfig(self, deck: str) -> dict[str, Any]:
        return self.config

    def _do_modelTemplates(self, modelName: str) -> dict[str, Any]:
        return TEMPLATES[modelName]

    def _do_findCards(self, query: str) -> list[int]:
        deck = re.search(r'deck:"([^"]*)"', query)
        out = []
        for cid, c in self.cards.items():
            if deck and not mobile.in_deck(c["deckName"], deck.group(1)):
                continue
            if "-is:suspended" in query and c["queue"] == -1:
                continue
            if "-is:buried" in query and c["queue"] in (-2, -3):
                continue
            if "-is:new" in query:
                if c["type"] == 0:
                    continue
            elif "is:new" in query and c["type"] != 0:
                continue
            prop = re.search(r"prop:due<=(-?\d+)", query)
            if prop and (c["type"] == 0 or c["rel_due"] > int(prop.group(1))):
                continue
            out.append(cid)
        return list(reversed(out))  # AnkiConnect gives no order: don't rely on one

    def _do_cardsInfo(self, cards: list[int]) -> list[dict[str, Any]]:
        return [dict(self.cards[c]) if c in self.cards else {} for c in cards]

    def _do_answerCards(self, answers: list[dict[str, Any]]) -> list[bool]:
        out = []
        for a in answers:
            card = self.cards.get(a["cardId"])
            if card is None:
                out.append(False)
                continue
            interval, queue = self.replay.get(a["cardId"], (card["interval"], 2))
            card.update(interval=interval, queue=queue, type=2 if queue == 2 else 3)
            out.append(True)
        return out

    def _do_setDueDate(self, cards: list[int], days: str) -> bool:
        return True

    def _do_setSpecificValueOfCard(self, card: int, keys: list[str], newValues: list[Any]) -> Any:
        for k, v in zip(keys, newValues, strict=True):
            self.cards[card][k] = v
        return [True]

    def _do_suspend(self, cards: list[int]) -> bool:
        for c in cards:
            self.cards[c]["queue"] = -1
        return True

    def _do_retrieveMediaFile(self, filename: str) -> str | bool:
        data = self.media.get(filename)
        return base64.b64encode(data).decode() if data is not None else False


@pytest.fixture
def anki() -> FakeAnki:
    return FakeAnki()


@pytest.fixture
def log(tmp_path: Path) -> MobileLog:
    return MobileLog(tmp_path / "review_log.jsonl", tmp_path / "mobile_actions.jsonl")


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

DAY = 86_400
MONTH = 365 * DAY / 12
YEAR = 365 * DAY


@pytest.mark.parametrize(
    ("label", "seconds"),
    [
        # Real samples from the collection (French locale).
        ("<⁨1⁩m", 60),
        ("<⁨10⁩m", 600),
        ("<⁨15⁩m", 900),
        ("⁨2⁩j", 2 * DAY),
        ("⁨24⁩j", 24 * DAY),
        ("⁨3⁩mo", round(3 * MONTH)),
        ("⁨3,9⁩mo", round(3.9 * MONTH)),
        ("⁨10,6⁩mo", round(10.6 * MONTH)),
        ("⁨1,4⁩a", round(1.4 * YEAR)),
        ("⁨10,6⁩a", round(10.6 * YEAR)),
        # English locale, spaces, decimal point, hours and seconds.
        ("<10m", 600),
        ("3d", 3 * DAY),
        ("1.5y", round(1.5 * YEAR)),
        ("2 h", 2 * 3600),
        ("45s", 45),
        ("1,2 mo", round(1.2 * MONTH)),
        ("‎5‏min", 300),
    ],
)
def test_parse_interval(label: str, seconds: int) -> None:
    assert mobile.parse_interval(label) == seconds


@pytest.mark.parametrize("label", ["", "soon", "10", "mo", "3 parsecs", "1,2,3mo", "10w"])
def test_parse_interval_unknown_gives_none(label: str) -> None:
    assert mobile.parse_interval(label) is None


def test_clean_label_removes_isolation_marks() -> None:
    assert mobile.clean_label("⁨3,9⁩mo") == "3,9mo"
    assert mobile.clean_label("<⁨10⁩m") == "<10m"


# ----------------------------------------------------------- question / answer


def _card(anki: FakeAnki, cid: int) -> Any:
    return anki.cards_info([cid])[0]


def test_template_fields_skips_non_fields_and_filters() -> None:
    names = ["Front", "Back", "Back Extra"]
    tmpl = (
        "<p>{{info::New?}}</p> Tags - {{Tags}} {{FrontSide}} {{text:Front}} "
        "{{#Back Extra}}{{Back Extra}}{{/Back Extra}} {{ Back }}"
    )
    assert mobile.template_fields(tmpl, names) == ["Front", "Back Extra", "Back"]


def test_card_sides_basic_shows_back_extra_on_answer(anki: FakeAnki) -> None:
    cid = anki.add()
    assert mobile.card_sides(_card(anki, cid), TEMPLATES["Basic"]) == (
        ["Front"],
        ["Back", "Back Extra"],
        None,
    )


def test_card_sides_reversed_card_uses_template_at_ord(anki: FakeAnki) -> None:
    templates = {
        "Card 1": {"Front": "{{Front}}", "Back": "{{FrontSide}}<hr id=answer>{{Back}}"},
        "Card 2": {"Front": "{{Back}}", "Back": "{{FrontSide}}<hr id=answer>{{Front}}"},
    }
    cid = anki.add(ord_=1, fields={"Front": "f", "Back": "b"})
    assert mobile.card_sides(_card(anki, cid), templates) == (["Back"], ["Front"], None)


def test_card_sides_answer_drops_fields_already_asked(anki: FakeAnki) -> None:
    # AnkiBrain-Basic repeats {{Front}} on the back instead of {{FrontSide}}.
    templates = {"T": {"Front": "{{Front}}", "Back": '{{Front}} <hr id="answer">{{Back}}'}}
    cid = anki.add(fields={"Front": "f", "Back": "b"})
    assert mobile.card_sides(_card(anki, cid), templates) == (["Front"], ["Back"], None)


def test_card_sides_cloze_number_is_ord_plus_one(anki: FakeAnki) -> None:
    cid = anki.add(model="Cloze", ord_=2)
    assert mobile.card_sides(_card(anki, cid), TEMPLATES["Cloze"]) == (
        ["Text"],
        ["Back Extra"],
        3,
    )


def test_card_sides_without_template_falls_back_to_field_order(anki: FakeAnki) -> None:
    cid = anki.add(ord_=5)  # an orphan card: no template at this ordinal
    assert mobile.card_sides(_card(anki, cid), TEMPLATES["Basic"]) == (
        ["Front"],
        ["Back", "Back Extra"],
        None,
    )


def test_mobile_html_points_pictures_at_mobile_media() -> None:
    raw = 'Paris <img src="paste-a b.jpg"> <img src="latex.png" alt="\\(x\\)">'
    rendered, names = mobile.mobile_html(raw)
    assert names == ["paste-a b.jpg"]
    assert 'src="/api/mobile/media/paste-a%20b.jpg"' in rendered
    assert "/api/media/" not in rendered
    assert "\\(x\\)" in rendered


# ---------------------------------------------------------------------- batch


def test_batch_quotas_day0_from_stats_later_days_from_config(anki: FakeAnki) -> None:
    anki.stats = {"review_count": 3, "new_count": 1, "learn_count": 0}
    anki.config = {"new": {"perDay": 2}, "rev": {"perDay": 4}}
    batch = mobile.build_batch(anki, DECK, 4, NOW)
    assert [(q.review, q.new) for q in batch.quota] == [(3, 1), (4, 2), (4, 2)]
    assert batch.days == ["2026-10-06", "2026-10-07", "2026-10-08"]
    assert batch.rollover_hour == 4
    assert batch.generated_at == ms(NOW)


def test_batch_order_and_day_assignment(anki: FakeAnki) -> None:
    anki.stats = {"review_count": 2, "new_count": 1, "learn_count": 1}
    anki.config = {"new": {"perDay": 1}, "rev": {"perDay": 2}}
    r_recent = anki.add(due=50, rel_due=-1)
    r_overdue = anki.add(due=10, rel_due=-40)
    r_mid = anki.add(due=30, rel_due=-20)
    r_backlog = anki.add(due=45, rel_due=-5)
    r_tomorrow = anki.add(due=101, rel_due=1)
    r_day3 = anki.add(due=102, rel_due=2)
    r_later = anki.add(due=200, rel_due=3)  # past the window
    learn = anki.add(queue=1, due=1759740000, rel_due=0)
    n_second = anki.add(queue=0, due=8)
    n_first = anki.add(queue=0, due=7)
    n_third = anki.add(queue=0, due=9)
    anki.add(queue=-1, due=1, rel_due=-90)  # suspended
    anki.add(queue=-2, due=2, rel_due=-90)  # buried
    anki.add(deck="other", due=1, rel_due=-90)  # another deck

    batch = mobile.build_batch(anki, DECK, 4, NOW)
    got = [(c.card_id, c.day, c.kind) for c in batch.cards]
    assert got == [
        (learn, 0, "learn"),
        (r_overdue, 0, "review"),
        (r_mid, 0, "review"),
        (n_first, 0, "new"),
        (r_backlog, 1, "review"),
        (r_recent, 1, "review"),
        (n_second, 1, "new"),
        (r_tomorrow, 2, "review"),
        (r_day3, 2, "review"),
        (n_third, 2, "new"),
    ]
    assert r_later not in {c.card_id for c in batch.cards}


def test_batch_card_content(anki: FakeAnki) -> None:
    cid = anki.add(
        model="Cloze",
        ord_=1,
        flags=2,
        fields={"Text": '{{c1::A}} {{c2::B}} <img src="paste-1.png">', "Back Extra": "why"},
    )
    batch = mobile.build_batch(anki, DECK, 4, NOW)
    card = batch.cards[0]
    assert card.card_id == cid
    assert (card.model, card.ord, card.cloze, card.flag) == ("Cloze", 1, 2, 2)
    assert [f.name for f in card.question] == ["Text"]
    assert 'data-n="2"' in card.question[0].html
    assert [(f.name, f.html) for f in card.answer] == [("Back Extra", "why")]
    assert card.media == ["paste-1.png"]
    assert batch.media == ["paste-1.png"]
    assert card.outcomes == [600, 24 * DAY, round(3 * MONTH), round(3.9 * MONTH)]
    assert card.outcome_labels == ["<10m", "24j", "3mo", "3,9mo"]


def test_batch_card_with_unreadable_outcomes(anki: FakeAnki) -> None:
    anki.add(labels=["<10m", "???"])
    card = mobile.build_batch(anki, DECK, 4, NOW).cards[0]
    assert card.outcomes == [600, None, None, None]
    assert card.outcome_labels == ["<10m", "???", "", ""]


def test_batch_is_read_only(anki: FakeAnki) -> None:
    anki.add()
    mobile.build_batch(anki, DECK, 4, NOW)
    assert anki.writes() == []


# ----------------------------------------------------------------------- sync


def answer(action_id: str, cid: int, at: datetime, ease: int = 3, time_ms: int = 5000) -> Action:
    return Action(id=action_id, kind="answer", card_id=cid, at=ms(at), ease=ease, time_ms=time_ms)


def act(action_id: str, kind: Any, cid: int, at: datetime = NOW) -> Action:
    return Action(id=action_id, kind=kind, card_id=cid, at=ms(at))


def run(anki: FakeAnki, actions: list[Action], log: MobileLog) -> mobile.SyncResult:
    return mobile.sync(anki, actions, log, deck=DECK, rollover_hour=4, now=lambda: NOW)


def test_sync_replays_oldest_first(anki: FakeAnki, log: MobileLog) -> None:
    a, b, c = anki.add(), anki.add(), anki.add()
    t = NOW - timedelta(hours=1)
    actions = [
        answer("x3", c, t + timedelta(minutes=3), ease=4),
        answer("x1", a, t + timedelta(minutes=1), ease=1),
        answer("x2", b, t + timedelta(minutes=2), ease=2),
    ]
    result = run(anki, actions, log)
    replayed = [p["answers"][0] for name, p in anki.calls if name == "answerCards"]
    assert [(r["cardId"], r["ease"]) for r in replayed] == [(a, 1), (b, 2), (c, 4)]
    assert result.applied == ["x1", "x2", "x3"]
    assert result.dropped == []


def test_sync_same_instant_keeps_sent_order(anki: FakeAnki, log: MobileLog) -> None:
    a = anki.add()
    run(anki, [act("f", "flag", a), act("u", "unflag", a)], log)
    assert anki.cards[a]["flags"] == 0
    run(anki, [act("u2", "unflag", a), act("f2", "flag", a)], log)
    assert anki.cards[a]["flags"] == 2


@pytest.mark.parametrize(
    ("days_ago", "interval", "expected"),
    [
        (2, 10, "8"),  # due on answer day + 10 = today + 8
        (3, 1, "0"),  # answer day + 1 is already past: clamped to today
        (1, 1, "0"),
        (5, 30, "25"),
    ],
)
def test_sync_redates_late_review(
    anki: FakeAnki, log: MobileLog, days_ago: int, interval: int, expected: str
) -> None:
    cid = anki.add()
    anki.replay[cid] = (interval, 2)
    run(anki, [answer("a", cid, NOW - timedelta(days=days_ago))], log)
    assert ("setDueDate", {"cards": [cid], "days": expected}) in anki.calls


def test_sync_redating_uses_anki_day_not_calendar_day(anki: FakeAnki, log: MobileLog) -> None:
    cid = anki.add()
    anki.replay[cid] = (10, 2)
    # 02:00 today is still yesterday's Anki day (rollover 4): one day late.
    run(anki, [answer("a", cid, datetime(2026, 10, 6, 2, 0))], log)
    assert ("setDueDate", {"cards": [cid], "days": "9"}) in anki.calls


def test_sync_no_redating_for_today_or_learning(anki: FakeAnki, log: MobileLog) -> None:
    today = anki.add()
    anki.replay[today] = (10, 2)
    lapsed = anki.add()
    anki.replay[lapsed] = (1, 1)  # « again »: relearning, queue 1
    run(
        anki,
        [
            answer("t", today, NOW - timedelta(hours=2)),
            answer("l", lapsed, NOW - timedelta(days=2), ease=1),
        ],
        log,
    )
    assert [n for n, _ in anki.calls if n == "setDueDate"] == []
    assert [n for n, _ in anki.calls if n == "answerCards"] == ["answerCards"] * 2


def test_sync_flag_unflag_suspend(anki: FakeAnki, log: MobileLog) -> None:
    a, b, c = anki.add(), anki.add(flags=2), anki.add()
    result = run(anki, [act("1", "flag", a), act("2", "unflag", b), act("3", "suspend", c)], log)
    assert anki.cards[a]["flags"] == 2
    assert anki.cards[b]["flags"] == 0
    assert anki.cards[c]["queue"] == -1
    assert result.applied == ["1", "2", "3"]
    assert c not in {card.card_id for card in result.batch.cards}


def test_sync_dropped_actions(anki: FakeAnki, log: MobileLog) -> None:
    moved = anki.add()
    suspended = anki.add()
    edited = anki.add()
    anki.cards[moved]["deckName"] = "elsewhere"
    anki.cards[suspended]["queue"] = -1
    anki.cards[edited]["fields"]["Front"]["value"] = "rewritten on the Mac"
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
    answered = [p["answers"][0]["cardId"] for n, p in anki.calls if n == "answerCards"]
    assert answered == [edited]
    assert anki.cards[suspended]["flags"] == 0


def test_sync_subdeck_is_in_deck(anki: FakeAnki, log: MobileLog) -> None:
    cid = anki.add(deck="courant::04-maths")
    assert run(anki, [act("f", "flag", cid)], log).applied == ["f"]
    assert mobile.drop_reason(None, DECK) == "card_missing"
    assert mobile.in_deck("courantX", DECK) is False


def test_sync_suspend_then_answer_same_card_drops_answer(anki: FakeAnki, log: MobileLog) -> None:
    cid = anki.add()
    result = run(
        anki,
        [act("s", "suspend", cid, NOW - timedelta(minutes=2)), answer("a", cid, NOW)],
        log,
    )
    assert result.applied == ["s"]
    assert [(d.id, d.reason) for d in result.dropped] == [("a", "suspended")]


def test_sync_retry_is_idempotent(anki: FakeAnki, log: MobileLog) -> None:
    a = anki.add()
    gone = 424242
    actions = [answer("1", a, NOW), answer("2", gone, NOW)]
    first = run(anki, actions, log)
    anki.calls.clear()
    second = run(anki, actions, log)
    assert second.applied == first.applied == ["1"]
    assert [(d.id, d.reason) for d in second.dropped] == [("2", "card_missing")]
    assert anki.writes() == []
    assert len(log.reviews()) == 2  # logged once each


def test_sync_duplicate_id_in_one_request_applies_once(anki: FakeAnki, log: MobileLog) -> None:
    a = anki.add()
    result = run(anki, [answer("1", a, NOW), answer("1", a, NOW)], log)
    assert result.applied == ["1"]
    assert [n for n, _ in anki.calls].count("answerCards") == 1


def test_sync_failure_midway_keeps_what_was_done(anki: FakeAnki, log: MobileLog) -> None:
    a, b = anki.add(), anki.add()
    actions = [act("f", "flag", a, NOW - timedelta(minutes=1)), answer("x", b, NOW)]
    anki.fail_on = "answerCards"
    with pytest.raises(AnkiConnectError):
        run(anki, actions, log)
    assert set(log.processed()) == {"f"}
    anki.fail_on = None
    anki.calls.clear()
    result = run(anki, actions, log)
    assert result.applied == ["f", "x"]
    assert [n for n, _ in anki.writes()] == ["answerCards"]


def test_sync_rejects_answer_without_ease(anki: FakeAnki, log: MobileLog) -> None:
    a = anki.add()
    bad = Action(id="1", kind="answer", card_id=a, at=ms(NOW))
    with pytest.raises(ValueError):
        run(anki, [bad], log)
    assert anki.writes() == []


def test_review_log_entries(anki: FakeAnki, log: MobileLog) -> None:
    a = anki.add()
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
        },
    ]


def test_log_survives_a_truncated_line(log: MobileLog) -> None:
    log.actions_log.write_text('{"id": "a", "status": "applied"}\n{"id": "b", "sta\n')
    assert log.processed() == {"a": None}


# ---------------------------------------------------------------------- routes


@pytest.fixture
def http(anki: FakeAnki, log: MobileLog) -> TestClient:
    app = FastAPI()
    app.state.anki = anki
    app.state.mobile_deck = DECK
    app.state.rollover_hour = 4
    app.state.mobile_log = log
    app.include_router(routes_mobile.router, prefix="/api")
    return TestClient(app, raise_server_exceptions=False)


def test_route_batch(http: TestClient, anki: FakeAnki) -> None:
    cid = anki.add(fields={"Front": 'q <img src="paste-1.png">', "Back": "a", "Back Extra": ""})
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
    }
    assert body["quota"][0] == {"review": 20, "new": 5}
    card = body["cards"][0]
    assert card["card_id"] == cid
    assert card["question"][0]["name"] == "Front"
    assert 'src="/api/mobile/media/paste-1.png"' in card["question"][0]["html"]
    assert card["outcomes"][0] == 600
    assert body["media"] == ["paste-1.png"]
    assert anki.writes() == []


def test_route_sync(http: TestClient, anki: FakeAnki, log: MobileLog) -> None:
    cid = anki.add()
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
    http: TestClient, anki: FakeAnki, action: dict[str, Any]
) -> None:
    assert http.post("/api/mobile/sync", json={"actions": [action]}).status_code == 422
    assert anki.writes() == []


def test_route_sync_anki_unreachable(http: TestClient, anki: FakeAnki) -> None:
    anki.fail_on = "getDeckStats"
    assert http.post("/api/mobile/sync", json={"actions": []}).status_code == 503


def test_route_media(http: TestClient, anki: FakeAnki) -> None:
    anki.media["paste-1.png"] = b"\x89PNG"
    res = http.get("/api/mobile/media/paste-1.png")
    assert res.status_code == 200
    assert res.content == b"\x89PNG"
    assert res.headers["content-type"] == "image/png"
    assert http.get("/api/mobile/media/missing.png").status_code == 404
