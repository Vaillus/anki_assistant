"""Tests for the phone queue's daily limit (static/mobile-queue.js, specs/mobile.md#phone-queue),
run in node. Skipped when node is not installed."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

QUEUE_JS = Path(__file__).resolve().parents[1] / "src/anki_assistant/web/static/mobile-queue.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DAYS = ["2026-10-12", "2026-10-13", "2026-10-14"]


def at(day: int, hour: int) -> int:
    """ms since epoch of `hour`:00 local time on window day `day`, through node's clock."""
    return run_js(f"new Date(2026, 9, {12 + day}, {hour}).getTime()")


def run_js(expression: str, state: dict[str, Any] | None = None) -> Any:
    script = (
        f"const MQ = require({json.dumps(str(QUEUE_JS))});"
        f"const state = {json.dumps(state or {})};"
        f"process.stdout.write(JSON.stringify({expression}));"
    )
    assert NODE is not None
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def card(cid: int, day: int, kind: str, due_day: str | None = None, rank: int = 0) -> dict:
    return {
        "card_id": cid,
        "day": day,
        "kind": kind,
        "due_day": due_day,
        "deck_rank": rank,
        "outcomes": [600, 86_400, 86_400, 4 * 86_400],
    }


def queue_ids(state: dict[str, Any], now: int) -> tuple[list[int], dict[str, int]]:
    result = run_js(f"MQ.buildQueue(state, {now})", state)
    return [c["card_id"] for c in result["queue"]], result["counts"]


def batch(cards: list[dict], review_order: str = "due") -> dict[str, Any]:
    return {
        "rollover_hour": 4,
        "days": DAYS,
        "quota": [{"review": 2, "new": 1}] * 3,
        "cards": cards,
        "review_order": review_order,
    }


def test_offline_day_cuts_at_the_quota_in_ankis_review_order() -> None:
    a = card(1, 0, "review", "2026-10-07")  # most overdue, answered Good on day 0: back day 1
    b = card(2, 0, "review", "2026-10-11")  # not reached on day 0
    c = card(3, 1, "review", "2026-10-13")
    d = card(4, 1, "review", "2026-10-13")
    n0, n1 = card(5, 0, "new"), card(6, 1, "new")
    answers = [{"action_id": "x", "card_id": 1, "at": at(0, 9), "ease": 3, "outcome": 86_400}]
    state = {"batch": batch([a, b, c, d, n0, n1]), "answers": answers, "pending": []}
    state["burials"] = []

    ids, counts = queue_ids(state, at(1, 9))
    # Reviews of day 1: B (due 10-11), then A (back 10-13, earlier in the batch), C, D: 2 kept.
    assert ids == [2, 5, 1]
    assert counts == {"new": 1, "learn": 0, "review": 2}


def test_answers_of_the_day_use_up_its_quota() -> None:
    cards = [card(i, 0, "review", "2026-10-12") for i in (1, 2, 3)]
    answers = [{"action_id": "x", "card_id": 1, "at": at(0, 9), "ease": 3, "outcome": 864_000}]
    state = {"batch": batch(cards), "answers": answers, "pending": [], "burials": []}
    ids, _ = queue_ids(state, at(0, 10))
    assert ids == [2], "quota 2, one review answered today"


def test_deck_first_review_order() -> None:
    late_deck = card(1, 0, "review", "2026-10-01", rank=1)
    early_deck = card(2, 0, "review", "2026-10-11", rank=0)
    other = card(3, 0, "review", "2026-10-12", rank=0)
    state: dict[str, Any] = {"batch": batch([late_deck, early_deck, other], review_order="deck")}
    state |= {"answers": [], "pending": [], "burials": []}
    ids, _ = queue_ids(state, at(0, 10))
    assert ids == [2, 3], "deck first: the most overdue card's deck comes last"


def test_learning_cards_and_same_day_returns_ignore_the_quota() -> None:
    cards = [card(i, 0, "learn") for i in (1, 2, 3)] + [card(4, 0, "review", "2026-10-12")]
    answers = [{"action_id": "x", "card_id": 4, "at": at(0, 9), "ease": 1, "outcome": 600}]
    state = {"batch": batch(cards), "answers": answers, "pending": [], "burials": []}
    ids, counts = queue_ids(state, at(0, 10))
    assert ids == [4, 1, 2, 3]
    assert counts["learn"] == 4


def test_interday_learning_takes_the_review_quota_first() -> None:
    review = card(1, 0, "review", "2026-10-01")  # most overdue
    interday = [card(i, 0, "learn", "2026-10-12") for i in (2, 3)]
    intraday = card(4, 0, "learn")
    answers = [{"action_id": "x", "card_id": 2, "at": at(0, 9), "ease": 3, "outcome": 864_000}]
    state = {"batch": batch([review, *interday, intraday]), "answers": answers}
    state |= {"pending": [], "burials": []}
    ids, counts = queue_ids(state, at(0, 10))
    # Quota 2, one used by the interday card answered today: the other interday card takes
    # the last one, so the overdue review waits; the intraday card is never cut.
    assert ids == [3, 4]
    assert counts == {"new": 0, "learn": 2, "review": 0}


def test_learn_ahead_shows_returns_early_once_nothing_else_is_left() -> None:
    cards = [card(1, 0, "review", "2026-10-12"), card(2, 0, "review", "2026-10-12")]
    cards.append(card(3, 0, "learn"))
    answers = [
        {"action_id": "a", "card_id": 1, "at": at(0, 9), "ease": 1, "outcome": 600},
        {"action_id": "b", "card_id": 2, "at": at(0, 9), "ease": 1, "outcome": 3600},
    ]
    state = {"batch": batch(cards), "answers": answers, "pending": [], "burials": []}
    one_minute_later = at(0, 9) + 60_000
    ids, _ = queue_ids(state, one_minute_later)
    assert ids == [3], "a card is still left: returns wait for their time"

    state["batch"]["cards"] = cards[:2]
    ids, counts = queue_ids(state, one_minute_later)
    assert ids == [1], "nothing else left: the 10 min return is within the 20 min learn-ahead"
    assert counts == {"new": 0, "learn": 2, "review": 0}, "the 1 h return still counts today"

    state["batch"]["learn_ahead_secs"] = 0
    ids, _ = queue_ids(state, one_minute_later)
    assert ids == [], "learn-ahead off"
